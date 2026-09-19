"""TYCHE's Codex research loop, hosted only by Leadpoet lab PR #198."""

import importlib
import hashlib
import json
import os
import re
import stat
from decimal import Decimal
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import ROOT, SKILL
from .broker import Broker, DEEPLINE_WAIT_SECONDS, SCRAPINGDOG_RUNTIME_HANDLE
from .input import request_for
from .output import (CHECKPOINT_TRANSITION_REASONS, canonical_output_sha256,
                     checkpoint_transition, checkpointed_companies, read_output)
from research_tools import ResearchTools
from run_attempt import recover_completed_attempts
from validate_run import DELIVERY_STOPS

MODEL = "openai/gpt-5.6-luna"
REASONING_EFFORT = "xhigh"
CODEX_VERSION = "0.154.0"
RUN_SECONDS = 2670
FINALIZATION_SECONDS = 600
RESEARCH_SECONDS = RUN_SECONDS - FINALIZATION_SECONDS
MAX_CODEX_INVOCATIONS = 200
MAX_UNCHANGED_EXITS = 5
MAX_LOG_BYTES = 64 * 1024
MCP_TOOL_TIMEOUT_SECONDS = 3 * DEEPLINE_WAIT_SECONDS + 15  # Native max-three batch plus MCP return margin.
# Leave room for native contract reads, evidence paging and final approval,
# including the host's possible retries of the last admitted research call.
OPENROUTER_RESEARCH_HEADROOM = 40
QUOTA_SNAPSHOT_FRESHNESS_SECONDS = 1.05
QUOTA_READ_ATTEMPTS = 3
QUOTA_READ_RETRY_SECONDS = 1.05
DEEPLINE_USD_PER_CREDIT = Decimal("0.10")
SCRAPINGDOG_USD_PER_CREDIT = Decimal("0.00005")

EXECUTION_DIAGNOSTIC_PREFIX = "LAB_ARENA_EXECUTION_DIAGNOSTIC "
MAX_EXECUTION_DIAGNOSTIC_BYTES = 256
MAX_CHECKPOINT_DIAGNOSTIC_BYTES = 512
FAILURE_DIAGNOSTIC_CLASSES = {"timeout", "runtime_error", "validation_error", "os_error", "other"}
FAILURE_DIAGNOSTIC_REASONS = {
    "deadline_or_idle_timeout", "saved_dispatch_accounting", "operational_block",
    "two_failed_codex_exits", "unchanged_exit_limit", "invocation_limit",
    "checkpoint_unavailable", "output_validation", "unexpected",
}


def _diagnostic_line(document):
    if (type(document) is not dict
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1):
        return None
    maximum = MAX_EXECUTION_DIAGNOSTIC_BYTES
    if document.get("event") == "supervisor_failure":
        if (set(document) != {"schema_version", "event", "failure_class", "reason"}
                or type(document.get("failure_class")) is not str
                or document["failure_class"] not in FAILURE_DIAGNOSTIC_CLASSES
                or type(document.get("reason")) is not str
                or document["reason"] not in FAILURE_DIAGNOSTIC_REASONS):
            return None
    elif document.get("event") == "checkpoint_transition":
        maximum = MAX_CHECKPOINT_DIAGNOSTIC_BYTES
        fields = {
            "schema_version", "event", "reason", "checkpoint_count", "final_count",
            "rejected_count", "unresolved_count", "changed_count", "missing_count",
            "checkpoint_sha256", "final_sha256",
        }
        counts = [document.get(name) for name in (
            "checkpoint_count", "final_count", "rejected_count", "unresolved_count",
            "changed_count", "missing_count",
        )]
        digest = re.compile(r"sha256:[0-9a-f]{64}")
        if (set(document) != fields
                or document.get("reason") not in CHECKPOINT_TRANSITION_REASONS
                or any(type(value) is not int or not 0 <= value <= 5 for value in counts)
                or (document["checkpoint_count"] != document["final_count"]
                    + document["rejected_count"] + document["unresolved_count"]
                    + document["changed_count"] + document["missing_count"])
                or any(type(document.get(name)) is not str or not digest.fullmatch(document[name])
                       for name in ("checkpoint_sha256", "final_sha256"))):
            return None
        active = sum(value > 0 for value in counts[2:])
        expected = ("unchanged" if active == 0 else
                    ("rejected", "unresolved", "changed_accepted", "missing_accepted")[
                        next(index for index, value in enumerate(counts[2:]) if value > 0)
                    ] if active == 1 else "mixed")
        if (document["reason"] != expected
                or (document["checkpoint_sha256"] == document["final_sha256"]) != (active == 0)):
            return None
    else:
        return None
    line = EXECUTION_DIAGNOSTIC_PREFIX + json.dumps(
        document, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    ) + "\n"
    return line if len(line.encode("ascii")) <= maximum else None


def _emit_execution_diagnostic(document):
    """Write one closed, payload-free observation without affecting execution."""
    try:
        line = _diagnostic_line(document)
        if line is not None:
            sys.stderr.write(line)
            sys.stderr.flush()
    except BaseException:
        # Diagnostics are informational and must never alter model behavior.
        return


def emit_supervisor_failure(exc):
    """Classify a supervisor exception without emitting its message or payload."""
    try:
        failure_class, reason = "other", "unexpected"
        if isinstance(exc, subprocess.TimeoutExpired):
            failure_class, reason = "timeout", "deadline_or_idle_timeout"
        elif isinstance(exc, RuntimeError):
            failure_class = "runtime_error"
            message = str(exc)
            if message.startswith("TYCHE saved dispatch accounting is incomplete:"):
                reason = "saved_dispatch_accounting"
            elif message.startswith("TYCHE run is operationally blocked:"):
                reason = "operational_block"
            elif message == "Lab Codex failed twice before delivery":
                reason = "two_failed_codex_exits"
            elif message == "Lab Codex exited repeatedly without saved progress":
                reason = "unchanged_exit_limit"
            elif message == "Arena Codex invocation limit reached before delivery":
                reason = "invocation_limit"
        elif isinstance(exc, ValueError):
            failure_class = "validation_error"
            message = str(exc)
            if message == "No reviewed TYCHE checkpoint was delivered":
                reason = "checkpoint_unavailable"
            elif (message.startswith("Lab output differs from the reviewed TYCHE checkpoint")
                  or message.startswith("Approve the current final evidence review before Arena delivery")):
                reason = "output_validation"
        elif isinstance(exc, OSError):
            failure_class = "os_error"
        _emit_execution_diagnostic({
            "schema_version": 1, "event": "supervisor_failure",
            "failure_class": failure_class, "reason": reason,
        })
    except BaseException:
        return


def emit_checkpoint_transition(summary):
    """Emit one closed successful-result observation without exposing payloads."""
    try:
        _emit_execution_diagnostic({
            "schema_version": 1, "event": "checkpoint_transition", **summary,
        })
    except BaseException:
        return


def _bounded_codex_log(run_dir):
    """Read no more than the configured Codex log bound."""
    try:
        with (Path(run_dir) / "codex.log").open("rb") as stream:
            payload = stream.read(MAX_LOG_BYTES + 1)
    except OSError:
        return None
    return payload if len(payload) <= MAX_LOG_BYTES else None


def _closed_checkpoint_lines(payload):
    """Return only canonical payload-free checkpoint records from log bytes."""
    if not isinstance(payload, bytes):
        return []
    lines = []
    for raw in payload.splitlines(keepends=True):
        if (len(raw) > MAX_CHECKPOINT_DIAGNOSTIC_BYTES
                or not raw.endswith(b"\n") or b"\r" in raw):
            continue
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError:
            continue
        if not line.startswith(EXECUTION_DIAGNOSTIC_PREFIX):
            continue
        try:
            document = json.loads(line.removeprefix(EXECUTION_DIAGNOSTIC_PREFIX))
        except (json.JSONDecodeError, TypeError):
            continue
        if (_diagnostic_line(document) == line
                and document.get("event") == "checkpoint_transition"):
            lines.append(raw)
    return lines


def retain_checkpoint_transition(run_dir, summary):
    """Retain the newest closed MCP record in the bounded native log tail."""
    try:
        line = _diagnostic_line({
            "schema_version": 1, "event": "checkpoint_transition", **summary,
        })
        if line is None:
            return False
        encoded = line.encode("ascii")
        path = Path(run_dir) / "codex.log"
        flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                return False
            start = max(0, metadata.st_size - MAX_LOG_BYTES)
            os.lseek(descriptor, start, os.SEEK_SET)
            existing = os.read(descriptor, MAX_LOG_BYTES)
            separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
            retained = (existing + separator + encoded)[-MAX_LOG_BYTES:]
            os.lseek(descriptor, 0, os.SEEK_SET)
            written = 0
            while written < len(retained):
                count = os.write(descriptor, retained[written:])
                if count <= 0:
                    return False
                written += count
            os.ftruncate(descriptor, len(retained))
            return True
        finally:
            os.close(descriptor)
    except (OSError, TypeError, ValueError):
        return False


def _logged_checkpoint_transition(run_dir, rows):
    """Select one closed MCP observation from the existing bounded Codex log."""
    payload = _bounded_codex_log(run_dir)
    if payload is None:
        return None
    expected_hash = canonical_output_sha256(rows)
    matching = []
    for raw in _closed_checkpoint_lines(payload):
        document = json.loads(
            raw.decode("ascii").removeprefix(EXECUTION_DIAGNOSTIC_PREFIX))
        if (document["final_count"] != len(rows)
                or document["final_sha256"] != expected_hash):
            continue
        matching.append(document)
    if not matching:
        return None
    return next((document for document in reversed(matching)
                 if document["reason"] != "unchanged"), matching[-1])


class ArenaQuotaGuard:
    """Keep model-owned finalization capacity without changing Arena quotas."""

    def __init__(self, quota_usage, quota_unavailable, research_deadline,
                 response_deadline, *, clock=None):
        if not callable(quota_usage):
            raise RuntimeError("The Arena quota snapshot capability is unavailable")
        if not isinstance(quota_unavailable, type) or not issubclass(quota_unavailable, Exception):
            raise RuntimeError("The Arena quota snapshot capability is unavailable")
        self._quota_usage = quota_usage
        self._quota_unavailable = quota_unavailable
        self._research_deadline = research_deadline
        self._response_deadline = response_deadline
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._phase = "research"
        self._last_used = None
        self._last_snapshot_at = None
        self._research_denial = None
        self._finalization_closed = False

    @staticmethod
    def _openrouter(snapshot):
        try:
            provider = snapshot["providers"]["openrouter"]
            limit = provider["limit"]
            used = provider["used"]
            remaining = provider["remaining"]
            inflight = provider["inflight"]
        except (KeyError, TypeError):
            raise ValueError("invalid Arena quota snapshot") from None
        values = (limit, used, remaining, inflight)
        if (any(isinstance(value, bool) or not isinstance(value, int) for value in values)
                or limit < 1 or not 0 <= used <= limit or remaining != limit - used
                or not 0 <= inflight <= used):
            raise ValueError("invalid Arena quota snapshot")
        return provider

    def _read(self):
        return self._openrouter(self._quota_usage())

    def _read_available(self, phase):
        """Retry only passive quota reads; never authorize a call from stale data."""
        phase_end = (self._research_deadline if phase == "research"
                     else self._response_deadline)
        for attempt in range(QUOTA_READ_ATTEMPTS):
            if self._clock() >= phase_end:
                raise self._quota_unavailable("quota unavailable")
            try:
                return self._read()
            except self._quota_unavailable:
                if attempt + 1 == QUOTA_READ_ATTEMPTS:
                    raise
                delay = min(QUOTA_READ_RETRY_SECONDS, phase_end - self._clock())
                if delay <= 0:
                    raise
                threading.Event().wait(delay)
        raise self._quota_unavailable("quota unavailable")

    def preflight(self):
        """Prove the passive host capability before any provider work begins."""
        try:
            provider = self._read_available("research")
        except self._quota_unavailable:
            raise RuntimeError("Arena quota snapshot unavailable") from None
        with self._lock:
            self._last_used = provider["used"]
            self._last_snapshot_at = self._clock()

    def _wait_for_fresh_snapshot(self, phase):
        """Outwait the host's one-second cache before another admission."""
        now = self._clock()
        if self._last_snapshot_at is None:
            return True
        fresh_at = self._last_snapshot_at + QUOTA_SNAPSHOT_FRESHNESS_SECONDS
        phase_end = (self._research_deadline if phase == "research"
                     else self._response_deadline)
        delay = min(fresh_at, phase_end) - now
        if delay > 0:
            threading.Event().wait(delay)
        return self._clock() >= fresh_at and self._clock() < phase_end

    def set_phase(self, phase):
        if phase not in {"research", "finalization"}:
            raise ValueError("invalid Arena quota guard phase")
        with self._lock:
            self._phase = phase

    @property
    def research_denial(self):
        with self._lock:
            return self._research_denial

    def __call__(self):
        """Admit one valid Responses dispatch or fail closed without spending."""
        with self._lock:
            now = self._clock()
            if self._phase == "research":
                if self._research_denial is not None:
                    return False
                if now >= self._research_deadline:
                    self._research_denial = "research_deadline"
                    return False
            elif self._finalization_closed or now >= self._response_deadline:
                self._finalization_closed = True
                return False
            if not self._wait_for_fresh_snapshot(self._phase):
                if self._phase == "research":
                    self._research_denial = "research_deadline"
                else:
                    self._finalization_closed = True
                return False
            try:
                provider = self._read_available(self._phase)
            except self._quota_unavailable:
                if self._phase == "research":
                    self._research_denial = "quota_unavailable"
                else:
                    self._finalization_closed = True
                return False
            now = self._clock()
            self._last_snapshot_at = now
            if self._phase == "research" and now >= self._research_deadline:
                self._research_denial = "research_deadline"
                return False
            if self._phase == "finalization" and now >= self._response_deadline:
                self._finalization_closed = True
                return False
            used = provider["used"]
            if self._last_used is None:
                self._last_used = used
            elif used < self._last_used:
                if self._phase == "research":
                    self._research_denial = "quota_regressed"
                else:
                    self._finalization_closed = True
                return False
            elif used > self._last_used:
                self._last_used = used
            if self._phase == "research":
                if provider["remaining"] <= OPENROUTER_RESEARCH_HEADROOM:
                    self._research_denial = "finalization_headroom"
                    return False
            elif provider["remaining"] <= 0:
                self._finalization_closed = True
                return False
            return True


def require_lab():
    """Refuse local/legacy execution before starting a model or provider call."""
    if ROOT != Path("/agent/source"):
        raise RuntimeError("This harness runs only in the Leadpoet lab execute sandbox")
    for name in ("LAB_ARENA_WORKER_SOCKET", "LAB_ARENA_WEB_EGRESS_SOCKET"):
        value = os.environ.get(name, "")
        if not Path(value).is_absolute() or not Path(value).is_socket():
            raise RuntimeError("PR #198 lab socket is missing: " + name)
    if os.environ.get("LAB_ARENA_OUTPUT_PATH") != "/output/companies.json":
        raise RuntimeError("The lab output mount is missing")
    try:
        runtime = importlib.import_module("lab_arena_codex")
        checkpoint = importlib.import_module("lab_arena_checkpoint")
    except ImportError as exc:
        raise RuntimeError("Leadpoet PR #198 runtime must be installed before using this bundle") from exc
    if (Path(runtime.__file__).resolve() != Path("/agent/lab_arena_codex.py")
            or Path(checkpoint.__file__).resolve() != Path("/agent/lab_arena_checkpoint.py")
            or not callable(getattr(runtime, "session", None))
            or not callable(getattr(checkpoint, "quota_usage", None))
            or not isinstance(getattr(checkpoint, "QuotaUnavailable", None), type)
            or getattr(runtime, "CODEX_VERSION", None) != CODEX_VERSION
            or runtime.CODEX_BINARY != "/usr/local/bin/codex"
            or not os.access(runtime.CODEX_BINARY, os.X_OK)):
        raise RuntimeError("The host-mounted PR #198 Codex runtime is unavailable")
    return runtime


def instructions():
    skill = (SKILL / "SKILL.md").read_text().replace("(references/", "(" + str(SKILL / "references") + "/")
    return skill + "\n\n# Leadpoet lab execution context\n" + (
        "You are already the isolated TYCHE sourcing worker. Use the shared research loop above. "
        "The lab initialized the authoritative ICP, budget and deadline. Start with tyche_inspect; "
        "do not call tyche_start, alter the request, launch another Codex process, or use global skills. "
        "Use native TYCHE tools for all lookups, state changes, reviews and delivery. "
        "Read the shared references at the absolute paths above. No shell bookkeeping or direct provider calls. "
        "The lab owns isolation, credentials, model/provider costs and quotas. "
        "The Arena host applies the current OpenRouter, Deepline and ScrapingDog limits for this attempt. "
        "All dispatched OpenRouter failures and transparent free 429 retries consume OpenRouter slots. "
        "The Arena adapter passively tracks OpenRouter capacity and reserves finalization headroom; a refused "
        "research turn at that boundary does not authorize early or incomplete delivery. Tool response "
        "arena_budget contains local Deepline and ScrapingDog dispatch telemetry. "
        "It is not a capacity allowance or billing. The host broker remains authoritative, and uncertain dispatched "
        "calls can consume quota. Plan the next pair's missing buyer discovery, "
        "profile verification, email enrichment and email validation. Reuse completed steps. "
        "As each account passes all required gates, complete its buyer before expanding account research. "
        "Hosted web search is available for bounded initial discovery through the Arena host session. The host permits at "
        "most one search tool call and five total results per Responses request. Its usage and cost remain part of the "
        "host-accounted OpenRouter request; it is not a free native TYCHE tool call. Treat search results and citations as "
        "discovery only. Use tyche_open to read an exact cited public page URL through the Arena host proxy before using it "
        "as qualification evidence; use tyche_inspect to page text beyond the preview. Successful tyche_open reads are "
        "free tool-captured page evidence; "
        "reuse their refs under the unchanged native quote, date and qualification checks. Finalization rereads are "
        "corroboration only, and legacy authored observations remain discovery notes. Paid provider lookups remain brokered through catalogued Deepline research operations, "
        "such as exa_search and exa_contents. "
        "For initial discovery, inspect the free contextdev_post_web_search and contextdev_post_news_search "
        "contracts and use them when they fit the question. Read and review the returned original sources; "
        "use paid specialized feeds for remaining evidence gaps rather than repeating broad paid discovery. "
        "Catalog metadata is bundled; unlisted Deepline tools are unavailable. ScrapingDog supports only google_search, "
        "scrape, linkedin_company, linkedin_person, linkedin_job, google_jobs, google_news, linkedin_post, x_profile, "
        "x_post, youtube_search, youtube_video, youtube_transcript and tiktok_profile through existing Arena routes; "
        "for google_search and google_news pass only query and optional country, and omit language, custom options and "
        "results or limit because Arena fixes results to 10; for google_jobs pass only query and optional country. "
        "set max_cost_credits to 100 for linkedin_person, 10 for linkedin_company and 5 for the other supported routes. "
        "Unsupported operations, options or lower bounds fail before dispatch. Both paid providers share the one initialized "
        "USD cap, including the unchanged email-verification reserve; provider credit caps do not add dollars. "
        "Read original_text and requirements from tyche_inspect before research. "
        "When Arena requires a company stage, set company.company_stage in tyche_review to the concise observed current stage label supported by the reviewed stage evidence; keep the factual explanation in its qualification check and never copy the requested label without proof. "
        "arena_signal_0 is mandatory; later signals are optional bonuses with their own age limits. "
        "After three successive predictleads_company_news_events checks return no_results, try a different "
        "catalogued source or capture a first-party page for the same required signal before another news check. "
        "Empty results remain unknown; this source change does not prove rejection or budget exhaustion. "
        "Preserve contact_geography and target_seniority for the selected contact. "
        "When buyer discovery is needed, use supported currentCompanies and currentJobTitles filters in "
        "harvestapi_search_leads for the saved company and requested_roles; reuse saved candidates first. "
        "Prefer exact or normalized target titles. Semantic role families remain valid when the observed current "
        "title's function and seniority meet a requested role. Review that comparison before findEmail=true; "
        "seniority alone does not establish role fit. "
        "First review a HarvestAPI profile with main='true'. Then use its contact_ref with "
        "harvestapi_get_profile and findEmail='true', omitting main and other add-ons. "
        "Review that enriched profile as primary_contact.ref. While research remains allowed, validate its saved address "
        "with tyche_lookup: use zerobounce_validate, inputs.email, contact_ref=enriched_profile_ref, and the saved target "
        "and purpose in checks. After the existing email gate passes, set primary_contact.email_ref to the validation "
        "result ref and primary_contact.email_source.ref to the finder result ref in tyche_review; email_ref supplies "
        "the exact address. Never save email_source alone. Reuse saved profile, finder and validation results; "
        "use BounceBan only through the existing eligible fallback, then approve the returned evidence packet. "
        "Keep the profile's provider record ID in the saved raw receipt. "
        "Company HQ country/state and stage must come from observed evidence. "
        "Intent Details must be one plain paragraph of at most 2000 characters. "
        "After each accepted company, tyche_review returns its evidence packet. Review the original source passages "
        "and approve its current review_ref with source-based review_findings in tyche_review "
        "before researching the next company. "
        "Approval automatically publishes /output/companies.json through the host checkpoint writer; no separate checkpoint call is needed. "
        "The file grows as leads are confirmed, without ending research or reducing the target. "
        "At a cost or time cutoff only an already saved valid checkpoint counts; drafts and final prose do not. "
        "Begin final evidence review before the research deadline. Inspect original source passages in "
        "tyche_finish's packet; use tyche_inspect with field='evidence_review' if a view is truncated. "
        "Source excerpts are truncated independently of review-packet paging. Before holding or rejecting a "
        "company because a fact is absent from a truncated excerpt, use tyche_inspect on its saved source ref, "
        "select the source's text field, and follow next_offset to inspect the relevant passage. "
        "This reads saved evidence; do not repeat a paid lookup. "
        "Approve only the current review_ref with one source-based review_findings entry per company. "
        "Successful tyche_finish saves and checkpoints reviewed lab JSON. "
        "For this lab run, JSON replaces the workbook, preview and local cost report. "
        "After successful delivery, end the turn immediately. Final prose is not company output. "
        "Do not claim delivery when finish is blocked; report the actual blocker."
    )


def tool_configuration(run_file, deadline, response_deadline):
    args = ["-B", "-m", "tyche_arena.mcp", "--run-file", str(run_file),
            "--deadline", str(deadline), "--response-deadline", str(response_deadline)]
    forwarded = ["PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "LAB_ARENA_WORKER_SOCKET",
                 "LAB_ARENA_WEB_EGRESS_SOCKET", "LAB_ARENA_OUTPUT_PATH", "LAB_ARENA_EVALUATION_DATE",
                 "LAB_ARENA_WEB_PROXY_URL", "SCRAPINGDOG_API_KEY", "TYCHE_FINALIZATION_ONLY"]
    return ('\n[mcp_servers.tyche]\ncommand = ' + json.dumps(sys.executable)
            + '\nargs = ' + json.dumps(args) + '\ncwd = ' + json.dumps(str(run_file.parent))
            + '\nenv_vars = ' + json.dumps(forwarded)
            + f'\nrequired = true\nstartup_timeout_sec = 40\ntool_timeout_sec = {MCP_TOOL_TIMEOUT_SECONDS}\n'
              'default_tools_approval_mode = "approve"\n')


def full_delivery(run_dir):
    """A process exit is complete only after the strict Arena finish was saved."""
    run_file = run_dir / "results.json"
    validation = run_dir / "validation.json"
    checkpoint = run_dir / "checkpoint-results.json"
    companies = run_dir / "companies.json"
    if not (run_file.exists() and validation.exists() and checkpoint.exists() and companies.exists()):
        return False
    try:
        saved = json.loads(validation.read_text())
        run_bytes = run_file.read_bytes()
        document = json.loads(run_bytes)
        icp = json.loads(document["request"]["original_text"])
        checkpointed_companies(run_file, icp, os.environ["LAB_ARENA_OUTPUT_PATH"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (isinstance(saved, dict) and saved.get("delivery_allowed") is True
            and saved.get("results_sha256") == hashlib.sha256(run_bytes).hexdigest())


def progress(run_file):
    """Read the shared native stop decision without dispatching or reconciling."""
    return ResearchTools(run_file)._overview()


def state_fingerprint(run_dir):
    """Bound clean no-op continuations without interpreting model prose."""
    digest = hashlib.sha256()
    for name in ("results.json", "results.json.budget.json", "checkpoint-results.json",
                 "companies.json", "validation.json"):
        path = run_dir / name
        digest.update(name.encode())
        if path.exists():
            digest.update(path.read_bytes())
    return digest.digest()


def _codex_once(runtime, run_dir, environment, prompt, timeout, tail):
    """Run one bounded Codex worker and retain one bounded log across continuations."""
    with tempfile.TemporaryFile() as incoming:
        incoming.write(prompt.encode())
        incoming.seek(0)
        process = subprocess.Popen(
            [runtime.CODEX_BINARY, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
             "-c", "features.image_generation=false", "-c", "agents.enabled=false",
             "-c", "features.multi_agent_v2=false",
             "-C", str(run_dir), "-o", str(run_dir / "final.txt"), "-"],
            stdin=incoming, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=environment, start_new_session=True,
        )

        def drain():
            with process.stdout:
                for block in iter(lambda: process.stdout.read(8192), b""):
                    tail.extend(block)
                    del tail[:-MAX_LOG_BYTES]

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            process.wait(timeout=max(0.001, timeout))
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            reader.join(timeout=5)
            native = _bounded_codex_log(run_dir)
            if native is not None:
                for line in _closed_checkpoint_lines(native):
                    tail.extend(line)
                    del tail[:-MAX_LOG_BYTES]
            (run_dir / "codex.log").write_bytes(tail)
        return process.returncode


def _passive_wait_until(deadline):
    """Wait for one fixed model deadline without dispatching or changing state."""
    delay = deadline - time.monotonic()
    if delay > 0:
        threading.Event().wait(delay)


def launch(runtime, run_dir, deadline, response_deadline, remaining, quota_guard):
    """Continue one saved Arena run, then finalize it without new research."""
    # session owns the Responses bridge and isolated provider configuration.
    # Configure MCP there, rather than relying on untrusted project config.
    with runtime.session(model=MODEL, reasoning_effort=REASONING_EFFORT,
                         request_guard=quota_guard,
                         response_deadline=response_deadline,
                         web_search="live") as environment:
        wait_idle = getattr(environment, "wait_idle", None)
        if not callable(wait_idle):
            raise RuntimeError("The Arena Codex runtime requires passive idle-wait support")
        environment["TYCHE_ISOLATED_RUN"] = "1"
        config = Path(environment["CODEX_HOME"]) / "config.toml"
        additions = 'developer_instructions = ' + json.dumps(instructions()) + '\n'
        config.write_text(additions + config.read_text()
                          + tool_configuration(run_dir / "results.json", deadline, response_deadline))
        prompt = ("Research the authoritative saved ICP with native TYCHE tools. Start with tyche_inspect. "
                  "Checkpoint and review each completed company before continuing research. "
                  "Finish through reviewed JSON delivery within " + str(RESEARCH_SECONDS) + " seconds.")
        continuation = (
            "Continue the SAME saved Arena run. Start with tyche_inspect. Preserve its request, start time, "
            "budget, receipts, reviews and checkpoints. Recover saved responses and never replay an uncertain "
            "paid call. Continue useful research while time and budget remain, checkpoint each reviewed lead, "
            "then finish through reviewed JSON delivery."
        )
        finalization = (
            "Finalize the SAME saved Arena run now. First inspect field='completion_candidates' and explicitly "
            "review every fully ready unresolved contact against its saved evidence. Never promote one automatically. "
            "Then request the final evidence packet with tyche_finish before individual field inspections. "
            "It contains the request, source passages, contacts and draft writing. "
            "Assess exact requirements from the source passages before editing prose; correct evidence or "
            "qualification decisions when needed, not just their wording. Use saved evidence. When native TYCHE permits, "
            "tyche_open may reread only an accepted company's exact saved source URL for corroboration; preserve the "
            "captured qualification ref. Do not search, open another URL, or "
            "start a paid provider lookup. If native TYCHE refuses the reread, finish from saved evidence without retrying. "
            "Inspect missing details as needed, then approve the current packet and finish through reviewed JSON delivery. "
            "If a correction leaves the target incomplete, save it and return; "
            "the supervisor will re-evaluate the original research deadline and budget."
        )
        run_file = run_dir / "results.json"
        tail = bytearray()
        failures = 0
        unchanged_exits = 0
        finalizing_until = None
        research_window_closed = False
        for invocation in range(MAX_CODEX_INVOCATIONS):
            if full_delivery(run_dir):
                return
            # Killing a Codex process does not cancel an already admitted host
            # request. Drain it before receipt recovery and the strict ledger
            # audit so the completed response gets its one durable result.
            now = time.monotonic()
            idle_deadline = min(response_deadline, finalizing_until or response_deadline)
            idle_timeout = min(remaining, idle_deadline - now)
            if idle_timeout <= 0 or not wait_idle(idle_timeout):
                raise subprocess.TimeoutExpired(runtime.CODEX_BINARY, max(0, idle_timeout))
            recovery = recover_completed_attempts(run_file)
            if recovery["errors"]:
                raise RuntimeError("TYCHE saved dispatch accounting is incomplete: "
                                   + json.dumps(recovery, sort_keys=True))
            state = progress(run_file)
            blocker = state.get("operational_block") or (
                state.get("stop") if state.get("stop") in {"provider_stop", "input_or_configuration_stop"} else None)
            if blocker:
                raise RuntimeError("TYCHE run is operationally blocked: " + str(blocker))
            now = time.monotonic()
            stop = state.get("stop")
            research_window_closed = research_window_closed or now >= deadline
            terminal = (stop in DELIVERY_STOPS or research_window_closed
                        or (finalizing_until is not None and stop != "continue"))
            if not terminal:
                # A final review may demote an accepted row. Resume the same
                # saved run only while its original research window remains.
                finalizing_until = None
            elif finalizing_until is None:
                finalizing_until = min(
                    response_deadline, max(now, deadline) + FINALIZATION_SECONDS
                )
            phase_end = finalizing_until if finalizing_until is not None else response_deadline
            if now >= phase_end:
                raise subprocess.TimeoutExpired(runtime.CODEX_BINARY, max(0, phase_end - now))
            worker_environment = dict(environment)
            if finalizing_until is not None:
                worker_environment["TYCHE_FINALIZATION_ONLY"] = "1"
            else:
                worker_environment["TYCHE_FINALIZATION_ONLY"] = "0"
            try:
                before = state_fingerprint(run_dir)
                now = time.monotonic()
                if now >= min(phase_end, response_deadline):
                    raise subprocess.TimeoutExpired(runtime.CODEX_BINARY, idle_timeout)
                quota_guard.set_phase("finalization" if finalizing_until is not None else "research")
                code = _codex_once(runtime, run_dir, worker_environment,
                                   finalization if finalizing_until is not None else prompt if invocation == 0 else continuation,
                                   min(remaining, phase_end - now, response_deadline - now), tail)
            except subprocess.TimeoutExpired:
                if finalizing_until is not None:
                    raise
                research_window_closed = True
                now = time.monotonic()
                finalizing_until = min(
                    response_deadline, max(now, deadline) + FINALIZATION_SECONDS
                )
                continue
            if full_delivery(run_dir):
                return
            if finalizing_until is None and quota_guard.research_denial is not None:
                # The model-owned guard ended research before another paid
                # Responses dispatch. Wait for any admitted request to settle,
                # then preserve the native stop decision. A capacity boundary
                # cannot fabricate target completion or an early empty result.
                now = time.monotonic()
                idle_timeout = min(remaining, response_deadline - now)
                if idle_timeout <= 0 or not wait_idle(idle_timeout):
                    raise subprocess.TimeoutExpired(runtime.CODEX_BINARY, max(0, idle_timeout))
                state = progress(run_file)
                stop = state.get("stop")
                if stop not in DELIVERY_STOPS and time.monotonic() < deadline:
                    _passive_wait_until(min(deadline, response_deadline))
                research_window_closed = True
                failures = 0
                unchanged_exits = 0
                continue
            changed = state_fingerprint(run_dir) != before
            # A later model-request failure must not discard an invocation's
            # saved research. Bound consecutive failures without saved progress.
            failures = failures + 1 if code and not changed else 0
            unchanged_exits = unchanged_exits + 1 if not code and not changed else 0
            if failures >= 2:
                raise RuntimeError("Lab Codex failed twice before delivery")
            if unchanged_exits >= MAX_UNCHANGED_EXITS:
                raise RuntimeError("Lab Codex exited repeatedly without saved progress")
        raise RuntimeError("Arena Codex invocation limit reached before delivery")


def run(icp):
    runtime = require_lab()
    limit = int(os.environ["LAB_ARENA_COMPANY_LIMIT"])
    if not 1 <= limit <= 5:
        raise ValueError("LAB_ARENA_COMPANY_LIMIT must be 1 through 5")
    request = request_for(icp, limit, RESEARCH_SECONDS)
    started = time.monotonic()
    research_deadline = started + RESEARCH_SECONDS
    response_deadline = started + RUN_SECONDS
    checkpoint = importlib.import_module("lab_arena_checkpoint")
    quota_guard = ArenaQuotaGuard(
        checkpoint.quota_usage, checkpoint.QuotaUnavailable,
        research_deadline, response_deadline,
    )
    run_dir = Path(tempfile.mkdtemp(prefix="tyche-arena-", dir="/tmp"))
    run_file = run_dir / "results.json"
    broker = Broker(os.environ["LAB_ARENA_WORKER_SOCKET"], research_deadline,
                    response_deadline=response_deadline)
    reported_exception = None
    try:
        max_usd = Decimal("0.8") * limit
        start_options = {"request": request, "max_usd": max_usd}
        if os.environ.get("SCRAPINGDOG_API_KEY") == SCRAPINGDOG_RUNTIME_HANDLE:
            # Mirror Arena's existing provider rates. These provider allocations
            # remain subordinate to the one shared USD cap enforced by TYCHE.
            request["budget"] = {
                "deepline_credits": float(max_usd / DEEPLINE_USD_PER_CREDIT),
                "scrapingdog_credits": float(max_usd / SCRAPINGDOG_USD_PER_CREDIT),
                "hard_stop": True,
            }
            start_options["scrapingdog_usd_per_credit"] = SCRAPINGDOG_USD_PER_CREDIT
        ResearchTools(run_file, execute=broker.execute).start(**start_options)
        # Initialize TYCHE's native clock from the same original research
        # boundary before the passive host read can block. Catalog setup above
        # is local and cannot dispatch or bill a provider request.
        quota_guard.preflight()
        try:
            launch(runtime, run_dir, research_deadline, response_deadline,
                   response_deadline - time.monotonic(), quota_guard)
        except Exception as exc:
            emit_supervisor_failure(exc)
            reported_exception = exc
            (run_dir / "failure.json").write_text(json.dumps({"error": type(exc).__name__, "message": str(exc)[:2000]}))
            if not Path(os.environ["LAB_ARENA_OUTPUT_PATH"]).exists():
                raise
        # The host commit may precede a failed local diagnostic write.
        # Arena also retains this atomic output if its hard deadline kills us.
        try:
            checkpoint_rows = read_output(os.environ["LAB_ARENA_OUTPUT_PATH"])["companies"]
        except (OSError, TypeError, ValueError):
            checkpoint_rows = None
        rows = checkpointed_companies(
            run_file, icp, os.environ["LAB_ARENA_OUTPUT_PATH"], checkpoint=checkpoint.write)
        transition = checkpoint_transition(run_file, checkpoint_rows, rows)
        logged = _logged_checkpoint_transition(run_dir, rows)
        if transition is None:
            transition = logged
        elif transition["reason"] == "unchanged" and logged is not None:
            transition = {key: value for key, value in logged.items()
                          if key not in {"schema_version", "event"}}
        if transition is not None:
            emit_checkpoint_transition(transition)
        return rows
    except Exception as exc:
        if exc is not reported_exception:
            emit_supervisor_failure(exc)
        (run_dir / "failure.json").write_text(json.dumps({"error": type(exc).__name__, "message": str(exc)[:2000]}))
        raise
    finally:
        broker.stopped.set()

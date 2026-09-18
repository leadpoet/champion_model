"""TYCHE's Codex research loop, hosted only by Leadpoet lab PR #198."""

import importlib
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import ROOT, SKILL
from .broker import Broker, SCRAPINGDOG_RUNTIME_HANDLE
from .input import request_for
from .output import checkpointed_companies
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
MCP_TOOL_TIMEOUT_SECONDS = 320  # 305-second Deepline envelope plus MCP return margin.
OPENROUTER_RESEARCH_HEADROOM = 19
QUOTA_SNAPSHOT_FRESHNESS_SECONDS = 1.05
QUOTA_READ_ATTEMPTS = 3
QUOTA_READ_RETRY_SECONDS = 1.05
DEEPLINE_USD_PER_CREDIT = Decimal("0.10")
SCRAPINGDOG_USD_PER_CREDIT = Decimal("0.00005")

EXECUTION_DIAGNOSTIC_PREFIX = "LAB_ARENA_EXECUTION_DIAGNOSTIC "
MAX_EXECUTION_DIAGNOSTIC_BYTES = 256
FAILURE_DIAGNOSTIC_CLASSES = {"timeout", "runtime_error", "validation_error", "os_error", "other"}
FAILURE_DIAGNOSTIC_REASONS = {
    "deadline_or_idle_timeout", "saved_dispatch_accounting", "operational_block",
    "two_failed_codex_exits", "unchanged_exit_limit", "invocation_limit",
    "checkpoint_unavailable", "output_validation", "unexpected",
}


def _diagnostic_line(document):
    if (type(document) is not dict
            or set(document) != {"schema_version", "event", "failure_class", "reason"}
            or type(document.get("schema_version")) is not int or document["schema_version"] != 1
            or document.get("event") != "supervisor_failure"
            or type(document.get("failure_class")) is not str
            or document["failure_class"] not in FAILURE_DIAGNOSTIC_CLASSES
            or type(document.get("reason")) is not str
            or document["reason"] not in FAILURE_DIAGNOSTIC_REASONS):
        return None
    line = EXECUTION_DIAGNOSTIC_PREFIX + json.dumps(
        document, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    ) + "\n"
    return line if len(line.encode("ascii")) <= MAX_EXECUTION_DIAGNOSTIC_BYTES else None


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
        "The current Arena contract allows at most 200 OpenRouter, 30 Deepline and 30 ScrapingDog dispatches per attempt. "
        "All dispatched OpenRouter failures and transparent free 429 retries consume OpenRouter slots. "
        "The Arena adapter passively tracks OpenRouter capacity and reserves finalization headroom; a refused "
        "research turn at that boundary does not authorize early or incomplete delivery. Tool response "
        "arena_budget contains only the "
        "local Deepline and ScrapingDog adapter dispatch counts: uncertain or refused dispatched calls can consume them, and it "
        "is not authoritative billing. "
        "Hosted web search is disabled. Use tyche_open only to read an exact public page URL through the Arena host proxy; "
        "use tyche_inspect to page text beyond the preview. Public page reads are free discovery or corroboration notes, "
        "not qualifying evidence. Capture qualifying page bodies with tyche_lookup using ScrapingDog scrape or a Deepline "
        "page reader, and reuse those captured refs as native TYCHE requires. Discovery and paid search remain brokered through catalogued Deepline research operations, "
        "such as exa_search and exa_contents. "
        "Catalog metadata is bundled; unlisted Deepline tools are unavailable. ScrapingDog supports only google_search, "
        "scrape, linkedin_company, linkedin_person, linkedin_job, google_jobs, google_news, linkedin_post, x_profile, "
        "x_post, youtube_search, youtube_video, youtube_transcript and tiktok_profile through existing Arena routes; "
        "set max_cost_credits to 100 for linkedin_person, 10 for linkedin_company and 5 for the other supported routes. "
        "Unsupported operations, options or lower bounds fail before dispatch. Both paid providers share the one initialized "
        "USD cap, including the unchanged email-verification reserve; provider credit caps do not add dollars. "
        "Read original_text and requirements from tyche_inspect before research. "
        "arena_signal_0 is mandatory; later signals are optional bonuses with their own age limits. "
        "Preserve contact_geography and target_seniority for the selected contact. "
        "First review a HarvestAPI profile with main='true'. Then use its contact_ref with "
        "harvestapi_get_profile and findEmail='true', omitting main and other add-ons. "
        "Review that enriched profile as primary_contact.ref, select its returned email and apply the normal email gate. "
        "Keep the profile's provider record ID in the saved raw receipt. "
        "Company HQ country/state and stage must come from observed evidence. "
        "Intent Details must be one plain paragraph of at most 2000 characters. "
        "After each accepted company, tyche_review immediately returns its exact evidence packet. Review the "
        "original source passages, then call tyche_review with only the current review_ref before researching "
        "the next company. The legacy tyche_checkpoint path remains compatible but is not a separate required step. "
        "This atomically saves completed companies without ending research or reducing the target. "
        "At the lab deadline only an already saved valid checkpoint counts; drafts and final prose do not. "
        "Begin final evidence review before the research deadline. Inspect original source passages in "
        "tyche_finish's packet; use tyche_inspect with field='evidence_review' if a view is truncated. "
        "Approve only the current review_ref. Successful tyche_finish saves and checkpoints reviewed lab JSON. "
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
                         request_guard=quota_guard) as environment:
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
            "Finalize the SAME saved Arena run now. Request the final evidence packet with tyche_finish before "
            "individual field inspections. It contains the request, source passages, contacts and draft writing. "
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
                # Killing a Codex process does not cancel its already paid
                # request. Let that request settle before another invocation
                # can use the same bridge. This wait never dispatches a call.
                idle_timeout = min(remaining, phase_end - now, response_deadline - now)
                if not wait_idle(idle_timeout):
                    raise subprocess.TimeoutExpired(runtime.CODEX_BINARY, idle_timeout)
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
        max_usd = Decimal("0.5") * limit
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
            if not (run_dir / "checkpoint-results.json").exists():
                raise
        # Revalidate the published snapshot, not subsequently unfinished work.
        # Arena also retains this atomic output if its hard deadline kills us.
        return checkpointed_companies(run_file, icp, os.environ["LAB_ARENA_OUTPUT_PATH"])
    except Exception as exc:
        if exc is not reported_exception:
            emit_supervisor_failure(exc)
        (run_dir / "failure.json").write_text(json.dumps({"error": type(exc).__name__, "message": str(exc)[:2000]}))
        raise
    finally:
        broker.stopped.set()

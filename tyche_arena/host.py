"""Arena transport and output adapter for the main TYCHE runner."""

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
from scripts import codex_tyche as runner

MODEL = "openai/" + runner.MODEL
REASONING_EFFORT = runner.REASONING_EFFORT
CODEX_VERSION = runner.CODEX_VERSION
RUN_SECONDS = 2670
FINALIZATION_SECONDS = runner.FINALIZATION_SECONDS
RESEARCH_SECONDS = RUN_SECONDS - FINALIZATION_SECONDS
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
    """The shared skill and worker instructions, plus the Arena I/O contract."""
    skill = (SKILL / "SKILL.md").read_text().replace("(references/", "(" + str(SKILL / "references") + "/")
    return runner.ISOLATION_INSTRUCTIONS + "\n\n" + skill + "\n\n" + (
        "Arena execution context: The authoritative ICP, start time and budget are initialized. "
        "Begin with tyche_inspect. The host owns credentials, provider billing, quotas and the hard deadline. "
        "Native tools adapt the shared sourcing workflow to this contract. Approved leads are "
        "checkpointed automatically; tyche_finish writes reviewed /output/companies.json in place "
        "of local workbook/preview artifacts. Final prose is not company output. "
        "Use the shared research and review rules; preserve all saved state on interruption."
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


class ArenaHost:
    """Transport hooks only; the local runner owns every continuation decision."""

    def __init__(self, runtime, run_dir, environment, response_deadline, quota_guard):
        self.runtime = runtime
        self.run_dir = run_dir
        self.environment = environment
        self.response_deadline = response_deadline
        self.quota_guard = quota_guard
        self.tail = bytearray()
        self.wait_idle = getattr(environment, "wait_idle", None)
        if not callable(self.wait_idle):
            raise RuntimeError("The Arena Codex runtime requires passive idle-wait support")

    def finalization_deadline(self, proposed):
        return min(proposed, time.time() + max(0, self.response_deadline - time.monotonic()))

    @staticmethod
    def recover_access(run_file, env):
        # A bundled description cannot prove the host's private credential or
        # quota was repaired. Preserve the provider stop without another call.
        return False

    @staticmethod
    def export_partial(run_file, env):
        # The host already owns the reviewed incremental checkpoint; the outer
        # adapter revalidates it before returning. Never run the local exporter.
        return {"partial": True, "output_path": os.environ["LAB_ARENA_OUTPUT_PATH"]}

    def prepare(self, request_file, env):
        # Arena's broker settles provider/model usage. Never create local model
        # receipts or poll personal-account billing from inside the sandbox.
        if time.monotonic() >= self.response_deadline:
            return {"status": "blocked", "delivery_allowed": False, "reason": "host_deadline"}
        return None

    def before_recovery(self, request_file, env):
        """Drain every admitted host response before strict saved-call audit."""
        timeout = self.response_deadline - time.monotonic()
        if timeout <= 0:
            return {"status": "blocked", "delivery_allowed": False,
                    "reason": "host_deadline", "run_file": str(self.run_dir / "results.json")}
        if not self.wait_idle(timeout):
            return {"status": "blocked", "delivery_allowed": False,
                    "reason": "host_limit", "run_file": str(self.run_dir / "results.json")}
        return None

    def run_once(self, command, request_file, worker_env, profile, *, deadline,
                 terminal, attempt):
        execution = {"status": "failed", "exit_code": 1}
        self.quota_guard.set_phase("finalization" if terminal else "research")
        timeout = self.response_deadline - time.monotonic()
        if deadline() is not None:
            timeout = min(timeout, deadline() - time.time())
        try:
            if timeout <= 0:
                raise subprocess.TimeoutExpired(self.runtime.CODEX_BINARY, 0)
            code = _codex_once(self.runtime, self.run_dir, worker_env,
                               command[-1], timeout, self.tail)
            execution.update(status="complete" if code == 0 else "failed", exit_code=code)
        except subprocess.TimeoutExpired:
            execution["failure_kind"] = "deadline_reached"
        if self.quota_guard.research_denial is not None and not terminal:
            # A host capacity boundary is an operational stop, never a
            # fabricated research deadline or permission to deliver drafts.
            execution["failure_kind"] = "host_limit"
        delivered = full_delivery(self.run_dir)
        status = {"status": "complete" if delivered else "incomplete",
                  "delivery_allowed": delivered,
                  "host_reason": self.quota_guard.research_denial}
        runner.write_worker_status(request_file, status)
        return status, execution


def launch(runtime, run_dir, deadline, response_deadline, remaining, quota_guard):
    """Use the main runner with Arena authentication and reviewed JSON output."""
    with runtime.session(model=MODEL, reasoning_effort=REASONING_EFFORT,
                         web_search="live", request_guard=quota_guard,
                         response_deadline=response_deadline) as environment:
        environment["TYCHE_ISOLATED_RUN"] = "1"
        environment["TYCHE_PARALLEL_WORKERS"] = "1"
        document = json.loads((run_dir / "results.json").read_text())
        environment["TYCHE_RUN_STARTED_AT"] = document["stop_check"]["started_at"]
        request_file = run_dir / "request.txt"
        request_file.write_text(document["request"]["original_text"], encoding="utf-8")
        environment["TYCHE_REQUEST_FILE"] = str(request_file)
        config = Path(environment["CODEX_HOME"]) / "config.toml"
        additions = 'developer_instructions = ' + json.dumps(instructions()) + '\n'
        config.write_text(additions + config.read_text()
                          + tool_configuration(run_dir / "results.json", deadline, response_deadline))
        prompt = ("Research the authoritative saved ICP with native TYCHE tools. "
                  "Read the local skill and start with tyche_inspect.\n" + request_file.read_text())
        host = ArenaHost(runtime, run_dir, environment, response_deadline, quota_guard)
        code = runner.supervise_worker([runtime.CODEX_BINARY, "exec", prompt], request_file,
                                       environment, Path(environment["CODEX_HOME"]), host=host)
        if code:
            status = json.loads((run_dir / "worker-status.json").read_text())
            raise RuntimeError("TYCHE shared runner stopped: " + str(status.get("reason", "worker_failure")))


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
            start_options["provider_credit_limits"] = {
                "deepline": float(max_usd / DEEPLINE_USD_PER_CREDIT),
                "scrapingdog": float(max_usd / SCRAPINGDOG_USD_PER_CREDIT),
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

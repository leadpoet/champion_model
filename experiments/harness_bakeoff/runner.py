"""Orchestrate live PydanticAI smoke and scored runs with fresh child sessions."""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import random
import re
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Iterator

from .icp_loader import describe_icps, external_icp_file, load_icps
from .model_preflight import select_model
from .models import validate_companies
from .providers import LiveProviderTools, deepline_preflight, verify_live_smoke_company
from .secrets import load_provider_secrets
from .tool_server import ToolServer
from .worker import SENTINEL


ARMS = ("pydantic_ai",)
DEFAULT_OUTPUT = (
    Path.home() / "Downloads" / "deepline" / "data" / "live-pydantic-harness"
)
ATTEMPT_SECONDS = 12 * 60
MAX_COMPANIES = 5
MAX_PROVIDER_CALLS = 30
MAX_PROVIDER_COST_USD = Decimal("4")
TOOL_TIMEOUT_SECONDS = 90
MAX_COMBINED_COST_USD = Decimal("4")
MAX_INPUT_TOKENS = Decimal("120000")
MAX_OUTPUT_TOKENS = Decimal("15000")
REASONING_EFFORT = "medium"
RUNNER_SCHEMA = "leadpoet-pydantic-harness-run-v1"
SMOKE_EVIDENCE_TIMEOUT_SECONDS = 20.0

_PLAN_FIELDS = (
    "runner_schema",
    "phase",
    "block_id",
    "repetition",
    "order",
    "arm",
    "icp_id",
    "input",
    "model",
    "model_pricing",
    "evaluation_date",
    "attempt_timeout_seconds",
    "max_companies",
    "max_provider_calls",
    "max_provider_cost_usd",
    "tool_timeout_seconds",
    "max_input_tokens",
    "max_output_tokens",
    "max_combined_cost_usd",
    "reasoning_effort",
    "fresh_session",
)
_RESULT_FIELDS = frozenset(
    _PLAN_FIELDS
    + (
        "ok",
        "eligible_for_scoring",
        "companies",
        "company_count",
        "completed_end_to_end",
        "smoke_evidence_status",
        "smoke_evidence_check",
        "usage",
        "provider_calls",
        "provider_call_count",
        "estimated_provider_cost_usd",
        "model_cost_usd",
        "model_cost_source",
        "model_cost_unknown_reason",
        "estimated_combined_cost_usd",
        "cost_limit_status",
        "token_limit_status",
        "latency_seconds",
        "error",
        "worker_returncode",
    )
)

SmokeEvidenceChecker = Callable[[dict[str, Any], float], dict[str, Any]]
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _run_plan_icp_ids(
    phase: str,
    available_icp_ids: Iterable[str],
    requested_icp_ids: Iterable[str] | None,
    smoke_icp_id: str | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Select the one smoke ICP and the independent scored ICP matrix."""

    if phase not in {"smoke", "scored", "all"}:
        raise ValueError("phase must be smoke, scored, or all")
    available_ids = tuple(str(value) for value in available_icp_ids)
    if not available_ids or any(not value for value in available_ids):
        raise ValueError("the ICP file must contain at least one identified ICP")
    if len(set(available_ids)) != len(available_ids):
        raise ValueError("the ICP file contains duplicate IDs")
    scored_ids = tuple(str(value) for value in (requested_icp_ids or available_ids))
    if not scored_ids or any(not value for value in scored_ids):
        raise ValueError("the scored ICP selection must not be empty")
    if len(set(scored_ids)) != len(scored_ids):
        raise ValueError("the scored ICP selection contains duplicates")
    missing = [value for value in scored_ids if value not in set(available_ids)]
    if missing:
        raise ValueError("the ICP file is missing selected IDs: " + ", ".join(missing))
    selected_smoke_id = str(smoke_icp_id or scored_ids[0])
    if selected_smoke_id not in set(available_ids):
        raise ValueError(f"the ICP file is missing smoke ID: {selected_smoke_id}")
    smoke_ids = (selected_smoke_id,)
    if phase == "smoke":
        return smoke_ids, smoke_ids, ()
    load_ids = tuple(dict.fromkeys((*smoke_ids, *scored_ids)))
    return load_ids, smoke_ids, scored_ids


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _usage_number(usage: dict[str, Any], *keys: str) -> Decimal | None:
    """Return the largest valid alias so conflicting counters fail conservatively."""
    values: list[Decimal] = []
    for key in keys:
        if key not in usage or usage[key] is None:
            continue
        value = _decimal(usage[key])
        if value is None:
            return None
        values.append(value)
    return max(values) if values else None


def _detail_number(usage: dict[str, Any], group: str, *keys: str) -> Decimal | None:
    details = usage.get(group)
    if isinstance(details, list) and len(details) == 1:
        details = details[0]
    if isinstance(details, dict):
        return _usage_number(details, *keys)
    # Accept a serialized Pydantic usage detail object when a provider returns
    # that stable field representation.
    if isinstance(details, str):
        for key in keys:
            match = re.search(rf"(?:^|[\s,(]){re.escape(key)}=([^,\s)]+)", details)
            if match and (value := _decimal(match.group(1))) is not None:
                return value
    return None


def _reported_model_cost(usage: dict[str, Any]) -> Decimal | None:
    return _usage_number(usage, "cost_usd", "total_cost_usd", "cost", "total_cost")


def _pricing_number(pricing: dict[str, Any], key: str) -> Decimal | None:
    return _decimal(pricing.get(key))


def _pricing_for_input(
    pricing: dict[str, Any], input_tokens: Decimal
) -> dict[str, Any]:
    selected = dict(pricing)
    overrides = pricing.get("overrides")
    if not isinstance(overrides, list):
        return selected
    for override in overrides:
        if not isinstance(override, dict):
            continue
        minimum = _decimal(override.get("min_prompt_tokens"))
        if minimum is not None and input_tokens >= minimum:
            selected.update(override)
    return selected


def _estimate_entry_cost(
    usage: dict[str, Any],
    pricing: dict[str, Any],
    *,
    include_request_price: bool,
) -> tuple[Decimal | None, str]:
    input_tokens = _usage_number(
        usage, "input_tokens", "prompt_tokens", "request_tokens"
    )
    output_tokens = _usage_number(
        usage, "output_tokens", "completion_tokens", "response_tokens"
    )
    if input_tokens is None or output_tokens is None:
        return None, "model input or output token usage is missing"

    cache_read = _usage_number(usage, "cache_read_tokens", "cached_input_tokens")
    if cache_read is None:
        cache_read = _detail_number(
            usage, "input_tokens_details", "cached_tokens", "cache_read_tokens"
        )
    cache_write = _usage_number(usage, "cache_write_tokens", "cache_write_input_tokens")
    if cache_write is None:
        cache_write = _detail_number(
            usage, "input_tokens_details", "cache_write_tokens"
        )
    cache_read = cache_read or Decimal(0)
    cache_write = cache_write or Decimal(0)

    # PydanticAI reports cache tokens inside its input-token total.
    total_tokens = _usage_number(usage, "total_tokens")
    cached_tokens = cache_read + cache_write
    if cached_tokens:
        inclusive_total = input_tokens + output_tokens
        disjoint_total = inclusive_total + cached_tokens
        if total_tokens == disjoint_total and disjoint_total != inclusive_total:
            uncached_input = input_tokens
        elif total_tokens is None or total_tokens == inclusive_total:
            if cached_tokens > input_tokens:
                return None, "cache token usage exceeds inclusive input token usage"
            uncached_input = input_tokens - cached_tokens
        else:
            return None, "cache token accounting shape is ambiguous"
    else:
        uncached_input = input_tokens

    active_pricing = _pricing_for_input(pricing, input_tokens)
    prompt_price = _pricing_number(active_pricing, "prompt")
    completion_price = _pricing_number(active_pricing, "completion")
    if uncached_input and prompt_price is None:
        return None, "live OpenRouter prompt price is unavailable"
    if output_tokens and completion_price is None:
        return None, "live OpenRouter completion price is unavailable"
    prompt_price = prompt_price or Decimal(0)
    completion_price = completion_price or Decimal(0)
    cache_read_price = _pricing_number(active_pricing, "input_cache_read")
    cache_write_price = _pricing_number(active_pricing, "input_cache_write")
    # A missing cache-specific price means the tokens use the normal input price.
    cache_read_price = prompt_price if cache_read_price is None else cache_read_price
    cache_write_price = prompt_price if cache_write_price is None else cache_write_price

    reasoning_tokens = _detail_number(
        usage, "output_tokens_details", "reasoning_tokens"
    )
    if reasoning_tokens is None:
        reasoning_tokens = _detail_number(
            usage, "completion_tokens_details", "reasoning_tokens"
        )
    internal_reasoning_price = _pricing_number(active_pricing, "internal_reasoning")
    if internal_reasoning_price is not None:
        if reasoning_tokens is None:
            return None, "reasoning-token usage is missing for this model price"
        if reasoning_tokens > output_tokens:
            return None, "reasoning-token usage exceeds output token usage"
        output_cost = (
            output_tokens - reasoning_tokens
        ) * completion_price + reasoning_tokens * internal_reasoning_price
    else:
        output_cost = output_tokens * completion_price

    request_cost = Decimal(0)
    request_price = _pricing_number(active_pricing, "request") or Decimal(0)
    if include_request_price and request_price:
        requests = _usage_number(usage, "requests", "request_count")
        if requests is None:
            return None, "request count is missing for this model price"
        request_cost = requests * request_price
    return (
        uncached_input * prompt_price
        + cache_read * cache_read_price
        + cache_write * cache_write_price
        + output_cost
        + request_cost,
        "",
    )


def _estimate_model_cost(
    usage: dict[str, Any], pricing: dict[str, Any]
) -> tuple[Decimal | None, str]:
    if not pricing:
        return None, "live OpenRouter pricing is unavailable"
    input_tokens = _usage_number(
        usage, "input_tokens", "prompt_tokens", "request_tokens"
    )
    if input_tokens is None:
        return None, "model input token usage is missing"

    overrides = pricing.get("overrides")
    first_override: Decimal | None = None
    if isinstance(overrides, list):
        thresholds = [
            value
            for item in overrides
            if isinstance(item, dict)
            if (value := _decimal(item.get("min_prompt_tokens"))) is not None
        ]
        first_override = min(thresholds) if thresholds else None
    entries = usage.get("request_usage_entries")
    if first_override is not None and input_tokens >= first_override:
        if not isinstance(entries, list) or not entries:
            return None, "per-request usage is missing for tiered model pricing"
        costs: list[Decimal] = []
        for entry in entries:
            if not isinstance(entry, dict):
                return None, "per-request model usage is invalid"
            cost, reason = _estimate_entry_cost(
                entry, pricing, include_request_price=True
            )
            if cost is None:
                return None, reason
            costs.append(cost)
        return sum(costs, Decimal(0)), ""
    return _estimate_entry_cost(usage, pricing, include_request_price=True)


def _model_cost(
    usage: dict[str, Any], pricing: dict[str, Any]
) -> tuple[Decimal | None, str, str]:
    """Return cost, source, and an explicit reason when cost is unknown."""
    reported = _reported_model_cost(usage)
    token_total = sum(
        filter(
            None,
            (
                _usage_number(usage, "input_tokens", "prompt_tokens", "request_tokens"),
                _usage_number(
                    usage, "output_tokens", "completion_tokens", "response_tokens"
                ),
            ),
        ),
        Decimal(0),
    )
    if reported is not None and (reported > 0 or token_total == 0):
        return reported, "framework_reported", ""

    estimated, reason = _estimate_model_cost(usage, pricing)
    if estimated is not None:
        return estimated, "openrouter_pricing", ""
    if reported == 0 and token_total > 0:
        reason = f"reported zero cost with nonzero usage; {reason}"
    return None, "unknown", reason or "model cost is not reported"


def _token_limit_error(usage: dict[str, Any]) -> tuple[str, str]:
    """Apply one aggregate token contract to every harness usage shape."""
    input_tokens = _usage_number(
        usage,
        "aggregate_input_tokens",
        "input_tokens",
        "prompt_tokens",
        "request_tokens",
    )
    output_tokens = _usage_number(
        usage, "output_tokens", "completion_tokens", "response_tokens"
    )
    if input_tokens is None or output_tokens is None:
        return "unknown", "model token usage is missing"
    violations: list[str] = []
    if input_tokens > MAX_INPUT_TOKENS:
        violations.append(
            f"input token limit exceeded: {int(input_tokens)} > {int(MAX_INPUT_TOKENS)}"
        )
    if output_tokens > MAX_OUTPUT_TOKENS:
        violations.append(
            f"output token limit exceeded: {int(output_tokens)} > {int(MAX_OUTPUT_TOKENS)}"
        )
    return ("exceeded", "; ".join(violations)) if violations else ("within_limit", "")


def _default_smoke_evidence_checker(
    company: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    return verify_live_smoke_company(company, timeout_seconds=timeout_seconds)


def _validate_passed_smoke_evidence(check: dict[str, Any]) -> None:
    for label in ("company_website", "intent_evidence"):
        observation = check.get(label)
        if not isinstance(observation, dict):
            raise ValueError(f"passed smoke check is missing {label}")
        if not isinstance(observation.get("url"), str) or not observation["url"]:
            raise ValueError(f"passed smoke check has no {label} URL")
        status_code = observation.get("status_code")
        if type(status_code) is not int or not 200 <= status_code < 400:
            raise ValueError(f"passed smoke check has invalid {label} HTTP status")
    if check["intent_evidence"].get("body_present") is not True:
        raise ValueError("passed intent evidence check must confirm a response body")


def _clean_child_env(
    provider_secrets: dict[str, str],
    *,
    model: str,
    tool_url: str,
    tool_token: str,
    max_companies: int,
    evaluation_date: str,
    timeout_seconds: int,
) -> dict[str, str]:
    kept = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "USER",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "NODE_EXTRA_CA_CERTS",
        }
    }
    kept.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "OPENROUTER_API_KEY": provider_secrets["OPENROUTER_API_KEY"],
            "BAKEOFF_OPENROUTER_MODEL": model,
            "BAKEOFF_TOOL_URL": tool_url,
            "BAKEOFF_TOOL_TOKEN": tool_token,
            "BAKEOFF_MAX_COMPANIES": str(max_companies),
            "BAKEOFF_MAX_PROVIDER_CALLS": str(MAX_PROVIDER_CALLS),
            "BAKEOFF_RUN_TIMEOUT_SECONDS": str(timeout_seconds),
            "BAKEOFF_TOOL_TIMEOUT_SECONDS": str(TOOL_TIMEOUT_SECONDS),
            "BAKEOFF_MAX_INPUT_TOKENS": str(int(MAX_INPUT_TOKENS)),
            "BAKEOFF_MAX_OUTPUT_TOKENS": str(int(MAX_OUTPUT_TOKENS)),
            "BAKEOFF_REASONING_EFFORT": REASONING_EFFORT,
            "BAKEOFF_EVALUATION_DATE": evaluation_date,
            "BAKEOFF_DEEPLINE_BIN": os.environ.get("BAKEOFF_DEEPLINE_BIN", "deepline"),
        }
    )
    if os.environ.get("BAKEOFF_TRACE_TOOLS", "").strip() == "1":
        kept["BAKEOFF_TRACE_TOOLS"] = "1"
    return kept


def _parse_worker(stdout: str) -> dict[str, Any]:
    candidates = [
        line[len(SENTINEL) :]
        for line in stdout.splitlines()
        if line.startswith(SENTINEL)
    ]
    if not candidates:
        return {
            "ok": False,
            "error": "worker emitted no result",
            "worker_output_tail": stdout[-4000:],
        }
    try:
        return json.loads(candidates[-1])
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid worker result: {exc}"}


def _redact_error(value: Any, provider_secrets: dict[str, str]) -> str:
    message = str(value or "")
    for secret in provider_secrets.values():
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(
        r"(?i)(api[_-]?key|authorization|bearer|token)=([^&\s]+)",
        r"\1=[redacted]",
        message,
    )
    message = re.sub(
        r"(?i)(authorization\s*:\s*(?:bearer\s+)?)([^\s,;\"'}]+)",
        r"\1[redacted]",
        message,
    )
    return re.sub(r"(?i)(\bbearer\s+)([^\s,;\"'}]+)", r"\1[redacted]", message)[:4_000]


def _run_worker_process(
    command: list[str],
    *,
    input_text: str,
    environment: dict[str, str],
    timeout_seconds: int,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Run one arm in an isolated process group and reap it on every exit."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        cwd=cwd,
        start_new_session=True,
    )

    def terminate_group() -> tuple[str, str]:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return process.communicate()

    try:
        stdout, stderr = process.communicate(
            input=input_text, timeout=max(1, timeout_seconds)
        )
    except subprocess.TimeoutExpired:
        stdout, stderr = terminate_group()
        raise subprocess.TimeoutExpired(
            command, timeout_seconds, output=stdout, stderr=stderr
        )
    except BaseException:
        terminate_group()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_attempt(
    *,
    arm: str,
    icp: dict[str, Any],
    provider_secrets: dict[str, str],
    model: str,
    model_pricing: dict[str, Any],
    max_companies: int,
    python: str,
    deepline_bin: str,
    evaluation_date: str,
    timeout_seconds: int = ATTEMPT_SECONDS,
    require_live_evidence: bool = False,
    smoke_evidence_checker: SmokeEvidenceChecker | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    providers = LiveProviderTools(
        deepline_api_key=provider_secrets["DEEPLINE_API_KEY"],
        scrapingdog_api_key=provider_secrets["SCRAPINGDOG_API_KEY"],
        exa_api_key=provider_secrets.get("EXA_API_KEY", ""),
        deepline_bin=deepline_bin,
        deadline=started + timeout_seconds,
        max_provider_calls=MAX_PROVIDER_CALLS,
        max_provider_cost_usd=float(MAX_PROVIDER_COST_USD),
        evaluation_date=evaluation_date,
        allow_contacts=icp.get("contact_policy") == "contacts_v1",
        intent_details_policy=icp.get("intent_details_policy"),
    )
    error = ""
    worker: dict[str, Any] = {}
    returncode: int | None = None
    with ToolServer(providers) as server:
        env = _clean_child_env(
            provider_secrets,
            model=model,
            tool_url=server.url,
            tool_token=server.token,
            max_companies=max_companies,
            evaluation_date=evaluation_date,
            timeout_seconds=timeout_seconds,
        )
        try:
            completed = _run_worker_process(
                [python, "-m", "experiments.harness_bakeoff.worker", arm],
                input_text=json.dumps(icp),
                environment=env,
                timeout_seconds=timeout_seconds,
                cwd=Path(__file__).resolve().parents[2],
            )
            returncode = completed.returncode
            worker = _parse_worker(completed.stdout)
            if not worker.get("ok"):
                error = str(
                    worker.get("error") or completed.stderr[-2000:] or "worker failed"
                )
        except subprocess.TimeoutExpired:
            error = "attempt timeout"
            worker = {"ok": False, "error": error}
    usage = worker.get("usage") if isinstance(worker.get("usage"), dict) else {}
    token_limit_status, token_error = _token_limit_error(usage)
    if token_error:
        error = error or token_error
    model_cost, model_cost_source, model_cost_unknown_reason = _model_cost(
        usage, model_pricing
    )
    provider_cost = Decimal(str(providers.stats.estimated_cost_usd))
    if provider_cost > MAX_PROVIDER_COST_USD:
        error = error or (
            f"provider cost limit exceeded: ${provider_cost:.4f} > "
            f"${MAX_PROVIDER_COST_USD:.4f}"
        )
    total_cost = provider_cost + model_cost if model_cost is not None else None
    if total_cost is None:
        cost_limit_status = "unknown"
        error = error or "combined cost is unknown; challenger is ineligible"
    elif total_cost > MAX_COMBINED_COST_USD:
        cost_limit_status = "exceeded"
        error = error or f"combined cost limit exceeded: ${total_cost:.4f}"
    else:
        cost_limit_status = "within_limit"
    companies = (
        worker.get("companies") if isinstance(worker.get("companies"), list) else []
    )

    smoke_evidence_status = "not_required"
    smoke_evidence_check: dict[str, Any] = {}
    if require_live_evidence:
        smoke_evidence_status = "failed"
        if bool(worker.get("ok")) and not error and companies:
            remaining = min(
                SMOKE_EVIDENCE_TIMEOUT_SECONDS,
                max(0.0, started + timeout_seconds - time.monotonic()),
            )
            if remaining >= 1.0:
                checker = smoke_evidence_checker or _default_smoke_evidence_checker
                try:
                    checked = checker(companies[0], remaining)
                    if not isinstance(checked, dict) or not checked:
                        raise RuntimeError("smoke evidence checker returned no result")
                    # This data contains only public URL/status observations.
                    json.dumps(checked, allow_nan=False)
                    _validate_passed_smoke_evidence(checked)
                    smoke_evidence_check = checked
                    smoke_evidence_status = "passed"
                except Exception as exc:
                    error = error or f"live smoke evidence check failed: {exc}"
            else:
                error = error or "live smoke evidence check had no time remaining"
        elif not error:
            error = "live smoke evidence check requires a successful company result"

    elapsed = time.monotonic() - started
    if elapsed > timeout_seconds:
        error = error or (
            f"attempt time limit exceeded: {elapsed:.3f}s > {timeout_seconds}s"
        )
    error = _redact_error(
        error,
        {**provider_secrets, "BAKEOFF_TOOL_TOKEN": server.token},
    )
    eligible_for_scoring = bool(worker.get("ok")) and not error
    completed_end_to_end = (
        eligible_for_scoring
        and bool(companies)
        and smoke_evidence_status in {"not_required", "passed"}
    )
    return {
        "arm": arm,
        "icp_id": str(icp.get("icp_id") or ""),
        "ok": eligible_for_scoring,
        "eligible_for_scoring": eligible_for_scoring,
        "companies": companies,
        "completed_end_to_end": completed_end_to_end,
        "smoke_evidence_status": smoke_evidence_status,
        "smoke_evidence_check": smoke_evidence_check,
        "usage": usage,
        "provider_calls": providers.stats.calls,
        "provider_call_count": len(providers.stats.calls),
        "estimated_provider_cost_usd": round(providers.stats.estimated_cost_usd, 6),
        "model_cost_usd": (
            round(float(model_cost), 6) if model_cost is not None else None
        ),
        "model_cost_source": model_cost_source,
        "model_cost_unknown_reason": model_cost_unknown_reason,
        "estimated_combined_cost_usd": (
            round(float(total_cost), 6) if total_cost is not None else None
        ),
        "cost_limit_status": cost_limit_status,
        "token_limit_status": token_limit_status,
        "latency_seconds": round(elapsed, 3),
        "error": error,
        "worker_returncode": returncode,
    }


def _reject_symlink_components(path: Path) -> None:
    """Reject existing symlinks before creating private result directories."""
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"output path must not contain a symlink: {current}")


def _secure_directory(path: Path) -> None:
    _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(path)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"output directory must not be a symlink: {path}")
    os.chmod(path, 0o700)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    _secure_directory(path.parent)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def _initialize_jsonl(path: Path) -> None:
    """Create the durable empty source before the first long attempt."""
    _secure_directory(path.parent)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_json_number(value: Any) -> bool:
    return type(value) in {int, float} and _decimal(value) is not None


def _validate_result_shape(row: dict[str, Any]) -> None:
    """Validate the ordinary saved row and the limits needed for safe resume."""
    fields = frozenset(row)
    if fields != _RESULT_FIELDS:
        missing = sorted(_RESULT_FIELDS - fields)
        extra = sorted(fields - _RESULT_FIELDS)
        raise ValueError(f"result fields differ (missing={missing}, extra={extra})")

    phase = row["phase"]
    if phase not in {"smoke", "scored"}:
        raise ValueError("phase must be smoke or scored")
    for field, expected in _ordinary_run_fields(phase).items():
        if row[field] != expected:
            raise ValueError(f"saved result has a different {field}")
    if row["arm"] not in ARMS:
        raise ValueError("saved result has an unknown arm")
    if not isinstance(row["input"], dict):
        raise ValueError("input must be an object")
    if not isinstance(row["model"], str) or not row["model"]:
        raise ValueError("model must be a nonempty string")
    if not isinstance(row["model_pricing"], dict):
        raise ValueError("model_pricing must be an object")
    try:
        normalized_date = date.fromisoformat(str(row["evaluation_date"])).isoformat()
    except ValueError as exc:
        raise ValueError("evaluation_date must use YYYY-MM-DD") from exc
    if normalized_date != row["evaluation_date"]:
        raise ValueError("evaluation_date must be normalized")
    if type(row["repetition"]) is not int or row["repetition"] < 1:
        raise ValueError("repetition must be a positive integer")
    if type(row["order"]) is not int or not 1 <= row["order"] <= len(ARMS):
        raise ValueError("order is outside the arm range")
    expected_icp_id = str(row["input"].get("icp_id") or "")
    if not row["icp_id"] or row["icp_id"] != expected_icp_id:
        raise ValueError("icp_id does not match the saved input")
    expected_block = f"{row['icp_id']}:r{row['repetition']}"
    if row["block_id"] != expected_block:
        raise ValueError("block_id does not match the ICP and repetition")

    if type(row["ok"]) is not bool or type(row["eligible_for_scoring"]) is not bool:
        raise ValueError("ok and eligible_for_scoring must be booleans")
    if row["ok"] != row["eligible_for_scoring"]:
        raise ValueError("ok and eligible_for_scoring disagree")
    if type(row["completed_end_to_end"]) is not bool:
        raise ValueError("completed_end_to_end must be a boolean")
    if not isinstance(row["error"], str):
        raise ValueError("error must be a string")
    if row["ok"] and row["error"]:
        raise ValueError("a successful row cannot contain an error")
    if not row["ok"] and not row["error"]:
        raise ValueError("an unsuccessful row must contain an error")

    if not isinstance(row["companies"], list):
        raise ValueError("companies must be a list")
    try:
        validated_companies = validate_companies(
            row["companies"],
            max_companies=row["max_companies"],
            allow_contacts=row["input"].get("contact_policy") == "contacts_v1",
            intent_details_policy=row["input"].get("intent_details_policy"),
        )
    except Exception as exc:
        raise ValueError(
            f"companies do not match the public output schema: {exc}"
        ) from exc
    if validated_companies != row["companies"]:
        raise ValueError("companies are not saved in canonical public output form")
    if type(row["company_count"]) is not int or row["company_count"] != len(
        row["companies"]
    ):
        raise ValueError("company_count does not match companies")

    evidence_status = row["smoke_evidence_status"]
    if not isinstance(row["smoke_evidence_check"], dict):
        raise ValueError("smoke_evidence_check must be an object")
    if phase == "scored":
        if evidence_status != "not_required" or row["smoke_evidence_check"]:
            raise ValueError("scored rows cannot contain a smoke evidence check")
    elif evidence_status == "passed":
        check = row["smoke_evidence_check"]
        _validate_passed_smoke_evidence(check)
    elif evidence_status != "failed" or row["smoke_evidence_check"]:
        raise ValueError("smoke evidence status/check disagree")

    expected_completed = (
        row["ok"]
        and bool(row["companies"])
        and evidence_status in {"not_required", "passed"}
    )
    if row["completed_end_to_end"] != expected_completed:
        raise ValueError("completed_end_to_end disagrees with the saved result")

    if not isinstance(row["usage"], dict) or not isinstance(
        row["provider_calls"], list
    ):
        raise ValueError("usage and provider_calls must use ordinary JSON containers")
    if any(not isinstance(call, dict) for call in row["provider_calls"]):
        raise ValueError("each provider call must be an object")
    for field in (
        "estimated_provider_cost_usd",
        "model_cost_usd",
        "estimated_combined_cost_usd",
    ):
        if row[field] is not None and not _is_json_number(row[field]):
            raise ValueError(f"{field} must be a nonnegative JSON number or null")
    if not _is_json_number(row["latency_seconds"]):
        raise ValueError("latency_seconds must be a nonnegative JSON number")
    if row["latency_seconds"] > row["attempt_timeout_seconds"] and row["ok"]:
        raise ValueError("an over-time row cannot be successful")
    if not isinstance(row["model_cost_source"], str):
        raise ValueError("model_cost_source must be a string")
    if not isinstance(row["model_cost_unknown_reason"], str):
        raise ValueError("model_cost_unknown_reason must be a string")
    if (
        row["worker_returncode"] is not None
        and type(row["worker_returncode"]) is not int
    ):
        raise ValueError("worker_returncode must be an integer or null")

    if type(row["provider_call_count"]) is not int:
        raise ValueError("provider_call_count must be an integer")
    if row["provider_call_count"] != len(row["provider_calls"]):
        raise ValueError("provider_call_count does not match provider_calls")
    if row["provider_call_count"] > row["max_provider_calls"]:
        raise ValueError("saved result exceeds the provider-call limit")
    if row["estimated_provider_cost_usd"] is None:
        raise ValueError("provider cost must be measured")
    if row["estimated_provider_cost_usd"] > row["max_provider_cost_usd"] and row["ok"]:
        raise ValueError("an over-cost provider result cannot be successful")
    token_status = row["token_limit_status"]
    if token_status not in {"within_limit", "exceeded", "unknown"}:
        raise ValueError("token status is invalid")
    if token_status != "within_limit" and row["ok"]:
        raise ValueError(
            "an unmeasured or over-limit token result cannot be successful"
        )
    cost_status = row["cost_limit_status"]
    combined = row["estimated_combined_cost_usd"]
    if cost_status == "within_limit":
        if combined is None or combined > row["max_combined_cost_usd"]:
            raise ValueError("within-limit cost status disagrees with combined cost")
    elif cost_status == "exceeded":
        if combined is None or combined <= row["max_combined_cost_usd"]:
            raise ValueError("exceeded cost status disagrees with combined cost")
        if row["ok"]:
            raise ValueError("an over-limit cost result cannot be successful")
    elif cost_status == "unknown":
        if combined is not None or row["ok"]:
            raise ValueError("unknown cost must be null and ineligible")
    else:
        raise ValueError("cost status is invalid")

    try:
        json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"result is not plain JSON: {exc}") from exc


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"cannot resume: invalid JSON in {path} at line {line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise RuntimeError(
                    f"cannot resume: {path} line {line_number} is not a JSON object"
                )
            try:
                _validate_result_shape(value)
            except ValueError as exc:
                raise RuntimeError(
                    f"cannot resume: invalid result shape in {path} at line "
                    f"{line_number}: {exc}"
                ) from exc
            records.append(value)
    return records


@contextmanager
def _phase_lock(path: Path) -> Iterator[None]:
    _secure_directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another process is writing this phase: {path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_summary_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    fields = [
        "runner_schema",
        "phase",
        "block_id",
        "repetition",
        "order",
        "arm",
        "icp_id",
        "model",
        "evaluation_date",
        "attempt_timeout_seconds",
        "max_companies",
        "max_provider_calls",
        "max_provider_cost_usd",
        "tool_timeout_seconds",
        "max_input_tokens",
        "max_output_tokens",
        "max_combined_cost_usd",
        "reasoning_effort",
        "fresh_session",
        "ok",
        "eligible_for_scoring",
        "completed_end_to_end",
        "smoke_evidence_status",
        "company_count",
        "provider_call_count",
        "estimated_provider_cost_usd",
        "model_cost_usd",
        "model_cost_source",
        "model_cost_unknown_reason",
        "estimated_combined_cost_usd",
        "cost_limit_status",
        "token_limit_status",
        "latency_seconds",
        "error",
    ]
    _secure_directory(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _ordinary_run_fields(phase: str) -> dict[str, Any]:
    """Return the plain, user-visible configuration enforced for one phase."""
    return {
        "runner_schema": RUNNER_SCHEMA,
        "attempt_timeout_seconds": ATTEMPT_SECONDS,
        "max_companies": 1 if phase == "smoke" else MAX_COMPANIES,
        "max_provider_calls": MAX_PROVIDER_CALLS,
        "max_provider_cost_usd": float(MAX_PROVIDER_COST_USD),
        "tool_timeout_seconds": TOOL_TIMEOUT_SECONDS,
        "max_input_tokens": int(MAX_INPUT_TOKENS),
        "max_output_tokens": int(MAX_OUTPUT_TOKENS),
        "max_combined_cost_usd": float(MAX_COMBINED_COST_USD),
        "reasoning_effort": REASONING_EFFORT,
        "fresh_session": True,
    }


def _planned_attempts(
    *,
    phase: str,
    icps: list[dict[str, Any]],
    repetitions: int,
    model: str,
    model_pricing: dict[str, Any],
    seed: int,
    evaluation_date: str,
    arms: tuple[str, ...] = ARMS,
) -> list[dict[str, Any]]:
    if phase == "smoke" and len(icps) != 1:
        raise ValueError("smoke requires exactly one ICP")
    target_icps = icps
    target_repetitions = 1 if phase == "smoke" else repetitions
    plans: list[dict[str, Any]] = []
    for repetition in range(1, target_repetitions + 1):
        for icp in target_icps:
            block_id = f"{icp['icp_id']}:r{repetition}"
            block_arms = list(arms)
            random.Random(f"{seed}:{block_id}").shuffle(block_arms)
            for order, arm in enumerate(block_arms, start=1):
                plan = {
                    "phase": phase,
                    "block_id": block_id,
                    "repetition": repetition,
                    "order": order,
                    "arm": arm,
                    "icp_id": str(icp["icp_id"]),
                    "input": icp,
                    "model": model,
                    "model_pricing": model_pricing,
                    "evaluation_date": evaluation_date,
                }
                plan.update(_ordinary_run_fields(phase))
                plans.append(plan)
    return plans


def _validate_resume(
    path: Path,
    records: list[dict[str, Any]],
    plans: list[dict[str, Any]],
) -> None:
    if len(records) > len(plans):
        raise RuntimeError(
            f"cannot resume: {path} has more rows than the current run plan"
        )
    seen: set[tuple[Any, Any, Any]] = set()
    for line_number, row in enumerate(records, start=1):
        try:
            _validate_result_shape(row)
        except ValueError as exc:
            raise RuntimeError(
                f"cannot resume: invalid result shape in {path} at line "
                f"{line_number}: {exc}"
            ) from exc
        key = (row.get("phase"), row.get("block_id"), row.get("arm"))
        if key in seen:
            raise RuntimeError(
                f"cannot resume: duplicate attempt at {path} line {line_number}"
            )
        seen.add(key)
        expected = plans[line_number - 1]
        for field in _PLAN_FIELDS:
            if row.get(field) != expected[field]:
                raise RuntimeError(
                    f"cannot resume: {path} line {line_number} does not match "
                    f"the current plan field {field!r}"
                )


def _smoke_gate(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Separate narrow zero-score outcomes from all other smoke failures."""
    zero_outcomes: list[str] = []
    integration_failures: list[str] = []
    for row in rows:
        if row.get("completed_end_to_end"):
            continue
        arm = str(row.get("arm") or "")
        provider_calls = row.get("provider_call_count")
        discovery_success = any(
            isinstance(call, dict)
            and call.get("status") == "ok"
            and call.get("tool") == "hunter_discover"
            for call in row.get("provider_calls", [])
        )
        completed_empty_result = (
            row.get("worker_returncode") == 0
            and isinstance(provider_calls, int)
            and provider_calls > 0
            and discovery_success
            and row.get("company_count") == 0
            and row.get("companies") == []
            and row.get("cost_limit_status") == "within_limit"
            and row.get("token_limit_status") == "within_limit"
            and row.get("latency_seconds", float("inf"))
            <= row.get("attempt_timeout_seconds", 0)
            and row.get("smoke_evidence_status") == "failed"
            and row.get("smoke_evidence_check") == {}
            and row.get("error")
            == "live smoke evidence check requires a successful company result"
        )
        if completed_empty_result:
            zero_outcomes.append(arm)
        else:
            integration_failures.append(arm)
    return sorted(set(zero_outcomes)), sorted(set(integration_failures))


def _execute(
    *,
    phase: str,
    icps: list[dict[str, Any]],
    repetitions: int,
    provider_secrets: dict[str, str],
    model: str,
    model_pricing: dict[str, Any],
    output: Path,
    python: str,
    deepline_bin: str,
    seed: int,
    resume: bool,
    evaluation_date: str,
    arms: tuple[str, ...],
) -> list[dict[str, Any]]:
    destination = output / f"{phase}.jsonl"
    summary = output / f"{phase}.csv"
    plans = _planned_attempts(
        phase=phase,
        icps=icps,
        repetitions=repetitions,
        model=model,
        model_pricing=model_pricing,
        seed=seed,
        evaluation_date=evaluation_date,
        arms=arms,
    )
    with _phase_lock(output / f"{phase}.lock"):
        if destination.exists():
            if not resume:
                raise RuntimeError(
                    f"refusing to overwrite prior results: {destination}"
                )
            records = _load_jsonl(destination)
            _validate_resume(destination, records, plans)
        else:
            if summary.exists():
                raise RuntimeError(
                    f"cannot start safely: summary exists without its JSONL source: {summary}"
                )
            records = []
            _initialize_jsonl(destination)
        _write_summary_csv(summary, records)
        if records:
            print(
                json.dumps(
                    {
                        "phase": phase,
                        "resume": True,
                        "saved_attempts": len(records),
                        "remaining_attempts": len(plans) - len(records),
                    }
                ),
                flush=True,
            )

        for plan in plans[len(records) :]:
            row = run_attempt(
                arm=plan["arm"],
                icp=plan["input"],
                provider_secrets=provider_secrets,
                model=model,
                model_pricing=model_pricing,
                max_companies=plan["max_companies"],
                python=python,
                deepline_bin=deepline_bin,
                evaluation_date=evaluation_date,
                timeout_seconds=plan["attempt_timeout_seconds"],
                require_live_evidence=phase == "smoke",
            )
            row.update(plan)
            row["company_count"] = len(row["companies"])
            _validate_result_shape(row)
            _append_jsonl(destination, row)
            records.append(row)
            _write_summary_csv(summary, records)
            print(
                json.dumps(
                    {
                        "phase": phase,
                        "block": plan["block_id"],
                        "order": plan["order"],
                        "arm": plan["arm"],
                        "ok": row["ok"],
                        "end_to_end": row["completed_end_to_end"],
                        "companies": row["company_count"],
                        "seconds": row["latency_seconds"],
                        "cost_usd": row["estimated_combined_cost_usd"],
                        "cost_status": row["cost_limit_status"],
                        "token_status": row["token_limit_status"],
                        "error": row["error"][:180],
                    }
                ),
                flush=True,
            )
        return records


def _same_or_descendant(path: Path, parent: Path) -> bool:
    path_key = str(path.resolve()).rstrip(os.sep).casefold()
    parent_key = str(parent.resolve()).rstrip(os.sep).casefold()
    return path_key == parent_key or path_key.startswith(parent_key + os.sep)


def _safe_output_path(path: Path, repository: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    _reject_symlink_components(absolute)
    resolved = absolute.resolve()
    if _same_or_descendant(resolved, repository):
        raise ValueError("--output must be outside the repository")
    # Never chmod/write through a broad system or user directory supplied by
    # mistake. Normal experiment paths are descendants of these locations.
    for protected in (
        Path(resolved.anchor),
        Path.home().resolve(),
        repository.resolve(),
    ):
        if _same_or_descendant(protected, resolved):
            raise ValueError("--output must be a dedicated result directory")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "smoke", "scored", "all"))
    parser.add_argument(
        "--icp-file",
        type=Path,
        default=(
            Path(os.environ["BAKEOFF_ICP_FILE"])
            if os.environ.get("BAKEOFF_ICP_FILE")
            else None
        ),
        help="JSON ICP file outside this repository (or set BAKEOFF_ICP_FILE)",
    )
    parser.add_argument("--icp-id", action="append", dest="icp_ids")
    parser.add_argument(
        "--smoke-icp-id",
        help="ICP used for smoke tests; defaults to the first selected ICP",
    )
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--deepline-bin", default=os.environ.get("BAKEOFF_DEEPLINE_BIN", "deepline")
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--evaluation-date", default=date.today().isoformat())
    parser.add_argument("--arm", action="append", dest="arms", choices=ARMS)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="append only the missing suffix of a matching phase JSONL file",
    )
    parser.add_argument(
        "--allow-smoke-zero-outcomes",
        action="store_true",
        help=(
            "continue after a harness completes live execution with no company; "
            "every other smoke failure still blocks"
        ),
    )
    args = parser.parse_args(argv)

    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")
    selected_arms = tuple(args.arms or ARMS)
    if len(set(selected_arms)) != len(selected_arms):
        parser.error("--arm values must be unique")
    try:
        evaluation_date = date.fromisoformat(args.evaluation_date).isoformat()
    except ValueError:
        parser.error("--evaluation-date must use YYYY-MM-DD")
    repository = Path(__file__).resolve().parents[2]
    try:
        args.output = _safe_output_path(args.output, repository)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    loaded_icps: list[dict[str, Any]] = []
    smoke_icps: list[dict[str, Any]] = []
    scored_icps: list[dict[str, Any]] = []
    if args.phase != "preflight":
        if args.icp_file is None:
            parser.error(
                "--icp-file is required for smoke and scored runs "
                "(or set BAKEOFF_ICP_FILE)"
            )
        try:
            icp_file = external_icp_file(args.icp_file, repository)
            all_icps = load_icps(icp_file=icp_file)
            all_icp_ids = tuple(str(icp["icp_id"]) for icp in all_icps)
            load_icp_ids, smoke_icp_ids, scored_icp_ids = _run_plan_icp_ids(
                args.phase,
                all_icp_ids,
                args.icp_ids,
                args.smoke_icp_id,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        icp_by_id = {str(icp["icp_id"]): icp for icp in all_icps}
        loaded_icps = [icp_by_id[icp_id] for icp_id in load_icp_ids]
        smoke_icps = [icp_by_id[icp_id] for icp_id in smoke_icp_ids]
        scored_icps = [icp_by_id[icp_id] for icp_id in scored_icp_ids]

    providers = load_provider_secrets()
    provider_preflight = deepline_preflight(
        deepline_api_key=providers["DEEPLINE_API_KEY"],
        deepline_bin=args.deepline_bin,
    )
    print(json.dumps({"deepline_preflight": provider_preflight}), flush=True)
    preflight = select_model(providers["OPENROUTER_API_KEY"])
    _secure_directory(args.output)
    print(json.dumps({"preflight": preflight}), flush=True)
    if args.phase == "preflight":
        providers.clear()
        return 0
    print(json.dumps({"icps": describe_icps(loaded_icps)}), flush=True)
    model = str(preflight["selected"])
    model_pricing = (
        preflight.get("pricing") if isinstance(preflight.get("pricing"), dict) else {}
    )
    smoke: list[dict[str, Any]] | None = None
    if args.phase in {"smoke", "all"}:
        smoke = _execute(
            phase="smoke",
            icps=smoke_icps,
            repetitions=1,
            provider_secrets=providers,
            model=model,
            model_pricing=model_pricing,
            output=args.output,
            python=args.python,
            deepline_bin=args.deepline_bin,
            seed=args.seed,
            resume=args.resume,
            evaluation_date=evaluation_date,
            arms=selected_arms,
        )
        zero_outcomes, integration_failed = _smoke_gate(smoke)
        if integration_failed or (zero_outcomes and not args.allow_smoke_zero_outcomes):
            print(
                json.dumps(
                    {
                        "smoke_gate": "failed",
                        "integration_failures": integration_failed,
                        "zero_score_outcomes": zero_outcomes,
                    }
                ),
                flush=True,
            )
            return 1
        if zero_outcomes:
            print(
                json.dumps(
                    {
                        "smoke_gate": "zero_score_outcomes_allowed",
                        "zero_score_outcomes": zero_outcomes,
                    }
                ),
                flush=True,
            )
    if args.phase in {"scored", "all"}:
        if smoke is None:
            smoke_path = args.output / "smoke.jsonl"
            if not smoke_path.exists():
                raise RuntimeError("scored phase requires a completed smoke file")
            smoke_rows = _load_jsonl(smoke_path)
            smoke_plans = _planned_attempts(
                phase="smoke",
                icps=smoke_icps,
                repetitions=1,
                model=model,
                model_pricing=model_pricing,
                seed=args.seed,
                evaluation_date=evaluation_date,
                arms=selected_arms,
            )
            _validate_resume(smoke_path, smoke_rows, smoke_plans)
            if len(smoke_rows) != len(smoke_plans):
                raise RuntimeError(
                    "scored phase requires every selected live smoke attempt"
                )
        else:
            smoke_rows = smoke
        zero_outcomes, integration_failed = _smoke_gate(smoke_rows)
        if integration_failed or (zero_outcomes and not args.allow_smoke_zero_outcomes):
            raise RuntimeError(
                "live smoke has blocking failures: "
                + ", ".join(sorted(set(integration_failed + zero_outcomes)))
            )
        if zero_outcomes:
            print(
                json.dumps(
                    {
                        "scored_after_smoke_zero_outcomes": zero_outcomes,
                    }
                ),
                flush=True,
            )
        _execute(
            phase="scored",
            icps=scored_icps,
            repetitions=args.repetitions,
            provider_secrets=providers,
            model=model,
            model_pricing=model_pricing,
            output=args.output,
            python=args.python,
            deepline_bin=args.deepline_bin,
            seed=args.seed,
            resume=args.resume,
            evaluation_date=evaluation_date,
            arms=selected_arms,
        )
    providers.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

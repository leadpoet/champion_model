"""PydanticAI harness for live lead sourcing."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelRetry, RunContext, Tool, ToolOutput, messages
from pydantic_ai.capabilities import PrepareTools, ProcessHistory
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage, UsageLimits

from experiments.harness_bakeoff.contacts import ContactLookup
from experiments.harness_bakeoff.models import (
    CompaniesResult,
    _canonical_company_stage,
    validate_companies,
)
from experiments.harness_bakeoff.prompt import SYSTEM_PROMPT, build_prompt
from experiments.harness_bakeoff.tool_client import ToolClient
from experiments.harness_bakeoff.tool_contract import (
    TOOL_DESCRIPTIONS,
    tool_input_schema,
)


DEFAULT_MODEL = "openai/gpt-5.6-sol"
LAST_USAGE: dict[str, Any] = {}
_RESEARCH_TOOL_NAMES = frozenset(
    {
        "search_companies",
        "get_company_profile",
        "get_company_events",
        "search_web",
        "fetch_page",
        "get_company_contact",
    }
)
_COMPACTABLE_TOOL_NAMES = _RESEARCH_TOOL_NAMES - {"fetch_page"}
_MAX_PRIOR_TOOL_RESULT_BYTES = 1_200
_FINALIZE_INPUT_TOKENS = 82_000
_FINALIZE_REQUESTS = 45
_FINALIZE_TOOL_CALLS = 44
_ARENA_FINALIZE_RESERVE_SECONDS = 45.0
_CONTACT_RESERVE_SECONDS = 45.0
_CONTACT_SUBMIT_RESERVE_SECONDS = 2.0
_CONTACT_MIN_CALL_SECONDS = 1.0
_CONTACT_CALLS_PER_COMPANY = 2  # Minimum: one search and one profile/email lookup.
_ARENA_REQUEST_OUTPUT_TOKENS = 4_096
_RUN_OUTPUT_TOKENS_LIMIT = 15_000
_FINALIZE_MARKER = "[research-budget-reserve]"
_FINALIZE_PROMPT = (
    f"{_FINALIZE_MARKER} Research is complete because the run must reserve capacity "
    "for its final structured output. Do not request more research tools. Call "
    "submit_companies now with only the strongest companies supported by evidence already "
    "collected. Preserve exact evidence dates, URLs, and quotes. Omit any company that is "
    "not fully verified; do not invent missing facts."
)
_KNOWN_COMPANY_STAGES = frozenset(
    {
        "Seed",
        "Bootstrapped",
        "Series A",
        "Series B",
        "Series C+",
        "Private Equity",
        "Public",
    }
)


def _filter_explicit_stage_conflicts(
    icp: dict[str, Any], companies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop only returned canonical stages that contradict a canonical ICP stage."""

    requested = _canonical_company_stage(icp.get("company_stage"))
    if not isinstance(requested, str) or requested not in _KNOWN_COMPANY_STAGES:
        return companies

    return [
        company
        for company in companies
        if not (
            (returned := _canonical_company_stage(company.get("company_stage")))
            in _KNOWN_COMPANY_STAGES
            and returned != requested
        )
    ]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")


def _ordered_fit_evidence_urls(urls: list[str]) -> list[str]:
    """Keep direct evidence ahead of generic LinkedIn profile hints."""

    def is_linkedin(url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host == "linkedin.com" or host.endswith(".linkedin.com")

    return sorted(urls, key=is_linkedin)


def _key_priority(key: Any) -> tuple[int, str]:
    normalized = str(key).lower()
    if any(token in normalized for token in ("url", "link", "domain", "website")):
        return (0, normalized)
    if any(
        token in normalized
        for token in (
            "date",
            "time",
            "_at",
            "financing",
            "quote",
            "snippet",
            "title",
            "description",
        )
    ):
        return (1, normalized)
    if any(
        token in normalized
        for token in (
            "company",
            "name",
            "industry",
            "employee",
            "stage",
            "country",
            "state",
            "location",
        )
    ):
        return (2, normalized)
    return (3, normalized)


def _compact_tool_value(
    value: Any,
    *,
    string_chars: int,
    list_items: int,
    dict_items: int,
    depth: int = 0,
    field_name: str = "",
) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, str):
        if any(
            token in field_name.lower()
            for token in ("url", "link", "domain", "website", "date", "time")
        ):
            return value
        return value if len(value) <= string_chars else value[:string_chars] + "..."
    if isinstance(value, list):
        return [
            _compact_tool_value(
                item,
                string_chars=string_chars,
                list_items=list_items,
                dict_items=dict_items,
                depth=depth + 1,
                field_name=field_name,
            )
            for item in value[:list_items]
        ]
    if isinstance(value, dict):
        # This projector is already bounded and identity-validated. Keep every
        # proof field exact; the enclosing 1,200-byte check still applies.
        if field_name == "linkedin_structured_evidence":
            return value
        prioritized = sorted(value.items(), key=lambda item: _key_priority(item[0]))
        return {
            str(key): _compact_tool_value(
                item,
                string_chars=string_chars,
                list_items=list_items,
                dict_items=dict_items,
                depth=depth + 1,
                field_name=str(key),
            )
            for key, item in prioritized[:dict_items]
        }
    return value


def _without_event_source_url_hints(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_event_source_url_hints(item)
            for key, item in value.items()
            if key != "untrusted_source_url_hints"
        }
    if isinstance(value, list):
        return [_without_event_source_url_hints(item) for item in value]
    return value


def _append_event_source_url_hints(compacted: Any, source: Any) -> Any:
    """Use spare history bytes for whole hints without replacing prior facts."""

    result_events = compacted.get("events") if isinstance(compacted, dict) else None
    source_events = source.get("events") if isinstance(source, dict) else None
    if not isinstance(result_events, list) or not isinstance(source_events, list):
        return compacted
    for result_event, source_event in zip(result_events, source_events):
        result_data = result_event.get("data") if isinstance(result_event, dict) else None
        source_data = source_event.get("data") if isinstance(source_event, dict) else None
        result_items = result_data.get("items") if isinstance(result_data, dict) else None
        source_items = source_data.get("items") if isinstance(source_data, dict) else None
        if not isinstance(result_items, list) or not isinstance(source_items, list):
            continue
        for result_item, source_item in zip(result_items, source_items):
            result_attributes = (
                result_item.get("attributes")
                if isinstance(result_item, dict)
                else None
            )
            source_attributes = (
                source_item.get("attributes")
                if isinstance(source_item, dict)
                else None
            )
            if not isinstance(result_attributes, dict) or not isinstance(
                source_attributes, dict
            ):
                continue
            hints = source_attributes.get("untrusted_source_url_hints")
            if not isinstance(hints, list):
                continue
            for hint in hints:
                if not isinstance(hint, str):
                    continue
                retained = result_attributes.setdefault(
                    "untrusted_source_url_hints", []
                )
                retained.append(hint)
                if len(_json_bytes(compacted)) > _MAX_PRIOR_TOOL_RESULT_BYTES:
                    retained.pop()
                    if not retained:
                        result_attributes.pop("untrusted_source_url_hints")
    return compacted


def _bounded_history_tool_result(value: Any, *, tool_name: str = "") -> Any:
    """Keep prior evidence useful without replaying full provider payloads forever."""

    if len(_json_bytes(value)) <= _MAX_PRIOR_TOOL_RESULT_BYTES:
        return value
    if tool_name == "get_company_events":
        without_hints = _without_event_source_url_hints(value)
        compacted = _bounded_history_tool_result(without_hints)
        return _append_event_source_url_hints(compacted, value)
    results = value.get("results") if isinstance(value, dict) else None
    if (
        tool_name == "search_web"
        and isinstance(results, list)
        and 5 < len(results) <= 10
        and all(
            isinstance(row, dict) and isinstance(row.get("url"), str)
            for row in results
        )
    ):
        # Keep the discovery queue, not only the first few search ranks. Full
        # evidence was shown on arrival; these are bounded lookup hints.
        for text_chars in (80, 60, 40, 20, 0):
            hints = []
            for row in results:
                hint = {"url": row["url"]}
                key = (
                    "snippet"
                    if isinstance(row.get("snippet"), str) and row["snippet"]
                    else "title"
                )
                if text_chars and isinstance(row.get(key), str):
                    hint[key] = row[key][:text_chars] + (
                        "..." if len(row[key]) > text_chars else ""
                    )
                hints.append(hint)
            compacted = {**value, "results": hints, "prior_result_truncated": True}
            if len(_json_bytes(compacted)) <= _MAX_PRIOR_TOOL_RESULT_BYTES:
                return compacted
    for string_chars, list_items, dict_items in (
        (320, 5, 40),
        (180, 5, 30),
        (120, 4, 24),
        (80, 3, 18),
        (60, 2, 14),
        (60, 1, 10),
        (50, 1, 10),
        (40, 1, 6),
    ):
        compacted = _compact_tool_value(
            value,
            string_chars=string_chars,
            list_items=list_items,
            dict_items=dict_items,
        )
        if isinstance(compacted, dict):
            compacted["prior_result_truncated"] = True
        else:
            compacted = {
                "prior_result_truncated": True,
                "result": compacted,
            }
        if len(_json_bytes(compacted)) <= _MAX_PRIOR_TOOL_RESULT_BYTES:
            return compacted

    raw = _json_bytes(value).decode("utf-8", errors="replace")
    preview = raw[:800]
    fallback = {"prior_result_truncated": True, "json_preview": preview}
    while len(_json_bytes(fallback)) > _MAX_PRIOR_TOOL_RESULT_BYTES:
        preview = preview[: max(1, len(preview) // 2)]
        fallback["json_preview"] = preview
    return fallback


def _finalization_due(
    usage: RunUsage,
    *,
    finalize_at: float | None = None,
    force_finalize: bool = False,
    input_token_limit: int | None = _FINALIZE_INPUT_TOKENS,
    tool_calls_limit: int | None = _FINALIZE_TOOL_CALLS,
) -> bool:
    return (
        force_finalize
        or (input_token_limit is not None and usage.input_tokens >= input_token_limit)
        or usage.requests >= _FINALIZE_REQUESTS
        or (tool_calls_limit is not None and usage.tool_calls >= tool_calls_limit)
        or (finalize_at is not None and time.monotonic() >= finalize_at)
    )


def _process_history(
    context: RunContext[Any],
    history: list[messages.ModelMessage],
    *,
    finalize_at: float | None = None,
    force_finalize: bool = False,
    input_token_limit: int | None = _FINALIZE_INPUT_TOKENS,
    tool_calls_limit: int | None = _FINALIZE_TOOL_CALLS,
) -> list[messages.ModelMessage]:
    """Project old tool payloads and add one native final-output warning."""

    research_return_requests = {
        message_index
        for message_index, message in enumerate(history)
        if isinstance(message, messages.ModelRequest)
        if any(
            isinstance(part, messages.ToolReturnPart)
            and part.tool_name in _RESEARCH_TOOL_NAMES
            for part in message.parts
        )
    }
    latest_return_request = max(research_return_requests, default=None)
    compactable_returns = [
        (message_index, part_index)
        for message_index, message in enumerate(history)
        if isinstance(message, messages.ModelRequest)
        for part_index, part in enumerate(message.parts)
        if isinstance(part, messages.ToolReturnPart)
        and part.tool_name in _COMPACTABLE_TOOL_NAMES
    ]
    prior_returns = {
        (message_index, part_index)
        for message_index, part_index in compactable_returns
        if message_index != latest_return_request
    }
    processed: list[messages.ModelMessage] = []
    for message_index, message in enumerate(history):
        if not isinstance(message, messages.ModelRequest):
            processed.append(message)
            continue
        parts = [
            dataclasses.replace(
                part,
                content=_bounded_history_tool_result(
                    part.content, tool_name=part.tool_name
                ),
            )
            if (message_index, part_index) in prior_returns
            else part
            for part_index, part in enumerate(message.parts)
        ]
        processed.append(dataclasses.replace(message, parts=parts))

    if _finalization_due(
        context.usage,
        finalize_at=finalize_at,
        force_finalize=force_finalize,
        input_token_limit=input_token_limit,
        tool_calls_limit=tool_calls_limit,
    ):
        already_warned = any(
            isinstance(part, messages.UserPromptPart)
            and isinstance(part.content, str)
            and _FINALIZE_MARKER in part.content
            for message in processed
            if isinstance(message, messages.ModelRequest)
            for part in message.parts
        )
        if not already_warned:
            last = processed[-1]
            if not isinstance(last, messages.ModelRequest):
                raise RuntimeError(
                    "processed PydanticAI history must end in a model request"
                )
            processed[-1] = dataclasses.replace(
                last, parts=[*last.parts, messages.UserPromptPart(_FINALIZE_PROMPT)]
            )
    return processed


def _prepare_research_tools(
    context: RunContext[Any],
    tool_definitions: list[ToolDefinition],
    *,
    finalize_at: float | None = None,
    force_finalize: bool = False,
    input_token_limit: int | None = _FINALIZE_INPUT_TOKENS,
    tool_calls_limit: int | None = _FINALIZE_TOOL_CALLS,
) -> list[ToolDefinition]:
    """Leave only the output tool available once the final-output reserve starts."""

    return (
        []
        if _finalization_due(
            context.usage,
            finalize_at=finalize_at,
            force_finalize=force_finalize,
            input_token_limit=input_token_limit,
            tool_calls_limit=tool_calls_limit,
        )
        else tool_definitions
    )


def _run_usage_limits(*, arena_mode: bool = False) -> UsageLimits:
    """Keep cumulative run limits separate from the per-request model cap."""

    return UsageLimits(
        cost_limit=Decimal("4"),
        request_limit=60,
        # Arena's semantic/provider budget rejects excess research calls. Do
        # not let a batched research response consume the final output call.
        tool_calls_limit=None if arena_mode else 60,
        # Arena already meters every model/provider call against its hard
        # dollar cap. Re-reading cached evidence must not end affordable work.
        input_tokens_limit=None if arena_mode else 120_000,
        output_tokens_limit=_RUN_OUTPUT_TOKENS_LIMIT,
    )


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _positive_integer(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be from 1 through {maximum}")
    return value


def _positive_float(name: str, default: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0 or value > maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum:g}")
    return value


class _ToolBudget:
    def __init__(self, client: ToolClient, maximum: int, contact_reserve: int = 0) -> None:
        self.client = client
        self.maximum = maximum
        self.research_maximum = max(0, maximum - contact_reserve)
        self.calls = 0

    def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        dispatch: Any = None,
        research: bool = False,
    ) -> Any:
        try:
            if name != "submit_companies":
                maximum = (
                    self.research_maximum
                    if research or name in _RESEARCH_TOOL_NAMES else self.maximum
                )
                if self.calls >= maximum:
                    raise RuntimeError(f"provider-call limit of {maximum} reached")
                self.calls += 1
            provider_call = dispatch if dispatch is not None else self.client.call
            return provider_call(name, arguments)
        except Exception as exc:
            if name == "submit_companies":
                raise
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}


class _DeadlineProviderCall:
    """Bound synchronous provider calls to their remaining run window."""

    def __init__(
        self,
        call: Any,
        client: Any,
        deadline: float,
        *,
        reserve_seconds: float = _CONTACT_SUBMIT_RESERVE_SECONDS,
        deadline_error: str = "contact provider deadline reached",
        clock: Any = time.monotonic,
    ) -> None:
        self._call = call
        self._client = client
        self._deadline = deadline
        self._reserve_seconds = reserve_seconds
        self._deadline_error = deadline_error
        self._clock = clock

    def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        available = self._deadline - self._clock() - self._reserve_seconds
        if available < _CONTACT_MIN_CALL_SECONDS:
            raise RuntimeError(self._deadline_error)
        original_timeout = getattr(self._client, "timeout", None)
        has_request_deadline = hasattr(self._client, "request_deadline")
        original_request_deadline = getattr(self._client, "request_deadline", None)
        if isinstance(original_timeout, (int, float)):
            self._client.timeout = min(float(original_timeout), available)
        if has_request_deadline:
            request_deadline = self._deadline - self._reserve_seconds
            if isinstance(original_request_deadline, (int, float)):
                request_deadline = min(request_deadline, original_request_deadline)
            self._client.request_deadline = request_deadline
        try:
            return self._call(name, arguments)
        finally:
            if isinstance(original_timeout, (int, float)):
                self._client.timeout = original_timeout
            if has_request_deadline:
                self._client.request_deadline = original_request_deadline


def _contact_time_reserve(run_timeout: float, *, arena_mode: bool) -> float:
    if arena_mode:
        return 0.0
    return min(
        _CONTACT_RESERVE_SECONDS,
        max(0.0, run_timeout - _CONTACT_MIN_CALL_SECONDS),
    )


def _contact_call_reserve(
    max_companies: int, *, contact_enabled: bool, arena_mode: bool
) -> int:
    if arena_mode:
        return 0
    reserve = _CONTACT_CALLS_PER_COMPANY * max_companies if contact_enabled else 0
    return reserve


async def _run(icp: dict[str, Any]) -> list[dict[str, Any]]:
    arena_mode = bool(str(os.environ.get("LAB_ARENA_WORKER_SOCKET") or "").strip())
    api_key = "arena-host" if arena_mode else _required_environment("OPENROUTER_API_KEY")
    model_name = os.environ.get("BAKEOFF_OPENROUTER_MODEL", DEFAULT_MODEL).strip()
    if not model_name:
        raise RuntimeError("BAKEOFF_OPENROUTER_MODEL cannot be empty")

    max_companies = _positive_integer(
        "LAB_ARENA_COMPANY_LIMIT" if arena_mode else "BAKEOFF_MAX_COMPANIES",
        5,
        5,
    )
    max_provider_calls = _positive_integer(
        "BAKEOFF_MAX_PROVIDER_CALLS", 60 if arena_mode else 30, 100
    )
    run_timeout = _positive_float(
        "BAKEOFF_RUN_TIMEOUT_SECONDS",
        285.0 if arena_mode else 720.0,
        3600.0,
    )
    tool_timeout = _positive_float("BAKEOFF_TOOL_TIMEOUT_SECONDS", 90.0, 600.0)
    contact_enabled = (
        icp.get("contact_policy") == "contacts_v1"
        and isinstance(icp.get("target_roles"), list)
        and bool(icp["target_roles"])
    )
    contact_call_reserve = _contact_call_reserve(
        max_companies,
        contact_enabled=contact_enabled,
        arena_mode=arena_mode,
    )
    run_started_at = time.monotonic()
    run_deadline = run_started_at + run_timeout
    contact_reserve = (
        _contact_time_reserve(run_timeout, arena_mode=arena_mode)
        if contact_enabled
        else 0.0
    )
    model_deadline = run_deadline - contact_reserve
    tool_client: Any = None
    arena_http_client: httpx.AsyncClient | None = None

    async def close_resources() -> None:
        close = getattr(tool_client, "close", None)
        if callable(close):
            close()
        if arena_http_client is not None:
            await arena_http_client.aclose()

    try:
        if arena_mode:
            from arena_transport import ArenaToolClient, arena_openrouter_http_client

            tool_client = ArenaToolClient(timeout=tool_timeout)
            tool_client.allow_contacts = contact_enabled
            arena_http_client = arena_openrouter_http_client(timeout=120.0)
            openai_client = AsyncOpenAI(
                api_key=api_key,
                base_url="http://openrouter.ai/api/v1",
                http_client=arena_http_client,
            )
            provider = OpenRouterProvider(openai_client=openai_client)
        else:
            tool_client = ToolClient(timeout=tool_timeout)
            provider = OpenRouterProvider(api_key=api_key)
    except Exception:
        await close_resources()
        raise
    budget = _ToolBudget(tool_client, max_provider_calls, contact_call_reserve)
    contact_lookup = ContactLookup(icp, allow_role_selection=arena_mode)
    research_dispatch: Any = None

    def early_contact_call(name: str, arguments: dict[str, Any]) -> Any:
        # Contact checks share the bounded Arena research capacity.
        return budget.call(name, arguments, dispatch=research_dispatch, research=True)

    def get_company_contact(
        company_name: str,
        company_website: str,
        company_linkedin: str,
        selected_observed_role: str = "",
        role_query_hints: list[str] | None = None,
    ) -> Any:
        company = {
            "company_name": company_name,
            "company_website": company_website,
            "company_linkedin": company_linkedin,
        }
        try:
            contact = contact_lookup.find(
                company,
                early_contact_call,
                retry_unavailable=arena_mode,
                selected_observed_role=selected_observed_role,
                role_query_hints=role_query_hints,
            )
        except ValueError as exc:
            # Keep an invented or stale role choice inside the model correction
            # loop. ContactLookup rejects it before any provider call.
            raise ModelRetry(str(exc)) from exc
        # The finalizer attaches the cached full provider-bound contact. The
        # model only needs to know whether this candidate has a suitable role.
        lookup_status = contact_lookup.status(company)
        result = {
            "contact_found": (
                None if lookup_status == "unavailable" else contact is not None
            ),
            "lookup_status": lookup_status,
            "role": contact["role"] if contact else None,
            "location": contact["location"] if contact else None,
        }
        if lookup_status == "role_selection_required":
            result["contact_found"] = None
            result["observed_role_options"] = contact_lookup.role_options(company)
        return result

    def search_companies(
        query: str,
        industry: str = "",
        geography: str = "",
        employee_count: list[str] = [],
        limit: int = 5,
    ) -> Any:
        """Discover candidate companies with Deepline and the supplied ICP filters."""

        return budget.call(
            "search_companies",
            {
                "query": query,
                "industry": industry,
                "geography": geography,
                "employee_count": employee_count,
                "limit": limit,
            },
            dispatch=research_dispatch,
        )

    def get_company_profile(domain: str, company_linkedin: str = "") -> Any:
        """Get current company profile evidence for one company domain."""

        arguments = {"domain": domain}
        if company_linkedin:
            arguments["company_linkedin"] = company_linkedin
        return budget.call(
            "get_company_profile",
            arguments,
            dispatch=research_dispatch,
        )

    def get_company_events(
        domain: str,
        categories: list[str] = [],
        job_category: str = "",
        limit: int = 5,
    ) -> Any:
        """Find events, optionally filtering jobs by one coarse provider category."""

        return budget.call(
            "get_company_events",
            {
                "domain": domain,
                "categories": categories,
                "job_category": job_category,
                "limit": limit,
            },
            dispatch=research_dispatch,
        )

    def search_web(
        query: str,
        mode: str = "search",
        limit: int = 10,
        recency_days: int | None = None,
    ) -> Any:
        """Search the public web, news, or jobs for evidence."""

        return budget.call(
            "search_web",
            {
                "query": query,
                "mode": mode,
                "limit": limit,
                "recency_days": recency_days,
            },
            dispatch=research_dispatch,
        )

    def fetch_page(url: str, max_chars: int = 4000) -> Any:
        """Fetch one public evidence page and return its extracted text."""

        return budget.call(
            "fetch_page",
            {"url": url, "max_chars": max_chars},
            dispatch=research_dispatch,
        )

    max_output_tokens = (
        _ARENA_REQUEST_OUTPUT_TOKENS if arena_mode else _RUN_OUTPUT_TOKENS_LIMIT
    )
    arena_finalize_at: float | None = None

    def research_capacity_exhausted() -> bool:
        if budget.calls >= max(1, budget.research_maximum):
            return True
        return bool(
            arena_mode
            and tool_client.deepline_limit_reached
            and getattr(tool_client, "scrapingdog_limit_reached", True)
        )

    def process_history(
        context: RunContext[Any], history: list[messages.ModelMessage]
    ) -> list[messages.ModelMessage]:
        return _process_history(
            context,
            history,
            finalize_at=arena_finalize_at,
            force_finalize=research_capacity_exhausted(),
            input_token_limit=None if arena_mode else _FINALIZE_INPUT_TOKENS,
            tool_calls_limit=None if arena_mode else _FINALIZE_TOOL_CALLS,
        )

    def prepare_research_tools(
        context: RunContext[Any], tool_definitions: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        prepared = _prepare_research_tools(
            context,
            tool_definitions,
            finalize_at=arena_finalize_at,
            force_finalize=research_capacity_exhausted(),
            input_token_limit=None if arena_mode else _FINALIZE_INPUT_TOKENS,
            tool_calls_limit=None if arena_mode else _FINALIZE_TOOL_CALLS,
        )
        if arena_mode and tool_client.deepline_limit_reached:
            # Web search and page retrieval can use ScrapingDog without
            # spending the Deepline capacity reserved for contacts.
            return [
                tool for tool in prepared
                if tool.name in {"search_web", "fetch_page"}
            ]
        return prepared

    model_settings: OpenRouterModelSettings = {
        "max_tokens": max_output_tokens,
        "parallel_tool_calls": True,
        "timeout": 120,
        "openrouter_reasoning": {"effort": "medium", "exclude": arena_mode},
        "openrouter_usage": {"include": True},
    }
    try:
        model = OpenRouterModel(
            model_name,
            provider=provider,
            settings=model_settings,
        )
        contact_description = TOOL_DESCRIPTIONS["get_company_contact"]
        contact_schema = tool_input_schema("get_company_contact")
        if arena_mode:
            contact_description += (
                " On the first lookup only, role_query_hints may add at most three "
                "same-seniority equivalent titles that differ from target_roles. The exact "
                "target roles are already searched; do not repeat them. Omit hints when no "
                "distinct equivalent title is suitable. Hints broaden provider discovery "
                "only; they do not qualify a contact and cannot be changed later."
                " When lookup_status is role_selection_required, choose at most one exact "
                "title from observed_role_options only if it satisfies the requested role, "
                "then repeat this tool with that exact selected_observed_role. Never invent "
                "or rewrite an offered title."
            )
            contact_schema["properties"]["selected_observed_role"] = {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
            }
            contact_schema["properties"]["role_query_hints"] = {
                "type": "array",
                "maxItems": 3,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                },
            }
        agent = Agent(
            model,
            instructions=SYSTEM_PROMPT,
            tools=[
                Tool.from_schema(
                    search_companies,
                    "search_companies",
                    TOOL_DESCRIPTIONS["search_companies"],
                    tool_input_schema("search_companies"),
                    sequential=True,
                ),
                Tool.from_schema(
                    get_company_profile,
                    "get_company_profile",
                    TOOL_DESCRIPTIONS["get_company_profile"],
                    tool_input_schema("get_company_profile"),
                    sequential=True,
                ),
                Tool.from_schema(
                    get_company_events,
                    "get_company_events",
                    TOOL_DESCRIPTIONS["get_company_events"],
                    tool_input_schema("get_company_events"),
                    sequential=True,
                ),
                Tool.from_schema(
                    search_web,
                    "search_web",
                    TOOL_DESCRIPTIONS["search_web"],
                    tool_input_schema("search_web"),
                    sequential=True,
                ),
                Tool.from_schema(
                    fetch_page,
                    "fetch_page",
                    TOOL_DESCRIPTIONS["fetch_page"],
                    tool_input_schema("fetch_page"),
                    sequential=True,
                ),
                *([
                    Tool.from_schema(
                        get_company_contact,
                        "get_company_contact",
                        contact_description,
                        contact_schema,
                        sequential=True,
                    )
                ] if contact_enabled else []),
            ],
            output_type=ToolOutput(
                CompaniesResult,
                name="submit_companies",
                description=TOOL_DESCRIPTIONS["submit_companies"],
                strict=True,
            ),
            capabilities=[
                ProcessHistory(process_history),
                PrepareTools(prepare_research_tools),
            ],
            model_settings=model_settings,
            tool_timeout=tool_timeout,
        )
    except Exception:
        await close_resources()
        raise
    run_usage = RunUsage()
    try:
        if arena_mode:
            if contact_enabled:
                arena_finalize_at = run_started_at + max(
                    0.0,
                    run_timeout - contact_reserve - _ARENA_FINALIZE_RESERVE_SECONDS,
                )
            else:
                arena_finalize_at = time.monotonic() + max(
                    0.0, run_timeout - _ARENA_FINALIZE_RESERVE_SECONDS
                )
            research_dispatch = _DeadlineProviderCall(
                tool_client.call,
                tool_client,
                arena_finalize_at,
                reserve_seconds=0.0,
                deadline_error="research provider deadline reached",
                clock=time.monotonic,
            )
        model_timeout = (
            max(_CONTACT_MIN_CALL_SECONDS, model_deadline - time.monotonic())
            if contact_enabled
            else run_timeout
        )
        result = await asyncio.wait_for(
            agent.run(
                build_prompt(icp, max_companies=max_companies),
                usage_limits=_run_usage_limits(arena_mode=arena_mode),
                usage=run_usage,
            ),
            timeout=model_timeout,
        )
        model_output = result.output
        companies = validate_companies(
            model_output.model_dump(mode="json"), max_companies
        )
        companies = _filter_explicit_stage_conflicts(icp, companies)
        for company in companies:
            company["fit_evidence_urls"] = _ordered_fit_evidence_urls(
                company["fit_evidence_urls"]
            )
        if arena_mode:
            companies = contact_lookup.enrich(companies, None)
        else:
            contact_call = _DeadlineProviderCall(
                budget.call,
                tool_client,
                run_deadline,
                clock=time.monotonic,
            )
            companies = contact_lookup.enrich(companies, contact_call)
        companies = validate_companies(
            companies, max_companies, allow_contacts=contact_enabled
        )
        budget.call("submit_companies", {"companies": companies})
        return companies
    finally:
        await close_resources()
        usage = dataclasses.asdict(run_usage)
        LAST_USAGE.clear()
        LAST_USAGE.update(json.loads(json.dumps(usage, default=str)))
        LAST_USAGE["provider_calls"] = budget.calls
        if arena_mode:
            LAST_USAGE["deepline_calls"] = tool_client.deepline_calls
            LAST_USAGE["scrapingdog_calls"] = getattr(tool_client, "scrapingdog_calls", 0)


def run_icp(icp: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one ICP through a fresh PydanticAI agent."""

    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run(icp))
    raise RuntimeError("run_icp must be called outside an active asyncio event loop")


def get_last_usage() -> dict[str, Any]:
    """Return an isolated copy of usage data from the last completed run."""

    return dict(LAST_USAGE)


__all__ = ["LAST_USAGE", "get_last_usage", "run_icp"]

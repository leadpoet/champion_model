"""Live Deepline, ScrapingDog, Exa, and public-web tools for sourcing.

Credentials are supplied to this host-owned object and never returned to an
agent. Provider results are deliberately plain JSON and truncated before they
cross the local tool boundary.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from .models import validate_companies
from .linkedin_profile import (
    exa_reported_error,
    linkedin_company_profile_url,
    project_harvestapi_company_evidence,
    project_linkedin_profile_evidence,
)
from .tool_contract import validate_job_category


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_MAX_TOOL_RESPONSE_BYTES = 6_000
_MAX_CONTACT_TOOL_RESPONSE_BYTES = 1_000_000
_MAX_PAGE_TEXT_CHARS = 2_500
_MAX_JOB_DESCRIPTION_CHARS = 1_000
_MAX_JOB_DESCRIPTION_SOURCE_CHARS = 20_000
_SCRAPINGDOG_REQUEST_USD = 0.001
_HARVESTAPI_SEARCH_LEADS_FALLBACK_USD = 0.07
_HARVESTAPI_GET_COMPANY_FALLBACK_USD = 0.003
_CONTACT_TOOL_NAMES = frozenset(
    {"harvestapi_search_leads", "harvestapi_get_profile"}
)
_HTML_BLOCK_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_EVENT_TOOLS = {
    "HIRING": "predictleads_company_job_openings",
    "JOBS": "predictleads_company_job_openings",
    "FUNDING": "predictleads_company_financing_events",
    "FINANCING": "predictleads_company_financing_events",
    "PRODUCT_LAUNCH": "predictleads_company_news_events",
    "ACQUISITION": "predictleads_company_news_events",
    "PARTNERSHIP": "predictleads_company_news_events",
    "MARKET_EXPANSION": "predictleads_company_news_events",
    "LEADERSHIP_CHANGE": "predictleads_company_news_events",
    "FACILITY_OPENING": "predictleads_company_news_events",
    "NEWS": "predictleads_company_news_events",
}
_NEWS_CATEGORIES = {
    "PRODUCT_LAUNCH": ["launches"],
    "ACQUISITION": ["acquires", "merges_with", "sells_assets_to"],
    "PARTNERSHIP": ["partners_with"],
    "MARKET_EXPANSION": ["expands_offices_in", "expands_offices_to"],
    "LEADERSHIP_CHANGE": ["hires", "promotes"],
    "FACILITY_OPENING": [
        "expands_facilities",
        "expands_offices_in",
        "expands_offices_to",
        "opens_new_location",
    ],
}
_US_REGIONS = {
    "west coast": ("CA", "OR", "WA"),
    "northeast": ("CT", "ME", "MA", "NH", "NJ", "NY", "PA", "RI", "VT"),
    "midwest": ("IA", "IL", "IN", "KS", "MI", "MN", "MO", "ND", "NE", "OH", "SD", "WI"),
}
_HUNTER_HEADCOUNT_BANDS = frozenset(
    {
        "1-10",
        "11-50",
        "51-200",
        "201-500",
        "501-1000",
        "1001-5000",
        "5001-10000",
        "10001+",
    }
)
_STORED_EMPLOYEE_COUNT_RE = re.compile(
    r"(?:[0-9]+(?:\.[0-9]+)?|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)"
)
_SUBPROCESS_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
}


def _evaluation_day(value: str | None = None) -> date:
    """Return the fixed evaluation date, or the current UTC date."""
    raw = str(
        value if value is not None else os.environ.get("BAKEOFF_EVALUATION_DATE", "")
    ).strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def _deepline_environment(deepline_api_key: str) -> dict[str, str]:
    """Give the CLI only process basics and its own credential."""
    environment = {
        key: value for key, value in os.environ.items() if key in _SUBPROCESS_ENV_KEYS
    }
    environment.update(
        {
            "DEEPLINE_API_KEY": deepline_api_key,
            "DEEPLINE_SKIP_SELF_UPDATE": "1",
        }
    )
    return environment


def deepline_preflight(*, deepline_api_key: str, deepline_bin: str) -> dict[str, Any]:
    """Verify the pinned Deepline CLI, authentication, and usable balance safely."""
    if not deepline_api_key:
        raise RuntimeError("Deepline credential is unavailable")
    completed = subprocess.run(
        [deepline_bin, "preflight", "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=_deepline_environment(deepline_api_key),
    )
    if completed.returncode != 0:
        raise RuntimeError("Deepline preflight failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Deepline preflight returned invalid JSON") from exc
    health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    billing = payload.get("billing") if isinstance(payload.get("billing"), dict) else {}
    try:
        balance = float(billing.get("balance"))
    except (TypeError, ValueError):
        balance = 0.0
    if health.get("status") != "ok" or not auth.get("connected"):
        raise RuntimeError(
            "Deepline preflight did not confirm health and authentication"
        )
    if balance <= 0:
        raise RuntimeError(
            "Deepline preflight did not confirm a positive credit balance"
        )
    return {
        "health": "ok",
        "auth_status": str(auth.get("status") or "connected"),
        "connected": True,
        "balance_status": str(billing.get("balance_status") or "available"),
        "balance_available": True,
    }


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:20_000]
    if isinstance(value, list):
        return [_json_safe(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key)[:200]: _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:150]
            if str(key).lower() not in {"api_key", "apikey", "authorization", "token"}
        }
    return str(value)[:2_000]


def _project_employee_count(company: dict[str, Any], value: Any) -> None:
    """Keep stored numeric headcounts distinct from supported public bands."""

    company.pop("employee_count", None)
    company.pop("employee_count_estimate", None)
    if value in (None, "", [], {}) or isinstance(value, bool):
        return
    numeric = isinstance(value, (int, float)) or (
        isinstance(value, str)
        and _STORED_EMPLOYEE_COUNT_RE.fullmatch(value.strip()) is not None
    )
    field = "employee_count_estimate" if numeric else "employee_count"
    company[field] = _json_safe(value)


def _result_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    tool_response = payload.get("toolResponse")
    if isinstance(tool_response, dict):
        for key in ("rawV2", "raw", "data"):
            value = tool_response.get(key)
            if isinstance(value, dict):
                return value
    result = payload.get("result")
    if isinstance(result, dict):
        data = result.get("data")
        return data if isinstance(data, dict) else result
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _profile_lookup_company(payload: Any, domain: str) -> dict[str, Any]:
    """Return one stored profile row, rejecting provider failure envelopes."""

    if exa_reported_error(payload):
        raise ValueError("company profile provider reported an error")
    data = _result_data(payload)
    rows = data.get("rows")
    if rows is None and isinstance(data.get("data"), dict):
        rows = data["data"].get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("company profile rows are malformed")
    exact = next(
        (
            row
            for row in rows
            if _maybe_host(row.get("primary_domain") or row.get("domain")) == domain
        ),
        rows[0] if rows else {},
    )
    return _json_safe(exact)


def _profile_lookup_error(exc: BaseException) -> dict[str, str]:
    """Project a bounded profile-source error without provider response text."""

    status = re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(exc))
    reason = f"HTTP {status.group(1)}" if status else type(exc).__name__
    return {
        "source": "free_simple_company_search",
        "error": f"profile lookup failed: {reason}",
    }


def _raise_profile_run_limit(exc: BaseException) -> None:
    """Keep the standalone run's existing provider limits fail-closed."""

    if isinstance(exc, TimeoutError) or str(exc) in {
        "provider call limit exhausted",
        "provider cost limit exhausted",
    }:
        raise exc


def _job_description_excerpt(value: Any) -> str | None:
    """Return bounded plain text from an untrusted provider description."""

    if not isinstance(value, str):
        return None
    text = html.unescape(value[:_MAX_JOB_DESCRIPTION_SOURCE_CHARS])
    text = _HTML_BLOCK_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_JOB_DESCRIPTION_CHARS] or None


def _project_event_data(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    """Keep only sales-research fields from verbose PredictLeads envelopes."""
    included: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in payload.get("included") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "")
        identifier = str(raw.get("id") or "")
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
        allowed = (
            ("company_name", "domain", "ticker")
            if kind == "company"
            else ("title", "url", "published_at", "author")
        )
        included[(kind, identifier)] = {
            key: _json_safe(attrs.get(key))
            for key in allowed
            if attrs.get(key) not in (None, "", [])
        }

    attribute_names = {
        "amount",
        "amount_normalized",
        "article_sentence",
        "categories",
        "category",
        "confidence",
        "contract_types",
        "effective_date",
        "event",
        "financing_type",
        "financing_type_normalized",
        "first_seen_at",
        "found_at",
        "headcount",
        "job_title",
        "last_seen_at",
        "location",
        "normalized_title",
        "planning",
        "posted_at",
        "product",
        "recognition",
        "salary",
        "seniority",
        "status",
        "summary",
        "title",
        "url",
        "vulnerability",
    }
    items: list[dict[str, Any]] = []
    raw_items = payload.get("data")
    if not isinstance(raw_items, list):
        raw_items = []
    for raw in raw_items[:limit]:
        if not isinstance(raw, dict):
            continue
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
        event_type = str(raw.get("type") or "event")
        projected_attributes = {
            key: _json_safe(value)
            for key, value in attrs.items()
            if key in attribute_names and value not in (None, "", [], {})
        }
        if event_type == "job_opening":
            projected_attributes["description"] = _job_description_excerpt(
                attrs.get("description")
            )
        item: dict[str, Any] = {
            "type": event_type,
            "attributes": projected_attributes,
        }
        relations = (
            raw.get("relationships")
            if isinstance(raw.get("relationships"), dict)
            else {}
        )
        related: dict[str, Any] = {}
        for name, relation in relations.items():
            data = relation.get("data") if isinstance(relation, dict) else None
            if not isinstance(data, dict):
                continue
            key = (str(data.get("type") or ""), str(data.get("id") or ""))
            if key in included:
                related[str(name)] = included[key]
        if related:
            item["related"] = related
        items.append(item)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "items": items,
        "returned_count": len(items),
        "available_count": meta.get("count"),
    }


def _compact_json_value(
    value: Any, *, string_chars: int, list_items: int, dict_items: int, depth: int = 0
) -> Any:
    if depth > 7:
        return "[truncated]"
    if isinstance(value, str):
        return value[:string_chars]
    if isinstance(value, list):
        return [
            _compact_json_value(
                item,
                string_chars=string_chars,
                list_items=list_items,
                dict_items=dict_items,
                depth=depth + 1,
            )
            for item in value[:list_items]
        ]
    if isinstance(value, dict):
        return {
            str(key): _compact_json_value(
                item,
                string_chars=string_chars,
                list_items=list_items,
                dict_items=dict_items,
                depth=depth + 1,
            )
            for key, item in list(value.items())[:dict_items]
        }
    return value


def _bounded_tool_result(value: Any) -> Any:
    """Apply the same deterministic response-size bound before every adapter."""
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(raw.encode("utf-8")) <= _MAX_TOOL_RESPONSE_BYTES:
        return value
    for string_chars, list_items, dict_items in (
        (700, 12, 50),
        (350, 8, 30),
        (180, 5, 20),
    ):
        compacted = _compact_json_value(
            value,
            string_chars=string_chars,
            list_items=list_items,
            dict_items=dict_items,
        )
        if isinstance(compacted, dict):
            compacted["response_truncated"] = True
        encoded = json.dumps(
            compacted, ensure_ascii=False, separators=(",", ":"), default=str
        )
        if len(encoded.encode("utf-8")) <= _MAX_TOOL_RESPONSE_BYTES:
            return compacted
    preview = raw[:2_000]
    fallback = {"response_truncated": True, "json_preview": preview}
    while (
        len(json.dumps(fallback, ensure_ascii=False).encode("utf-8"))
        > _MAX_TOOL_RESPONSE_BYTES
    ):
        preview = preview[: max(1, len(preview) // 2)]
        fallback["json_preview"] = preview
    return fallback


def _host_from_domain(value: str) -> str:
    raw = str(value or "").strip().lower()
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlsplit(raw).hostname or "").rstrip(".").removeprefix("www.")
    if not host or "." not in host or len(host) > 253:
        raise ValueError("a public company domain is required")
    return host


def _maybe_host(value: Any) -> str:
    try:
        return _host_from_domain(str(value or ""))
    except ValueError:
        return ""


def _hunter_locations(value: str) -> list[dict[str, str]]:
    """Translate the benchmark's common geography labels into Hunter filters."""
    normalized = " ".join(str(value or "").lower().replace(",", " ").split())
    for label, states in _US_REGIONS.items():
        if label in normalized:
            return [{"country": "US", "state": state} for state in states]
    if "london" in normalized:
        return [{"country": "GB", "city": "London"}]
    if "united kingdom" in normalized or normalized in {"uk", "great britain"}:
        return [{"country": "GB"}]
    if "united states" in normalized or normalized in {"us", "usa"}:
        return [{"country": "US"}]
    return []


def _hunter_headcount_bands(values: Any) -> list[str]:
    bands = values if isinstance(values, list) else [values]
    normalized: list[str] = []
    for value in bands:
        band = (
            str(value or "")
            .strip()
            .replace(",", "")
            .replace("–", "-")
            .replace("—", "-")
        )
        if band == "2-10":
            band = "1-10"
        if band in _HUNTER_HEADCOUNT_BANDS and band not in normalized:
            normalized.append(band)
    return normalized


def _sql_literal(value: Any) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:200]
    return "'" + text.replace("'", "''") + "'"


def _assert_public_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("an absolute HTTP(S) URL is required")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("private URL rejected")
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError("URL hostname did not resolve") from exc
    if not addresses:
        raise ValueError("URL hostname did not resolve")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise ValueError("private URL rejected")
    return parsed.geturl()


def _probe_live_public_url(
    value: str,
    *,
    timeout_seconds: float,
    require_body: bool,
) -> dict[str, Any]:
    """Open one public URL without forwarding credentials or private redirects."""
    current = _assert_public_url(value)
    timeout = max(1.0, min(float(timeout_seconds), 10.0))
    with httpx.Client(
        headers={"User-Agent": "LeadpoetBakeoffSmoke/1.0"},
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        for _ in range(5):
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError(
                            "public URL redirect omitted its destination"
                        )
                    current = _assert_public_url(urljoin(current, location))
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"public URL returned HTTP {response.status_code}"
                    )
                body_bytes = 0
                if require_body:
                    for chunk in response.iter_bytes():
                        body_bytes += len(chunk)
                        if body_bytes >= 256:
                            break
                    if body_bytes == 0:
                        raise RuntimeError("public URL returned an empty response")
                return {
                    "url": current,
                    "status_code": response.status_code,
                    "body_present": body_bytes > 0 if require_body else None,
                }
    raise RuntimeError("public URL redirected too many times")


def verify_live_smoke_company(
    company: dict[str, Any], *, timeout_seconds: float = 20.0
) -> dict[str, Any]:
    """Require one structurally valid company plus live company and intent URLs."""
    parsed = validate_companies(
        [company], max_companies=1, allow_contacts="contact" in company
    )[0]
    intent_urls = [
        str(signal.get("url") or "")
        for signal in parsed.get("intent_signals", [])
        if isinstance(signal, dict)
    ]
    evidence_url = next(
        (
            value
            for value in intent_urls
            if value and urlsplit(value).path not in {"", "/"}
        ),
        "",
    )
    if not evidence_url:
        raise ValueError("smoke result requires a non-homepage intent evidence URL")
    per_url_timeout = max(1.0, min(float(timeout_seconds) / 2.0, 10.0))
    website_check = _probe_live_public_url(
        parsed["company_website"],
        timeout_seconds=per_url_timeout,
        require_body=False,
    )
    intent_check = _probe_live_public_url(
        evidence_url,
        timeout_seconds=per_url_timeout,
        require_body=True,
    )
    if urlsplit(str(intent_check.get("url") or "")).path in {"", "/"}:
        raise ValueError("smoke intent evidence redirected to a homepage")
    return {"company_website": website_check, "intent_evidence": intent_check}


def _extract_text(raw: str, url: str) -> tuple[str, str]:
    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
    if match:
        title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()[:500]
    extracted = trafilatura.extract(
        raw, url=url, include_links=False, include_tables=False
    )
    if not extracted:
        extracted = re.sub(
            r"<script\b.*?</script>|<style\b.*?</style>", " ", raw, flags=re.I | re.S
        )
        extracted = re.sub(r"<[^>]+>", " ", extracted)
        extracted = html.unescape(extracted)
    return title, re.sub(r"\s+", " ", extracted or "").strip()


@dataclass
class ProviderStats:
    calls: list[dict[str, Any]] = field(default_factory=list)
    estimated_cost_usd: float = 0.0

    def add(
        self,
        tool: str,
        provider: str,
        started: float,
        status: str,
        cost_usd: float = 0.0,
    ) -> None:
        self.estimated_cost_usd += max(0.0, float(cost_usd or 0.0))
        self.calls.append(
            {
                "tool": tool,
                "provider": provider,
                "status": status,
                "latency_ms": round((time.monotonic() - started) * 1_000),
                "estimated_cost_usd": round(max(0.0, float(cost_usd or 0.0)), 6),
            }
        )


class LiveProviderTools:
    """Bounded live provider implementation shared by every challenger."""

    def __init__(
        self,
        *,
        deepline_api_key: str,
        scrapingdog_api_key: str,
        exa_api_key: str = "",
        deepline_bin: str = "deepline",
        deadline: float | None = None,
        max_provider_calls: int = 30,
        max_provider_cost_usd: float = 2.0,
        evaluation_date: str | None = None,
        allow_contacts: bool = False,
    ) -> None:
        if not deepline_api_key or not scrapingdog_api_key:
            raise RuntimeError("Deepline and ScrapingDog credentials are required")
        self._deepline_key = deepline_api_key
        self._scrapingdog_key = scrapingdog_api_key
        self._exa_key = str(exa_api_key or "").strip()
        self._deepline_bin = deepline_bin
        self.deadline = deadline or (time.monotonic() + 720)
        self.max_provider_calls = max_provider_calls
        self.max_provider_cost_usd = max_provider_cost_usd
        self.evaluation_date = _evaluation_day(evaluation_date)
        self.allow_contacts = bool(allow_contacts)
        self.stats = ProviderStats()
        self._execution_lock = threading.Lock()
        self._submitted = False

    def public_error(self, exc: BaseException) -> str:
        """Return a bounded tool error with credentials defensively removed."""
        message = f"{type(exc).__name__}: {str(exc)}"
        for secret in (self._deepline_key, self._scrapingdog_key, self._exa_key):
            if secret:
                message = message.replace(secret, "[redacted]")
        message = re.sub(
            r"(?i)(api[_-]?key|authorization|token)=([^&\s]+)",
            r"\1=[redacted]",
            message,
        )
        return message[:1_000]

    def _remaining(self, cap: float = 90.0) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 1:
            raise TimeoutError("attempt deadline exhausted")
        return max(1.0, min(cap, remaining - 0.5))

    def _reserve(self) -> None:
        if len(self.stats.calls) >= self.max_provider_calls:
            raise RuntimeError("provider call limit exhausted")
        if self.stats.estimated_cost_usd >= self.max_provider_cost_usd:
            raise RuntimeError("provider cost limit exhausted")

    @staticmethod
    def _billing_cost(payload: Any, fallback: float) -> float:
        def walk(value: Any) -> float | None:
            if isinstance(value, dict):
                for key in (
                    "totalDeeplineCostUsd",
                    "total_deepline_cost_usd",
                    "costUsd",
                    "cost_usd",
                ):
                    if key in value:
                        try:
                            return float(value[key])
                        except (TypeError, ValueError):
                            pass
                for child in value.values():
                    found = walk(child)
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = walk(child)
                    if found is not None:
                        return found
            return None

        found = walk(payload)
        return fallback if found is None else max(0.0, found)

    def _deepline(
        self, tool_id: str, payload: dict[str, Any], *, fallback_cost: float
    ) -> Any:
        self._reserve()
        started = time.monotonic()
        status = "error"
        result: Any = None
        cost = fallback_cost
        try:
            completed = subprocess.run(
                [
                    self._deepline_bin,
                    "tools",
                    "execute",
                    tool_id,
                    "--payload",
                    json.dumps(payload, separators=(",", ":")),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self._remaining(75),
                env=_deepline_environment(self._deepline_key),
            )
            if completed.returncode != 0:
                message = (
                    completed.stderr or completed.stdout or "Deepline command failed"
                ).strip()
                raise RuntimeError(message[-1_500:])
            try:
                result = json.loads(completed.stdout)
            except json.JSONDecodeError:
                lines = [
                    line
                    for line in completed.stdout.splitlines()
                    if line.strip().startswith(("{", "["))
                ]
                if not lines:
                    raise RuntimeError("Deepline returned non-JSON output")
                result = json.loads(lines[-1])
            cost = self._billing_cost(result, fallback_cost)
            status = "ok"
            return _json_safe(result)
        finally:
            self.stats.add(tool_id, "deepline", started, status, cost)

    def harvestapi_search_leads(self, arguments: dict[str, Any]) -> Any:
        allowed = {
            "currentCompanies",
            "currentJobTitles",
            "locations",
            "page",
            "search",
        }
        if set(arguments) - allowed:
            raise ValueError("invalid harvestapi_search_leads arguments")
        if type(arguments.get("page")) is not int or arguments["page"] != 1:
            raise ValueError("harvestapi_search_leads page must be 1")
        for key, value in arguments.items():
            if key != "page" and (
                not isinstance(value, str) or not value.strip() or len(value) > 2_048
            ):
                raise ValueError(f"harvestapi_search_leads {key} is invalid")
        if not arguments.get("currentJobTitles"):
            raise ValueError("harvestapi_search_leads currentJobTitles is required")
        if not (arguments.get("currentCompanies") or arguments.get("search")):
            raise ValueError(
                "harvestapi_search_leads company constraint is required"
            )
        return self._deepline(
            "harvestapi_search_leads",
            arguments,
            fallback_cost=_HARVESTAPI_SEARCH_LEADS_FALLBACK_USD,
        )

    def harvestapi_get_profile(self, arguments: dict[str, Any]) -> Any:
        if set(arguments) != {"url", "findEmail"}:
            raise ValueError("invalid harvestapi_get_profile arguments")
        url = str(arguments.get("url") or "").strip()
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("harvestapi_get_profile url is invalid") from exc
        host = (parsed.hostname or "").casefold().rstrip(".")
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or host not in {"linkedin.com", "www.linkedin.com"}
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or len(parts) != 2
            or parts[0].casefold() != "in"
            or not parts[1]
            or len(url) > 2_048
        ):
            raise ValueError("harvestapi_get_profile url is invalid")
        if arguments.get("findEmail") != "true":
            raise ValueError("harvestapi_get_profile findEmail must be true")
        # The live catalog declares provider-usage pricing for this tool. If a
        # response has no measured USD amount, fail closed by charging the
        # attempt's full remaining provider budget.
        fallback_cost = max(
            0.0, self.max_provider_cost_usd - self.stats.estimated_cost_usd
        )
        return self._deepline(
            "harvestapi_get_profile",
            {"url": url, "findEmail": "true"},
            fallback_cost=fallback_cost,
        )

    def search_companies(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        limit = max(1, min(int(arguments.get("limit") or 5), 6))
        industry = str(arguments.get("industry") or "").strip()
        geography = str(arguments.get("geography") or "").strip()
        headcount = _hunter_headcount_bands(arguments.get("employee_count") or [])
        context = ". ".join(
            part
            for part in (
                query,
                f"Industry: {industry}" if industry else "",
                f"Headquarters: {geography}" if geography else "",
                f"Employees: {' or '.join(headcount)}" if headcount else "",
            )
            if part
        )
        payload: dict[str, Any] = {"query": context[:1000], "limit": limit}
        if headcount:
            payload["headcount"] = headcount[:8]
        if industry:
            values = [
                part.strip() for part in re.split(r"[/|]", industry) if part.strip()
            ]
            payload["industry"] = {"include": values[:6]}
        if locations := _hunter_locations(geography):
            payload["headquarters_location"] = {"include": locations}
        raw = self._deepline("hunter_discover", payload, fallback_cost=0.0)
        data = _result_data(raw)
        rows = data.get("data") or data.get("rows") or []
        if not isinstance(rows, list):
            rows = []
        companies: list[dict[str, Any]] = []
        seen_companies: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            domain = _maybe_host(row.get("domain") or row.get("website"))
            identity = (
                domain
                or str(
                    row.get("organization")
                    or row.get("company_name")
                    or row.get("name")
                    or ""
                )
                .strip()
                .casefold()
            )
            if not identity or identity in seen_companies:
                continue
            seen_companies.add(identity)
            company: dict[str, Any] = {
                "company_name": str(
                    row.get("organization")
                    or row.get("company_name")
                    or row.get("name")
                    or ""
                )[:300],
                "domain": domain,
            }
            for source, target in (
                ("linkedin_url", "company_linkedin"),
                ("industry", "industry"),
                ("location", "location"),
            ):
                if row.get(source) not in (None, "", [], {}):
                    company[target] = _json_safe(row[source])
            for source in ("employee_count", "headcount"):
                if row.get(source) not in (None, "", [], {}):
                    _project_employee_count(company, row[source])
            if domain:
                company["company_website"] = f"https://{domain}/"
            companies.append(company)
            if len(companies) >= limit:
                break
        return {"companies": companies, "count": len(companies)}

    def get_company_profile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        domain = _host_from_domain(str(arguments.get("domain") or ""))
        columns = "normalized_domain, domain, company_name, industry, location, linkedin_url, employee_count, year_founded, updated_at"
        errors: list[dict[str, str]] = []
        supplied_linkedin = arguments.get("company_linkedin")
        linkedin_url: str | None = None
        if supplied_linkedin not in (None, ""):
            try:
                linkedin_url = linkedin_company_profile_url(supplied_linkedin)
            except ValueError as exc:
                raise ValueError(
                    "company_linkedin must be a LinkedIn company profile URL"
                ) from exc
            if linkedin_url is None:
                raise ValueError("company_linkedin must be a LinkedIn company profile URL")
            company: dict[str, Any] = {}
        else:
            try:
                raw = self._deepline(
                    "free_simple_company_search",
                    {
                        "sql": f"SELECT {columns} FROM companies WHERE normalized_domain = {_sql_literal(domain)} LIMIT 3"
                    },
                    fallback_cost=0.0,
                )
                company = _profile_lookup_company(raw, domain)
            except Exception as exc:
                _raise_profile_run_limit(exc)
                company = {}
                errors.append(_profile_lookup_error(exc))
        _project_employee_count(company, company.get("employee_count"))
        profile: dict[str, Any] = {
            "domain": domain,
            "company": company,
            "errors": errors,
        }
        stored_linkedin_url = company.get("linkedin_url") if linkedin_url is None else None
        if stored_linkedin_url not in (None, ""):
            try:
                linkedin_url = linkedin_company_profile_url(stored_linkedin_url)
                if linkedin_url is None:
                    raise ValueError("stored LinkedIn profile URL is invalid")
            except ValueError as exc:
                errors.append(
                    {"source": "linkedin_profile_evidence", "error": str(exc)[:160]}
                )
        if linkedin_url:
            try:
                structured_payload = self._deepline(
                    "harvestapi_get_company",
                    {"url": linkedin_url},
                    fallback_cost=_HARVESTAPI_GET_COMPANY_FALLBACK_USD,
                )
                profile["linkedin_structured_evidence"] = (
                    project_harvestapi_company_evidence(
                        domain,
                        linkedin_url,
                        structured_payload,
                    )
                )
            except Exception as exc:
                if supplied_linkedin not in (None, ""):
                    _raise_profile_run_limit(exc)
                errors.append(
                    {
                        "source": "linkedin_structured_evidence",
                        "error": f"structured profile fetch failed: {type(exc).__name__}",
                    }
                )
        structured_evidence = profile.get("linkedin_structured_evidence")
        structured_evidence = (
            structured_evidence if isinstance(structured_evidence, dict) else {}
        )
        linkedin_identity_established = (
            supplied_linkedin in (None, "") or bool(structured_evidence)
        )
        if linkedin_url and linkedin_identity_established and not {
            "employee_count",
            "headquarters",
        } <= structured_evidence.keys():
            try:
                exa_payload = self._deepline(
                    "exa_contents",
                    {
                        "urls": [linkedin_url],
                        "text": {"maxCharacters": 4_000},
                        "maxAgeHours": 0,
                        "livecrawlTimeout": 20_000,
                    },
                    fallback_cost=0.002,
                )
                if exa_reported_error(exa_payload):
                    raise RuntimeError("Exa contents reported an error")
                exa_data = _result_data(exa_payload)
                results = exa_data.get("results")
                result = (
                    next((item for item in results if isinstance(item, dict)), None)
                    if isinstance(results, list)
                    else None
                )
                profile["linkedin_profile_evidence"] = (
                    project_linkedin_profile_evidence(linkedin_url, result)
                )
            except ValueError as exc:
                errors.append(
                    {"source": "linkedin_profile_evidence", "error": str(exc)[:160]}
                )
            except Exception as exc:
                if supplied_linkedin not in (None, ""):
                    _raise_profile_run_limit(exc)
                errors.append(
                    {
                        "source": "linkedin_profile_evidence",
                        "error": f"profile fetch failed: {type(exc).__name__}",
                    }
                )
        return profile

    def get_company_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        domain = _host_from_domain(str(arguments.get("domain") or ""))
        job_category = validate_job_category(arguments.get("job_category"))
        categories = arguments.get("categories") or ["NEWS", "HIRING", "FUNDING"]
        if not isinstance(categories, list):
            categories = [categories]
        tools: list[str] = []
        unsupported: list[str] = []
        for category in categories:
            normalized_category = str(category).upper()
            tool = _EVENT_TOOLS.get(normalized_category)
            if tool is None:
                unsupported.append(normalized_category)
                continue
            if tool not in tools:
                tools.append(tool)
        limit = max(1, min(int(arguments.get("limit") or 5), 5))
        events: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = [
            {"source": category, "error": "use targeted web search"}
            for category in unsupported
        ]
        for tool in tools[:3]:
            payload = {"company_id_or_domain": domain, "page": 1, "limit": limit}
            if tool == "predictleads_company_job_openings":
                payload.update({"active_only": True, "not_closed": True})
                if job_category:
                    payload["categories"] = [job_category]
            elif tool == "predictleads_company_news_events":
                news_categories: list[str] = []
                for category in categories:
                    for value in _NEWS_CATEGORIES.get(str(category).upper(), []):
                        if value not in news_categories:
                            news_categories.append(value)
                if news_categories:
                    payload["categories"] = news_categories
            try:
                fallback_cost = (
                    0.001 if tool == "predictleads_company_job_openings" else 0.004
                )
                raw = self._deepline(tool, payload, fallback_cost=fallback_cost)
                events.append(
                    {
                        "source": tool,
                        "data": _project_event_data(_result_data(raw), limit),
                    }
                )
            except Exception as exc:
                errors.append({"source": tool, "error": self.public_error(exc)[:500]})
        return {"domain": domain, "events": events, "errors": errors}

    def _scrapingdog_search(
        self, query: str, limit: int, mode: str
    ) -> list[dict[str, Any]]:
        self._reserve()
        started = time.monotonic()
        status = "error"
        # Official endpoints charge five request credits. The estimate uses the
        # highest public paid-plan unit price ($40 / 200k credits), so it is a
        # conservative spend bound rather than a false zero.
        cost = _SCRAPINGDOG_REQUEST_USD
        try:
            qualified = query
            if mode == "news" and " after:" not in f" {query.lower()}":
                start_date = self.evaluation_date - timedelta(days=365)
                qualified = f"{query} after:{start_date.isoformat()}"
            elif mode == "jobs":
                qualified = f"{query} (jobs OR careers OR hiring)"
            endpoint = {
                "search": "https://api.scrapingdog.com/google",
                "news": "https://api.scrapingdog.com/google_news",
                "jobs": "https://api.scrapingdog.com/google_jobs",
            }[mode]
            params: dict[str, Any] = {
                "api_key": self._scrapingdog_key,
                "query": qualified,
            }
            if mode != "jobs":
                params["results"] = limit
            try:
                response = httpx.get(
                    endpoint,
                    params=params,
                    timeout=self._remaining(45),
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"ScrapingDog search returned HTTP {exc.response.status_code}"
                ) from None
            except httpx.HTTPError:
                raise RuntimeError("ScrapingDog search request failed") from None
            data = response.json()
            result_key = {
                "search": "organic_results",
                "news": "news_results",
                "jobs": "jobs_results",
            }[mode]
            rows = data.get(result_key, []) if isinstance(data, dict) else []
            status = "ok"
            projected: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item: dict[str, Any] = {}
                for key in (
                    "title",
                    "snippet",
                    "source",
                    "company_name",
                    "location",
                    "via",
                ):
                    if row.get(key) not in (None, "", [], {}):
                        item[key] = _json_safe(row[key])
                url = row.get("url") or row.get("link")
                if url:
                    normalized_url = str(url).split("#", 1)[0]
                    if normalized_url in seen_urls:
                        continue
                    seen_urls.add(normalized_url)
                    item["url"] = _json_safe(url)
                observed = row.get("date") or row.get("lastUpdated")
                if observed:
                    item["date"] = _json_safe(observed)
                extensions = row.get("extensions")
                if isinstance(extensions, list):
                    item["details"] = _json_safe(extensions[:6])
                apply_links = row.get("apply_links")
                if (
                    isinstance(apply_links, list)
                    and apply_links
                    and isinstance(apply_links[0], dict)
                ):
                    apply_url = apply_links[0].get("link") or apply_links[0].get("url")
                    if apply_url:
                        item["apply_url"] = _json_safe(apply_url)
                projected.append(item)
                if len(projected) >= limit:
                    break
            return projected
        finally:
            self.stats.add("search_web", "scrapingdog", started, status, cost)

    def _exa_search(
        self, query: str, limit: int, mode: str, start_date: str | None
    ) -> list[dict[str, Any]]:
        if not self._exa_key or mode == "jobs":
            return []
        self._reserve()
        started = time.monotonic()
        status = "error"
        cost = 0.01
        try:
            payload: dict[str, Any] = {
                "query": query,
                "numResults": limit,
                "type": "auto",
                "contents": {"highlights": True, "livecrawl": "preferred"},
            }
            if mode == "news":
                payload["category"] = "news"
            if start_date:
                payload["startPublishedDate"] = f"{start_date}T00:00:00.000Z"
            try:
                response = httpx.post(
                    "https://api.exa.ai/search",
                    headers={"x-api-key": self._exa_key},
                    json=payload,
                    timeout=self._remaining(45),
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Exa search returned HTTP {exc.response.status_code}"
                ) from None
            except (httpx.HTTPError, ValueError):
                raise RuntimeError("Exa search request failed") from None
            if not isinstance(data, dict):
                raise RuntimeError("Exa search returned invalid JSON")
            cost_doc = data.get("costDollars")
            if isinstance(cost_doc, dict):
                try:
                    cost = max(0.0, float(cost_doc.get("total") or cost))
                except (TypeError, ValueError):
                    pass
            projected: list[dict[str, Any]] = []
            for row in data.get("results") or []:
                if not isinstance(row, dict) or not row.get("url"):
                    continue
                highlights = row.get("highlights")
                snippet = " ".join(
                    str(item).strip()
                    for item in (highlights if isinstance(highlights, list) else [])
                    if str(item).strip()
                )
                projected.append(
                    {
                        "title": str(row.get("title") or "")[:500],
                        "url": str(row["url"]).split("#", 1)[0],
                        "date": str(row.get("publishedDate") or "")[:100],
                        "snippet": snippet[:1_000],
                        "source": "Exa",
                    }
                )
                if len(projected) >= limit:
                    break
            status = "ok"
            return projected
        finally:
            self.stats.add("search_web", "exa", started, status, cost)

    def search_web(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        mode = str(arguments.get("mode") or "search").strip().lower()
        if mode not in {"search", "news", "jobs"}:
            raise ValueError("mode must be search, news, or jobs")
        limit = max(1, min(int(arguments.get("limit") or 10), 10))
        recency = arguments.get("recency_days")
        start_date: str | None = None
        if recency not in (None, ""):
            start_date = (
                self.evaluation_date - timedelta(days=max(1, int(recency)))
            ).isoformat()
        scrapingdog_query = f"{query} after:{start_date}" if start_date else query
        scrapingdog_rows = self._scrapingdog_search(scrapingdog_query, limit, mode)
        try:
            exa_rows = self._exa_search(query, limit, mode, start_date)
        except Exception:
            exa_rows = []
        rows: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for index in range(max(len(exa_rows), len(scrapingdog_rows))):
            for source_rows in (exa_rows, scrapingdog_rows):
                if index >= len(source_rows):
                    continue
                row = source_rows[index]
                url = str(row.get("url") or "").split("#", 1)[0]
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                rows.append(row)
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        return {"results": rows, "count": len(rows), "mode": mode}

    def _direct_fetch(self, url: str) -> tuple[str, str, int]:
        current = _assert_public_url(url)
        with httpx.Client(
            headers={"User-Agent": "LeadpoetBakeoff/1.0"}, follow_redirects=False
        ) as client:
            for _ in range(4):
                response = client.get(current, timeout=self._remaining(12))
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        break
                    current = _assert_public_url(urljoin(current, location))
                    continue
                response.raise_for_status()
                return current, response.text[:1_500_000], response.status_code
        raise RuntimeError("too many redirects")

    def fetch_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requested = _assert_public_url(str(arguments.get("url") or ""))
        max_chars = max(
            1_000,
            min(
                int(arguments.get("max_chars") or _MAX_PAGE_TEXT_CHARS),
                _MAX_PAGE_TEXT_CHARS,
            ),
        )
        self._reserve()
        started = time.monotonic()
        try:
            final_url, raw, status_code = self._direct_fetch(requested)
            title, text = _extract_text(raw, final_url)
            self.stats.add("fetch_page", "direct", started, "ok", 0.0)
            return {
                "url": final_url,
                "status_code": status_code,
                "title": title,
                "text": text[:max_chars],
                "source": "direct",
            }
        except Exception as direct_exc:
            self.stats.add("fetch_page", "direct", started, "error", 0.0)
            self._reserve()
            fallback_started = time.monotonic()
            status = "error"
            cost = _SCRAPINGDOG_REQUEST_USD
            try:
                try:
                    response = httpx.get(
                        "https://api.scrapingdog.com/scrape",
                        params={
                            "api_key": self._scrapingdog_key,
                            "url": requested,
                            "dynamic": "false",
                        },
                        timeout=self._remaining(60),
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise RuntimeError(
                        f"ScrapingDog scrape returned HTTP {exc.response.status_code}"
                    ) from None
                except httpx.HTTPError:
                    raise RuntimeError("ScrapingDog scrape request failed") from None
                title, text = _extract_text(response.text[:1_500_000], requested)
                status = "ok"
                return {
                    "url": requested,
                    "status_code": response.status_code,
                    "title": title,
                    "text": text[:max_chars],
                    "source": "scrapingdog",
                    "direct_error": type(direct_exc).__name__,
                }
            finally:
                self.stats.add(
                    "fetch_page", "scrapingdog", fallback_started, status, cost
                )

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        # Serialize one attempt's tool execution so a batched model response
        # cannot race another provider call past the terminal submission.
        with self._execution_lock:
            if self._submitted:
                raise RuntimeError(
                    "submit_companies already terminated provider access"
                )
            if name == "submit_companies":
                companies = validate_companies(
                    arguments.get("companies"),
                    max_companies=5,
                    allow_contacts=self.allow_contacts,
                )
                self._submitted = True
                return {"companies": companies}
            method = getattr(self, name, None)
            allowed_tools = {
                "search_companies",
                "get_company_profile",
                "get_company_events",
                "search_web",
                "fetch_page",
            }
            if self.allow_contacts:
                allowed_tools.update(_CONTACT_TOOL_NAMES)
            if (
                name.startswith("_")
                or method is None
                or name not in allowed_tools
            ):
                raise ValueError(f"unknown tool: {name}")
            result = method(arguments)
            if name in _CONTACT_TOOL_NAMES:
                # The deterministic contact parser needs the structured profile,
                # not the generic LLM-facing truncation preview.
                encoded = json.dumps(
                    result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                if len(encoded) > _MAX_CONTACT_TOOL_RESPONSE_BYTES:
                    raise RuntimeError("contact provider response exceeds size limit")
                return result
            return _bounded_tool_result(result)

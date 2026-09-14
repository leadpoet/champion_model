"""Credential-free provider transport for the Leadpoet agent Arena.

The Arena exposes one HTTP bridge over a Unix socket.  Requests keep the
provider host and path, but carry no provider credential.  The Arena host adds
credentials and enforces its own call, cost, token, and time limits.
"""

from __future__ import annotations

from datetime import date, timedelta
import html
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from experiments.harness_bakeoff.models import _public_http_url, validate_companies
from experiments.harness_bakeoff.linkedin_profile import (
    exa_reported_error,
    linkedin_company_profile_url,
    project_harvestapi_company_evidence,
    project_linkedin_profile_evidence,
)
from experiments.harness_bakeoff.tool_contract import validate_job_category


_ALLOWED_ARENA_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "connection",
        "content-length",
        "content-type",
        "date",
        "expect",
        "host",
        "http-referer",
        "keep-alive",
        "pragma",
        "te",
        "user-agent",
        "x-title",
    }
)
_ALLOWED_OPENROUTER_FIELDS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning",
        "reasoning_effort",
        "temperature",
        "max_tokens",
        "top_p",
        "stop",
        "seed",
        "response_format",
        "include_reasoning",
    }
)
_ALLOWED_MESSAGE_FIELDS = frozenset(
    {"role", "content", "name", "tool_call_id", "tool_calls"}
)
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
_MAX_JOB_DESCRIPTION_CHARS = 1_000
_MAX_JOB_DESCRIPTION_SOURCE_CHARS = 20_000
_MAX_DEEPLINE_CALLS = 30
_BLOCK_PAGE_MARKERS = (
    "access denied",
    "cloudflare ray id",
    "verify you are human",
)
_BINARY_PAGE_PREFIXES = (
    b"%PDF-",
    b"GIF8",
    b"PK\x03\x04",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
)
_STRICT_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_SCRAPINGDOG_CALLS = 30
_DEEPLINE_QUOTA_ERRORS = frozenset({"budget_exhausted", "budget_refused"})
_HTML_BLOCK_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_JOB_RESPONSIBILITIES_HEADING_RE = re.compile(
    r"(?:^|\s)(?:(?:#{1,6}\s+|\*{1,2})(?:key\s+)?responsibilities"
    r"(?:\s*:)?(?:\*{1,2})?|(?:key\s+)?responsibilities\s*:)(?=\s|$)",
    re.IGNORECASE,
)


def arena_socket_path() -> str:
    """Return the absolute worker socket supplied by the Arena."""

    value = str(os.environ.get("LAB_ARENA_WORKER_SOCKET") or "").strip()
    if not value.startswith("/"):
        raise RuntimeError("LAB_ARENA_WORKER_SOCKET is required")
    return value


async def strip_arena_request_headers(request: httpx.Request) -> None:
    """Remove SDK and credential headers before a request reaches the broker."""

    for name in list(request.headers):
        if name.lower() not in _ALLOWED_ARENA_HEADERS:
            del request.headers[name]


class ArenaOpenRouterTransport(httpx.AsyncBaseTransport):
    """Send OpenAI SDK requests over the Arena socket in its closed schema."""

    def __init__(
        self,
        socket_path: str | None = None,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(
            uds=socket_path or arena_socket_path()
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OpenRouter request body is invalid") from exc
        if not isinstance(body, dict):
            raise RuntimeError("OpenRouter request body must be an object")
        # Keep only the Arena's published OpenRouter request fields. The Arena
        # pins streaming off and owns provider usage accounting.
        body = {
            name: value
            for name, value in body.items()
            if name in _ALLOWED_OPENROUTER_FIELDS
        }
        messages = body.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                for name in list(message):
                    if name not in _ALLOWED_MESSAGE_FIELDS:
                        del message[name]
                for name in ("content", "name", "tool_call_id", "tool_calls"):
                    if message.get(name) is None:
                        message.pop(name, None)
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() in _ALLOWED_ARENA_HEADERS
            and name.lower() not in {"content-length", "transfer-encoding"}
        }
        forwarded = httpx.Request(
            request.method,
            request.url,
            headers=headers,
            content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        )
        return await self._inner.handle_async_request(forwarded)

    async def aclose(self) -> None:
        await self._inner.aclose()


def arena_openrouter_http_client(timeout: float) -> httpx.AsyncClient:
    """Build the HTTP client used by PydanticAI inside the Arena sandbox."""

    return httpx.AsyncClient(
        transport=ArenaOpenRouterTransport(),
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
    )


def _evidence_url(value: Any) -> str:
    try:
        return _public_http_url(str(value or ""))
    except ValueError:
        return ""


def _domain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlsplit(raw).hostname or "").rstrip(".").removeprefix("www.")
    if not host or "." not in host or len(host) > 253:
        return ""
    return host


def _canonical_linkedin_person_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host not in {"linkedin.com", "www.linkedin.com"}
        or len(parts) != 2
        or parts[0].lower() != "in"
    ):
        return ""
    return f"https://www.linkedin.com/in/{parts[1]}/"


def _sql_literal(value: str) -> str:
    clean = re.sub(r"[\x00-\x1f\x7f]", " ", value)[:253]
    return "'" + clean.replace("'", "''") + "'"


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
            if _domain(row.get("primary_domain") or row.get("domain")) == domain
        ),
        rows[0] if rows else {},
    )
    return dict(exact)


def _profile_lookup_error(exc: BaseException) -> dict[str, str]:
    """Project a bounded profile-source error without provider response text."""

    status = re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(exc))
    reason = f"HTTP {status.group(1)}" if status else type(exc).__name__
    return {
        "source": "free_simple_company_search",
        "error": f"profile lookup failed: {reason}",
    }


def _raise_profile_run_limit(exc: BaseException) -> None:
    """Keep Arena quota refusal behavior unchanged."""

    if str(exc) in {"budget_exhausted", "budget_refused"}:
        raise exc


def _exa_reported_error(payload: Any, *, depth: int = 0) -> bool:
    """Detect failed Exa replies even when Deepline returns HTTP 200."""

    if not isinstance(payload, dict) or depth > 5:
        return False
    if payload.get("error") not in (None, "", False, [], {}):
        return True
    if payload.get("errors") not in (None, "", False, [], {}):
        return True
    if str(payload.get("status") or "").casefold() in {
        "error",
        "failed",
        "failure",
    }:
        return True
    for key in ("toolResponse", "result", "data", "raw", "rawV2"):
        if _exa_reported_error(payload.get(key), depth=depth + 1):
            return True
    for key in ("statuses", "results"):
        items = payload.get(key)
        if isinstance(items, list) and any(
            _exa_reported_error(item, depth=depth + 1) for item in items[:100]
        ):
            return True
    return False


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


def _hunter_locations(value: str) -> list[dict[str, str]]:
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


def _extract_html_text(raw: str, max_chars: int) -> tuple[str, str]:
    title = ""
    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", raw, flags=re.IGNORECASE | re.DOTALL
    )
    if title_match:
        title = html.unescape(title_match.group(1))
        title = _STRICT_CONTROL_CHAR_RE.sub(" ", title)
        title = re.sub(r"\s+", " ", title).strip()[:500]
    text = re.sub(
        r"<head\b.*?</head>|<title\b.*?</title>|<script\b.*?</script>|<style\b.*?</style>",
        " ",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = _STRICT_CONTROL_CHAR_RE.sub(" ", html.unescape(text))
    text = re.sub(r"\s+", " ", text).strip()[:max_chars]
    return title, text


def _strip_strict_controls(value: str) -> str:
    return _STRICT_CONTROL_CHAR_RE.sub(" ", value)


def _validate_page_text(
    text: str, source: str, *, reject_block_page: bool = False
) -> None:
    if len(text) < 300:
        raise RuntimeError(f"{source} returned fewer than 300 text characters")
    sample = text[:2_000].lower()
    if reject_block_page and any(marker in sample for marker in _BLOCK_PAGE_MARKERS):
        raise RuntimeError(f"{source} returned a block page")


def _job_description_excerpt(value: Any) -> str | None:
    """Return bounded plain text from an untrusted provider description."""

    if not isinstance(value, str):
        return None
    text = html.unescape(value[:_MAX_JOB_DESCRIPTION_SOURCE_CHARS])
    text = _HTML_BLOCK_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    heading = _JOB_RESPONSIBILITIES_HEADING_RE.search(text)
    if heading:
        text = text[heading.start() :].lstrip()
    return text[:_MAX_JOB_DESCRIPTION_CHARS] or None


def _project_event_data(payload: dict[str, Any], limit: int) -> dict[str, Any]:
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
        relations = raw.get("relationships") if isinstance(raw.get("relationships"), dict) else {}
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


class ArenaToolClient:
    """Implement the public semantic tools through approved Arena operations."""

    def __init__(self, timeout: float = 90.0, client: httpx.Client | None = None):
        self.timeout = max(1.0, min(float(timeout), 120.0))
        self.request_deadline: float | None = None
        self.allow_contacts = False
        self.deepline_calls = 0
        self._deepline_call_limit = _MAX_DEEPLINE_CALLS
        self.scrapingdog_calls = 0
        self._arena_budget_error = ""
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=httpx.HTTPTransport(uds=arena_socket_path()),
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=False,
            trust_env=False,
        )

    @property
    def deepline_call_limit(self) -> int:
        return self._deepline_call_limit

    @deepline_call_limit.setter
    def deepline_call_limit(self, maximum: int) -> None:
        if type(maximum) is not int or not 1 <= maximum <= _MAX_DEEPLINE_CALLS:
            raise ValueError(
                f"Arena Deepline call limit must be from 1 to {_MAX_DEEPLINE_CALLS}"
            )
        self._deepline_call_limit = maximum

    @property
    def deepline_limit_reached(self) -> bool:
        return bool(self._arena_budget_error) or (
            self.deepline_calls >= self.deepline_call_limit
        )

    @property
    def scrapingdog_limit_reached(self) -> bool:
        return bool(self._arena_budget_error) or (
            self.scrapingdog_calls >= _MAX_SCRAPINGDOG_CALLS
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._arena_budget_error:
            raise RuntimeError(self._arena_budget_error)
        request_timeout = self.timeout
        if self.request_deadline is not None:
            remaining = self.request_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Arena provider deadline reached")
            request_timeout = min(request_timeout, remaining)
        response = self._client.request(
            method,
            url,
            params=params,
            json=body,
            timeout=request_timeout,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Arena provider returned HTTP {response.status_code} with invalid JSON"
            ) from exc
        if not response.is_success:
            code = (
                (payload.get("error") or {}).get("code")
                if isinstance(payload.get("error"), dict)
                else ""
            )
            if str(code) in _DEEPLINE_QUOTA_ERRORS:
                self._arena_budget_error = str(code)
            raise RuntimeError(str(code or f"Arena provider returned HTTP {response.status_code}"))
        if not isinstance(payload, dict):
            raise RuntimeError("Arena provider returned a non-object")
        return payload

    def _text_request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        if self._arena_budget_error:
            raise RuntimeError(self._arena_budget_error)
        request_timeout = self.timeout
        if self.request_deadline is not None:
            remaining = self.request_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Arena provider deadline reached")
            request_timeout = min(request_timeout, remaining)
        response = self._client.request(
            method,
            url,
            params=params,
            timeout=request_timeout,
        )
        if not response.is_success:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code = (
                (payload.get("error") or {}).get("code")
                if isinstance(payload, dict)
                and isinstance(payload.get("error"), dict)
                else ""
            )
            if str(code) in _DEEPLINE_QUOTA_ERRORS:
                self._arena_budget_error = str(code)
            raise RuntimeError(str(code or f"Arena provider returned HTTP {response.status_code}"))
        media_type = (response.headers.get("content-type") or "").split(";", 1)[
            0
        ].strip().lower()
        prefix = response.content[:16].lstrip(b"\xef\xbb\xbf \t\r\n")
        non_text_media = bool(media_type) and not (
            media_type.startswith("text/")
            or media_type in {"application/xhtml+xml", "application/xml"}
        )
        binary_prefix = any(
            prefix.startswith(signature) for signature in _BINARY_PAGE_PREFIXES
        )
        if non_text_media or binary_prefix:
            raise RuntimeError("Arena provider returned non-text page content")
        return response.text, response.status_code

    def _deepline(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._arena_budget_error:
            raise RuntimeError(self._arena_budget_error)
        if self.deepline_calls >= self.deepline_call_limit:
            raise RuntimeError("Arena Deepline call limit reached")
        self.deepline_calls += 1
        return self._json_request(
            "POST",
            f"http://code.deepline.com/api/v2/integrations/{tool}/execute",
            body={"payload": payload},
        )

    def _scrapingdog(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._arena_budget_error:
            raise RuntimeError(self._arena_budget_error)
        if self.scrapingdog_calls >= _MAX_SCRAPINGDOG_CALLS:
            raise RuntimeError("Arena ScrapingDog call limit reached")
        self.scrapingdog_calls += 1
        return self._json_request(
            "GET",
            f"http://api.scrapingdog.com/{path}",
            params=params,
        )

    def _scrapingdog_text(
        self, path: str, params: dict[str, Any]
    ) -> tuple[str, int]:
        if self._arena_budget_error:
            raise RuntimeError(self._arena_budget_error)
        if self.scrapingdog_calls >= _MAX_SCRAPINGDOG_CALLS:
            raise RuntimeError("Arena ScrapingDog call limit reached")
        self.scrapingdog_calls += 1
        return self._text_request(
            "GET",
            f"http://api.scrapingdog.com/{path}",
            params=params,
        )

    def _contact_provider(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool == "harvestapi_get_profile":
            if set(arguments) != {"url", "findEmail"}:
                raise ValueError("contact profile arguments are invalid")
            canonical_url = _canonical_linkedin_person_url(arguments.get("url"))
            if not canonical_url:
                raise ValueError("contact profile URL is invalid")
            if arguments.get("findEmail") != "true":
                raise ValueError("contact profile email lookup is required")
            arguments = {"url": canonical_url, "findEmail": "true"}
        elif tool == "harvestapi_search_leads":
            allowed = {
                "currentCompanies",
                "currentJobTitles",
                "locations",
                "page",
                "search",
            }
            if (
                set(arguments) - allowed
                or not str(arguments.get("currentJobTitles") or "").strip()
            ):
                raise ValueError("contact search arguments are invalid")
            if arguments.get("page") != 1:
                raise ValueError("contact search page is invalid")
            if not any(arguments.get(key) for key in ("currentCompanies", "search")):
                raise ValueError("contact search company is required")
            for key in allowed - {"page"}:
                if key in arguments and (
                    not isinstance(arguments[key], str)
                    or not arguments[key].strip()
                    or len(arguments[key]) > 2_048
                ):
                    raise ValueError("contact search arguments are invalid")
        else:
            raise ValueError(f"unknown contact provider tool: {tool}")
        return self._deepline(tool, dict(arguments))

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
        request: dict[str, Any] = {"query": context[:1_000], "limit": limit}
        if headcount:
            request["headcount"] = headcount[:8]
        if industry:
            request["industry"] = {
                "include": [
                    part.strip()
                    for part in re.split(r"[/|]", industry)
                    if part.strip()
                ][:6]
            }
        if locations := _hunter_locations(geography):
            request["headquarters_location"] = {"include": locations}
        data = _result_data(self._deepline("hunter_discover", request))
        rows = data.get("data") or data.get("rows") or []
        if not isinstance(rows, list):
            rows = []
        companies: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            domain = _domain(row.get("domain") or row.get("website"))
            identity = domain or str(
                row.get("organization")
                or row.get("company_name")
                or row.get("name")
                or ""
            ).strip().casefold()
            if not identity or identity in seen:
                continue
            seen.add(identity)
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
        domain = _domain(arguments.get("domain"))
        if not domain:
            raise ValueError("domain is required")
        sql = (
            "SELECT normalized_domain, domain, company_name, industry, location, "
            "linkedin_url, employee_count, year_founded, updated_at FROM companies "
            f"WHERE normalized_domain = {_sql_literal(domain)} LIMIT 3"
        )
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
                payload = self._deepline("free_simple_company_search", {"sql": sql})
                company = _profile_lookup_company(payload, domain)
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
        if linkedin_url and not self.deepline_limit_reached:
            try:
                structured_payload = self._deepline(
                    "harvestapi_get_company",
                    {"url": linkedin_url},
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
        domain = _domain(arguments.get("domain"))
        if not domain:
            raise ValueError("domain is required")
        job_category = validate_job_category(arguments.get("job_category"))
        categories = arguments.get("categories") or ["NEWS", "HIRING", "FUNDING"]
        if not isinstance(categories, list):
            categories = [categories]
        tools: list[str] = []
        unsupported: list[str] = []
        for category in categories:
            normalized = str(category).upper()
            tool = _EVENT_TOOLS.get(normalized)
            if tool is None:
                unsupported.append(normalized)
            elif tool not in tools:
                tools.append(tool)
        limit = max(1, min(int(arguments.get("limit") or 5), 5))
        events: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = [
            {"source": category, "error": "use targeted web search"}
            for category in unsupported
        ]
        for tool in tools[:3]:
            request: dict[str, Any] = {
                "company_id_or_domain": domain,
                "page": 1,
                "limit": limit,
            }
            if tool == "predictleads_company_job_openings":
                request.update({"active_only": True, "not_closed": True})
                if job_category:
                    request["categories"] = [job_category]
            elif tool == "predictleads_company_news_events":
                news_categories: list[str] = []
                for category in categories:
                    for value in _NEWS_CATEGORIES.get(str(category).upper(), []):
                        if value not in news_categories:
                            news_categories.append(value)
                if news_categories:
                    request["categories"] = news_categories
            try:
                data = _result_data(self._deepline(tool, request))
                events.append(
                    {"source": tool, "data": _project_event_data(data, limit)}
                )
            except Exception as exc:
                errors.append({"source": tool, "error": type(exc).__name__})
        return {"domain": domain, "events": events, "errors": errors}

    def _scrapingdog_search(
        self,
        query: str,
        mode: str,
        limit: int,
        start_published: date | None,
        evaluation: date,
    ) -> list[dict[str, Any]]:
        suffix_parts: list[str] = []
        if mode == "jobs":
            suffix_parts.append("(jobs OR careers OR hiring)")
        if start_published is not None:
            suffix_parts.extend(
                (
                    f"after:{start_published.isoformat()}",
                    f"before:{(evaluation + timedelta(days=1)).isoformat()}",
                )
            )
        suffix = " " + " ".join(suffix_parts) if suffix_parts else ""
        qualified_query = query[: max(0, 500 - len(suffix))].rstrip() + suffix
        payload = self._scrapingdog(
            "google",
            {"query": qualified_query, "country": "us"},
        )
        if _exa_reported_error(payload):
            raise RuntimeError("ScrapingDog search reported an error")
        data = _result_data(payload)
        raw_rows = data.get("organic_results")
        if not isinstance(raw_rows, list):
            raise RuntimeError("ScrapingDog search returned malformed results")
        rows: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            url = _evidence_url(raw.get("url") or raw.get("link"))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            row: dict[str, Any] = {
                "source": "ScrapingDog",
                "url": url.split("#", 1)[0],
            }
            for key in ("title", "company_name", "location", "via"):
                if raw.get(key) not in (None, ""):
                    row[key] = _json_safe(raw[key])
            snippet = raw.get("snippet") or raw.get("description")
            if snippet not in (None, ""):
                row["snippet"] = str(snippet)[:1_000]
            observed = raw.get("date") or raw.get("lastUpdated")
            if observed not in (None, ""):
                row["date"] = _json_safe(observed)
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def _exa_search(
        self,
        query: str,
        mode: str,
        limit: int,
        start_published: date | None,
        evaluation: date,
    ) -> list[dict[str, Any]]:
        suffix = " (jobs OR careers OR hiring)" if mode == "jobs" else ""
        query = query[: max(0, 500 - len(suffix))].rstrip() + suffix
        request: dict[str, Any] = {
            "query": query,
            "numResults": limit,
            "type": "auto",
            "contents": {
                "highlights": True,
                "livecrawl": "preferred",
                "maxAgeHours": 0,
            },
        }
        if mode == "news":
            request["category"] = "news"
        if start_published is not None:
            request["startPublishedDate"] = f"{start_published.isoformat()}T00:00:00Z"
            request["endPublishedDate"] = f"{evaluation.isoformat()}T23:59:59Z"
        payload = self._deepline("exa_search", request)
        if _exa_reported_error(payload):
            raise RuntimeError("Exa search reported an error")
        data = _result_data(payload)
        raw_rows = data.get("results")
        if not isinstance(raw_rows, list):
            raise RuntimeError("Exa search returned malformed results")
        rows: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            row: dict[str, Any] = {}
            highlights = raw.get("highlights")
            snippet = " ".join(
                str(item).strip()
                for item in (highlights if isinstance(highlights, list) else [])
                if str(item).strip()
            )
            if raw.get("title") not in (None, ""):
                row["title"] = _json_safe(raw["title"])
            if snippet:
                row["snippet"] = snippet[:1_000]
            observed = raw.get("publishedDate")
            if observed:
                row["date"] = _json_safe(observed)
            row["source"] = "Exa"
            url = _evidence_url(raw.get("url") or raw.get("id"))
            if not url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            row["url"] = url
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def search_web(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        mode = str(arguments.get("mode") or "search").strip().lower()
        if mode not in {"search", "news", "jobs"}:
            raise ValueError("mode must be search, news, or jobs")
        recency = arguments.get("recency_days")
        evaluation = date.fromisoformat(
            os.environ.get("BAKEOFF_EVALUATION_DATE")
            or os.environ.get("LAB_ARENA_EVALUATION_DATE")
            or date.today().isoformat()
        )
        start_published: date | None = None
        if recency not in (None, ""):
            start_published = evaluation - timedelta(days=max(1, int(recency)))
        elif mode == "news":
            start_published = evaluation - timedelta(days=365)
        limit = max(1, min(int(arguments.get("limit") or 10), 10))
        rows: list[dict[str, Any]] = []
        try:
            rows = self._scrapingdog_search(
                query,
                mode,
                limit,
                start_published,
                evaluation,
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            if str(exc) in _DEEPLINE_QUOTA_ERRORS or str(exc) == (
                "Arena provider deadline reached"
            ):
                raise
        if not rows:
            rows = self._exa_search(
                query,
                mode,
                limit,
                start_published,
                evaluation,
            )
        return {"results": rows, "count": len(rows), "mode": mode}

    def fetch_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        url = str(arguments.get("url") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("an absolute HTTPS URL is required")
        max_chars = max(1_000, min(int(arguments.get("max_chars") or 2_500), 4_000))
        exa_error: Exception | None = None
        try:
            payload = self._deepline(
                "exa_contents",
                {
                    "urls": [url],
                    "text": {"maxCharacters": max_chars},
                    "maxAgeHours": 0,
                },
            )
            if _exa_reported_error(payload):
                raise RuntimeError("Exa contents reported an error")
            data = _result_data(payload)
            results = data.get("results")
            result = (
                next((item for item in results if isinstance(item, dict)), None)
                if isinstance(results, list)
                else None
            )
            if result is None:
                raise RuntimeError("Exa contents returned no result")
            result_url = _evidence_url(result.get("url") or result.get("id"))
            if not result_url:
                raise RuntimeError("Exa contents returned no valid evidence URL")
            title = _strip_strict_controls(str(result.get("title") or ""))[:500]
            raw_text = result.get("text")
            text = (
                _strip_strict_controls(raw_text).strip()[:max_chars]
                if isinstance(raw_text, str)
                else ""
            )
            _validate_page_text(text, "Exa contents")
            return {
                "url": result_url,
                "status_code": 200,
                "title": title,
                "text": text,
                "source": "Exa",
            }
        except (RuntimeError, httpx.HTTPError) as exc:
            if str(exc) in _DEEPLINE_QUOTA_ERRORS or str(exc) == (
                "Arena provider deadline reached"
            ):
                raise
            exa_error = exc
        try:
            raw_html, status_code = self._scrapingdog_text(
                "scrape", {"url": url, "dynamic": False}
            )
        except RuntimeError as exc:
            if (
                str(exc) == "Arena ScrapingDog call limit reached"
                and exa_error is not None
            ):
                raise exa_error
            raise
        title, text = _extract_html_text(raw_html, max_chars)
        _validate_page_text(text, "ScrapingDog scrape", reject_block_page=True)
        return {
            "url": url,
            "status_code": status_code,
            "title": title,
            "text": text,
            "source": "ScrapingDog",
        }

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "submit_companies":
            companies = validate_companies(
                arguments.get("companies"),
                max_companies=5,
                allow_contacts=self.allow_contacts,
            )
            return {"companies": companies}
        if name not in {
            "search_companies",
            "get_company_profile",
            "get_company_events",
            "search_web",
            "fetch_page",
            "harvestapi_search_leads",
            "harvestapi_get_profile",
        }:
            raise ValueError(f"unknown tool: {name}")
        if name in {"harvestapi_search_leads", "harvestapi_get_profile"}:
            return self._contact_provider(name, arguments)
        return getattr(self, name)(arguments)


__all__ = [
    "ArenaOpenRouterTransport",
    "ArenaToolClient",
    "arena_openrouter_http_client",
    "arena_socket_path",
    "strip_arena_request_headers",
]

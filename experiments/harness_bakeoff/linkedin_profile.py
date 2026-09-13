"""Pure validation and projection for public LinkedIn company profile evidence."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


_CANONICAL_EMPLOYEE_BANDS = (
    "0-1",
    "2-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1,000",
    "1,001-5,000",
    "5,001-10,000",
    "10,001+",
)
_LINKEDIN_COMPANY_PATH_RE = re.compile(
    r"^/company/(?P<slug>[A-Za-z0-9][A-Za-z0-9._~-]{0,99})/?$"
)
_ABOUT_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*{1,2})?about(?: us)?(?:\*{1,2})?[ \t]*$"
)
_ABOUT_END_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*{1,2})?"
    r"(?:employees(?: at\b[^\r\n]*)?|updates)(?:\*{1,2})?[ \t]*$"
)
_BAND_PATTERN = "|".join(
    re.escape(value) for value in sorted(_CANONICAL_EMPLOYEE_BANDS, key=len, reverse=True)
)
_COMPANY_SIZE_RE = re.compile(
    rf"(?im)^[ \t]*(?:\*{{1,2}})?company size(?:\*{{1,2}})?[ \t]*"
    rf"(?::[ \t]*(?:\r?\n[ \t]*)*|[ \t]+|(?:[ \t]*\r?\n)+[ \t]*)"
    rf"(?P<band>{_BAND_PATTERN})"
    rf"(?:[ \t]+employees?)?[ \t]*$"
)
_MAX_TITLE_CHARS = 300
_MAX_HEADQUARTERS_CHARS = 300
_ABOUT_FIELD_LABELS = (
    "company size",
    "founded",
    "headquarters",
    "industry",
    "locations",
    "specialties",
    "type",
    "website",
)
_ABOUT_FIELD_PATTERN = "|".join(
    re.escape(value) for value in sorted(_ABOUT_FIELD_LABELS, key=len, reverse=True)
)
_HEADQUARTERS_RE = re.compile(
    rf"(?im)^[ \t]*(?:\*{{1,2}})?headquarters(?:\*{{1,2}})?[ \t]*"
    rf"(?::[ \t]*(?:\r?\n[ \t]*)*|[ \t]+|(?:[ \t]*\r?\n)+[ \t]*)"
    rf"(?P<value>[^\r\n]{{1,{_MAX_HEADQUARTERS_CHARS}}}?)[ \t]*"
    rf"(?=(?:[ \t]+(?:{_ABOUT_FIELD_PATTERN})(?:[ \t:]+|$))|$)"
)


def linkedin_company_profile_url(value: Any) -> str | None:
    """Return a validated public LinkedIn company profile URL, or None if absent."""

    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("stored LinkedIn profile URL is invalid")
    if value != value.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError("stored LinkedIn profile URL is invalid")
    url = value
    if re.match(r"(?i)^(?:www\.)?linkedin\.com/", url):
        url = "https://" + url
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("stored LinkedIn profile URL is invalid") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or host not in {"linkedin.com", "www.linkedin.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or _LINKEDIN_COMPANY_PATH_RE.fullmatch(parsed.path) is None
    ):
        raise ValueError("stored LinkedIn profile URL is invalid")
    return url


def exa_reported_error(payload: Any, *, depth: int = 0) -> bool:
    """Detect failed Exa replies that arrive inside a successful transport response."""

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
        if exa_reported_error(payload.get(key), depth=depth + 1):
            return True
    for key in ("statuses", "results"):
        items = payload.get(key)
        if isinstance(items, list) and any(
            exa_reported_error(item, depth=depth + 1) for item in items[:100]
        ):
            return True
    return False


def _profile_key(value: Any) -> tuple[str, str] | None:
    try:
        url = linkedin_company_profile_url(value)
    except ValueError:
        return None
    if url is None:
        return None
    parsed = urlsplit(url)
    match = _LINKEDIN_COMPANY_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return None
    return ("linkedin.com", match.group("slug").casefold())


def _website_domain(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".").removeprefix("www.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or "." not in host
    ):
        return ""
    return host


def _harvestapi_company_elements(value: Any) -> list[dict[str, Any]]:
    current = value
    for _ in range(8):
        if not isinstance(current, dict):
            return []
        element = current.get("element")
        if isinstance(element, dict):
            return [element]
        elements = current.get("elements")
        if isinstance(elements, list):
            return [item for item in elements[:10] if isinstance(item, dict)]
        moved = False
        for key in ("toolResponse", "rawV2", "raw", "result", "data", "output"):
            child = current.get(key)
            if isinstance(child, dict) and child is not current:
                current = child
                moved = True
                break
        if not moved:
            return [current] if any(
                key in current
                for key in ("website", "linkedinUrl", "linkedin_url")
            ) else []
    return []


def _canonical_employee_range(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    start = value.get("start")
    end = value.get("end")
    if isinstance(start, bool) or isinstance(end, bool):
        return ""
    if not isinstance(start, int) or (end is not None and not isinstance(end, int)):
        return ""
    return {
        (0, 1): "0-1",
        (2, 10): "2-10",
        (11, 50): "11-50",
        (51, 200): "51-200",
        (201, 500): "201-500",
        (501, 1_000): "501-1,000",
        (1_001, 5_000): "1,001-5,000",
        (5_001, 10_000): "5,001-10,000",
        (10_001, None): "10,001+",
    }.get((start, end), "")


def project_harvestapi_company_evidence(
    requested_domain: str,
    requested_url: str | None,
    payload: Any,
) -> dict[str, Any]:
    """Project exact-identity structured LinkedIn company fields."""

    domain = _website_domain(requested_domain)
    if not domain or exa_reported_error(payload):
        raise ValueError("HarvestAPI company result is invalid")
    requested_key = _profile_key(requested_url) if requested_url else None
    if requested_url and requested_key is None:
        raise ValueError("requested LinkedIn company URL is invalid")
    for element in _harvestapi_company_elements(payload):
        if _website_domain(element.get("website")) != domain:
            continue
        returned_url = element.get("linkedinUrl") or element.get("linkedin_url")
        returned_key = _profile_key(returned_url)
        if returned_key is None or (
            requested_key is not None and returned_key != requested_key
        ):
            continue
        evidence: dict[str, Any] = {
            "provider": "harvestapi_get_company",
            "linkedin_url": linkedin_company_profile_url(returned_url),
            "website": f"https://{domain}/",
        }
        name = element.get("name")
        if isinstance(name, str) and name.strip():
            evidence["company_name"] = name.strip()[:300]
        employee_count = _canonical_employee_range(element.get("employeeCountRange"))
        if employee_count:
            evidence["employee_count"] = employee_count
            evidence["employee_count_source_field"] = "employeeCountRange"
        locations = element.get("locations")
        if isinstance(locations, list):
            headquarters = next(
                (
                    item
                    for item in locations[:50]
                    if isinstance(item, dict) and item.get("headquarter") is True
                ),
                None,
            )
            if headquarters is not None:
                parsed = headquarters.get("parsed")
                parsed = parsed if isinstance(parsed, dict) else {}
                text = parsed.get("text")
                if isinstance(text, str) and text.strip():
                    evidence["headquarters"] = text.strip()[:300]
                    evidence["headquarters_source_field"] = (
                        "locations[headquarter=true].parsed.text"
                    )
        return evidence
    raise ValueError("HarvestAPI company identity does not match the requested company")


def _about_section(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    about = _ABOUT_HEADING_RE.search(text)
    if about is None:
        return None
    end = _ABOUT_END_RE.search(text, about.end())
    section_end = end.start() if end is not None else len(text)
    return text[about.end() : section_end]


def _company_size_from_about(text: Any) -> tuple[str, str] | None:
    section = _about_section(text)
    if section is None:
        return None
    match = _COMPANY_SIZE_RE.search(section)
    if match is None:
        return None
    return match.group("band"), match.group(0)


def _headquarters_from_about(text: Any) -> tuple[str, str] | None:
    section = _about_section(text)
    if section is None:
        return None
    match = _HEADQUARTERS_RE.search(section)
    if match is None:
        return None
    value = match.group("value").strip()
    normalized_value = value.casefold()
    if (
        not value
        or len(value) > _MAX_HEADQUARTERS_CHARS
        or any(
            normalized_value == label
            or normalized_value.startswith(label + " ")
            or normalized_value.startswith(label + ":")
            for label in _ABOUT_FIELD_LABELS
        )
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value, match.group(0).strip()


def project_linkedin_profile_evidence(
    requested_url: str, result: Any
) -> dict[str, str]:
    """Project independently present fields from one current LinkedIn page."""

    if not isinstance(result, dict):
        raise ValueError("LinkedIn profile result is missing")
    result_url = result.get("url")
    requested_key = _profile_key(requested_url)
    if requested_key is None or _profile_key(result_url) != requested_key:
        raise ValueError("LinkedIn profile result URL does not match the request")
    title = result.get("title")
    text = result.get("text")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("LinkedIn profile result title is missing")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("LinkedIn profile result text is missing")
    evidence = {
        "url": str(result_url),
        "title": title.strip()[:_MAX_TITLE_CHARS],
    }
    extracted = _company_size_from_about(text)
    if extracted is not None:
        evidence["employee_count"], evidence["quote"] = extracted
    headquarters = _headquarters_from_about(text)
    if headquarters is not None:
        evidence["listed_headquarters"], evidence["headquarters_quote"] = headquarters
    return evidence


__all__ = [
    "exa_reported_error",
    "linkedin_company_profile_url",
    "project_harvestapi_company_evidence",
    "project_linkedin_profile_evidence",
]

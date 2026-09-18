"""Read run-bound receipts and verify structured company-fact provenance."""

from pathlib import Path
import hashlib
import ipaddress
import json
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import budget_guard
import deepline
from linkedin_receipts import _entity


FUNDING_TOOL = "aviato_get_company_funding_rounds"


ARENA_WEB_CAPTURE = "arena_host_public_web_v1"
ARENA_WEB_ROW_FIELDS = {"url", "text", "content_sha256", "saved_characters",
    "observed_characters", "raw_bytes_observed", "raw_truncated", "text_truncated",
    "truncated", "capture", "http_status"}


def arena_public_web_row(row, requested_url):
    """Validate the bounded successful host-fetch child result."""
    if not isinstance(row, dict) or set(row) != ARENA_WEB_ROW_FIELDS:
        return False
    text = row.get("text")
    if not isinstance(text, str):
        return False
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError:
        return False
    return (row.get("url") == requested_url and row.get("capture") == "arena_public_web_proxy"
        and type(row.get("http_status")) is int and row["http_status"] == 200
        and isinstance(text, str) and 0 < len(text) <= 64 * 1024
        and row.get("content_sha256") == digest
        and type(row.get("saved_characters")) is int and row["saved_characters"] == len(text)
        and type(row.get("observed_characters")) is int
        and len(text) <= row["observed_characters"] <= 2 * 1024 * 1024
        and type(row.get("raw_bytes_observed")) is int and 0 < row["raw_bytes_observed"] <= 1024 * 1024
        and all(type(row.get(key)) is bool for key in ("raw_truncated", "text_truncated", "truncated"))
        and row["text_truncated"] == (row["observed_characters"] > len(text))
        and (not row["raw_truncated"] or row["raw_bytes_observed"] == 1024 * 1024)
        and (not row["text_truncated"] or len(text) == 64 * 1024)
        and row["truncated"] == (row["raw_truncated"] or row["text_truncated"]))


def arena_public_web_capture(row, receipt):
    """Recognize only helper-owned, run-bound Arena research captures."""
    if not isinstance(receipt, dict):
        return False
    attempt = receipt.get("attempt")
    request = attempt.get("request") if isinstance(attempt, dict) else None
    capture = receipt.get("provider_response")
    if (receipt.get("provider") != "public_web" or receipt.get("operation") != "open"
            or receipt.get("receipt_status") != "complete" or receipt.get("pending_verification")
            or receipt.get("status") not in {"ok", "partial"}
            or receipt.get("results") != [row]
            or not isinstance(request, dict)
            or set(request) != {"operation", "query", "arena_review_phase", "arena_target_scope"}
            or request.get("operation") != "open" or request.get("arena_review_phase") != "research"
            or not isinstance(capture, dict) or set(capture) != {"capture", "run_fingerprint",
                "request_fingerprint", "request", "http_status", "url", "body", "content_sha256"}):
        return False
    url = request.get("query")
    if not isinstance(url, str) or not url or len(url) > 4096 or url != url.strip():
        return False
    try:
        address = urlsplit(url)
        host = (address.hostname or "").rstrip(".").lower()
        port = address.port
        if (address.scheme not in {"http", "https"} or not address.hostname
                or address.username is not None or address.password is not None or address.fragment
                or port is not None and not 0 < port < 65536
                or host == "localhost" or host.endswith((".internal", ".invalid", ".local", ".localhost", ".onion", ".test"))
                or any(ord(c) < 32 or ord(c) == 127 for c in url)):
            return False
    except ValueError:
        return False
    try:
        if not ipaddress.ip_address(host).is_global:
            return False
    except ValueError:
        if "." not in host or not any(c.isalpha() for c in host.rsplit(".", 1)[1]):
            return False
    fingerprint = receipt.get("run_fingerprint")
    target = request.get("arena_target_scope")
    return (isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint) is not None
        and isinstance(target, str) and 0 < len(target) <= 253
        and capture["capture"] == ARENA_WEB_CAPTURE
        and capture["run_fingerprint"] == fingerprint
        and capture["request_fingerprint"] == receipt.get("request_fingerprint") == request_fingerprint("public_web", request)
        and capture["request"] == request and capture["url"] == url
        and type(capture["http_status"]) is int and capture["http_status"] == 200
        and arena_public_web_row(row, url)
        and capture["body"] == row["text"] and capture["content_sha256"] == row["content_sha256"]
        and receipt["status"] == ("partial" if row["truncated"] else "ok"))


def content_kind(row, receipt):
    """Use adapter classification; recognize only the trusted legacy scrape path."""
    if receipt.get("provider") == "public_web":
        return "captured_page" if arena_public_web_capture(row, receipt) else "unverified"
    if "content_kind" in row:
        return row["content_kind"]
    if (receipt.get("provider") == "scrapingdog" and receipt.get("operation") == "scrape"
            and row.get("signal") == "web_page"):
        return "captured_page"  # Legacy adapter receipts saved only the scrape body.
    return "search_excerpt" if row.get("snippet") else "unverified"


def source_date(row):
    """Read captured date metadata once; undated pages stay observations."""
    date = next((row.get(k) for k in ("evidence_date", "date", "published_date", "publishedDate", "publication_date") if row.get(k)), None)
    if not date and isinstance(row.get("metadata"), dict):
        date = next((row["metadata"].get(k) for k in ("publishedTime", "article:published_time", "datePublished") if row["metadata"].get(k)), None)
    if isinstance(date, str) and "T" in date:
        try:
            date = datetime.fromisoformat(date.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass  # Validation reports malformed metadata; never invent a date.
    basis = row.get("evidence_date_basis") or row.get("date_basis") or ("published" if date else "observed_current")
    return date, basis


def request_fingerprint(provider, request):
    ignored = {"spend", "timeout_seconds", "entity_type", "output_file", "target_company_linkedin_url"}
    if provider == "deepline":
        ignored.update({"limit", "input", "name", "op", "q"})
    payload = {k: v for k, v in request.items() if k not in ignored}
    encoded = json.dumps([provider, payload], sort_keys=True, ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def read_receipt(run_file, route_id):
    """Read a saved response without dispatching or changing accounting."""
    if not isinstance(route_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", route_id):
        raise ValueError("invalid route ID")
    run_file = Path(run_file).resolve(strict=True)
    path = run_file.parent / "receipts" / (route_id + ".json")
    body = budget_guard.read_object(path)
    if body.get("run_fingerprint") != budget_guard.run_fingerprint(run_file):
        raise ValueError("saved response belongs to another run or lacks run identity; preserve it and reconcile its origin")
    document = budget_guard.read_object(run_file)
    routes = document.get("routes", []) + document.get("stop_audit", {}).get("route_frontier", [])
    if not any(isinstance(route, dict) and route.get("route_id") == route_id for route in routes):
        raise ValueError("saved response has no planned route in this run")
    for route in routes:
        if isinstance(route, dict) and route.get("route_id") == route_id:
            if any(route.get(key) != body.get(key) for key in ("request_fingerprint", "provider")):
                raise ValueError("saved response does not match this route's request/provider; preserve it and reconcile its origin")
    return {"route_id": route_id, "receipt_file": str(path), "result": body}


def web_passage(run_file, document, evidence):
    """Verify captured web provenance, not whether its meaning satisfies the ICP."""
    source = evidence.get("source", {})
    saved = read_receipt(run_file, source.get("route_id"))["result"]
    normalized = saved
    if saved.get("provider") == "deepline":
        normalized, _ = deepline.normalize_response(saved["attempt"]["request"], saved["provider_response"])
    rows = normalized.get("results", [])
    if (saved.get("receipt_status") != "complete" or saved.get("status") not in {"ok", "partial"}
            or saved.get("pending_verification")
            or any(source.get(k) != saved.get(k) for k in ("provider", "operation", "tool"))):
        raise ValueError("qualification evidence requires a matching completed successful source receipt")
    if saved.get("provider") == "public_web" and not any(arena_public_web_capture(row, saved) for row in rows):
        raise ValueError("required web evidence needs a tool-captured page, not an agent-recorded passage. Use tyche_lookup with ScrapingDog scrape or a Deepline page reader, then reuse its ref. Keep this observation for discovery; do not rewrite it.")
    if rows and all(r.get("content_kind") == "structured_record" for r in rows):
        return  # Company/profile/funding records retain their specialized checks.
    pages = [r for r in rows if content_kind(r, saved) == "captured_page"]
    if not pages:
        raise ValueError("selected source has no captured source body; search excerpts and unverified records cannot qualify. Capture the page with tyche_lookup or keep the requirement unknown")
    def url_key(value):
        parsed = urlsplit(value or "")
        return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), parsed.query, ""))
    selected = url_key(evidence.get("evidence_url", evidence.get("url")))
    matched = [r for r in pages if url_key(r.get("evidence_url", r.get("url"))) == selected]
    passages = [r.get("evidence_text") or r.get("text") for r in matched]
    if any(isinstance(p, str) and re.match(r"\s*Internal Error \(\)\s*(?:\n|$)", p) for p in passages):
        raise ValueError("selected web observation is a tool error, not source text; keep the requirement unknown or select a successfully read source")
    excerpt = " ".join(str(evidence.get("evidence_text", evidence.get("text")) or "").split())
    if not excerpt or not any(isinstance(p, str) and excerpt in " ".join(p.split()) for p in passages):
        raise ValueError("required web evidence must quote captured source text at the selected URL; omit text to reuse its passage and put interpretation in claim")
    date = evidence.get("evidence_date", evidence.get("date"))
    basis = evidence.get("evidence_date_basis", evidence.get("date_basis"))
    if not any(basis == source_date(row)[1] and (source_date(row)[0] is None or date == source_date(row)[0]) for row in matched):
        raise ValueError("source date/date_basis must match captured metadata; undated pages use observed_current, with event_date separately supported by the passage")


def funding_record(run_file, document, company, evidence):
    """Verify one captured funding row; the LLM still judges the requested stage."""
    if run_file is None:
        raise ValueError("structured funding evidence requires the saved run and receipts")
    source = evidence.get("source", {})
    index = source.get("result_index")
    if (source.get("provider") != "deepline" or source.get("operation") != "execute"
            or source.get("tool") != FUNDING_TOOL or type(index) is not int or index < 0):
        raise ValueError("select a saved structured funding result with its exact result index")
    saved = read_receipt(run_file, source.get("route_id"))["result"]
    routes = [r for r in document.get("routes", []) if r.get("route_id") == source["route_id"]]
    if (len(routes) != 1 or any(saved.get(k) != source.get(k) or routes[0].get(k) != source.get(k)
                               for k in ("provider", "operation", "tool"))
            or saved.get("receipt_status") != "complete" or saved.get("status") not in {"ok", "partial"}
            or saved.get("pending_verification") or routes[0].get("provider_status") != saved.get("status")):
        raise ValueError("structured funding evidence requires a completed successful receipt")
    request = saved.get("attempt", {}).get("request", {})
    if request.get("operation") != "execute" or request.get("tool") != FUNDING_TOOL:
        raise ValueError("captured request does not match the funding tool")
    if request_fingerprint("deepline", request) != saved.get("request_fingerprint"):
        raise ValueError("captured funding request does not match its saved fingerprint")
    identifier = request.get("payload", {}).get("website") or request.get("payload", {}).get("linkedinUrl")
    linkedin = _entity(identifier, "company")
    domain = deepline._domain(identifier) if not linkedin else None
    if not (linkedin and linkedin == _entity(company.get("linkedin_url"), "company")
            or domain and domain == deepline._domain(company.get("domain"))):
        raise ValueError("funding request must identify this company by its website or LinkedIn URL")
    raw = saved.get("provider_response")
    if not isinstance(raw, dict) or "body" not in raw:
        raise ValueError("structured funding evidence requires the captured provider response")
    normalized, _ = deepline.normalize_response(request, raw)
    rows = normalized.get("results", [])
    if normalized.get("status") not in {"ok", "partial"} or normalized.get("pending_verification") or index >= len(rows):
        raise ValueError("selected funding result is absent from the captured response")
    row = rows[index]
    if (row.get("domain") and row["domain"] != deepline._domain(company.get("domain"))
            or row.get("company_linkedin_url") and _entity(row["company_linkedin_url"], "company") != _entity(company.get("linkedin_url"), "company")):
        raise ValueError("funding response identifies a different company")
    if not row.get("stage") or not row.get("evidence_date") or not row.get("evidence_text"):
        raise ValueError("funding record must contain a stage, announcement date and supporting text")
    for field, expected in (("date", row["evidence_date"]), ("date_basis", "published"),
                            ("text", row["evidence_text"])):
        if evidence.get(field) != expected:
            raise ValueError(f"structured funding {field} must match the captured record; put interpretation in claim")
    return row

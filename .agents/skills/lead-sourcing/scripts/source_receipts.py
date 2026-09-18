"""Read run-bound receipts and verify structured company-fact provenance."""

from pathlib import Path
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import budget_guard
import deepline
from linkedin_receipts import _entity


FUNDING_TOOL = "aviato_get_company_funding_rounds"


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
    routes = [r for r in document.get("routes", []) if r.get("route_id") == source.get("route_id")]
    saved = read_receipt(run_file, source.get("route_id"))["result"]
    normalized = saved
    if saved.get("provider") == "deepline":
        normalized, _ = deepline.normalize_response(saved["attempt"]["request"], saved["provider_response"])
    rows = normalized.get("results", [])
    page_reader = any(r.get("provider") == "public_web" or r.get("tool") == "firecrawl_scrape"
                      or r.get("operation") == "scrape" for r in [source, *routes])
    # Page bodies/HTTP metadata also identify failed or incomplete captures. Do not
    # let an unfamiliar crawler bypass provenance by falling through as data.
    if not page_reader and not any(r.get("signal") == "web_page" or "markdown" in r or "html" in r or
            isinstance(r.get("metadata"), dict) and "statusCode" in r["metadata"] for r in rows):
        return  # Structured company/profile/funding records retain their checks.
    if (saved.get("receipt_status") != "complete" or saved.get("status") not in {"ok", "partial"}
            or saved.get("pending_verification")
            or any(source.get(k) != saved.get(k) for k in ("provider", "operation", "tool"))):
        raise ValueError("qualification evidence requires a matching completed successful source receipt")
    if saved.get("provider") == "public_web":
        raise ValueError("required web evidence needs a tool-captured page, not an agent-recorded passage. Use tyche_lookup with ScrapingDog scrape or a Deepline page reader, then reuse its ref. Keep this observation for discovery; do not rewrite it.")
    # Structured company/profile/funding records keep their specialized checks.
    pages = [r for r in rows if r.get("signal") == "web_page"]
    if not pages:
        raise ValueError("selected page reader has no captured source body; keep the requirement unknown")
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

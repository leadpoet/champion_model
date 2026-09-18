"""Read email decisions from this run's captured validator responses."""

from pathlib import Path
import re

import budget_guard
import deepline
from provider_pricing import call_credits

FAILURES = {"provider_error", "timeout", "rate_limited", "auth_failed", "quota_exceeded"}


def _text(value):
    return value.strip().casefold() if isinstance(value, str) else ""


def validator_for_tool(tool):
    """Recognize validation operations, not every tool sold by the provider."""
    name = _text(tool)
    words = set(re.findall(r"[a-z]+", name))
    family = next((provider for provider in ("zerobounce", "bounceban") if provider in words), None)
    if words & {"find", "finder", "discovery", "credits", "balance", "score"}:
        return None
    return family if name == family or words & {"validate", "validation", "verify", "verification", "status", "result", "results"} else None


class OtherEmail(ValueError):
    """A valid saved receipt applies to a different address."""


def receipt_path(run_file, rid):
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", rid):
        raise ValueError("email validation requires a saved route ID")
    return Path(run_file).resolve(strict=True).parent / "receipts" / (rid + ".json")


def _saved_receipt(run_file, route):
    saved = budget_guard.read_object(receipt_path(run_file, route.get("route_id")))
    if saved.get("run_fingerprint") != budget_guard.run_fingerprint(run_file):
        raise ValueError("email receipt belongs to another run or lacks run identity")
    if not route.get("request_fingerprint") or route["request_fingerprint"] != saved.get("request_fingerprint"):
        raise ValueError("email receipt does not match the route request")
    return saved


def discovered_addresses(row):
    """Exact addresses returned by a finder or published in source text."""
    if isinstance(row, str):
        return {_text(address.rstrip('.')) for address in re.findall(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", row)}
    values = ([value for key, value in row.items() if key not in {"request", "input", "payload", "query"}]
              if isinstance(row, dict) else row if isinstance(row, list) else [])
    return set().union(*(discovered_addresses(value) for value in values))


def discovery_source(run_file, routes, email, *, before=None):
    """Find prior exact-address discovery in this run, never in validation/input."""
    for route in routes:
        if route.get("route_id") == before:
            break
        if (route.get("provider") not in {"deepline", "scrapingdog"}
                or route.get("entity_type") == "tool_catalog"
                or route.get("phase") == "email_validation" or validator_for_tool(route.get("tool"))
                or route.get("provider") == "deepline" and route.get("operation") != "execute"):
            continue
        try:
            saved = _saved_receipt(run_file, route)
            if (saved.get("receipt_status") != "complete" or saved.get("status") not in {"ok", "partial"}
                    or any(saved.get(k) != route.get(k) for k in ("provider", "operation", "tool"))):
                continue
            if saved.get("provider") == "deepline":
                saved, _ = deepline.normalize_response({"limit": 10, **saved["attempt"]["request"]}, saved["provider_response"])
            for index, row in enumerate(saved.get("results", [])):
                if isinstance(row, dict) and _text(email) in discovered_addresses(row):
                    source = {k: route[k] for k in ("provider", "operation", "tool", "route_id") if k in route}
                    return {"source": dict(source, result_index=index)}
        except (ValueError, OSError, KeyError, TypeError):
            continue  # An unusable receipt cannot establish discovery.
    return None


def saved_result(run_file, routes, source, email):
    """Return the exact address verdict, never a domain-level catch-all flag."""
    rid = source.get("route_id")
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", rid):
        raise ValueError("email validation requires a saved route ID")
    matching = [r for r in routes if isinstance(r, dict) and r.get("route_id") == rid]
    if len(matching) != 1:
        raise ValueError("email validation requires one matching route")
    route = matching[0]
    saved = _saved_receipt(run_file, route)
    if (source.get("provider") != "deepline" or source.get("operation") != "execute"
            or any(route.get(k) != source.get(k) or saved.get(k) != source.get(k)
                   for k in ("provider", "operation", "tool"))
            or route.get("phase") != "email_validation" or saved.get("receipt_status") != "complete"
            or saved.get("pending_verification") or route.get("provider_status") != saved.get("status")):
        raise ValueError("requires a completed matching email-validation receipt")
    response = saved.get("provider_response", {})
    if not isinstance(response, dict) or "body" not in response:
        raise ValueError("email receipt lacks the original provider response")
    # Reuse the live parser for verdicts and failures; saved labels are not evidence.
    records = [r for r in deepline._records(response["body"]) if deepline._is_email_validation_record(r)]
    normalized, _ = deepline.normalize_response({"operation": "execute", "tool": source["tool"],
        "entity_type": "email_validation", "limit": max(1, len(records))}, response)
    if normalized.get("status") != saved.get("status") or normalized.get("pending_verification"):
        raise ValueError("saved status conflicts with the original provider response or remains pending")
    # An explicit address verdict still blocks fallback even if its envelope failed.
    matches = [r for r in records if _text(r.get("address", r.get("email"))) == _text(email)]
    if len(matches) == 1:
        record = matches[0]
        verdict = {"email": email, "status": _text(record.get("status"))}
        if "result" in record:
            verdict["result"] = _text(record["result"])
        return verdict
    # Service failures often omit the address. Bind those to the original request.
    requested = saved.get("attempt", {}).get("request", {}).get("payload", {}).get("email")
    if not matches and (records or _text(requested) and _text(requested) != _text(email)):
        raise OtherEmail("saved receipt applies to another email")
    if not records and normalized.get("status") in FAILURES and _text(requested) == _text(email):
        return {"email": email, "status": None, "provider_status": normalized["status"]}
    raise ValueError("original provider response must identify exactly one matching email")


def _pending_job(run_file, route):
    saved = _saved_receipt(run_file, route)
    if (route.get("phase") != "email_validation" or saved.get("receipt_status") != "complete"
            or route.get("provider_status") != "partial" or saved.get("status") != "partial"
            or route.get("provider") != "deepline" or route.get("operation") != "execute"
            or any(saved.get(k) != route.get(k) for k in ("provider", "operation", "tool"))):
        raise ValueError("requires a captured pending email-validation receipt")
    request = saved.get("attempt", {}).get("request", {})
    if any(request.get(k) != route.get(k) for k in ("operation", "tool")):
        raise ValueError("pending job request must match its provider route")
    normalized, _ = deepline.normalize_response(dict(request, entity_type="email_validation", limit=1), saved["provider_response"])
    pending = normalized.get("pending_verification")
    if not pending or normalized.get("status") != "partial" or pending != saved.get("pending_verification"):
        raise ValueError("pending job must match the original provider response")
    return pending, request.get("payload", {})


def _pending_email(run_file, document, route_id, pending):
    """Resolve a pending job to its original address, including status chains."""
    job_id = pending.get("id")
    if not job_id:
        raise ValueError("pending verification has no job ID")
    frontier = {r["route_id"]: r for r in document.get("stop_audit", {}).get("route_frontier", [])}
    routes = {r["route_id"]: r for r in document.get("routes", [])}
    # A pending getter can omit email too. Follow its recorded parents back
    # to the submission; response emails only corroborate that request.
    todo, seen, emails, response_emails = [route_id], set(), set(), set()
    while todo:
        rid = todo.pop()
        if rid in seen:
            continue
        seen.add(rid)
        route = routes[rid]
        actual, payload = _pending_job(run_file, route)
        if actual["id"] != job_id or (rid == route_id and actual != pending):
            raise ValueError("pending verification job identity conflicts")
        if _text(actual.get("email")):
            response_emails.add(_text(actual["email"]))
        if route.get("status_read"):
            parents = [key for key, r in frontier.items() if rid in r.get("continuation_route_ids", [])]
            if payload.get("id") != job_id or not parents:
                raise ValueError("pending status read lacks its original submission")
            todo.extend(parents)
        elif _text(payload.get("email")):
            emails.add(_text(payload["email"]))
        else:
            raise ValueError("pending verification has no original email")
    if len(emails) != 1 or response_emails - emails:
        raise ValueError("pending verification email identity conflicts")
    return next(iter(emails))


def verification_finished(run_file, document, route_id, pending, links=None):
    """Bind a completed getter to its saved submission's job and exact email."""
    try:
        email = _pending_email(run_file, document, route_id, pending)
        job_id = pending["id"]
        frontier = {r["route_id"]: r for r in document.get("stop_audit", {}).get("route_frontier", [])}
        routes = {r["route_id"]: r for r in document.get("routes", [])}
        todo = list(links if links is not None else frontier.get(route_id, {}).get("continuation_route_ids", []))
        seen = {route_id}
        while todo:
            rid = todo.pop()
            if rid in seen:
                continue
            seen.add(rid)
            route = routes.get(rid, {})
            if not route.get("status_read"):
                continue
            saved = _saved_receipt(run_file, route)
            payload = saved.get("attempt", {}).get("request", {}).get("payload", {})
            if payload.get("id") != job_id or (_text(payload.get("email")) and _text(payload["email"]) != email):
                continue
            if route.get("provider_status") == "partial":
                actual, _ = _pending_job(run_file, route)
                if actual["id"] == job_id and (not _text(actual.get("email")) or _text(actual["email"]) == email):
                    todo.extend(frontier.get(rid, {}).get("continuation_route_ids", []))
            elif route.get("provider_status") == "ok":
                saved_result(run_file, document["routes"],
                             {k: route.get(k) for k in ("route_id", "provider", "operation", "tool")}, email)
                return True
            elif route.get("provider_status") in FAILURES:
                # A failed read does not cancel the job; a later linked read
                # still has to prove the same request ID and address verdict.
                todo.extend(frontier.get(rid, {}).get("continuation_route_ids", []))
    except (ValueError, OSError, KeyError, TypeError):
        pass
    return False


def verification_status_parent(run_file, document, action, request):
    """Allow a catalog-priced free getter only for this run's saved submission."""
    tool, payload = request.get("tool", ""), request.get("payload", {})
    family = validator_for_tool(tool)
    if (not action.get("status_read") or action.get("provider") != "deepline"
            or request.get("operation") != "execute" or action.get("phase") != "email_validation"
            or action.get("cost_upper_bound_credits") != 0 or not family
            or "get" not in re.findall(r"[a-z]+", tool.casefold()) or not payload.get("id")):
        return None
    try:
        description = next(r for r in reversed(document["routes"]) if r.get("provider") == "deepline"
                           and r.get("operation") == "describe" and r.get("tool") == tool)
        saved = _saved_receipt(run_file, description)
        if saved.get("receipt_status") != "complete" or saved.get("status") != "ok":
            return None
        normalized, _ = deepline.normalize_response({"operation": "describe", "tool": tool}, saved["provider_response"])
        contract = next(r for r in normalized["results"] if r.get("toolId", r.get("id")) == tool)
        if call_credits(contract, payload) != 0:
            return None
        for route in reversed(document["routes"]):
            if (route.get("provider_status") != "partial"
                    or route.get("scope") != action.get("scope") or validator_for_tool(route.get("tool")) != family):
                continue
            pending, _ = _pending_job(run_file, route)
            if pending["id"] != payload["id"]:
                continue
            email = _pending_email(run_file, document, route["route_id"], pending)
            if (_text(payload.get("email")) and _text(payload["email"]) != email
                    or verification_finished(run_file, document, route["route_id"], pending)):
                return None
            return route["route_id"]
    except (ValueError, OSError, KeyError, TypeError, StopIteration):
        pass
    return None


def unused_pending_verifications(document, run_file):
    """Keep unselected, unfinished jobs in the audit without blocking delivery."""
    contacts = []
    for row in document.get("accepted", []):
        if isinstance(row, dict):
            backups = row.get("backup_contacts", [])
            contacts.extend(c for c in [row.get("primary_contact"), *(backups if isinstance(backups, list) else [])]
                            if isinstance(c, dict))
    emails = {_text(c.get("email")) for c in contacts}
    used = set()
    for contact in contacts:
        validation = contact.get("email_validation")
        if isinstance(validation, dict):
            for verdict in (validation, validation.get("fallback")):
                if isinstance(verdict, dict) and isinstance(verdict.get("source"), dict):
                    used.add(verdict["source"].get("route_id"))
    frontier = {r["route_id"]: r for r in document.get("stop_audit", {}).get("route_frontier", [])}
    unused = set()
    for route in document.get("routes", []):
        rid = route.get("route_id")
        if (route.get("phase") != "email_validation" or route.get("provider_status") != "partial"
                or rid in used or frontier.get(rid, {}).get("state") not in {"untried", "continuable"}):
            continue
        try:
            pending, _ = _pending_job(run_file, route)
            if _pending_email(run_file, document, rid, pending) not in emails:
                unused.add(rid)
        except (ValueError, OSError, KeyError, TypeError):
            pass  # Malformed or cross-run receipts remain validation errors.
    return unused


def _sources(routes, validator):
    for route in reversed(routes):
        if (isinstance(route, dict) and route.get("provider") == "deepline" and route.get("operation") == "execute"
                and route.get("phase") == "email_validation"
                and validator_for_tool(route.get("tool")) == validator):
            yield {"provider": "deepline", "operation": "execute", "validator": validator,
                   "tool": route["tool"], "route_id": route["route_id"]}


def pending_verification_errors(document, run_file, *, allow_unused=False):
    errors = []
    unused = unused_pending_verifications(document, run_file) if allow_unused else set()
    for route in document.get("routes", []):
        if not isinstance(route, dict) or route.get("phase") != "email_validation" or route.get("provider_status") != "partial":
            continue
        try:
            pending, _ = _pending_job(run_file, route)
            if route["route_id"] in unused or verification_finished(run_file, document, route["route_id"], pending):
                continue
        except (ValueError, OSError, KeyError, TypeError):
            pass
        errors.append("Pending verification needs status recovery: " + str(route.get("route_id")))
    return errors


def fallback_allowed(verdict):
    return verdict.get("status") in {"catch-all", "unknown"} or (
        verdict.get("status") is None and verdict.get("provider_status") in FAILURES)


def original_validation(run_file, routes, email):
    for source in _sources(routes, "zerobounce"):
        try:
            return saved_result(run_file, routes, source, email), source
        except OtherEmail:
            continue
    return None, None


def email_work(action, request):
    """Identify email operations, including email add-ons to profile enrichment."""
    if not action.get("paid_calls") or action.get("status_read"):
        return False
    tool = request.get("tool", request.get("operation", ""))
    words = set(re.findall(r"[a-z]+", tool.casefold()))
    payload = request.get("payload", request)
    addon = any(str(value).casefold() in {"true", "1"} for key, value in payload.items()
                if re.sub(r"[^a-z]", "", key.casefold()) in {"findemail", "enrichemail", "includeemail"})
    # A selected, reviewed profile explicitly identifies email work even when
    # the provider calls its operation a domain/person search. The caller still
    # verifies that receipt and role before dispatch or reserving spending.
    return bool(action.get("contact_ref") or validator_for_tool(tool) or addon or action.get("phase") == "email_validation"
                or "email" in tool.casefold() and not words & {"balance", "credits"})


def decision(run_file, routes, source, email):
    """One receipt-derived presentation of usability; never promote a domain flag."""
    verdict = saved_result(run_file, routes, source, email)
    validator = validator_for_tool(source.get("tool"))
    usable = (validator == "zerobounce" and verdict.get("status") == "valid" or
              validator == "bounceban" and verdict.get("status") == "success" and verdict.get("result") == "deliverable")
    fallback = validator == "zerobounce" and fallback_allowed(verdict)
    note = None
    if fallback:
        try:
            check_fallback(run_file, budget_guard.read_object(Path(run_file)),
                           {"operation": "execute", "tool": "bounceban_verify", "payload": {"email": email}})
        except (ValueError, OSError) as exc:
            fallback, note = False, str(exc)
    if usable and validator == "bounceban":
        original, original_source = original_validation(run_file, routes, email)
        ids = [r.get("route_id") for r in routes]
        usable = bool(original) and fallback_allowed(original) and ids.index(original_source["route_id"]) < ids.index(source["route_id"])
    return {**verdict, "usable": bool(usable), "fallback_allowed": fallback,
            "next": ("Use this saved valid email; a domain catch-all flag does not invalidate the address."
                     if usable else "Eligible for one budgeted BounceBan check; reuse any existing attempt."
                     if fallback else note or "Do not deliver this address or override a hard negative; inspect saved evidence or choose another contact.")}


def route_decisions(run_file, document, route):
    """Expose saved validation results without confusing them with contact identity."""
    if not validator_for_tool(route.get("tool")) or route.get("phase") != "email_validation":
        return []
    saved = _saved_receipt(run_file, route)
    records = deepline._records(saved.get("provider_response", {}).get("body"))
    addresses = {r.get("address", r.get("email")) for r in records if isinstance(r, dict)}
    addresses.add(saved.get("attempt", {}).get("request", {}).get("payload", {}).get("email"))
    source = {k: route[k] for k in ("provider", "operation", "tool", "route_id")}
    results = []
    for email in sorted(a for a in addresses if isinstance(a, str) and a):
        try:
            value = decision(run_file, document["routes"], source, email)
        except (ValueError, OSError, KeyError) as exc:
            value = {"email": email, "usable": False, "fallback_allowed": False, "next": str(exc)}
        results.append({"ref": route["route_id"], **value})
    return results


def check_fallback(run_file, document, request):
    """Refuse unnecessary or repeated BounceBan dispatch before reserving spend."""
    if request.get("operation") != "execute" or validator_for_tool(request.get("tool")) != "bounceban":
        return
    email = request.get("payload", {}).get("email")
    if not _text(email):
        raise ValueError("BounceBan verification requires an exact email")
    routes = document.get("routes", [])
    found, _ = original_validation(run_file, routes, email)
    if found is None or not fallback_allowed(found):
        raise ValueError("BounceBan requires a saved same-email ZeroBounce catch-all/unknown or service failure; valid and hard-negative verdicts cannot use fallback")
    # A changed mode or route ID is not permission to repeat a billed verification.
    seen = set()
    for route in routes + document.get("stop_audit", {}).get("route_frontier", []):
        if (route.get("provider") != "deepline" or route.get("operation") != "execute"
                or route.get("phase") != "email_validation" or route.get("route_id") in seen):
            continue
        seen.add(route["route_id"])
        path = receipt_path(run_file, route["route_id"])
        if route.get("state") == "untried" and not path.exists():
            continue  # Planned work has not entered preparation yet.
        saved = _saved_receipt(run_file, route)
        attempted = saved.get("attempt", {}).get("request", {})
        if (validator_for_tool(attempted.get("tool")) == "bounceban"
                and _text(attempted.get("payload", {}).get("email")) == _text(email)):
            raise ValueError("BounceBan was already attempted for this email; recover its saved job")


def email_receipt_errors(document, run_file, *, fill_missing=False):
    errors, routes = [], document.get("routes", [])
    for index, row in enumerate(document.get("accepted", [])):
        if not isinstance(row, dict):
            continue
        contacts = [(f"accepted[{index}].primary_contact", row.get("primary_contact"))]
        backups = row.get("backup_contacts", [])
        contacts += [(f"accepted[{index}].backup_contacts[{i}]", c) for i, c in enumerate(backups if isinstance(backups, list) else [])]
        for path, contact in contacts:
            if not isinstance(contact, dict) or not _text(contact.get("email")):
                continue
            email = contact["email"]
            receipt = contact.get("email_validation")
            if receipt is None and fill_missing:
                for source in _sources(routes, "zerobounce"):
                    try:
                        verdict = saved_result(run_file, routes, source, email)
                    except OtherEmail:
                        continue
                    except (ValueError, OSError) as exc:
                        errors.append(f"{path}.email_validation: {exc}")
                        break
                    receipt = contact["email_validation"] = dict(verdict, source=source)
                    break
            source = receipt.get("source") if isinstance(receipt, dict) else None
            before = source.get("route_id") if isinstance(source, dict) else None
            if not discovery_source(run_file, routes, email, before=before):
                errors.append(f"{path}.email_source: exact address must be discovered in a saved finder/page before validation")
            pending = [(path + ".email_validation", receipt)]
            if isinstance(receipt, dict) and "fallback" in receipt:
                pending.append((path + ".email_validation.fallback", receipt["fallback"]))
            for field, supplied in pending:
                if not isinstance(supplied, dict):
                    continue  # Existing structural checks report missing receipts.
                try:
                    source = supplied.get("source")
                    actual = saved_result(run_file, routes, source if isinstance(source, dict) else {}, email)
                    for key in ("email", "status", "result", "provider_status"):
                        if key not in actual:
                            if key in supplied:
                                raise ValueError(f"{key} is absent from the saved provider verdict")
                            continue
                        if fill_missing and key not in supplied:
                            supplied[key] = actual[key]
                        if key not in supplied or _text(supplied[key]) != _text(actual[key]) or (supplied[key] is None) != (actual[key] is None):
                            raise ValueError(f"{key} must match the saved provider verdict ({actual[key]!r})")
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    errors.append(f"{field}: {exc}")
    return errors

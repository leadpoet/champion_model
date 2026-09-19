"""Match a bounded read-only billing page to this run's uncertain calls."""

import hashlib
import json
import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections import Counter
from contextlib import ExitStack, contextmanager

import budget_guard as budget
import deepline
import deepline_http
import run_coordination
from record_route import mutate


FIELDS = ("id", "request_id", "provider", "operation", "status", "charge_state", "credits", "delta", "created_at",
          "outcome", "provider_units", "pricing_basis", "pricing_model", "reason",
          "source", "charge_credits", "billing_stage")
MAX_ATTEMPTS = 3
RETRY_AFTER_SECONDS = 60
READ_TIMEOUT_SECONDS = 30


class _RecoveryBusy(Exception):
    """Retry local state later; never count lock contention as a billing error."""


@contextmanager
def _state_update(path):
    # Native finish may already hold this reentrant lock. Never wait behind a
    # different owner while holding the recovery guard or a caller's deadline.
    with ExitStack() as state:
        try:
            state.enter_context(run_coordination.locked(path, blocking=False))
        except BlockingIOError:
            raise _RecoveryBusy from None
        yield


def _catalog_contract(run_file, receipt, route_id=None, *, before_route=None, bound_call=None):
    """Reuse a run-bound descriptor; alias resolution never calls a provider."""
    from source_receipts import read_receipt
    if bound_call and bound_call.get("catalog_route_id"):
        bound_id = bound_call["catalog_route_id"]
        path = Path(run_file).parent / "receipts" / (bound_id + ".json")
        if (route_id is not None and route_id != bound_id
                or hashlib.sha256(path.read_bytes()).hexdigest() != bound_call.get("catalog_sha256")):
            raise ValueError("billing catalog changed since dispatch; preserve the original contract")
        route_id = bound_id
    routes = budget.read_object(run_file).get("routes", [])
    if before_route is not None:
        position = next((i for i, row in enumerate(routes) if row.get("route_id") == before_route), None)
        if position is None:
            return None, None
        routes = routes[:position]
    for route in reversed(routes):
        if (route.get("provider") != "deepline" or route.get("operation") != "describe"
                or route.get("provider_status") != "ok" or route.get("tool") != receipt.get("tool")
                or (route_id is not None and route.get("route_id") != route_id)):
            continue
        body = read_receipt(run_file, route["route_id"])["result"]
        contracts = [c for c in body.get("results", []) if receipt.get("tool") in _aliases(c)]
        if len(contracts) == 1:
            return contracts[0], route["route_id"]
    if route_id is not None:
        raise ValueError("billing catalog reference is missing or belongs to another tool")
    return None, None  # Legacy prefixed tools can still use exact matching.


def _aliases(contract):
    if not isinstance(contract, dict):
        return set()
    names = [contract.get(k) for k in ("toolId", "id", "tool", "operation", "operationId")]
    aliases = contract.get("operationAliases", [])
    if isinstance(aliases, list):
        names.extend(aliases)
    return {name for name in names if isinstance(name, str) and name}


def free_call_evidence(run_file, route_id, call):
    """A completed call to an unconditionally free, previously described tool.

    This is contract evidence, not a fabricated billing record. Paid, variable,
    conditional, failed and incompletely captured calls still need billing.
    """
    from source_receipts import read_receipt
    if not call.get("catalog_route_id") or not call.get("catalog_sha256"):
        return None  # Older calls without a dispatch-bound contract need billing.
    saved = read_receipt(run_file, route_id)
    receipt = saved["result"]
    if (receipt.get("status") == "schema_error" and receipt.get("error_stage") == "response"
            and receipt.get("receipt_status") == "complete" and receipt.get("provider_response")):
        # A parser repair can recognize a saved success without rewriting its
        # original receipt or replaying the request. Audit repeats this proof.
        normalized, _ = deepline.normalize_response(receipt["attempt"]["request"], receipt["provider_response"])
        receipt = dict(receipt, **normalized)
    if (receipt.get("provider") != "deepline" or receipt.get("operation") != "execute"
            or receipt.get("status") != "ok" or receipt.get("receipt_status") != "complete"
            or receipt.get("billing") or receipt.get("pending_verification")):
        return None
    contract, catalog_id = _catalog_contract(run_file, receipt, call["catalog_route_id"], before_route=route_id)
    pricing = (contract or {}).get("pricing", {})
    if (not contract or not isinstance(pricing, dict)
            or contract.get("callable") is not True or contract.get("disabled")
            or pricing.get("unit") not in {"call", "request"}
            or type(pricing.get("creditsPerUnit")) not in (int, float) or pricing["creditsPerUnit"] != 0
            or (pricing.get("usdPerUnit") is not None and
                (type(pricing["usdPerUnit"]) not in (int, float) or pricing["usdPerUnit"] != 0))
            or pricing.get("summary") or pricing.get("details")):
        return None
    catalog_path = Path(run_file).parent / "receipts" / (catalog_id + ".json")
    digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    if digest != call["catalog_sha256"]:
        return None
    return {"catalog_route_id": catalog_id,
            "catalog_sha256": digest,
            "response_sha256": hashlib.sha256(Path(saved["receipt_file"]).read_bytes()).hexdigest()}


def settle_free_calls(run_file):
    """Settle only saved version 2 free-call contracts, without network I/O."""
    run_file = Path(run_file).resolve(strict=True)
    ledger = budget.load_ledger(run_file)
    if ledger["version"] != 2:
        return []
    recorded = {row["route_id"] for row in budget.read_object(run_file).get("routes", [])}
    proofs = {}
    for rid, call in ledger["calls"].items():
        if (rid in recorded and call["provider"] == "deepline" and call.get("state") == "pending_billing"
                and call.get("actual_credits") is None and call.get("actual_usd") is None
                and not call.get("billing_evidence") and not call.get("billing_issue")):
            if proof := free_call_evidence(run_file, rid, call):
                proofs[rid] = proof
    settled = []
    if proofs:
        with budget.transaction(budget.ledger_path(run_file)) as saved:
            budget.check_run_identity(run_file, saved)
            for rid, proof in proofs.items():
                call = saved["calls"][rid]
                if (call.get("state") == "pending_billing" and call.get("actual_credits") is None
                        and call.get("actual_usd") is None and not call.get("billing_evidence")):
                    call.update(actual_credits="0", state="settled", free_evidence=proof)
                    settled.append(rid)
    # Also repair a prior interruption between ledger settlement and route save.
    if any(call.get("free_evidence") for call in budget.load_ledger(run_file)["calls"].values()):
        _synchronize(run_file)
    return settled


def _free_per_call(contract):
    pricing = (contract or {}).get("pricing", {})
    return (isinstance(pricing, dict) and pricing.get("unit") in {"call", "request"}
            and type(pricing.get("creditsPerUnit")) in (int, float) and pricing["creditsPerUnit"] == 0
            and (pricing.get("usdPerUnit") is None or
                 (type(pricing["usdPerUnit"]) in (int, float) and pricing["usdPerUnit"] == 0))
            and not pricing.get("summary") and not pricing.get("details"))


def matching_charge(receipt, rows, contract=None):
    ids = {receipt.get(key) for key in ("job_id", "request_id") if receipt.get(key)}
    tool = receipt.get("tool", "")
    if contract and contract.get("provider"):
        if tool not in _aliases(contract):
            return None
        matches = [row for row in rows if row.get("request_id") in ids
                   and row.get("provider") == contract["provider"] and row.get("operation") in _aliases(contract)]
    else:
        matches = [row for row in rows if row.get("request_id") in ids and row.get("operation") == tool
                   and isinstance(row.get("provider"), str) and row["provider"]
                   and tool.casefold().startswith(row["provider"].casefold() + "_")]
    # A usage summary can group several executions or duplicate a ledger debit.
    # Prefer the individual debit; never split an aggregate or count both feeds.
    ledger_matches = [row for row in matches if row.get("source") == "credit_ledger"]
    if ledger_matches:
        matches = ledger_matches
    if len(matches) != 1:
        return None
    row = matches[0]
    metadata = row.get("metadata")
    groups = metadata.get("chargeGroupIds", []) if isinstance(metadata, dict) else [] if metadata is None else None
    if not isinstance(groups, list) or len(groups) > 1 or (groups and groups != [row["request_id"]]):
        return None  # Never assign an aggregate charge to an individual call.
    # Billing's failed-attempt record is a final zero-charge outcome. An HTTP
    # error alone is not: require the exact billing identity and explicit zeros.
    failed_attempt = (row.get("status") == "error" and row.get("charge_state") == "failed"
                      and row.get("reason") == "operation_attempt"
                      and receipt.get("status") in deepline._FAILURE_STATUSES
                      and not receipt.get("results"))
    empty_result = (row.get("status") == "no_result" and row.get("charge_state") == "free"
                    and row.get("outcome") == "miss"
                    and ((receipt.get("status") == "no_results" and not receipt.get("results"))
                         or _free_per_call(contract)))
    completed = row.get("status") == "completed" and row.get("charge_state") in {"posted", "free"}
    if not row.get("id") or not (completed or failed_attempt or empty_result):
        return None
    try:
        charge = budget.amount(row.get("credits"), "posted credits")
        if (isinstance(row.get("delta"), bool) or not isinstance(row.get("delta"), (int, float, str))
                or -Decimal(str(row["delta"])) != charge
                or ((row["charge_state"] == "free" or failed_attempt) and charge != 0)):
            return None
        if row.get("source") == "credit_ledger" and (
                row.get("reason") != "charge_settle" or row.get("billing_stage") != "posted"
                or row.get("charge_state") != "posted"
                or budget.amount(row.get("charge_credits"), "ledger credits") != charge):
            return None
    except (ValueError, TypeError, InvalidOperation):
        return None
    proof = {key: row[key] for key in FIELDS if key in row}
    if groups:
        proof["metadata"] = {"chargeGroupIds": groups}
    return proof


def _ledger_rows(rows):
    """Project individual final debits, retaining the fields needed to re-audit.

    Holds, releases, top-ups and refunds are not per-execution final prices.
    Duplicate/adjusted debits remain ambiguous in matching_charge.
    """
    result = []
    for row in rows:
        if row.get("reason") != "charge_settle":
            continue
        metadata, audit = row.get("metadata") or {}, row.get("billing_audit") or {}
        if not isinstance(metadata, dict) or not isinstance(audit, dict):
            continue
        groups = metadata.get("chargeGroupIds", [])
        if not isinstance(groups, list) or (groups and groups != [row.get("request_id")]):
            continue
        if any(value is not None and value != row.get(field) for field, value in (
                ("request_id", metadata.get("chargeGroupId")),
                ("request_id", metadata.get("requestId")),
                ("request_id", audit.get("request_id")),
                ("provider", audit.get("provider")), ("operation", audit.get("operation")),
                ("charge_credits", audit.get("charge_credits")),
                ("charge_credits", metadata.get("postedCredits")))):
            continue
        result.append({**{key: row[key] for key in FIELDS if key in row},
                       "source": "credit_ledger", "status": row.get("status", "completed"), "credits": row.get("charge_credits")})
    return result


def billing_issue(receipt, proof, contract=None):
    """Flag billing/result contradictions, without interpreting company fit."""
    if budget.amount(proof["credits"], "posted credits") != 0 or not (proof.get("outcome") == "miss" or
            (proof.get("pricing_basis") == "result" and proof.get("provider_units") == 0)):
        return None
    # A catalog-confirmed free call has no result-based charge to contradict.
    # The matched billing record is still required; a price quote alone is not spend.
    if _free_per_call(contract):
        return None
    rows = receipt.get("results", [])
    # Share the adapter's explicit no-address interpretation. Only matched
    # zero-charge billing can settle this; an empty response alone never does.
    if deepline.empty_email_finder_records(receipt.get("tool"), rows):
        return None
    # Some tools return one envelope even when its actual contact list is empty.
    def populated(row):
        if isinstance(row, dict):
            for key in ("persons", "contacts", "results", "items", "data"):
                if isinstance(row.get(key), list):
                    return bool(row[key])
        return bool(row)
    if any(populated(row) for row in rows):
        return "Results returned, but billing reports a miss or zero result units; charge remains pending."
    return None


def _save_status(path, status):
    with _state_update(path), budget.transaction(path) as saved:
        saved.clear()
        saved.update(status)


def _settle_charges(run_file, receipts, rows, status):
    """Commit attributable charges before advancing the billing cursor."""
    matched, contracts = {}, {}
    ledger = budget.load_ledger(run_file)
    for rid, receipt in receipts.items():
        contract, catalog_id = _catalog_contract(run_file, receipt, bound_call=ledger["calls"][rid])
        proof = matching_charge(receipt, rows, contract)
        if proof and catalog_id:
            proof["catalog_route_id"] = catalog_id
        matched[rid] = proof
        contracts[rid] = contract
    counts = Counter(proof["id"] for proof in matched.values() if proof)
    with budget.transaction(budget.ledger_path(run_file)) as saved:
        budget.check_run_identity(run_file, saved)
        used = {call["billing_evidence"]["id"]: rid for rid, call in saved["calls"].items()
                if call.get("billing_evidence")}
        for rid, receipt in receipts.items():
            call, proof = saved["calls"][rid], matched[rid]
            if (proof is None or counts[proof["id"]] != 1 or used.get(proof["id"], rid) != rid
                    or call["actual_credits"] is not None
                    or (saved["version"] == 2 and call.get("actual_usd") is not None)):
                continue
            issue = billing_issue(receipt, proof, contract=contracts[rid])
            if call.get("billing_evidence") and call["billing_evidence"] != proof:
                call.setdefault("billing_history", []).append(call["billing_evidence"])
            call.update(billing_evidence=proof, billing_issue=issue,
                        actual_credits=None if issue else str(budget.amount(proof["credits"], "posted credits")))
            if saved["version"] == 2:
                call["state"] = "pending_billing" if issue else "settled"
            if budget.price_overrun(call, saved):
                saved["blocked"] = budget.PRICE_OVERRUN
            if rid not in status["matched"]:
                status["matched"].append(rid)
            if not issue:
                status["unmatched"].remove(rid)


def reconcile(run_file, *, fetch=None, refresh=False, resume=False, timeout_seconds=READ_TIMEOUT_SECONDS):
    """One recovery owner per run; peers read progress without waiting on I/O."""
    run_file = Path(run_file).resolve(strict=True)
    with ExitStack() as owner:
        try:
            owner.enter_context(run_coordination.locked(run_file, 'billing-reconciliation', blocking=False))
        except BlockingIOError:
            path = run_file.parent / 'billing-status.json'
            return {**(budget.read_object(path) if path.exists() else {}), 'in_progress': True}
        try:
            return _reconcile(run_file, fetch=fetch, refresh=refresh, resume=resume, timeout_seconds=timeout_seconds)
        except _RecoveryBusy:
            path = run_file.parent / 'billing-status.json'
            return {**(budget.read_object(path) if path.exists() else {}),
                    'in_progress': True, 'waiting_for': 'run_state'}


def _reconcile(run_file, *, fetch, refresh, resume, timeout_seconds):
    """Up to three billing reads per call set, persisted across resume.

    Failed reads get one immediate retry. Pending/contradictory rows can be
    revisited after a cooldown or at final approval, within the same limit.
    Paid requests are never replayed. Unknown charges remain pending.
    """
    run_file = Path(run_file).resolve(strict=True)
    with _state_update(run_file):
        settle_free_calls(run_file)
    document = budget.read_object(run_file)
    ledger = budget.load_ledger(run_file)
    routes = {row["route_id"]: row for row in document.get("routes", [])}
    receipts, missing_ids = {}, []
    for rid, call in ledger["calls"].items():
        if call["provider"] != "deepline" or call["actual_credits"] is not None or (ledger["version"] == 2 and call.get("actual_usd") is not None) or rid not in routes:
            continue
        receipt = budget.read_object(run_file.parent / "receipts" / (rid + ".json"))
        if (receipt.get("run_fingerprint") != budget.run_fingerprint(run_file)
                or receipt.get("request_fingerprint") != routes[rid].get("request_fingerprint")):
            raise ValueError("Billing reconciliation requires this run's matching receipt")
        if receipt.get("job_id") or receipt.get("request_id"):
            receipts[rid] = receipt
        else:
            missing_ids.append(rid)
    signature = hashlib.sha256(json.dumps(sorted(ledger["calls"])).encode()).hexdigest()
    status_path = run_file.parent / "billing-status.json"
    status = budget.read_object(status_path) if status_path.exists() else {}
    if status.get("attempt_signature") != signature:
        status = {"attempt_signature": signature, "attempts": 0,
                  **({"billing_org_id": status["billing_org_id"]} if status.get("billing_org_id") else {})}
    status["missing_request_ids"] = sorted(missing_ids)
    if missing_ids:
        status["action_required"] = "Provider omitted billing correlation IDs. Preserve receipts; obtain attributable billing evidence. Never replay the paid calls."
    else:
        status.pop("action_required", None)
    if resume:
        # Explicit billing-only recovery grants a bounded read window, preserving
        # attempt history, all dispatched calls and the original spending limit.
        status["attempt_limit"] = status.get("attempts", 0) + MAX_ATTEMPTS
        status.pop("next_cursor", None)  # Preserve the legacy CLI resume contract.
    attempt_limit = status.get("attempt_limit", MAX_ATTEMPTS)
    attempts = status.get("attempts", 0)
    # Old status files used the signature as a permanent cache, even on errors.
    # Persist each attempt before I/O so resume cannot reset the retry allowance.
    due = resume or refresh or time.time() >= status.get("last_attempt_at", 0) + RETRY_AFTER_SECONDS
    if receipts and attempts < attempt_limit and due:
        read_deadline = time.monotonic() + max(0, min(timeout_seconds, READ_TIMEOUT_SECONDS))
        status.update(matched=[], unmatched=sorted(receipts))
        for _ in range(2):  # One immediate retry for a failed read; never a paid dispatch.
            if status.get("attempts", 0) >= attempt_limit or time.monotonic() >= read_deadline:
                break
            status.update(attempts=status.get("attempts", 0) + 1, last_attempt_at=time.time())
            status.pop("error", None)
            attempt = {"attempt": status["attempts"], "pages": []}
            status.setdefault("read_attempts", []).append(attempt)
            _save_status(status_path, status)
            entries, cursor = [], status.get("next_cursor")
            cursors = {cursor} if cursor else set()
            deadline = read_deadline
            try:
                key = deepline_http.api_key() if fetch is None else None
                transport_failed = False
                if key and status.get("http_scan_version") != 2:
                    # Old cursors may have skipped pages. Start a contiguous scan;
                    # keep the CLI cursor separate from the API's cursor format.
                    status.update(http_scan_version=2, ledger_cursor=None, api_usage_offset=None)
                for source in (("ledger", "usage") if key else ("usage",)):
                    cursor_field = "ledger_cursor" if source == "ledger" else "api_usage_offset" if key else "next_cursor"
                    backlog = status.get(cursor_field)
                    cursor = None if key else backlog
                    cursors = {cursor} if cursor else set()
                    page_identities = set()
                    for page_number in range(1, 2 if fetch else 5):
                        started = time.monotonic()
                        page_read = {"page": page_number, "continued": bool(cursor), "source": source}
                        attempt["pages"].append(page_read)
                        failure_kind = "response"
                        try:
                            if key:
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    raise deepline.CallTimeout("Read-only billing lookup timed out; charges remain pending", "", "")
                                failure_kind = "command"
                                payload = deepline_http.billing_page(source, key=key, cursor=cursor, timeout=remaining)
                                failure_kind = "response"
                                if source == "ledger":
                                    raw_rows = payload.get("entries") if isinstance(payload, dict) else None
                                    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
                                        raise ValueError("Billing response has no recognized ledger rows")
                                    payload = dict(payload, recent={"entries": _ledger_rows(raw_rows),
                                        "next_cursor": payload.get("next_cursor") if payload.get("has_more") else None})
                            elif fetch is None:
                                command = [os.environ.get("DEEPLINE_BIN") or "deepline", "billing", "usage", "--limit", "50", "--json"]
                                if cursor:
                                    command += ["--cursor", cursor]
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    raise deepline.CallTimeout("Read-only billing lookup timed out; charges remain pending", "", "")
                                failure_kind = "command"
                                code, stdout, _ = deepline._invoke(command, remaining)
                                if code:
                                    raise ValueError("Read-only billing lookup unavailable; charges remain pending")
                                failure_kind = "response"
                                payload = deepline._json_from_text(stdout, billing_feed=True)
                            else:
                                payload = fetch()
                            if not isinstance(payload, dict) or not isinstance(payload.get("recent"), dict):
                                raise ValueError("Billing response has no recognized recent-call rows")
                            page = payload["recent"].get("entries")
                            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                                raise ValueError("Billing response has no recognized recent-call rows")
                            failure_kind = "organization"
                            if payload.get("org_id"):
                                if status.get("billing_org_id", payload["org_id"]) != payload["org_id"]:
                                    raise ValueError("Billing organization changed; preserve the original run")
                                status["billing_org_id"] = payload["org_id"]
                            failure_kind = "pagination"
                            next_cursor = payload["recent"].get("next_cursor")
                            if key and source == "usage":
                                recent = payload["recent"]
                                next_cursor = None
                                if recent.get("has_more"):
                                    offset, next_offset = recent.get("offset"), recent.get("next_offset")
                                    if (type(offset) is not int or offset != int(cursor or 0)
                                            or type(next_offset) is not int or next_offset <= offset):
                                        raise ValueError("Invalid or non-advancing billing usage offset")
                                    next_cursor = str(next_offset)
                            if next_cursor and (not isinstance(next_cursor, str) or next_cursor in cursors):
                                raise ValueError("Invalid or repeated billing cursor")
                            identities = frozenset(json.dumps(row, sort_keys=True) for row in page)
                            if identities and identities in page_identities:
                                raise ValueError("Billing pagination repeated records without advancing")
                            page_identities.add(identities)
                            # Offset pages can overlap as new calls arrive. Keep
                            # conflicting rows and same-page duplicates ambiguous.
                            entries.extend([row for row in page if row not in entries])
                            failure_kind = "settlement"
                            with _state_update(run_file):
                                _settle_charges(run_file, receipts, entries, status)
                                # Keep derived costs and progress in this state
                                # section so another writer cannot interrupt them.
                                _synchronize(run_file)
                                # Save the ledger first. Re-reading a page after a
                                # crash is safe; skipping an unsettled page is not.
                                cursor = next_cursor if status["unmatched"] else None
                                if key and page_number == 1 and backlog and status["unmatched"]:
                                    cursor = backlog  # Freshness probe, then the saved scan.
                                status[cursor_field] = cursor
                                page_read.update(outcome="ok", rows=len(page),
                                                 elapsed_seconds=round(time.monotonic() - started, 3))
                                status.setdefault("feed_errors", {}).pop(source, None)
                                attempt["unmatched"] = list(status["unmatched"])
                                _save_status(status_path, status)
                            if cursor:
                                cursors.add(cursor)
                        except deepline_http.BillingUnavailable as exc:
                            page_read.update(outcome="error", failure_kind="transport")
                            status.setdefault("feed_errors", {})[source] = str(exc)
                            transport_failed = True
                            break  # Try the independent feed; never a provider execution.
                        except (ValueError, OSError, KeyError, TypeError, deepline.ConfigError, deepline.CallTimeout) as exc:
                            page_read.update(outcome="error", failure_kind="timeout" if isinstance(exc, deepline.CallTimeout) else failure_kind)
                            raise
                        finally:
                            page_read["elapsed_seconds"] = round(time.monotonic() - started, 3)
                            attempt["unmatched"] = list(status["unmatched"])
                            _save_status(status_path, status)
                        if not cursor:
                            break
                    if not status["unmatched"]:
                        break
                if transport_failed and status["unmatched"]:
                    continue
                break
            except (ValueError, OSError, KeyError, TypeError, deepline.ConfigError, deepline.CallTimeout) as exc:
                status["error"] = str(exc)[:500]
            finally:
                _save_status(status_path, status)
    if missing_ids:
        _save_status(status_path, status)
    # No original response is rewritten. Cost fields are derived from the
    # ledger; a crash between ledger settlement and this write is repairable.
    with _state_update(run_file):
        _synchronize(run_file)
    return status


def wait_for_billing(run_file, *, deadline=None, max_wait_seconds=120):
    """Briefly wait for attributable bills, without a model turn or paid retry."""
    until = time.monotonic() + max_wait_seconds
    if deadline is not None:
        until = min(until, time.monotonic() + max(0, deadline - time.time()))
    status = {}
    while True:
        remaining = until - time.monotonic()
        if remaining <= 0:
            return status
        status = reconcile(run_file, timeout_seconds=remaining)
        state = budget.load_ledger(run_file)
        if budget.spending_stop(state) != "billing_pending":
            return status
        if status.get("in_progress"):
            # Another process owns network recovery. Poll only local state and
            # retain this caller's deadline; do not open another read window.
            time.sleep(min(1, max(0, until - time.monotonic())))
            continue
        # Waiting cannot recover a lost identity or create an unavailable bill.
        if (status.get("missing_request_ids") or not status.get("unmatched")
                or status.get("attempts", 0) >= status.get("attempt_limit", MAX_ATTEMPTS)):
            return status
        delay = max(0, status.get("last_attempt_at", 0) + RETRY_AFTER_SECONDS - time.time())
        remaining = until - time.monotonic()
        if delay >= remaining:
            return status
        time.sleep(min(delay, RETRY_AFTER_SECONDS))


def _synchronize(run_file):
    ledger = budget.load_ledger(run_file)
    def synchronize(saved):
        for route in saved.get("routes", []):
            call = ledger["calls"].get(route["route_id"], {})
            if (call.get("billing_evidence") or call.get("free_evidence")) and call["actual_credits"] is not None:
                actual = budget.report_amount(call["actual_credits"])
                route.update(cost_credits=actual, cost_upper_bound_credits=actual, cost_basis="actual")
        from run_attempt import refresh
        refresh(saved)
        return saved
    if any(call.get("billing_evidence") or call.get("free_evidence") for call in ledger["calls"].values()):
        mutate(run_file, synchronize)


def evidence_error(run_file, route, call):
    proof = call.get("billing_evidence")
    if not proof:
        return None
    receipt = budget.read_object(Path(run_file).resolve().parent / "receipts" / (route["route_id"] + ".json"))
    contract, _ = _catalog_contract(Path(run_file), receipt, proof.get("catalog_route_id"), bound_call=call)
    matched = matching_charge(receipt, [proof], contract)
    issue = billing_issue(receipt, matched, contract=contract) if matched else None
    # Older runs may conservatively retain a now-resolvable free-call warning.
    # Keep that saved reservation auditable until normal reconciliation settles it.
    if matched and issue is None and call.get("billing_issue") and call["actual_credits"] is None:
        issue = billing_issue(receipt, matched)
    if (receipt.get("run_fingerprint") != budget.run_fingerprint(run_file)
            or receipt.get("request_fingerprint") != route.get("request_fingerprint")
            or matched is None or call.get("billing_issue") != issue
            or (issue and call["actual_credits"] is not None)
            or (not issue and budget.amount(matched["credits"], "posted credits") != budget.amount(call["actual_credits"], "ledger credits"))):
        return "posted billing evidence does not match the saved request and charge"
    return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Reconcile saved billing only; never dispatch research or reset spend.")
    parser.add_argument("run_file", type=Path)
    parser.add_argument("--resume", action="store_true", help="Allow up to three more read-only attempts, retaining the audit history")
    args = parser.parse_args()
    print(json.dumps(reconcile(args.run_file, resume=args.resume), indent=2))

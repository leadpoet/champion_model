"""Match a bounded read-only billing page to this run's uncertain calls."""

import hashlib
import json
import os
import time
from pathlib import Path
from collections import Counter

import budget_guard as budget
import deepline
from record_route import mutate


FIELDS = ("id", "request_id", "provider", "operation", "status", "charge_state", "credits", "delta", "created_at",
          "outcome", "provider_units", "pricing_basis", "pricing_model")
MAX_ATTEMPTS = 3
RETRY_AFTER_SECONDS = 60
READ_TIMEOUT_SECONDS = 30


def _catalog_contract(run_file, receipt, route_id=None):
    """Reuse a run-bound descriptor; alias resolution never calls a provider."""
    from source_receipts import read_receipt
    routes = budget.read_object(run_file).get("routes", [])
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
    if len(matches) != 1:
        return None
    row = matches[0]
    metadata = row.get("metadata")
    groups = metadata.get("chargeGroupIds", []) if isinstance(metadata, dict) else [] if metadata is None else None
    if not isinstance(groups, list) or len(groups) > 1 or (groups and groups != [row["request_id"]]):
        return None  # Never assign an aggregate charge to an individual call.
    if not row.get("id") or row.get("status") != "completed" or row.get("charge_state") not in {"posted", "free"}:
        return None
    try:
        charge = budget.amount(row.get("credits"), "posted credits")
        if (type(row.get("delta")) not in (int, float) or -row["delta"] != float(charge)
                or (row["charge_state"] == "free" and charge != 0)):
            return None
    except (ValueError, TypeError):
        return None
    proof = {key: row[key] for key in FIELDS if key in row}
    if groups:
        proof["metadata"] = {"chargeGroupIds": groups}
    return proof


def billing_issue(receipt, proof, contract=None):
    """Flag billing/result contradictions, without interpreting company fit."""
    if budget.amount(proof["credits"], "posted credits") != 0 or not (proof.get("outcome") == "miss" or
            (proof.get("pricing_basis") == "result" and proof.get("provider_units") == 0)):
        return None
    # A catalog-confirmed free call has no result-based charge to contradict.
    # The matched billing record is still required; a price quote alone is not spend.
    pricing = (contract or {}).get("pricing", {})
    rate = pricing.get("creditsPerUnit")
    if pricing.get("unit") in {"call", "request"} and type(rate) in (int, float) and rate == 0:
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


def reconcile(run_file, *, fetch=None, refresh=False, resume=False):
    """Up to three billing reads per call set, persisted across resume.

    Failed reads get one immediate retry. Pending/contradictory rows can be
    revisited after a cooldown or at final approval, within the same limit.
    Paid requests are never replayed. Unknown charges remain pending.
    """
    run_file = Path(run_file).resolve(strict=True)
    document = budget.read_object(run_file)
    ledger = budget.load_ledger(run_file)
    routes = {row["route_id"]: row for row in document.get("routes", [])}
    receipts = {}
    for rid, call in ledger["calls"].items():
        if call["provider"] != "deepline" or call["actual_credits"] is not None or (ledger["version"] == 2 and call.get("actual_usd") is not None) or rid not in routes:
            continue
        receipt = budget.read_object(run_file.parent / "receipts" / (rid + ".json"))
        if (receipt.get("run_fingerprint") != budget.run_fingerprint(run_file)
                or receipt.get("request_fingerprint") != routes[rid].get("request_fingerprint")):
            raise ValueError("Billing reconciliation requires this run's matching receipt")
        if receipt.get("job_id") or receipt.get("request_id"):
            receipts[rid] = receipt
    signature = hashlib.sha256(json.dumps(sorted(ledger["calls"])).encode()).hexdigest()
    status_path = run_file.parent / "billing-status.json"
    status = budget.read_object(status_path) if status_path.exists() else {}
    if status.get("attempt_signature") != signature:
        status = {"attempt_signature": signature, "attempts": 0,
                  **({"billing_org_id": status["billing_org_id"]} if status.get("billing_org_id") else {})}
    if resume:
        # Explicit billing-only recovery grants a bounded read window, preserving
        # attempt history, all dispatched calls and the original spending limit.
        status["attempt_limit"] = status.get("attempts", 0) + MAX_ATTEMPTS
    attempt_limit = status.get("attempt_limit", MAX_ATTEMPTS)
    attempts = status.get("attempts", 0)
    # Old status files used the signature as a permanent cache, even on errors.
    # Persist each attempt before I/O so resume cannot reset the retry allowance.
    due = resume or refresh or time.time() >= status.get("last_attempt_at", 0) + RETRY_AFTER_SECONDS
    if receipts and attempts < attempt_limit and due:
        for _ in range(2):  # One immediate retry for a failed read; never a paid dispatch.
            if status.get("attempts", 0) >= attempt_limit:
                break
            status.update(attempts=status.get("attempts", 0) + 1, last_attempt_at=time.time(),
                          matched=[], unmatched=sorted(receipts))
            status.pop("error", None)
            with budget.transaction(status_path) as saved:
                saved.clear()
                saved.update(status)
            try:
                if fetch is None:
                    entries, cursors, cursor = [], set(), status.get("next_cursor")
                    deadline = time.monotonic() + READ_TIMEOUT_SECONDS
                    for _page in range(4):
                        command = [os.environ.get("DEEPLINE_BIN") or "deepline", "billing", "usage", "--limit", "50", "--json"]
                        if cursor:
                            command += ["--cursor", cursor]
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ValueError("Read-only billing lookup timed out; charges remain pending")
                        code, stdout, _ = deepline._invoke(command, remaining)
                        if code:
                            raise ValueError("Read-only billing lookup unavailable; charges remain pending")
                        payload = json.loads(stdout)
                        if payload.get("org_id"):
                            if status.get("billing_org_id", payload["org_id"]) != payload["org_id"]:
                                raise ValueError("Billing organization changed; preserve the original run")
                            status["billing_org_id"] = payload["org_id"]
                        recent = payload.get("recent", {})
                        page = recent.get("entries")
                        if not isinstance(page, list):
                            raise ValueError("Billing response has no recognized recent-call rows")
                        entries.extend(page)
                        cursor = recent.get("next_cursor")
                        if not cursor:
                            break
                        if not isinstance(cursor, str) or cursor in cursors:
                            raise ValueError("Invalid or repeated billing cursor")
                        cursors.add(cursor)
                    status["next_cursor"] = cursor
                    payload["recent"]["entries"] = entries
                else:
                    payload = fetch()
                if not isinstance(payload, dict) or not isinstance(payload.get("recent"), dict):
                    raise ValueError("Billing response has no recognized recent-call rows")
                rows = payload["recent"].get("entries")
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError("Billing response has no recognized recent-call rows")
                if payload.get("org_id"):
                    if status.get("billing_org_id", payload["org_id"]) != payload["org_id"]:
                        raise ValueError("Billing organization changed; preserve the original run")
                    status["billing_org_id"] = payload["org_id"]
                matched, contracts = {}, {}
                for rid, receipt in receipts.items():
                    contract, catalog_id = _catalog_contract(run_file, receipt)
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
                        status["matched"].append(rid)
                        if not issue:
                            status["unmatched"].remove(rid)
                break
            except (ValueError, OSError, KeyError, TypeError, deepline.ConfigError, deepline.CallTimeout) as exc:
                status["error"] = str(exc)[:500]
            finally:
                with budget.transaction(status_path) as saved:
                    saved.clear()
                    saved.update(status)
    # No original response is rewritten. Cost fields are derived from the
    # ledger; a crash between ledger settlement and this write is repairable.
    ledger = budget.load_ledger(run_file)
    def synchronize(saved):
        for route in saved.get("routes", []):
            call = ledger["calls"].get(route["route_id"], {})
            if call.get("billing_evidence") and call["actual_credits"] is not None:
                actual = float(budget.amount(call["actual_credits"], "posted credits"))
                route.update(cost_credits=actual, cost_upper_bound_credits=actual, cost_basis="actual")
        from run_attempt import refresh
        refresh(saved)
        return saved
    if any(call.get("billing_evidence") for call in ledger["calls"].values()):
        mutate(run_file, synchronize)
    return status


def evidence_error(run_file, route, call):
    proof = call.get("billing_evidence")
    if not proof:
        return None
    receipt = budget.read_object(Path(run_file).resolve().parent / "receipts" / (route["route_id"] + ".json"))
    contract, _ = _catalog_contract(Path(run_file), receipt, proof.get("catalog_route_id"))
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

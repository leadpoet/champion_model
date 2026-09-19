#!/usr/bin/env python3
"""Record dispatches and stop at observed spend; retain historical ledger rules."""

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from run_coordination import locked


PROVIDERS = ("deepline", "scrapingdog")
PRICE_OVERRUN = "provider billed above its reserved bound; reconcile pricing before further paid calls"
_TRANSACTION_LOCK = threading.RLock()


class BudgetError(ValueError):
    pass


class PlanChanged(BudgetError):
    """Progress changed before spending; the proven-unsent check may be replanned."""


def amount(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise BudgetError(f"{name} requires a finite nonnegative amount")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise BudgetError(f"invalid {name}") from exc
    if not result.is_finite() or result < 0:
        raise BudgetError(f"{name} requires a finite nonnegative amount")
    return result


def count(value, name):
    if type(value) is not int or value < 0:
        raise BudgetError(f"{name} requires a nonnegative integer")
    return value


def ledger_path(run_file):
    if not isinstance(run_file, (str, Path)) or not str(run_file).strip():
        raise BudgetError("spend.run_file is required")
    path = Path(run_file).resolve(strict=True)
    return path.with_name(path.name + ".budget.json")


def read_object(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise BudgetError("budget state must be a regular file, not a symlink")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BudgetError("budget state must be a JSON object")
    return value


def load_ledger(run_file, *, allow_unbound=False):
    path = ledger_path(run_file)
    state = read_object(path) if os.path.lexists(path) else None
    if state is not None:
        check_run_identity(run_file, state, allow_unbound=allow_unbound)
    return state


def run_fingerprint(run_file):
    """Bind saved state to its original location, including after path rewrites."""
    return hashlib.sha256(str(Path(run_file).resolve(strict=True)).encode("utf-8")).hexdigest()


def check_run_identity(run_file, state, *, allow_unbound=False):
    if state.get("version") not in {1, 2}:
        raise BudgetError("initialize the run budget ledger before execution")
    if state.get("run_file") != str(Path(run_file).resolve(strict=True)):
        raise BudgetError("ledger belongs to a different run; do not copy or reset budget state")
    if allow_unbound and "run_fingerprint" not in state:
        return  # Historical read-only audit; never authorizes execution.
    if state.get("run_fingerprint") != run_fingerprint(run_file):
        raise BudgetError("ledger run identity is missing or mismatched; preserve state and reconcile its origin")


@contextmanager
def transaction(path):
    # Batch workers share this process. Serialize only ledger writes, never I/O
    # to a provider. Keep the existing fail-closed lock for other processes.
    with locked(path), _TRANSACTION_LOCK:
        with _file_transaction(path) as state:
            yield state


@contextmanager
def _file_transaction(path):
    # Use the route writer's fail-closed lock convention; never expire a lock.
    lock = path.with_name(path.name + ".lock")
    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    temporary = None
    try:
        state = read_object(path) if os.path.lexists(path) else {}
        yield state
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        lock.unlink()


def _initial_state(run_file, document, *, max_usd=None, scrapingdog_usd_per_credit=None, verification_reserve_credits=None):
    """Validate initialization before writing either the run or its ledger."""
    request = document["request"]
    target = count(request["target_count"], "target_count")
    if target == 0:
        raise BudgetError("target_count must be positive")
    if any(row.get("paid_calls", 0) for row in document.get("routes", [])) or document["budget"].get("paid_calls", 0):
        raise BudgetError("initialize before the first paid call; existing paid runs need billing reconciliation")
    limits = document["budget"]["limits"]
    request_limits = request.get("budget", {})
    if any(key != "max_paid_calls" and key in request_limits and request_limits[key] != value
           for key, value in limits.items()):
        raise BudgetError("request.budget and budget.limits must agree")
    credits = {provider: str(amount(limits[f"{provider}_credits"], provider)) for provider in PROVIDERS}
    rates = {"deepline": "0.10", "scrapingdog": None}
    if scrapingdog_usd_per_credit is not None:
        rates["scrapingdog"] = str(amount(scrapingdog_usd_per_credit, "ScrapingDog USD rate"))
    for provider in PROVIDERS:
        if Decimal(credits[provider]) > 0 and (rates[provider] is None or Decimal(rates[provider]) <= 0):
            raise BudgetError(f"a positive USD-per-credit rate is required for enabled {provider}")
    actual_cost = document["budget"].get("policy") == "actual_cost"
    email_required = not actual_cost and "email" in request.get("contact_fields", ["email"])
    if email_required and verification_reserve_credits is None:
        raise BudgetError("email is required: price and supply verification_reserve_credits before discovery")
    reserve = amount(0 if actual_cost or verification_reserve_credits is None else verification_reserve_credits, "verification reserve")
    cap = amount(max_usd if max_usd is not None else str(Decimal("0.50") * target), "USD cap")
    if reserve > Decimal(credits["deepline"]) or reserve * Decimal(rates["deepline"]) > cap:
        raise BudgetError("verification reserve exceeds the run budget")
    next_lead = limits.get("max_deepline_credits_per_next_lead")
    canonical = str(Path(run_file).resolve())
    state = dict(version=2 if actual_cost else 1, run_file=canonical,
                 run_fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                 credit_limits=credits, usd_limit=str(cap), usd_per_credit=rates,
                 next_lead_limit=None if next_lead is None else str(amount(next_lead, "next-lead cap")),
                 verification_reserve_credits=str(reserve), calls={}, blocked=None)
    check_limits(document, state)
    return state


def initialize(run_file, **options):
    path = ledger_path(run_file)
    document = read_object(Path(run_file).resolve(strict=True))
    initial = _initial_state(run_file, document, **options)
    with transaction(path) as state:
        if state or path.exists():
            raise BudgetError("budget ledger already exists; resume it instead of resetting spend")
        state.update(initial)
    return path


def create_run(run_file, document, **options):
    """Create a run and ledger recoverably; never reset an existing ledger.

    The run lock covers both writes. Ledger-first persistence lets a retry
    verify its original caps after interruption before the run file was saved.
    No provider can dispatch until both files exist.
    """
    run_file = Path(run_file).resolve()
    ledger = run_file.with_name(run_file.name + ".budget.json")
    initial = _initial_state(run_file, document, **options)
    initial["initial_request_fingerprint"] = hashlib.sha256(json.dumps(document["request"], sort_keys=True, allow_nan=False).encode()).hexdigest()
    initial["initial_started_at"] = document["stop_check"]["started_at"]
    with transaction(run_file) as saved:
        if run_file.exists() and not saved:
            raise BudgetError("existing run state is empty; reconcile it instead of reinitializing")
        if saved and saved.get("request") != document["request"]:
            raise BudgetError("run already exists with another request; resume its saved criteria")
        with transaction(ledger) as state:
            if (ledger.exists() and not state) or (not saved and state.get("calls")):
                raise BudgetError("existing ledger needs its saved run; reconcile missing state before continuing")
            if state:
                # Older ledgers have no initialization metadata; the existing
                # run's request above is authoritative for their resume.
                compare = {key: value for key, value in initial.items() if key not in {"calls", "blocked"}
                           and not (saved and key.startswith("initial_") and key not in state)}
                if any(state.get(key) != value for key, value in compare.items()):
                    raise BudgetError("initialization settings differ from the saved ledger; preserve its original caps")
            else:
                if saved and (saved.get("routes") or saved.get("budget", {}).get("paid_calls")):
                    raise BudgetError("existing research has no ledger; reconcile it before continuing")
                state.update(initial)
        if not saved:
            saved.update(document)
    return run_file


def check_limits(document, state):
    if (state["version"] == 2) != (document["budget"].get("policy") == "actual_cost"):
        raise BudgetError("budget policy changed; preserve the original run accounting")
    expected = {f"{provider}_credits": Decimal(state["credit_limits"][provider]) for provider in PROVIDERS}
    if state["next_lead_limit"] is not None:
        expected["max_deepline_credits_per_next_lead"] = Decimal(state["next_lead_limit"])
    limits = document["budget"]["limits"]
    requested = document["request"].get("budget", {})
    if any(amount(limits.get(key), key) != value or
           (key in requested and amount(requested[key], key) != value) for key, value in expected.items()):
        raise BudgetError("budget limits changed; reconcile the existing ledger before further paid work")
    if state["next_lead_limit"] is None and any(
        source.get("max_deepline_credits_per_next_lead") is not None for source in (limits, requested)
    ):
        raise BudgetError("next-lead limit changed after ledger initialization")


def model_cost_summary(directory, active_model_receipt=None):
    """Read run-owned response costs once each, including interrupted workers."""
    directory = Path(directory).resolve()
    total, seen, missing = Decimal(0), {}, []
    active_model_receipt = active_model_receipt or os.environ.get("TYCHE_ACTIVE_MODEL_RECEIPT")
    active = []
    # List first, then wait for registry publication: a concurrently created receipt
    # must not appear after the live-generation snapshot and look abandoned.
    paths = sorted((directory / "model-usage").glob("*.json"))
    from run_coordination import snapshot
    workers = (snapshot(directory / "results.json") or {}).get("workers", {})
    live = {v["generation"]: key for key, v in workers.items() if v.get("status") == "running"}
    for path in paths:
        receipt = read_object(path)
        if Path(receipt.get("request_file", "")).resolve().parent != directory:
            raise BudgetError("model receipt belongs to another run")
        if not receipt.get("finished_at"):
            if path.stem == active_model_receipt or live.get(path.stem) == receipt.get("worker_id") and path.stem in live:
                active.append(path.stem)
            else:
                missing.append(path.name)
        responses = receipt.get("responses", [])
        if receipt.get("capture_errors") or (receipt.get("finished_at") and not receipt.get("usage_reconciled")):
            missing.append(path.name)
        for response in responses:
            identity = response.get("response_id")
            if not identity:
                raise BudgetError("model response has no identity")
            cost = response.get("estimated_base_usd")
            # Historical exact estimates can be read without rewriting receipts.
            old = response.get("standard_api_equivalent_usd", {})
            if cost is None and old.get("minimum") == old.get("maximum"):
                cost = old.get("minimum")
            if cost is None:
                missing.append(path.name)
                continue
            value = amount(cost, "base model cost")
            proof = (response.get("model"), response.get("usage"), value)
            if identity in seen:
                if seen[identity] != proof:
                    raise BudgetError("conflicting usage for the same model response")
                continue
            seen[identity] = proof
            total += value
    return {"estimated_llm_usd": float(total), "model_responses": len(seen),
            "missing_model_usage": sorted(set(missing)), "active_model_receipts": active, "model_usage_recorded": bool(paths)}


def actual_cost_summary(state, active_model_receipt=None):
    providers = {}
    for provider in PROVIDERS:
        billed, credits, pending, in_flight = Decimal(0), Decimal(0), [], []
        for rid, call in state["calls"].items():
            if call["provider"] != provider:
                continue
            actual = call.get("actual_credits")
            usd = call.get("actual_usd")
            if actual is not None:
                credits += amount(actual, "billed credits")
            if usd is not None:
                billed += amount(usd, "billed USD")
            elif actual is not None:
                billed += amount(actual, "billed credits") * amount(state["usd_per_credit"][provider], "USD rate")
            elif call.get("state") == "in_flight":
                in_flight.append(rid)
            else:
                pending.append(rid)
        providers[provider] = {"billed_usd": float(billed), "billed_credits": float(credits),
            "catalog_free_calls": [rid for rid, call in state["calls"].items()
                                   if call["provider"] == provider and call.get("free_evidence")],
            "pending_calls": pending, "in_flight_calls": in_flight, "unresolved_calls": len(pending) + len(in_flight)}
    model = model_cost_summary(Path(state["run_file"]).parent, active_model_receipt)
    provider_usd = sum((amount(p["billed_usd"], "provider USD") for p in providers.values()), Decimal(0))
    pending = sum(p["unresolved_calls"] for p in providers.values())
    return {"providers": providers, **model, "provider_usd": float(provider_usd),
            "total_usd": float(provider_usd + amount(model["estimated_llm_usd"], "LLM USD")),
            "pending_provider_calls": pending,
            "status": "incomplete" if pending or model["missing_model_usage"] or model["active_model_receipts"] else "calculated",
            "basis": "reported_provider_charges_plus_estimated_base_llm",
            "note": "Known charges only; pending billing is not zero. LLM cost uses base API rates, not a subscription invoice. Model costs without local receipts belong to the host."}


def observed_credits(call, state):
    if call.get("actual_credits") is not None:
        return amount(call["actual_credits"], "billed credits")
    if call.get("actual_usd") is not None:
        return amount(call["actual_usd"], "billed USD") / amount(state["usd_per_credit"][call["provider"]], "USD rate")
    return Decimal(0)  # Unknown bills are handled by spending_stop's pending check.


def spending_stop(state, *, accepted_count=None, active_model_receipt=None):
    """A stopping threshold, not a guarantee against in-flight overshoot."""
    if state.get("blocked"):
        return state["blocked"]
    totals = actual_cost_summary(state, active_model_receipt)
    if amount(totals["total_usd"], "total") >= amount(state["usd_limit"], "USD limit"):
        return "budget_exhausted"
    for provider, usage in totals["providers"].items():
        cap = amount(state["credit_limits"][provider], "credit limit")
        spent = sum((observed_credits(c, state) for c in state["calls"].values()
                     if c["provider"] == provider), Decimal(0))
        if cap > 0 and spent >= cap:
            return "budget_exhausted"
    if accepted_count is not None and state.get("next_lead_limit") is not None:
        spent = sum((observed_credits(c, state) for c in state["calls"].values()
                     if c["provider"] == "deepline" and c["accepted_leads_before_call"] >= accepted_count), Decimal(0))
        if spent >= amount(state["next_lead_limit"], "next-lead limit"):
            return "budget_exhausted"
    if totals["missing_model_usage"]:
        return "model_usage_pending"
    if any(p["pending_calls"] for p in totals["providers"].values()):
        return "billing_pending"
    return None


def check_allowance(state, provider, bound, accepted_count, *, verification=False):
    """Use the same affordability calculation for planning and locked dispatch."""
    if state["version"] == 2:
        if Decimal(state["credit_limits"][provider]) == 0:
            raise BudgetError(f"{provider} is disabled by its zero credit cap")
        if reason := spending_stop(state, accepted_count=accepted_count):
            raise BudgetError(reason)
        return dict(provider=provider, actual_credits=None, actual_usd=None,
                    state="in_flight", total_before_usd=str(actual_cost_summary(state)["total_usd"]), verification=verification,
                    accepted_leads_before_call=accepted_count)
    if state.get("blocked"):
        raise BudgetError(state["blocked"])
    if any(price_overrun(call, state) and not call.get("reconciliation") for call in state["calls"].values()):
        raise BudgetError(PRICE_OVERRUN)
    if Decimal(state["credit_limits"][provider]) == 0:
        raise BudgetError(f"{provider} is disabled by its zero credit cap")
    entry = dict(provider=provider, maximum_credits=str(amount(bound, "maximum call cost")),
                 actual_credits=None, actual_usd=None, verification=verification,
                 accepted_leads_before_call=accepted_count)
    credits = {name: Decimal(0) for name in PROVIDERS}
    usd = verified = since_last_lead = Decimal(0)
    for call in [*state["calls"].values(), entry]:
        name = call["provider"]
        charge = amount(call["maximum_credits"] if call["actual_credits"] is None else call["actual_credits"], "reserved charge")
        credits[name] += charge
        usd += (charge * amount(state["usd_per_credit"][name], "USD rate")
                if call["actual_usd"] is None else amount(call["actual_usd"], "receipted USD"))
        if call["verification"]:
            verified += charge
        # A review can remove accepted leads. Keep spend at higher historical
        # counts in the current allowance; a correction never creates credit.
        if name == "deepline" and call["accepted_leads_before_call"] >= accepted_count:
            since_last_lead += charge
    hold = max(Decimal(0), Decimal(state["verification_reserve_credits"]) - verified)
    credits["deepline"] += hold
    usd += hold * Decimal(state["usd_per_credit"]["deepline"])
    if usd > Decimal(state["usd_limit"]):
        raise BudgetError(f"shared USD cap would be exceeded: ${usd} including this call, pending calls "
                          f"and ${hold * Decimal(state['usd_per_credit']['deepline'])} reserved for email verification; "
                          f"cap ${state['usd_limit']}. Verification can use its reserve; research cannot.")
    for name in PROVIDERS:
        if credits[name] > Decimal(state["credit_limits"][name]):
            raise BudgetError(f"{name} credit cap would be exceeded, including reservations")
    if provider == "deepline" and state["next_lead_limit"] is not None and since_last_lead > Decimal(state["next_lead_limit"]):
        raise BudgetError("per-next-lead Deepline cap would be exceeded")
    return entry


def reserve(spend, provider, *, verification=False, tool=None):
    if not isinstance(spend, dict):
        raise BudgetError("paid calls require spend with run_file, route_id and max_cost_credits")
    path = ledger_path(spend.get("run_file"))
    route_id = spend.get("route_id")
    if not isinstance(route_id, str) or not route_id.strip():
        raise BudgetError("spend.route_id is required")
    with locked(spend["run_file"]):
        document = read_object(Path(spend["run_file"]).resolve(strict=True))
        accepted = document.get("accepted")
        if not isinstance(accepted, list):
            raise BudgetError("accepted must be an array")
        if "accepted_before" in spend and count(spend["accepted_before"], "planned accepted count") != len(accepted):
            raise PlanChanged("Accepted lead count changed before reservation; no request sent. Replan this check using current run progress.")
        with transaction(path) as state:
            check_run_identity(spend["run_file"], state)
            check_limits(document, state)
            bound = None if state["version"] == 2 else amount(spend.get("max_cost_credits"), "maximum call cost")
            calls = state["calls"]
            if route_id in calls:
                raise BudgetError("route_id already reserved or charged; do not repeat a possibly billed call")
            # Catch recorded calls made outside the ledger instead of forgetting them.
            if any(row.get("paid_calls", 0) and row.get("route_id") not in calls for row in document.get("routes", [])):
                raise BudgetError("paid route missing from ledger; reconcile billing before further execution")
            calls[route_id] = check_allowance(state, provider, bound, len(accepted), verification=verification)
            if state["version"] == 1 and "pricing_basis" in spend:
                calls[route_id]["pricing_basis"] = spend["pricing_basis"]
            if state["version"] == 2 and provider == "deepline" and tool:
                catalog = next((r for r in reversed(document.get("routes", []))
                                if r.get("provider") == provider and r.get("operation") == "describe"
                                and r.get("provider_status") == "ok" and r.get("tool") == tool), None)
                if catalog:
                    receipt = Path(spend["run_file"]).parent / "receipts" / (catalog["route_id"] + ".json")
                    calls[route_id].update(catalog_route_id=catalog["route_id"],
                                          catalog_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
    return path, route_id


def price_overrun(call, state):
    if state["version"] == 2:
        return False  # New runs stop on the combined total, never a guessed call price.
    bound = amount(call["maximum_credits"], "reserved bound")
    return ((call["actual_credits"] is not None and amount(call["actual_credits"], "charge") > bound)
            or (call["actual_usd"] is not None and amount(call["actual_usd"], "USD charge") >
                bound * amount(state["usd_per_credit"][call["provider"]], "USD rate")))


def settle(path, route_id, billing):
    with transaction(path) as state:
        check_run_identity(path.with_name(path.name.removesuffix(".budget.json")), state)
        call = state["calls"][route_id]
        if call["actual_credits"] is not None or call["actual_usd"] is not None:
            raise BudgetError("charge is already settled")
        charge = amount(billing["credits_charged"], "billing.credits_charged") if "credits_charged" in billing else None
        usd = amount(billing["cost_usd"], "billing.cost_usd") if "cost_usd" in billing else None
        call.update(actual_credits=None if charge is None else str(charge), actual_usd=None if usd is None else str(usd))
        if state["version"] == 2:
            call["state"] = "settled" if charge is not None or usd is not None else "pending_billing"
        if price_overrun(call, state):
            state["blocked"] = PRICE_OVERRUN
        return state["blocked"]


def _reconciled_receipt(run_file, receipt_file, route_id, call):
    path = Path(receipt_file).absolute()
    if path.parent.resolve() != Path(run_file).resolve().parent / "receipts":
        raise BudgetError("reconciliation requires this run's saved receipt")
    receipt = read_object(path)
    spend = receipt.get("spend_receipt", {})
    billing = receipt.get("billing", {})
    action = receipt.get("attempt", {}).get("action", {})
    posted = call.get("billing_evidence")
    if posted:
        from billing_reconciliation import evidence_error
        route = next((r for r in read_object(Path(run_file)).get("routes", []) if r.get("route_id") == route_id), {})
        if evidence_error(run_file, route, call):
            raise BudgetError("posted billing proof does not match this run")
        billing = {"credits_charged": posted["credits"]}
        if call["actual_usd"] is not None:
            billing["cost_usd"] = float(call["actual_usd"])
    if (receipt.get("run_fingerprint") != run_fingerprint(run_file)
            or receipt.get("provider") != call["provider"]
            or receipt.get("status") in {"partial", "timeout"}
            or spend != {"route_id": route_id, "ledger": str(ledger_path(run_file)), "state": "reserved" if posted else "settled"}
            or action.get("id") != route_id
            or amount(action.get("cost_upper_bound_credits"), "original reservation") != amount(call["maximum_credits"], "ledger reservation")
            or call["actual_credits"] is None
            or amount(billing.get("credits_charged"), "receipt credits") != amount(call["actual_credits"], "ledger credits")
            or (billing.get("cost_usd") is None) != (call["actual_usd"] is None)
            or (call["actual_usd"] is not None and amount(billing["cost_usd"], "receipt USD") != amount(call["actual_usd"], "ledger USD"))):
        raise BudgetError("receipt identity, reservation and settled billing must match this ledger")
    return {"receipt_file": str(path), "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def reconcile_overruns(run_file, receipt_files, *, pricing_note):
    """Operator recovery after pricing repair; preserve bills, bounds and caps."""
    if not isinstance(pricing_note, str) or not pricing_note.strip() or not receipt_files:
        raise BudgetError("provide saved receipts and a note describing the pricing repair")
    document = read_object(Path(run_file).resolve(strict=True))
    with transaction(ledger_path(run_file)) as state:
        check_run_identity(run_file, state)
        check_limits(document, state)
        if state.get("blocked") != PRICE_OVERRUN:
            raise BudgetError("only a provider price overrun can be reconciled here")
        updated = copy.deepcopy(state)
        updated["blocked"] = None
        for receipt_file in receipt_files:
            receipt = read_object(Path(receipt_file))
            rid = receipt.get("spend_receipt", {}).get("route_id")
            call = updated["calls"].get(rid)
            if call is None or not price_overrun(call, updated) or call.get("reconciliation"):
                raise BudgetError("receipt must identify an unreconciled, settled overrun")
            call["reconciliation"] = {**_reconciled_receipt(run_file, receipt_file, rid, call),
                "pricing_note": pricing_note.strip(), "reconciled_at": datetime.now(timezone.utc).isoformat()}
        errors = audit_ledger(run_file, document, state=updated)
        if errors:
            raise BudgetError("; ".join(errors))
        # Zero additional spend still includes every uncertain call, protected
        # verification allowance, and the existing per-next-lead limit.
        for provider in PROVIDERS:
            if amount(updated["credit_limits"][provider], "credit cap") > 0:
                check_allowance(updated, provider, 0, len(document["accepted"]))
        state.update(updated)
    return {"ledger": str(ledger_path(run_file)), "reconciled": len(receipt_files), "blocked": None}


def guarded_call(request, provider, execute):
    try:
        path, route_id = reserve(request.get("spend"), provider,
                                 tool=request.get("tool"),
                                 verification=provider == "deepline" and request.get("entity_type") == "email_validation")
    except PlanChanged as exc:
        return {"status": "config_error", "error_stage": "coordination", "provider": provider,
                "error": {"message": str(exc)}, "request_sent": False}, 2
    except (ValueError, OSError, KeyError, TypeError, ArithmeticError, AttributeError) as exc:
        return {"status": "quota_exceeded", "error_stage": "budget", "provider": provider,
                "error": {"message": str(exc)}, "request_sent": False}, 2
    body, code = execute()
    body["spend_receipt"] = {"route_id": route_id, "ledger": str(path), "state": "reserved"}
    billing = body.get("billing") if provider == "deepline" else None
    if isinstance(billing, dict) and billing and body.get("status") not in {"partial", "timeout"}:
        try:
            error = settle(path, route_id, billing)
            body["spend_receipt"]["state"] = "settled" if "credits_charged" in billing else "reserved"
            if error:
                body["budget_error"], code = error, 2
        except (ValueError, OSError, KeyError, TypeError, ArithmeticError, AttributeError) as exc:
            body["budget_error"], code = f"settlement failed; charge remains pending: {exc}", 2
    with transaction(path) as state:
        if state["version"] == 2:
            call = state["calls"][route_id]
            if call["state"] == "in_flight":
                call["state"] = "pending_billing"
            body["spend_receipt"]["state"] = call["state"]
    return body, code


def audit_ledger(run_file, document, *, state=None, allow_unbound=False, allow_pending=False):
    """Cross-check dispatch accounting; only draft reviews may retain known pending receipts."""
    errors = []
    try:
        state = load_ledger(run_file, allow_unbound=allow_unbound) if state is None else state
        if state is None:
            return errors
        check_run_identity(run_file, state, allow_unbound=allow_unbound)
        check_limits(document, state)
        if state.get("blocked"):
            errors.append(state["blocked"])
        routes = document.get("routes", [])
        paid = {row["route_id"]: row for row in routes if row.get("paid_calls", 0)}
        recorded = {row["route_id"] for row in routes}
        pending = set()
        if allow_pending:
            from source_receipts import read_receipt, request_fingerprint
            from validate_run import DETERMINATE_PROVIDER_STATUSES, BLOCKING_PROVIDER_STATUSES
            frontier = {r["route_id"]: r for r in document.get("stop_audit", {}).get("route_frontier", [])}
            for rid in state["calls"].keys() - recorded:
                call = state["calls"][rid]
                saved = read_receipt(run_file, rid)["result"]
                action = saved.get("attempt", {}).get("action", {})
                planned = frontier.get(rid, {})
                # Settlement and response capture precede the parent's route
                # write. Draft reviews may cross that window; final audit may not.
                if (saved.get("receipt_status") in {"pending", "response_received", "complete"}
                        and (saved.get("receipt_status") != "complete" or saved.get("status") in
                             DETERMINATE_PROVIDER_STATUSES | BLOCKING_PROVIDER_STATUSES)
                        and not price_overrun(call, state)
                        and action.get("id") == rid and action.get("paid_calls") == 1
                        and action.get("provider") == call["provider"]
                        and all(action.get(key) == planned.get(key) for key in
                                ("provider", "operation", "phase", "scope", "request_fingerprint"))
                        and action.get("description") == planned.get("request_summary")
                        and request_fingerprint(call["provider"], saved.get("attempt", {}).get("request", {})) == saved.get("request_fingerprint")
                        and saved.get("accepted_before") == call["accepted_leads_before_call"]
                        and (state["version"] == 2 or amount(action.get("cost_upper_bound_credits"), "pending bound") == amount(call["maximum_credits"], "reserved bound"))):
                    pending.add(rid)
        if (set(paid) | pending) != set(state["calls"]):
            errors.append("paid route IDs must match the execution ledger; record every reserved call")
        for route_id in set(paid) & set(state["calls"]):
            route, call = paid[route_id], state["calls"][route_id]
            if call.get("billing_evidence"):
                from billing_reconciliation import evidence_error
                if error := evidence_error(run_file, route, call):
                    errors.append(f"{route_id}: {error}")
            if price_overrun(call, state):
                reconciliation = call.get("reconciliation")
                if not isinstance(reconciliation, dict) or not reconciliation.get("pricing_note"):
                    errors.append(f"{route_id}: unreconciled provider price overrun")
                else:
                    saved = _reconciled_receipt(run_file, reconciliation.get("receipt_file"), route_id, call)
                    if saved["receipt_sha256"] != reconciliation.get("receipt_sha256"):
                        errors.append(f"{route_id}: reconciled receipt changed")
            if state["version"] == 2:
                if call.get("free_evidence"):
                    from billing_reconciliation import free_call_evidence
                    if (call.get("billing_evidence") or call.get("state") != "settled"
                            or call.get("actual_credits") != "0" or call.get("actual_usd") is not None
                            or free_call_evidence(run_file, route_id, call) != call["free_evidence"]):
                        errors.append(f"{route_id}: free-call contract evidence does not match the saved call")
                if amount(call.get("total_before_usd"), "spend before dispatch") >= amount(state["usd_limit"], "USD limit"):
                    errors.append(f"{route_id}: dispatched after the total spending threshold")
                usd = call.get("actual_usd")
                if (route.get("cost_usd") is None) != (usd is None) or (usd is not None and amount(route["cost_usd"], "route USD") != amount(usd, "ledger USD")):
                    errors.append(f"{route_id}: USD cost must match the ledger")
                if not call.get("billing_evidence") and not call.get("free_evidence"):
                    receipt = read_object(Path(run_file).parent / "receipts" / (route_id + ".json"))
                    billing = receipt.get("billing", {}) if receipt.get("status") not in {"partial", "timeout"} else {}
                    for field, key in (("actual_credits", "credits_charged"), ("actual_usd", "cost_usd")):
                        if (call.get(field) is None) != (billing.get(key) is None) or (call.get(field) is not None and amount(call[field], field) != amount(billing[key], key)):
                            errors.append(f"{route_id}: charge does not match the saved billing receipt")
            if route.get("provider") != call["provider"] or route.get("paid_calls") != 1:
                errors.append(f"{route_id}: provider and paid_calls must match the ledger")
            if call["provider"] == "deepline" and route.get("accepted_leads_before_call") != call["accepted_leads_before_call"]:
                errors.append(f"{route_id}: accepted-lead count must match the ledger")
            actual = call["actual_credits"]
            basis = ("unknown" if state["version"] == 2 else "estimated") if actual is None else "actual"
            bound = call.get("maximum_credits") if actual is None else actual
            route_bound = route.get("cost_upper_bound_credits")
            if route.get("cost_basis") != basis or (route_bound is not None if bound is None else amount(route_bound, "route bound") != Decimal(bound)):
                errors.append(f"{route_id}: cost basis and bound must match the ledger")
            if (actual is None and route.get("cost_credits") is not None) or (actual is not None and amount(route.get("cost_credits"), "route charge") != Decimal(actual)):
                errors.append(f"{route_id}: actual cost must match the ledger")
    except (ValueError, OSError, KeyError, TypeError, ArithmeticError, AttributeError) as exc:
        errors.append(f"budget ledger: {exc}")
    return errors


def accounting_summary(state):
    """Report observed bills separately from unresolved call reservations."""
    if state["version"] == 2:
        return actual_cost_summary(state)
    providers = {}
    issues = []
    for provider in PROVIDERS:
        billed = reserved = Decimal(0)
        unresolved = 0
        for rid, call in state["calls"].items():
            if call["provider"] != provider:
                continue
            rate = amount(state["usd_per_credit"][provider], "USD rate")
            proof = call.get("billing_evidence", {})
            credits = call["actual_credits"] if call["actual_credits"] is not None else proof.get("credits")
            observed = (amount(call["actual_usd"], "billed USD") if call["actual_usd"] is not None else
                        amount(credits, "billed credits") * rate if credits is not None else Decimal(0))
            billed += observed
            if call["actual_credits"] is None:
                reserved += max(Decimal(0), amount(call["maximum_credits"], "call reservation") * rate - observed)
                unresolved += 1
            if call.get("billing_issue"):
                issues.append({"route_id": rid, "request_id": proof.get("request_id"), "issue": call["billing_issue"]})
        providers[provider] = {"billed_usd": float(billed), "unresolved_reserved_usd": float(reserved),
                               "maximum_usd": float(billed + reserved), "unresolved_calls": unresolved}
    return {"providers": providers, "billing_issues": issues,
            "note": "Billed amounts are provider observations as of reconciliation. Unresolved reservations are budget coverage, not charges. Missing billing is never zero; research receipts and original caps are retained."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", help="existing results.json, before the first paid call")
    parser.add_argument("--max-usd", help="explicit shared cap; default is USD 0.50 per requested lead")
    parser.add_argument("--scrapingdog-usd-per-credit", help="conservative current-plan rate; required when enabled")
    parser.add_argument("--verification-reserve-credits", help="Deepline allowance protected for email verification")
    parser.add_argument("--reconcile-receipt", action="append", help="saved settled overrun receipt; repeat for each overrun after pricing repair")
    parser.add_argument("--pricing-note", help="describe the verified pricing correction; required for reconciliation")
    args = parser.parse_args()
    try:
        if args.reconcile_receipt:
            if any(v is not None for v in (args.max_usd, args.scrapingdog_usd_per_credit, args.verification_reserve_credits)):
                raise BudgetError("reconciliation cannot change budget settings")
            print(json.dumps(reconcile_overruns(args.results, args.reconcile_receipt, pricing_note=args.pricing_note)))
            return
        path = initialize(args.results, max_usd=args.max_usd,
                          scrapingdog_usd_per_credit=args.scrapingdog_usd_per_credit,
                          verification_reserve_credits=args.verification_reserve_credits)
    except (ValueError, OSError, KeyError, TypeError, ArithmeticError, AttributeError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps({"ledger": str(path)}))


if __name__ == "__main__":
    main()

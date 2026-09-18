"""Catalog prices first; measured planning prices only for tested profile modes.

Reservations are estimates, not provider guarantees or bills. Actual returned
billing settles the existing ledger; a higher charge blocks further spending.
"""

import hashlib
import json
import re
from datetime import datetime, timezone, date
from pathlib import Path

import budget_guard as budget


MANAGED_PRICES = Path(__file__).resolve().parents[1] / "references" / "managed-prices.json"


def stored_profile_prices(contract, *, today=None):
    """Only the matching managed billing route may use the reviewed fallback."""
    if (contract.get("toolId", contract.get("id")) != "harvestapi_get_profile"
            or contract.get("billingSource") != "managed_by_deepline"
            or (contract.get("pricing") or {}).get("creditsPerUnit") is not None):
        return []
    try:
        raw = MANAGED_PRICES.read_bytes()
        catalog = json.loads(raw)
        if (type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1
                or not isinstance(catalog.get("version"), str) or not catalog["version"].strip()
                or catalog.get("provider") != "deepline"
                or catalog.get("billing_source") != contract["billingSource"]
                or catalog.get("tool") != "harvestapi_get_profile"
                or catalog.get("basis") != "measured_planning_price"):
            raise ValueError("invalid catalog identity/version")
        verified, expires = (date.fromisoformat(catalog[k]) for k in ("verified_at", "valid_until"))
        if not verified <= (today or datetime.now(timezone.utc).date()) < expires:
            raise ValueError("managed planning prices expired or are not yet effective; refresh their billing evidence")
        prices, seen = catalog["prices"], set()
        if not isinstance(prices, list) or not prices:
            raise ValueError("prices must be a nonempty array")
        result = []
        for row in prices:
            options = row["inputs"]
            if not isinstance(options, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in options.items()):
                raise ValueError("price inputs must contain literal string options")
            if set(options) - {"main", "findEmail", "skipSmtp", "includeAboutProfile"}:
                raise ValueError("price inputs contain identity fields or unknown charged options")
            key = json.dumps(options, sort_keys=True)
            if key in seen:
                raise ValueError("duplicate price input set")
            seen.add(key)
            budget.amount(row["credits"], "managed planning price")
            if not isinstance(row.get("mode"), str) or not row["mode"].strip() or not re.fullmatch(r"[a-f0-9]{64}", row["receipt_sha256"]):
                raise ValueError("price requires a mode and billing receipt hash")
            result.append({**row, **{k: catalog[k] for k in ("verified_at", "valid_until", "basis", "billing_source")},
                           "catalog_version": catalog["version"], "catalog_sha256": hashlib.sha256(raw).hexdigest()})
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("Managed price configuration unavailable: " + str(exc)) from exc


def profile_price(contract, inputs):
    options = {k: v for k, v in inputs.items() if k not in {"url", "publicIdentifier", "profileId"}}
    for price in stored_profile_prices(contract):
        if options == price["inputs"]:
            fingerprint = json.dumps([contract.get("toolId", contract.get("id")), contract["billingSource"], inputs],
                                     sort_keys=True, allow_nan=False)
            return {**price, "request_sha256": hashlib.sha256(fingerprint.encode()).hexdigest()}
    return None


def validate_reservation(request):
    """Recheck a stored managed price immediately before adapter dispatch."""
    spend = request.get("spend") or {}
    if not isinstance(spend, dict):
        return  # The existing budget guard reports malformed spending controls.
    saved = spend.get("pricing_basis")
    if saved is None:
        return  # Legacy/catalog-priced requests retain their existing contract.
    if not isinstance(saved, dict) or saved.get("basis") != "measured_planning_price":
        raise ValueError("Unsupported managed pricing basis")
    contract = {"toolId": request.get("tool"), "billingSource": saved.get("billing_source")}
    current = profile_price(contract, request["payload"])
    if current is None or current != saved:
        raise ValueError("Managed price no longer matches the request or current configuration; resolve pricing again")
    if budget.amount(spend.get("max_cost_credits"), "maximum call cost") < budget.amount(current["credits"], "managed price"):
        raise ValueError("Reservation is below the managed planning price")


def call_credits(contract, inputs, override=None):
    pricing = contract.get("pricing") or {}
    rate, unit = pricing.get("creditsPerUnit"), pricing.get("unit")
    fields = {f["name"]: f for f in contract.get("inputSchema", {}).get("fields", [])}
    quantity = None
    if unit in ("call", "request"):
        quantity = 1
    elif unit == "page" and "page" in fields and not any(k in fields for k in ("pages", "maxPages", "max_pages")):
        quantity = 1
    elif unit == "result":
        if "limit" in fields:
            quantity = inputs.get("limit", fields["limit"].get("default"))
        elif "page_size" in fields:
            quantity = inputs.get("page_size", fields["page_size"].get("default"))
        elif contract.get("toolId", contract.get("id")) == "serper_google_search" and "num" in fields:
            # This endpoint's catalog defines num as the exact result count.
            quantity = inputs.get("num", fields["num"].get("default"))
        elif contract.get("toolId", contract.get("id")) in {
                "zerobounce_validate", "bounceban_verify_single", "hunter_email_finder", "datagma_find_email"}:
            quantity = 1
    bound = None
    if rate is not None and type(quantity) is int and quantity > 0:
        bound = budget.amount(rate, "catalog price") * quantity
    # A published but unsupported pricing unit is not replaced by a guess.
    stored = profile_price(contract, inputs) if rate is None else None
    if bound is None and stored:
        bound = budget.amount(stored["credits"], "measured planning price")
    if bound is None and contract.get("toolId", contract.get("id")) == "harvestapi_get_profile":
        options = (" Stored-price optional input sets: "
                   + json.dumps([price["inputs"] for price in stored_profile_prices(contract)])
                   + ". Keep the profile identity/contact_ref and omit other options."
                   if rate is None else " The published catalog rate takes precedence over stored prices.")
        raise ValueError("No whole-call price is available for these profile options."
                         + options + " No paid call was made; an override cannot substitute for a verified price.")
    if override is not None:
        supplied = budget.amount(override, "whole-call price reservation")
        if bound is not None and supplied < bound:
            raise ValueError("Supplied price bound is below the catalog-derived or stored whole-call cost")
        return float(supplied)
    if bound is None:
        raise ValueError("No whole-call price is available for these options. Use a priced configuration or report the missing rate; do not guess max_cost_credits. No paid call was made.")
    return float(bound)

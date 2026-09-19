"""Versioned endpoint tariffs, independent of account-wide credit deltas.

Only request options actually forwarded by scrapingdog._params are priced.
Keep old versions when tariffs change: saved receipts must remain auditable.
"""
from decimal import Decimal

VERSION = "scrapingdog-2026-09-19"
DOCS = "https://www.scrapingdog.com/documentation/"
PRICING = "https://www.scrapingdog.com/pricing/"
FIXED_20260919 = {
    "universal_search": (20, "universal-search-api"),
    "linkedin_company": (10, "company-profile-scraper"),
    "linkedin_job": (5, "scrape-job-overview"),
    "linkedin_jobs": (5, "scrape-jobs-search-results"),
    "google_jobs": (5, "google-jobs-api"),
    "google_maps": (5, None), "google_maps_place": (5, None),
    "google_local": (5, "google-local-api"),
    "google_ai_mode": (10, "google-ai-mode-api"),
    "google_news": (5, "google-news-search-api"),
    "x_profile": (5, "x-profile-scraper-api"), "x_post": (5, "x-post-scraper-api"),
    "youtube_search": (5, "youtube-search-api"), "youtube_video": (5, "youtube-video-api"),
    "youtube_transcript": (1, "youtube-transcripts-api"),
    "google_ads_transparency": (5, "google-ads-transparency-api"),
    "google_patents": (5, "google-patents-api"), "google_patent_details": (5, "google-patent-details-api"),
    "tiktok_profile": (5, "tiktok-profile-api"), "tiktok_post": (5, "tiktok-post-scraper-api"),
    "tiktok_ads": (5, "tiktok-ads-scraper-api"),
}


def flag(value, default=False):
    if value in (None, ""):
        return default
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError("ScrapingDog pricing options must be true or false")


def quote(operation, params, *, version=VERSION):
    """Return exact credits or a documented range, never a model's price guess."""
    if version == "scrapingdog-2026-09-19":
        return _quote_20260919(operation, params)
    raise ValueError("Unknown saved ScrapingDog tariff version")


def _quote_20260919(operation, params):
    options, sources = {}, []
    if operation in FIXED_20260919:
        low, page = FIXED_20260919[operation]
        high = low
        sources = [DOCS + page + "/" if page else PRICING]
    elif operation == "google_search":
        options = {k: flag(params.get(k)) for k in ("advance_search", "mob_search")}
        low = high = 10 if any(options.values()) else 5
        sources = [DOCS + "google-search-api/"]
    elif operation == "scrape":
        options = {"dynamic": flag(params.get("dynamic"), True), "premium": flag(params.get("premium")),
                   "country": params.get("country") or None}
        if options["country"] and (options["dynamic"] or options["premium"]):
            # Public docs do not specify whether geo pricing replaces or adds
            # to these options. Do not invent a maximum for the combination.
            return None
        low = high = (10 if options["country"] else
                      (25 if options["dynamic"] else 10) if options["premium"] else
                      5 if options["dynamic"] else 1)
        sources = [DOCS + "javascript-rendering/", DOCS + "premium-residential-proxies/", PRICING]
    elif operation == "linkedin_person":
        options = {"premium": flag(params.get("premium"))}
        low, high = 50, 100  # Protected status is not proven by the request flag.
        sources = [DOCS + "person-profile-scraper/", "https://www.scrapingdog.com/profile-scraper-api/"]
    elif operation == "linkedin_post":
        low, high = 5, 25  # Endpoint reference and pricing page disagree.
        sources = [DOCS + "post-scraper/", PRICING]
    else:
        return None
    return {"version": "scrapingdog-2026-09-19", "operation": operation, "options": options,
            "minimum_credits": low, "maximum_credits": high, "sources": sources}


def outcome(tariff, response):
    """Completed success costs the tariff; a lost response retains its ceiling."""
    if not tariff:
        return {}
    status = response.get("http_status")
    complete = not response.get("incomplete") and not response.get("transport_status") and type(status) is int
    body = response.get("body")
    failed = complete and 400 <= status <= 599
    ambiguous = isinstance(body, dict) and (body.get("success") is False
                or any(body.get(k) not in (None, "", [], {}) for k in ("error", "errors")))
    if failed:
        return {"billing": {"credits_charged": 0, "basis": "documented_failed_request_policy",
                            "source": PRICING}, "billing_final": True}
    if complete and status == 200 and not ambiguous and tariff["minimum_credits"] == tariff["maximum_credits"]:
        return {"billing": {"credits_charged": tariff["maximum_credits"], "basis": "documented_endpoint_tariff"},
                "billing_final": True}
    return {"billing_hold": {"maximum_credits": tariff["maximum_credits"],
                             "reason": "variable_tariff" if complete and status == 200 and not ambiguous else "response_unresolved"}}


def audit(receipt, call, route_id=None, run_file=None):
    """Bind charges/holds to the saved request, raw response and dispatch tariff."""
    import scrapingdog
    from source_receipts import request_fingerprint
    from pathlib import Path
    if (receipt.get("provider") != "scrapingdog" or call.get("provider") != "scrapingdog"
            or receipt.get("request_fingerprint") != request_fingerprint("scrapingdog", receipt["attempt"]["request"])):
        return "ScrapingDog receipt does not match the dispatched request"
    if route_id is not None and (receipt["attempt"]["action"].get("id") != route_id
            or receipt["attempt"]["action"].get("provider") != "scrapingdog"
            or receipt.get("spend_receipt", {}).get("route_id") != route_id
            or receipt.get("spend_receipt", {}).get("ledger") != str(Path(run_file).resolve()) + ".budget.json"):
        return "ScrapingDog receipt does not match the dispatched route"
    request = scrapingdog.validate_request(receipt["attempt"]["request"])
    _, params = scrapingdog._params(dict(request, api_key="unused"))
    expected = quote(request["operation_kind"], params, version=call["tariff"]["version"])
    if expected != call.get("tariff") or expected != receipt.get("tariff"):
        return "ScrapingDog tariff does not match the original request"
    derived = outcome(expected, receipt.get("provider_response") or {})
    for key in ("billing", "billing_final", "billing_hold"):
        if receipt.get(key) != derived.get(key):
            return "ScrapingDog accounting does not match its captured response"
    maximum = derived.get("billing_hold", {}).get("maximum_credits")
    actual = derived.get("billing", {}).get("credits_charged")
    if (maximum is None) != (call.get("held_credits") is None) or (maximum is not None and Decimal(str(maximum)) != Decimal(call["held_credits"])):
        return "ScrapingDog hold does not match its captured response"
    if (actual is None) != (call.get("actual_credits") is None) or (actual is not None and Decimal(str(actual)) != Decimal(call["actual_credits"])):
        return "ScrapingDog charge does not match its captured response"
    if call.get("actual_usd") is not None or call.get("billing_evidence") or call.get("free_evidence"):
        return "ScrapingDog tariff cannot be replaced by unrelated billing evidence"
    return None

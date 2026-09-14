"""One prompt used unchanged by all challenger harnesses."""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

from .models import normalize_icp


SYSTEM_PROMPT = """You are a rigorous B2B account researcher. Find companies that fit the supplied ICP and have the REQUIRED recent intent. Use only the provided tools. Never rely on memory for a factual claim. Verify company fit, company stage, every required attribute, and each intent against public source content. Preserve exact source URLs and reject stale, ambiguous, homepage-only, or wrong-company evidence. Prefer direct company, job, regulatory, filing, or reputable news pages. Return at most the requested number, ranked best first. Explain fit and why-now in plain language useful to a salesperson. Do not invent missing facts. Call submit_companies exactly once when done."""


_REDUNDANT_INTENT_FIELDS = frozenset(
    {
        "bonus_intents",
        "intent_category",
        "intent_max_age_days",
        "intent_signal",
        "intent_signal_evidence_types",
        "intent_signal_max_age_days",
        "intent_signal_text",
        "intent_signals",
        "required_intents",
    }
)


def _prompt_icp(normalized: dict[str, Any]) -> dict[str, Any]:
    """Project duplicate intent shapes only when the canonical contract is complete."""

    contract = normalized.get("intent_contract")
    if not isinstance(contract, list) or not contract:
        return dict(normalized)
    canonical_fields = {"index", "signal", "category", "max_age_days", "required"}
    if any(
        not isinstance(row, dict) or not canonical_fields.issubset(row)
        for row in contract
    ):
        return dict(normalized)
    return {
        key: value
        for key, value in normalized.items()
        if key not in _REDUNDANT_INTENT_FIELDS
    }


def build_prompt(icp: dict[str, Any], max_companies: int | None = None) -> str:
    normalized = normalize_icp(icp)
    prompt_icp = _prompt_icp(normalized)
    limit = max(1, min(int(max_companies or 5), 5))
    required_geography = str(
        normalized.get("geography") or normalized.get("country") or ""
    ).strip()
    required_stage = str(normalized.get("company_stage") or "").strip()
    seed_guidance = (
        "Do not relabel pre-seed as Seed unless the ICP explicitly includes pre-seed. "
        if required_stage.casefold() == "seed"
        else ""
    )
    stage_confirmation = (
        "Confirm current stage with a neutral company-name latest-funding/ownership lookup, "
        "without the requested stage; check for later rounds/control changes. "
        if required_stage
        else ""
    )
    required_attribute = str(normalized.get("required_attribute") or "").strip()
    primary = (normalized.get("intent_contract") or [{}])[0]
    contact_guidance = (
        "- Contacts: when a company's basic fit is promising, use get_company_contact before "
        "spending the remaining research calls on it. Prefer candidates with a found contact. "
        "On a miss, continue to another promising candidate when possible; do not loosen fit "
        "or contact requirements. The finalizer reuses these checked contacts automatically; "
        "do not add contact fields to submit_companies.\n"
        if normalized.get("contact_policy") == "contacts_v1" and normalized.get("target_roles")
        else ""
    )
    certification_guidance = (
        "- Certification/compliance: verify the named clearance, standard, audit, or certification "
        "was actually granted to this company or product, with its date. Keep a stated issuer; do "
        "not invent or require an undisclosed auditor. A marketplace listing, partner badge, or "
        "generic compliance claim is not the event.\n"
        if primary.get("category") == "REGULATORY_CLEARANCE"
        else ""
    )
    hiring_guidance = (
        "- Hiring: require a source quote of actual job responsibilities that directly matches "
        "the function named in the required intent text, not merely a broader required_attribute. "
        "Generic sales, renewal, or adoption targets alone do not prove platform, integration, or "
        "RevOps ownership. Shared words such as systems or platform, generic hiring, or an adjacent "
        "function are insufficient. Check current employer/ATS listings and quote current canonical duties. "
        "Aggregator text or a retained Apply now page does not prove an open role.\n"
        if primary.get("category") == "HIRING"
        else ""
    )
    expansion_guidance = (
        "- Market expansion: require source proof of completed entry into a new geography, customer "
        "market, or distinct commercial segment. A non-binding MoU, plan, or added facility, asset, "
        "or capacity in an existing market is insufficient unless the source explicitly connects it "
        "to that new-market entry. Raising capital in a new country, issuing bonds, or accessing a new "
        "investor market alone is financing, not commercial market entry, unless the ICP explicitly "
        "requests financing-market access. A financial-services company entering a new customer market "
        "can qualify with direct evidence of that commercial entry. Verify each country separately; "
        "do not combine actual and planned entry.\n"
        if primary.get("category") == "MARKET_EXPANSION"
        else ""
    )
    funding_guidance = (
        "- Funding: verify capital raised by the target company itself. An investment fund close, "
        "LP commitments, assets under management, or loans the company makes to customers are not "
        "a company funding round unless the ICP explicitly requests those events. Corporate debt or "
        "equity financing can qualify when the ICP does not restrict the financing type.\n"
        if primary.get("category") == "FUNDING"
        else ""
    )
    raw_day = (
        os.environ.get("BAKEOFF_EVALUATION_DATE")
        or os.environ.get("LAB_ARENA_EVALUATION_DATE")
        or ""
    ).strip()
    evaluation_date = date.fromisoformat(raw_day) if raw_day else date.today()
    return (
        f"Evaluation date: {evaluation_date.isoformat()}\n"
        f"Return up to {limit} companies. Omit a company without verified required intent.\n\n"
        "Intent contract:\n"
        "- matched_icp_signal must preserve the listed index. Index 0 is the required primary. Research "
        "and verify every required=true row before any bonus; required=false is optional and never "
        "replaces required evidence.\n"
        "- Apply each max_age_days from the evaluation date.\n"
        f"{json.dumps(prompt_icp, ensure_ascii=False, separators=(',', ':'), sort_keys=True)}\n\n"
        "Required fit:\n"
        f"- Geography: {required_geography or 'not specified'}\n"
        f"- Company stage: {required_stage or 'not specified'}\n"
        f"- Required attribute: {required_attribute or 'not specified'}\n"
        f"{certification_guidance}"
        f"{hiring_guidance}"
        f"{expansion_guidance}"
        f"{funding_guidance}"
        "- Verify required industry, company-HQ geography, current employee band, stage, attribute, and "
        "other stated fit requirements from public evidence; omit missing or conflicting required facts. "
        "An office, facility, job, or served market is not HQ.\n"
        "- For stage, use latest funding/ownership. Preserve the proven stage; normalize only true synonyms "
        "of Seed, Series A, Series B, Series C+, Private Equity, Public, or Bootstrapped. Series C+ requires "
        "Series C or later. Private Equity requires current majority/controlling PE ownership, not an "
        "investment; Public requires listed shares. "
        f"{stage_confirmation}"
        f"{seed_guidance}"
        "Never copy an unproven requested stage.\n"
        "- Employee estimates from discovery/profile are shortlist clues, never current proof. Verify a current "
        "public band or an exact current integer with an identity-bound page, recent filing, or validated "
        "linkedin_structured_evidence.employee_count from employeeCountRange; never use associated/raw employeeCount. "
        "Any approximate, qualified, projected, historical, or snippet-only counts are insufficient. "
        "Map exact integers: 0 or 1 -> 0-1; 2-10 -> 2-10; "
        "11-50 -> 11-50; 51-200 -> 51-200; 201-500 -> 201-500; 501-1,000 -> 501-1,000; "
        "1,001-5,000 -> 1,001-5,000; 5,001-10,000 -> 5,001-10,000; 10,001 or more -> 10,001+. "
        "Use the mapped canonical employee_count when it is listed; never copy "
        "the requested bucket as proof. Include the size source URL in fit_evidence_urls.\n"
        "- Every submitted company needs a verified canonical company_linkedin; U.S. HQ needs a proven state. "
        "linkedin_profile_evidence fields stand alone: identity needs current URL/title matching the company; never generic "
        "login/sign-up; missing employee_count/quote needs other current proof; listed_headquarters proves only its "
        "literal label. linkedin_structured_evidence is separate exact-identity proof, not page quotes. Stored URLs are not proof; omit unproven companies.\n"
        "- If required_attribute exists, return its literal text, passed=true, direct evidence URL, quote, "
        "and explanation; omit the company if that evidence cannot be verified. With no requirement, no "
        "required_attribute object is needed.\n\n"
        "Research order and limits:\n"
        f"{contact_guidance}"
        "If verified_example_company is supplied, use it as the first untrusted discovery seed. Resolve its current "
        "domain with search_web, then profile it and verify current fit and required intent. You may batch independent "
        "candidate discovery alongside these steps. The example is never proof; omit it on missing or conflicting "
        "evidence and continue toward the requested limit.\n"
        "1. Keep fit discovery separate from event verification. For a narrow dated primary event, start with "
        "one focused search_web news/jobs query using business context and required stage. For Series C+, "
        "search Series C, Series D, or later. Queue distinct dated hits; select the best two or three independent "
        "candidates naming the required stage and primary event before more discovery.\n"
        "2. Otherwise use search_companies with a short business-type query and structured fit filters; omit "
        "intent and literal stage. After empty discovery, change the query and loosen one discovery filter or "
        "use one broad search_web fallback; never loosen final fit.\n"
        "3. Make at most two search_companies and three total candidate-finding calls before verifying a plausible "
        "candidate. Verify the strongest untested queued hit before revisiting an empty/failed domain; revisit only "
        "with new direct evidence for its failed fact. Never repeat an equivalent query, domain, or URL.\n"
        f"4. Shortlist at most {min(limit + 2, 7)} domains; expand once only if needed. Process two or three independent "
        "candidates per model response. Batch known-input calls by stage: profiles, event lookups, then exact-URL "
        "fetch_page calls. Never "
        "batch a call that depends on another call's result. Drop explicit fit mismatches; continue the strongest "
        "untested queue until the requested limit is filled or the queue, budget, or deadline is exhausted. "
        "Use get_company_profile or one focused fit search only for "
        "a missing required fact. Prefer current page evidence over stale profile data; do not repeat valid returned "
        "evidence. When a profile lacks current employee_count, for the strongest remaining candidate make one "
        "targeted search_web(mode='search') using its exact company name/domain plus current employees, headcount, "
        "or team size, then fetch_page the best non-LinkedIn identity-bound result. Search snippets select a URL only; "
        "the fetched page body must prove the count. Do not repeat this size fallback for the same candidate. "
        "Empty latest_financing_events does not prove a requested stage.\n"
        "5. For a named candidate whose site: search is empty, use the one allowed non-site alternate instead of a "
        "near-repeat; it is not an extra call. Verify a resulting dated hit before abandoning the candidate.\n\n"
        "Event evidence:\n"
        "- Per candidate, try get_company_events or one focused primary-intent search; use the other only if needed. "
        "Fetch the best returned URL, with one alternate after a failed/unsupported page. Never construct a URL. "
        "A homepage proves identity/fit, not a dated event.\n"
        "- Quote the fetched article body, not snippets, navigation, or related cards. From an annual report or "
        "announcement index, fetch the original dated announcement. A linked event uses its own page, URL, and date.\n"
        "- Use the actual event/announcement date, never crawl, update, or index dates. Preserve source status: beta, "
        "preview, pilot, planned, future, or merely announced is not completed/operational when completion is required. "
        "For appointments, distinguish announcement, effective, and start dates; a future start is not completed. "
        "For a completed launch/opening, use its stated event date, not a later article publication date.\n\n"
        "Explanation and output:\n"
        "- Fit and activity are not buying intent. In why_now, state the verified event, then label one commercial "
        "implication as possible; separate sourced fact from inference.\n"
        "- product_service is what the target sells, not the seller's pitch or a target purchase need. Tie why_now to "
        "the event's effect on the target's operations/growth. Never copy unrelated offerings. Do not invent procurement, "
        "budget, demand, evaluation, or purchase plans; avoid benchmark/scoring jargon and vague 'growing' claims.\n"
        "- Submit ranked, schema-valid JSON when enough companies pass or further work cannot help."
    )

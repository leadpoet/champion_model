from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, ModelRetry, ToolOutput, messages
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage, UsageLimitExceeded

from experiments.harness_bakeoff.adapters import pydantic_ai
from experiments.harness_bakeoff.contacts import enrich_contacts
from experiments.harness_bakeoff.models import CompaniesResult


def _context(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    requests: int = 0,
    tool_calls: int = 0,
    retry: int = 0,
    max_retries: int = 2,
):
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            requests=requests,
            tool_calls=tool_calls,
        ),
        retry=retry,
        max_retries=max_retries,
    )


def test_fit_hints_keep_non_linkedin_evidence_before_profile_without_losing_urls():
    urls = [
        "https://www.linkedin.com/company/example/",
        "https://example.com/investors/annual-report",
        "https://example.com/news/funding",
        "https://news.example/company-profile",
        "https://uk.linkedin.com/company/example/",
    ]
    ordered = pydantic_ai._ordered_fit_evidence_urls(urls)

    # Arena retains only three fit hints. Give it the specific sources first;
    # the independent company_linkedin field still supplies profile identity.
    assert ordered[:3] == urls[1:4]
    assert ordered[3:] == [urls[0], urls[4]]
    assert len(ordered) == len(urls)
    assert urls[0] == "https://www.linkedin.com/company/example/"
    assert json.loads(json.dumps(ordered)) == ordered


def _large_result(company: str, suffix: str) -> dict:
    tracking = "tracking-segment/" * 12
    return {
        "results": [
            {
                "company_name": f"{company} {index}",
                "domain": f"{company.lower()}{index}.example",
                "date": f"2026-08-{index + 10:02d}",
                "url": (
                    f"https://news.example/{company.lower()}/{index}/{tracking}{suffix}"
                ),
                "quote": f"{company} launched a verified product. " + (suffix * 300),
                "irrelevant_blob": suffix * 900,
            }
            for index in range(5)
        ]
    }


def _structured_profile(
    *,
    domain: str,
    company_name: str,
    linkedin_slug: str,
    employee_count: str,
    headquarters: str,
    financing_type: str,
    financing_type_normalized: str | None,
    profile_error: bool = False,
) -> dict:
    attributes = {
        "effective_date": "2026-06-22",
        "found_at": "2026-06-22T02:00:00+02:00",
        "categories": ["series", financing_type_normalized or "funding"],
        "financing_type": financing_type,
        "amount": "$130 million",
        "amount_normalized": 130_000_000,
    }
    if financing_type_normalized:
        attributes["financing_type_normalized"] = financing_type_normalized
    event = {"type": "financing_event", "attributes": attributes}
    errors = (
        [
            {
                "source": "linkedin_profile_evidence",
                "error": "profile fetch failed: RuntimeError",
            }
        ]
        if profile_error
        else []
    )
    profile = {
        "domain": domain,
        "company": {
            "normalized_domain": domain,
            "domain": domain,
            "company_name": company_name.lower(),
            "industry": "computer software",
            "location": headquarters.lower(),
            "linkedin_url": f"linkedin.com/company/{linkedin_slug}",
            "employee_count_estimate": 50,
            "year_founded": 2022,
            "updated_at": "2026-05-12 16:08:43.189 -0700",
        },
        "latest_financing_events": [
            {
                "source": "predictleads_company_financing_events",
                "data": {
                    "items": [event, event, event],
                    "returned_count": 3,
                    "available_count": 3,
                },
            }
        ],
        "errors": errors,
        "linkedin_structured_evidence": {
            "provider": "harvestapi_get_company",
            "linkedin_url": f"https://www.linkedin.com/company/{linkedin_slug}/",
            "website": f"https://{domain}/",
            "company_name": company_name,
            "employee_count": employee_count,
            "employee_count_source_field": "employeeCountRange",
            "headquarters": headquarters,
            "headquarters_source_field": (
                "locations[headquarter=true].parsed.text"
            ),
        },
    }
    if not profile_error:
        profile["linkedin_profile_evidence"] = {
            "url": f"https://linkedin.com/company/{linkedin_slug}",
            "title": f"{company_name} | LinkedIn",
        }
    return profile


def _history() -> list[messages.ModelMessage]:
    return [
        messages.ModelRequest.user_text_prompt("Find matching companies"),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "search_web", {"query": "Acme launch"}, tool_call_id="call-1"
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "search_web",
                    _large_result("Acme", "a"),
                    tool_call_id="call-1",
                )
            ]
        ),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "search_web", {"query": "Beta launch"}, tool_call_id="call-2"
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "search_web",
                    _large_result("Beta", "b"),
                    tool_call_id="call-2",
                )
            ]
        ),
    ]


def test_prior_tool_payload_is_bounded_but_latest_remains_full() -> None:
    original = _history()
    processed = pydantic_ai._process_history(_context(), original)

    old_return = processed[2].parts[0]
    latest_return = processed[4].parts[0]
    assert isinstance(old_return, messages.ToolReturnPart)
    assert isinstance(latest_return, messages.ToolReturnPart)
    assert len(pydantic_ai._json_bytes(old_return.content)) <= 1_200
    compact_json = json.dumps(old_return.content)
    assert "2026-08-10" in compact_json
    expected_url = original[2].parts[0].content["results"][0]["url"]
    assert expected_url in compact_json
    assert "Acme launched a verified product" in compact_json
    assert latest_return.content == original[4].parts[0].content


def test_prior_web_search_keeps_later_candidates_within_the_same_history_bound() -> None:
    original = _history()
    rows = [
        {
            "url": f"https://news.example/company-{index}/funding-announcement",
            "title": f"Company {index} completes new funding. " * 5,
            "snippet": f"Company {index} raised Series B funding. " * 6,
            "source": "search",
        }
        for index in range(10)
    ]
    original[2].parts[0].content = {"results": rows, "count": 10, "mode": "search"}

    processed = pydantic_ai._process_history(_context(), original)
    compacted = processed[2].parts[0].content

    assert len(pydantic_ai._json_bytes(compacted)) <= 1_200
    assert [row["url"] for row in compacted["results"]] == [row["url"] for row in rows]
    assert "Company 7" in compacted["results"][7]["snippet"]
    assert compacted["prior_result_truncated"] is True
    assert processed[4].parts[0].content == original[4].parts[0].content


def test_prior_web_search_with_oversized_urls_still_respects_history_bound() -> None:
    value = {
        "results": [
            {"url": "https://news.example/" + "long-path/" * 30 + str(index)}
            for index in range(10)
        ]
    }

    compacted = pydantic_ai._bounded_history_tool_result(value, tool_name="search_web")

    assert len(pydantic_ai._json_bytes(compacted)) <= 1_200


def test_latest_parallel_tool_batch_remains_full_while_prior_request_is_bounded() -> (
    None
):
    history = _history()[:3]
    fresh_results = [
        _large_result(company, suffix)
        for company, suffix in (
            ("Beta", "b"),
            ("Gamma", "c"),
            ("Delta", "d"),
        )
    ]
    history.extend(
        [
            messages.ModelResponse(
                parts=[
                    messages.ToolCallPart(
                        "search_web",
                        {"query": company},
                        tool_call_id=f"fresh-{index}",
                    )
                    for index, company in enumerate(("Beta", "Gamma", "Delta"))
                ]
            ),
            messages.ModelRequest(
                parts=[
                    messages.ToolReturnPart(
                        "search_web",
                        result,
                        tool_call_id=f"fresh-{index}",
                    )
                    for index, result in enumerate(fresh_results)
                ]
            ),
        ]
    )

    processed = pydantic_ai._process_history(_context(), history)

    assert len(pydantic_ai._json_bytes(processed[2].parts[0].content)) <= 1_200
    assert [part.content for part in processed[4].parts] == fresh_results


def test_prior_company_profile_keeps_fit_and_latest_financing_evidence() -> None:
    attributes = {
        "amount": "10000000",
        "amount_normalized": "$10M",
        "article_sentence": "Example raised Series B funding. " + ("context " * 80),
        "categories": ["funding", "venture_capital"],
        "category": "venture",
        "confidence": 0.99,
        "event": "funding",
        "financing_type": "Series B",
        "financing_type_normalized": "series_b",
        "first_seen_at": "2026-06-23T11:00:00Z",
        "found_at": "2026-06-23T11:00:00Z",
        "summary": "Example completed its latest financing. " + ("detail " * 80),
        "title": "Series B",
    }
    event = {
        "type": "financing_event",
        "attributes": attributes,
        "related": {
            "article": {
                "title": "Example raises Series B",
                "url": "https://example.com/news/series-b",
                "published_at": "2026-06-23",
                "author": "Reporter",
            }
        },
    }
    profile = {
        "domain": "example.com",
        "company": {
            "normalized_domain": "example.com",
            "domain": "example.com",
            "company_name": "Example",
            "industry": "Hardware",
            "location": "Austin, Texas, United States",
            "linkedin_url": "https://linkedin.com/company/example",
            "employee_count": "201-500",
            "year_founded": 2015,
            "updated_at": "2026-09-01T00:00:00Z",
        },
        "latest_financing_events": [
            {
                "source": "predictleads_company_financing_events",
                "data": {
                    "items": [event, event, event],
                    "returned_count": 3,
                    "available_count": 4,
                },
            }
        ],
        "linkedin_profile_evidence": {
            "url": "https://www.linkedin.com/company/example",
            "title": "Example | LinkedIn",
            "employee_count": "201-500",
            "quote": "Company size\n201-500 employees",
        },
        "errors": [],
    }
    history = [
        messages.ModelRequest.user_text_prompt("Verify Example"),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "get_company_profile",
                    {"domain": "example.com"},
                    tool_call_id="profile-1",
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "get_company_profile",
                    profile,
                    tool_call_id="profile-1",
                )
            ]
        ),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "search_web",
                    {"query": "another company"},
                    tool_call_id="search-2",
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "search_web",
                    _large_result("Beta", "b"),
                    tool_call_id="search-2",
                )
            ]
        ),
    ]

    processed = pydantic_ai._process_history(_context(), history)
    compact_profile = processed[2].parts[0].content

    assert len(pydantic_ai._json_bytes(profile)) > 1_200
    assert len(pydantic_ai._json_bytes(compact_profile)) <= 1_200
    assert compact_profile["company"] == profile["company"]
    financing = compact_profile["latest_financing_events"][0]
    assert financing["source"] == "predictleads_company_financing_events"
    latest = financing["data"]["items"][0]
    assert latest["attributes"]["financing_type"] == "Series B"
    assert latest["attributes"]["found_at"] == "2026-06-23T11:00:00Z"
    assert latest["related"]["article"]["url"] == (
        "https://example.com/news/series-b"
    )
    linkedin = compact_profile["linkedin_profile_evidence"]
    assert linkedin["url"] == "https://www.linkedin.com/company/example"
    assert linkedin["employee_count"] == "201-500"
    assert linkedin["quote"] == "Company size\n201-500 employees"


@pytest.mark.parametrize(
    (
        "domain",
        "company_name",
        "linkedin_slug",
        "employee_count",
        "headquarters",
        "financing_type",
        "financing_type_normalized",
        "profile_error",
    ),
    [
        (
            "sandstone.com",
            "Sandstone",
            "sandstone-ai",
            "11-50",
            "New York City, NY, United States",
            "Series A",
            "series_a",
            False,
        ),
        (
            "harborhealth.com",
            "Harbor Health",
            "harbor-health-team",
            "201-500",
            "Austin, TX, United States",
            "funding",
            None,
            False,
        ),
        (
            "vanna.health",
            "Vanna Health",
            "vannahealth",
            "51-200",
            "San Francisco, CA, United States",
            "Series B",
            "series_b",
            True,
        ),
    ],
)
def test_large_structured_company_profile_keeps_exact_verified_evidence(
    domain: str,
    company_name: str,
    linkedin_slug: str,
    employee_count: str,
    headquarters: str,
    financing_type: str,
    financing_type_normalized: str | None,
    profile_error: bool,
) -> None:
    profile = _structured_profile(
        domain=domain,
        company_name=company_name,
        linkedin_slug=linkedin_slug,
        employee_count=employee_count,
        headquarters=headquarters,
        financing_type=financing_type,
        financing_type_normalized=financing_type_normalized,
        profile_error=profile_error,
    )

    compacted = pydantic_ai._bounded_history_tool_result(profile)

    assert len(pydantic_ai._json_bytes(profile)) > 1_200
    assert len(pydantic_ai._json_bytes(compacted)) <= 1_200
    assert "json_preview" not in compacted
    assert (
        compacted["linkedin_structured_evidence"]
        == profile["linkedin_structured_evidence"]
    )
    assert compacted.get("errors", []) == profile["errors"]
    financing = compacted["latest_financing_events"][0]["data"]["items"][0]
    assert financing["attributes"]["financing_type"] == financing_type


def test_structured_profile_with_large_urls_keeps_exact_proof_and_cost_context() -> (
    None
):
    long_segment = "identity-context-" * 12
    linkedin_url = f"https://www.linkedin.com/company/example/{long_segment}"
    website = f"https://example.com/profile/{long_segment}"
    structured = {
        "provider": "harvestapi_get_company",
        "linkedin_url": linkedin_url,
        "website": website,
        "company_name": "Example",
        "employee_count": "51-200",
        "employee_count_source_field": "employeeCountRange",
        "headquarters": "Austin, TX, United States",
        "headquarters_source_field": "locations[headquarter=true].parsed.text",
    }
    cost_context = {"provider": "deepline", "actual_microusd": 3_000}
    profile = {
        "domain": "example.com",
        "company": {
            "company_name": "Example",
            "irrelevant_blob": "x" * 2_000,
        },
        "latest_financing_events": [],
        "linkedin_structured_evidence": structured,
        "cost_context": cost_context,
    }

    compacted = pydantic_ai._bounded_history_tool_result(profile)

    assert len(pydantic_ai._json_bytes(compacted)) <= 1_200
    assert compacted["linkedin_structured_evidence"] == structured
    assert compacted["cost_context"] == cost_context
    assert compacted["linkedin_structured_evidence"]["linkedin_url"] == linkedin_url
    assert compacted["linkedin_structured_evidence"]["website"] == website


def test_large_failure_envelope_stays_bounded_without_inventing_profile_proof() -> (
    None
):
    failure = {
        "domain": "failed.example",
        "company": {},
        "latest_financing_events": [],
        "errors": [
            {
                "source": "free_simple_company_search",
                "error": "profile lookup failed: RuntimeError " + ("detail " * 500),
            }
        ],
        "cost_context": {"provider": "deepline", "actual_microusd": 0},
    }

    compacted = pydantic_ai._bounded_history_tool_result(failure)
    serialized = json.dumps(compacted)

    assert len(pydantic_ai._json_bytes(compacted)) <= 1_200
    assert "profile lookup failed: RuntimeError" in serialized
    assert "cost_context" in serialized
    assert "linkedin_structured_evidence" not in serialized
    assert "employee_count" not in serialized
    assert "headquarters" not in serialized


def test_prior_fetch_page_keeps_full_quote_and_url() -> None:
    exact_url = "https://example.com/news/verified-launch?source=company"
    exact_quote = "The company launched its verified platform on August 20, 2026."
    page = {
        "url": exact_url,
        "text": ("Relevant page context. " * 180) + exact_quote,
    }
    history = [
        messages.ModelRequest.user_text_prompt("Verify the launch"),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "fetch_page", {"url": exact_url}, tool_call_id="fetch-1"
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "fetch_page", page, tool_call_id="fetch-1"
                )
            ]
        ),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "search_web", {"query": "another company"}, tool_call_id="call-2"
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "search_web",
                    _large_result("Beta", "b"),
                    tool_call_id="call-2",
                )
            ]
        ),
    ]

    processed = pydantic_ai._process_history(_context(), history)
    fetch_return = processed[2].parts[0]

    assert isinstance(fetch_return, messages.ToolReturnPart)
    assert fetch_return.content == page
    assert fetch_return.content["url"] == exact_url
    assert exact_quote in fetch_return.content["text"]


def test_budget_reserve_warns_once_and_hides_only_research_tools(
    monkeypatch,
) -> None:
    context = _context(input_tokens=82_000, requests=12, tool_calls=12)
    processed = pydantic_ai._process_history(context, _history())
    processed_again = pydantic_ai._process_history(context, processed)

    warnings = [
        part.content
        for message in processed_again
        if isinstance(message, messages.ModelRequest)
        for part in message.parts
        if isinstance(part, messages.UserPromptPart)
        and isinstance(part.content, str)
        and pydantic_ai._FINALIZE_MARKER in part.content
    ]
    assert len(warnings) == 1
    assert "do not invent" in warnings[0].lower()
    tool_definitions = [SimpleNamespace(name="search_web")]
    assert pydantic_ai._prepare_research_tools(context, tool_definitions) == []

    below_reserve = _context(input_tokens=81_999, requests=21, tool_calls=23)
    assert (
        pydantic_ai._prepare_research_tools(below_reserve, tool_definitions)
        == tool_definitions
    )

    monkeypatch.setattr(pydantic_ai.time, "monotonic", lambda: 199.999)
    assert (
        pydantic_ai._prepare_research_tools(
            below_reserve, tool_definitions, finalize_at=200.0
        )
        == tool_definitions
    )
    before_cutoff = pydantic_ai._process_history(
        below_reserve, _history(), finalize_at=200.0
    )
    assert not any(
        isinstance(part, messages.UserPromptPart)
        and isinstance(part.content, str)
        and pydantic_ai._FINALIZE_MARKER in part.content
        for message in before_cutoff
        if isinstance(message, messages.ModelRequest)
        for part in message.parts
    )

    monkeypatch.setattr(pydantic_ai.time, "monotonic", lambda: 200.0)
    assert (
        pydantic_ai._prepare_research_tools(
            below_reserve, tool_definitions, finalize_at=200.0
        )
        == []
    )
    at_cutoff = pydantic_ai._process_history(
        below_reserve, _history(), finalize_at=200.0
    )
    at_cutoff_again = pydantic_ai._process_history(
        below_reserve, at_cutoff, finalize_at=200.0
    )
    time_warnings = [
        part.content
        for message in at_cutoff_again
        if isinstance(message, messages.ModelRequest)
        for part in message.parts
        if isinstance(part, messages.UserPromptPart)
        and isinstance(part.content, str)
        and pydantic_ai._FINALIZE_MARKER in part.content
    ]
    assert len(time_warnings) == 1

    # Without an Arena deadline, elapsed time does not alter the shared limits.
    assert (
        pydantic_ai._prepare_research_tools(below_reserve, tool_definitions)
        == tool_definitions
    )


def test_arena_per_request_output_cap_is_not_the_cumulative_run_limit() -> None:
    limits = pydantic_ai._run_usage_limits()

    assert pydantic_ai._ARENA_REQUEST_OUTPUT_TOKENS == 4_096
    assert limits.output_tokens_limit == 15_000
    assert limits.input_tokens_limit == 120_000
    assert limits.request_limit == 60
    assert limits.tool_calls_limit == 60
    assert limits.cost_limit == Decimal("4")
    limits.check_tokens(
        RunUsage(
            input_tokens=89_296,
            output_tokens=4_384,
            requests=16,
            tool_calls=15,
        )
    )


def test_arena_can_revisit_evidence_within_existing_cost_and_call_limits(monkeypatch) -> None:
    context = _context(input_tokens=150_000, requests=11, tool_calls=38)
    definitions = [SimpleNamespace(name="fetch_page")]
    prepared = pydantic_ai._prepare_research_tools(
        context, definitions, input_token_limit=None
    )
    assert prepared == definitions
    processed = pydantic_ai._process_history(
        context, _history(), input_token_limit=None
    )
    assert not any(
        isinstance(part, messages.UserPromptPart)
        and pydantic_ai._FINALIZE_MARKER in str(part.content)
        for message in processed
        if isinstance(message, messages.ModelRequest)
        for part in message.parts
    )
    limits = pydantic_ai._run_usage_limits(arena_mode=True)
    limits.check_tokens(RunUsage(input_tokens=150_000, requests=11, tool_calls=38))
    assert limits.input_tokens_limit is None
    assert limits.cost_limit == Decimal("4")
    assert limits.request_limit == 60
    assert limits.tool_calls_limit is None
    assert limits.output_tokens_limit == 15_000
    # The Arena semantic budget limits provider work. A research batch may
    # cross 60 logical tool calls before the required output-tool call.
    limits.check_before_tool_call(RunUsage(tool_calls=61))
    with pytest.raises(UsageLimitExceeded, match="tool_calls_limit of 60"):
        pydantic_ai._run_usage_limits().check_before_tool_call(
            RunUsage(tool_calls=61)
        )
    assert pydantic_ai._prepare_research_tools(
        context, definitions, input_token_limit=None, force_finalize=True
    ) == []
    assert pydantic_ai._prepare_research_tools(
        _context(requests=45), definitions, input_token_limit=None,
        tool_calls_limit=None,
    ) == []
    assert pydantic_ai._prepare_research_tools(
        _context(tool_calls=44), definitions, input_token_limit=None,
        tool_calls_limit=None,
    ) == definitions
    monkeypatch.setattr(pydantic_ai.time, "monotonic", lambda: 165.0)
    assert pydantic_ai._prepare_research_tools(
        context, definitions, input_token_limit=None, finalize_at=165.0
    ) == []


def _submitted_company(index: int = 0) -> dict:
    return {
        "company_name": f"Example {index}",
        "company_website": f"https://example-{index}.com/",
        "company_linkedin": f"https://www.linkedin.com/company/example-{index}/",
        "industry": "Software",
        "employee_count": "51-200",
        "company_stage": "Series B",
        "country": "US",
        "state": "CA",
        "fit_summary": "Verified company fit.",
        "fit_evidence_urls": [f"https://example-{index}.com/about"],
        "intent_signals": [
            {
                "matched_icp_signal": 0,
                "description": "The company launched a verified product.",
                "date": "2026-09-01",
                "why_now": "The launch creates a current sales opportunity.",
                "url": f"https://example-{index}.com/news/launch",
                "snippet": "The company launched the product on September 1, 2026.",
            }
        ],
    }


def test_contact_completion_retry_headroom_preserves_final_request_and_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(pydantic_ai.time, "monotonic", lambda: 50.0)

    def allowed(**overrides):
        values = {
            "context": _context(requests=43, output_tokens=6_808),
            "finalize_at": 100.0,
            "force_finalize": False,
            "provider_calls": 54,
            "research_maximum": 56,
            "deepline_calls": 24,
            "deepline_limit": 26,
        }
        values.update(overrides)
        return pydantic_ai._completion_retry_has_headroom(**values)

    assert allowed()
    assert not allowed(context=_context(requests=44, output_tokens=6_808))
    assert not allowed(context=_context(requests=43, output_tokens=6_809))
    assert not allowed(context=_context(requests=43, output_tokens=6_808, retry=2))
    assert not allowed(provider_calls=55)
    assert not allowed(deepline_calls=25)
    assert not allowed(force_finalize=True)
    assert not allowed(finalize_at=94.999)

    assert not pydantic_ai._completion_retry_needs_finalization(
        _context(requests=43, output_tokens=6_808)
    )
    assert pydantic_ai._completion_retry_needs_finalization(
        _context(requests=44, output_tokens=6_808)
    )
    assert pydantic_ai._completion_retry_needs_finalization(
        _context(requests=43, output_tokens=6_809)
    )


def test_complete_cached_contact_rows_do_not_retry_or_read_discarded_misses() -> None:
    output = CompaniesResult.model_validate(
        {"companies": [_submitted_company(index) for index in range(5)]}
    )
    inspected = []

    def status(company):
        inspected.append(company["company_name"])
        return "found"

    result = pydantic_ai._validate_contact_completion(
        _context(),
        output,
        max_companies=5,
        contact_status=status,
        has_headroom=lambda _context: True,
        retry_state={"attempted": False, "fallback": None},
    )

    assert result is output
    assert inspected == [f"Example {index}" for index in range(5)]


def test_missing_cached_contact_retries_once_and_repeated_output_is_accepted() -> None:
    output = CompaniesResult.model_validate(
        {"companies": [_submitted_company(index) for index in range(5)]}
    )
    state = {"attempted": False, "fallback": None}

    def status(company):
        return "not_found" if company["company_name"] == "Example 4" else "found"

    with pytest.raises(ModelRetry, match="4 found, 1 not found"):
        pydantic_ai._validate_contact_completion(
            _context(),
            output,
            max_companies=5,
            contact_status=status,
            has_headroom=lambda _context: True,
            retry_state=state,
        )
    assert state == {"attempted": True, "fallback": output}
    assert (
        pydantic_ai._validate_contact_completion(
            _context(retry=1),
            output,
            max_companies=5,
            contact_status=status,
            has_headroom=lambda _context: True,
            retry_state=state,
        )
        is output
    )


@pytest.mark.parametrize("company_count", [0, 1])
def test_sdk_runs_exactly_one_completion_retry_for_repeated_partial_output(
    company_count,
) -> None:
    output = {"companies": [_submitted_company(0)] if company_count else []}
    agent = Agent(
        TestModel(custom_output_args=output),
        output_type=ToolOutput(CompaniesResult, name="submit_companies"),
        retries={"output": 2},
    )
    state = {"attempted": False, "fallback": None}
    validation_retries = []

    @agent.output_validator
    def validate(context, result):
        validation_retries.append(context.retry)
        return pydantic_ai._validate_contact_completion(
            context,
            result,
            max_companies=5,
            contact_status=lambda _company: "found",
            has_headroom=lambda _context: True,
            retry_state=state,
        )

    result = agent.run_sync("Find qualified companies")
    retry_prompts = [
        part
        for message in result.all_messages()
        if isinstance(message, messages.ModelRequest)
        for part in message.parts
        if isinstance(part, messages.RetryPromptPart)
        and pydantic_ai._COMPLETION_RETRY_MARKER in str(part.content)
    ]

    assert result.usage.requests == 2
    assert validation_retries == [0, 1]
    assert len(retry_prompts) == 1
    assert result.output == CompaniesResult.model_validate(output)


def test_sdk_keeps_schema_repair_before_the_one_completion_retry() -> None:
    calls = []
    partial = {"companies": [_submitted_company()]}

    def respond(_messages, info):
        calls.append(len(calls) + 1)
        arguments = {"companies": "invalid"} if len(calls) == 1 else partial
        return messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    info.output_tools[0].name,
                    arguments,
                    tool_call_id=f"output-{len(calls)}",
                )
            ]
        )

    agent = Agent(
        FunctionModel(respond),
        output_type=ToolOutput(CompaniesResult, name="submit_companies"),
        retries={"output": 2},
    )
    state = {"attempted": False, "fallback": None}
    validation_retries = []

    @agent.output_validator
    def validate(context, result):
        validation_retries.append(context.retry)
        return pydantic_ai._validate_contact_completion(
            context,
            result,
            max_companies=5,
            contact_status=lambda _company: "found",
            has_headroom=lambda _context: True,
            retry_state=state,
        )

    result = agent.run_sync("Find qualified companies")

    assert calls == [1, 2, 3]
    assert validation_retries == [1, 2]
    assert result.usage.requests == 3
    assert result.output == CompaniesResult.model_validate(partial)


def test_incomplete_output_is_kept_when_retry_headroom_is_exhausted() -> None:
    output = CompaniesResult.model_validate({"companies": [_submitted_company()]})
    state = {"attempted": False, "fallback": None}

    result = pydantic_ai._validate_contact_completion(
        _context(requests=44, output_tokens=6_809),
        output,
        max_companies=5,
        contact_status=lambda _company: None,
        has_headroom=lambda _context: False,
        retry_state=state,
    )

    assert result is output
    assert state == {"attempted": False, "fallback": None}


def test_research_batch_cannot_spend_reserved_contact_calls() -> None:
    calls = []
    client = SimpleNamespace(call=lambda name, arguments: calls.append(name) or {"ok": True})
    budget = pydantic_ai._ToolBudget(client, maximum=5, contact_reserve=2)
    for _ in range(3):
        assert budget.call("search_web", {}) == {"ok": True}
    assert budget.call("search_web", {})["ok"] is False
    assert budget.calls == 3
    assert budget.call("harvestapi_search_leads", {}) == {"ok": True}
    assert budget.call("harvestapi_get_profile", {}) == {"ok": True}
    assert budget.call("harvestapi_get_profile", {})["ok"] is False
    assert budget.call("submit_companies", {"companies": []}) == {"ok": True}
    assert budget.calls == 5
    assert calls == ["search_web"] * 3 + [
        "harvestapi_search_leads", "harvestapi_get_profile", "submit_companies"
    ]


def test_arena_contact_call_reserve_is_bounded_without_changing_standalone() -> None:
    assert pydantic_ai._contact_call_reserve(
        1, contact_enabled=True, arena_mode=True
    ) == 2
    assert pydantic_ai._contact_call_reserve(
        5, contact_enabled=True, arena_mode=True
    ) == 4
    assert pydantic_ai._contact_call_reserve(
        5, contact_enabled=True, arena_mode=False
    ) == 10
    assert pydantic_ai._contact_call_reserve(
        5, contact_enabled=False, arena_mode=True
    ) == 0


def test_contact_deadline_bounds_call_and_preserves_company_when_time_runs_out() -> (
    None
):
    now = [90.0]
    calls: list[tuple[str, float]] = []
    client = SimpleNamespace(timeout=90.0)

    def provider(name: str, _arguments: dict) -> object:
        calls.append((name, client.timeout))
        now[0] = 99.5
        return {
            "result": {
                "data": {
                    "elements": [
                        {"linkedinUrl": "https://www.linkedin.com/in/ada-lovelace/"}
                    ]
                }
            }
        }

    deadline_call = pydantic_ai._DeadlineProviderCall(
        provider,
        client,
        100.0,
        clock=lambda: now[0],
    )
    company = {
        "company_name": "Acme",
        "company_website": "https://acme.com/",
        "company_linkedin": "https://www.linkedin.com/company/acme/",
    }
    icp = {
        "contact_policy": "contacts_v1",
        "target_roles": ["Vice President of Sales"],
        "target_seniority": "VP+",
        "contact_geography": {"countries": ["United States"]},
    }

    assert enrich_contacts(icp, [company], deadline_call) == [company]
    assert calls == [("harvestapi_search_leads", 8.0)]
    assert client.timeout == 90.0
    with pytest.raises(RuntimeError, match="deadline"):
        deadline_call("harvestapi_search_leads", {})


def test_contact_reserve_keeps_final_output_window_inside_total_deadline() -> None:
    contact_reserve = pydantic_ai._contact_time_reserve(285.0, arena_mode=True)

    assert contact_reserve == 45.0
    assert (
        285.0 - contact_reserve - pydantic_ai._ARENA_FINALIZE_RESERVE_SECONDS == 195.0
    )

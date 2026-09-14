from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.usage import RunUsage, UsageLimitExceeded

from experiments.harness_bakeoff.contacts import (
    ContactLookup,
    _company_name,
    _fallback_search_request,
    _search_request,
    enrich_contacts,
)
from experiments.harness_bakeoff.models import CompaniesResult, validate_companies


def _company() -> dict:
    return {
        "company_name": "Acme",
        "company_website": "https://acme.com/",
        "company_linkedin": "https://www.linkedin.com/company/acme/",
        "industry": "Software",
        "employee_count": "51-200",
        "company_stage": "Series A",
        "country": "United States",
        "state": "California",
        "fit_summary": "Acme matches the requested company profile.",
        "fit_evidence_urls": ["https://acme.com/about"],
        "intent_signals": [
            {
                "matched_icp_signal": 0,
                "description": "Acme announced a new product.",
                "date": "2026-09-01",
                "why_now": "The launch creates a current sales opportunity.",
                "url": "https://acme.com/news/launch",
                "snippet": "Acme announced the product launch.",
            }
        ],
    }


def _icp(**updates: object) -> dict:
    value = {
        "contact_policy": "contacts_v1",
        "target_roles": ["Vice President of Sales"],
        "target_seniority": "VP+",
        "contact_geography": {
            "countries": ["United States"],
            "regions": ["California"],
            "cities": ["San Francisco"],
        },
    }
    value.update(updates)
    return value


def _profile(**updates: object) -> dict:
    value = {
        "id": "profile-1",
        "publicIdentifier": "ada-lovelace",
        "linkedinUrl": "https://www.linkedin.com/in/ada-lovelace/",
        "firstName": "Ada",
        "lastName": "Lovelace",
        "location": {
            "countryCode": "US",
            "parsed": {
                "countryFull": "United States",
                "state": "California",
                "city": "San Francisco",
            },
        },
        "workEmail": "ada@acme.com",
        "currentPosition": [
            {
                "title": "VP Sales",
                "companyName": "Acme, Inc.",
                "companyDomain": "acme.com",
                "companyLinkedinUrl": "https://www.linkedin.com/company/acme/",
                "isCurrent": True,
            }
        ],
    }
    value.update(updates)
    return value


def test_search_request_normalizes_legal_suffix_only_without_company_linkedin() -> (
    None
):
    company = {
        **_company(),
        "company_name": "Microsoft Corporation",
        "company_linkedin": "",
    }

    request = _search_request(_icp(), company)

    assert request == {
        "currentJobTitles": "Vice President of Sales",
        "page": 1,
        "search": "microsoft",
        "locations": "San Francisco",
    }


def test_search_request_keeps_linkedin_constraint_and_degenerate_name_fallback() -> (
    None
):
    linked = _search_request(_icp(), _company())
    degenerate = _search_request(
        _icp(), {**_company(), "company_name": "Corporation", "company_linkedin": ""}
    )

    assert linked["currentCompanies"] == "https://www.linkedin.com/company/acme/"
    assert "search" not in linked
    assert degenerate["search"] == "Corporation"


@pytest.mark.parametrize(
    ("regions", "expected"),
    [
        (["US-WA", "US-CA", "US-OR"], "Washington,California,Oregon"),
        (["US-NY", "US-NJ", "US-CT"], "New York,New Jersey,Connecticut"),
        (["US-DC"], "District of Columbia"),
        (["US-PR"], "Puerto Rico"),
        (["Washington", "CA"], "Washington,California"),
    ],
)
def test_search_request_uses_harvestapi_names_for_us_regions(
    regions: list[str], expected: str
) -> None:
    icp = _icp(
        contact_geography={"countries": ["US"], "regions": regions, "cities": []}
    )

    assert _search_request(icp, _company())["locations"] == expected


def test_search_request_does_not_rewrite_ambiguous_non_us_region_code() -> None:
    icp = _icp(
        contact_geography={"countries": ["India"], "regions": ["IN"], "cities": []}
    )

    assert _search_request(icp, _company())["locations"] == "IN"


def test_search_request_does_not_rewrite_ambiguous_multi_country_region() -> None:
    icp = _icp(
        contact_geography={
            "countries": ["United States", "India"],
            "regions": ["IN"],
            "cities": [],
        }
    )

    assert _search_request(icp, _company())["locations"] == "IN"


def test_search_request_keeps_explicit_us_region_without_country_constraint() -> None:
    icp = _icp(
        contact_geography={"countries": [], "regions": ["US-IN"], "cities": []}
    )

    assert _search_request(icp, _company())["locations"] == "Indiana"


@pytest.mark.parametrize(
    ("country", "expected"),
    [
        ("SG", "Singapore"),
        ("sg", "Singapore"),
        ("Singapore", "Singapore"),
        ("IN", "India"),
        ("US", "US"),
        ("GB", "GB"),
        ("ZZ", "ZZ"),
    ],
)
def test_search_request_uses_only_unambiguous_existing_country_names(
    country: str, expected: str
) -> None:
    icp = _icp(
        contact_geography={"countries": [country], "regions": [], "cities": []}
    )

    assert _search_request(icp, _company())["locations"] == expected


def test_functional_fallback_preserves_normalized_country_and_other_parameters() -> None:
    icp = _icp(
        contact_geography={"countries": ["SG"], "regions": [], "cities": []}
    )

    exact = _search_request(icp, _company())
    fallback = _fallback_search_request(icp, _company())

    assert fallback is not None
    assert fallback["locations"] == "Singapore"
    assert {
        key: value for key, value in fallback.items() if key != "currentJobTitles"
    } == {key: value for key, value in exact.items() if key != "currentJobTitles"}


class ScriptedProvider:
    def __init__(self, profile: dict | None = None) -> None:
        self.profile = profile or _profile()
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool: str, payload: dict) -> object:
        self.calls.append((tool, deepcopy(payload)))
        if tool == "harvestapi_search_leads":
            return {
                "result": {
                    "data": {
                        "elements": [
                            {
                                "id": "profile-1",
                                "publicIdentifier": "ada-lovelace",
                                "linkedinUrl": "https://www.linkedin.com/in/ACoOpaqueToken/",
                                "currentPositions": _profile()["currentPosition"],
                            }
                        ]
                    }
                }
            }
        if tool == "harvestapi_get_profile":
            return {
                "status": "completed",
                "result": {"data": {"element": self.profile}},
            }
        raise AssertionError(f"unexpected provider tool: {tool}")


class RankedProvider(ScriptedProvider):
    def __call__(self, tool: str, payload: dict) -> object:
        self.calls.append((tool, deepcopy(payload)))
        if tool == "harvestapi_search_leads":
            return {
                "result": {
                    "data": {
                        "elements": [
                            {
                                "id": "wrong-profile",
                                "linkedinUrl": "https://www.linkedin.com/in/wrong-profile/",
                                "currentPositions": [
                                    {
                                        "position": "Software Engineer",
                                        "companyName": "Other Company",
                                        "companyLinkedinUrl": "https://www.linkedin.com/company/other/",
                                    }
                                ],
                            },
                            {
                                "id": "profile-1",
                                "linkedinUrl": "https://www.linkedin.com/in/ada-lovelace/",
                                "currentPositions": [
                                    {
                                        **_profile()["currentPosition"][0],
                                        "companyId": "12345",
                                        "companyLinkedinUrl": "https://www.linkedin.com/company/12345/",
                                    }
                                ],
                            },
                        ]
                    }
                }
            }
        if tool == "harvestapi_get_profile":
            assert payload["url"].endswith("/ada-lovelace/")
            return {"result": {"data": {"element": self.profile}}}
        raise AssertionError(f"unexpected provider tool: {tool}")


def test_contact_lookup_reuses_checked_identity_and_isolates_returned_data():
    provider = ScriptedProvider()
    lookup = ContactLookup(_icp())
    first = lookup.find(_company(), provider)
    assert first is not None
    assert lookup.status(_company()) == "found"
    first["email"] = "changed@example.com"
    company = {**_company(), "company_name": "Acme, Inc."}
    rows = lookup.enrich([company, company], provider)
    assert rows[0]["contact"]["email"] == "ada@acme.com"
    assert len(provider.calls) == 2
    validated = validate_companies(rows[:1], 5, allow_contacts=True)
    assert validate_companies(json.loads(json.dumps(validated)), 5, allow_contacts=True) == validated
    rows[0]["contact"]["role"] = "changed"
    assert rows[1]["contact"]["role"] == "VP Sales"


@pytest.mark.parametrize(
    "updates",
    [
        {"company_website": "https://other.com/"},
        {"company_linkedin": "https://www.linkedin.com/company/other/"},
    ],
)
def test_contact_lookup_does_not_reuse_a_contact_for_changed_company_identity(updates):
    provider = ScriptedProvider()
    lookup = ContactLookup(_icp())
    assert lookup.find(_company(), provider)
    changed = {**_company(), **updates}
    assert lookup.find(changed, provider) is None
    calls = len(provider.calls)
    assert calls > 2
    assert lookup.find(changed, provider) is None
    assert len(provider.calls) == calls


def test_contact_lookup_suppresses_research_misses_but_retries_once_at_submission():
    provider = ScriptedProvider(_profile(workEmail=""))
    lookup = ContactLookup(_icp())
    assert lookup.find(_company(), provider) is None
    assert lookup.status(_company()) == "not_found"
    assert lookup.find(_company(), provider) is None
    assert len(provider.calls) == 2
    assert lookup.enrich([_company(), _company()], provider) == [_company(), _company()]
    assert len(provider.calls) == 4
    provider.profile = _profile()
    assert ContactLookup(_icp()).find(_company(), provider)
    assert len(provider.calls) == 6


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("provider timeout"),
        {
            "ok": False,
            "error": "budget_refused",
            "data": {"elements": [_profile()]},
        },
    ],
)
def test_contact_lookup_recovers_transient_early_failure_at_submission(failure):
    lookup = ContactLookup(_icp())

    def fail(tool, payload):
        if isinstance(failure, BaseException):
            raise failure
        return deepcopy(failure)

    assert lookup.find(_company(), fail) is None
    assert lookup.status(_company()) == "unavailable"
    provider = ScriptedProvider()
    rows = lookup.enrich([_company()], provider)
    assert rows[0]["contact"]["email"] == "ada@acme.com"
    assert lookup.status(_company()) == "found"
    assert len(provider.calls) == 2


def _semantic_role_fixture(
    *,
    search_title: str = "VP of Manufacturing",
    profile_title: str | None = None,
) -> tuple[dict, dict, dict]:
    company = {
        **_company(),
        "company_name": "Allen Control Systems",
        "company_website": "https://allencontrolsystems.com/",
        "company_linkedin": "https://www.linkedin.com/company/allen-control-systems/",
        "state": "Texas",
    }
    position = {
        "title": search_title,
        "companyName": "Allen Control Systems",
        "companyDomain": "allencontrolsystems.com",
        "companyLinkedinUrl": (
            "https://www.linkedin.com/company/allen-control-systems/"
        ),
        "isCurrent": True,
    }
    profile = _profile(
        id="allen-profile-1",
        publicIdentifier="test-allen-contact",
        linkedinUrl="https://www.linkedin.com/in/test-allen-contact/",
        firstName="Test",
        lastName="Contact",
        workEmail="test.contact@example.com",
        location={
            "countryCode": "US",
            "parsed": {
                "countryFull": "United States",
                "state": "Texas",
                "city": "Austin",
            },
        },
        currentPosition=[
            {
                **position,
                "title": profile_title if profile_title is not None else search_title,
            }
        ],
    )
    return company, position, profile


def _semantic_role_icp() -> dict:
    return _icp(
        target_roles=["VP Hardware", "VP Operations", "Head of Supply Chain"],
        target_seniority="VP+",
        contact_geography={
            "countries": ["United States"],
            "regions": ["US-TX", "US-FL", "US-GA", "US-NC"],
            "cities": [],
        },
    )


class SemanticRoleProvider:
    def __init__(
        self,
        profile: dict,
        positions: list[dict],
        *,
        elements: list[dict] | None = None,
        transient_profile_failures: int = 0,
    ) -> None:
        self.profile = profile
        self.elements = elements or [
            {
                "linkedinUrl": profile["linkedinUrl"],
                "currentPositions": positions,
            }
        ]
        self.transient_profile_failures = transient_profile_failures
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool: str, payload: dict) -> object:
        self.calls.append((tool, deepcopy(payload)))
        if tool == "harvestapi_search_leads":
            return {"result": {"data": {"elements": self.elements}}}
        assert tool == "harvestapi_get_profile"
        if self.transient_profile_failures:
            self.transient_profile_failures -= 1
            raise TimeoutError("temporary provider failure")
        return {"result": {"data": {"element": self.profile}}}


def test_arena_role_handoff_replays_allen_without_relaxing_profile_checks() -> None:
    company, position, profile = _semantic_role_fixture()
    provider = SemanticRoleProvider(profile, [position])

    lookup = ContactLookup(_semantic_role_icp(), allow_role_selection=True)

    assert lookup.find(company, provider) is None
    assert lookup.status(company) == "role_selection_required"
    assert lookup.role_options(company) == ["VP of Manufacturing"]
    assert [tool for tool, _payload in provider.calls] == ["harvestapi_search_leads"]

    with pytest.raises(ValueError, match="exactly match an offered role"):
        lookup.find(company, provider, selected_observed_role="VP Operations")
    assert [tool for tool, _payload in provider.calls] == ["harvestapi_search_leads"]

    contact = lookup.find(
        company,
        provider,
        selected_observed_role="VP of Manufacturing",
    )
    assert contact is not None
    assert contact["role"] == "VP of Manufacturing"
    assert contact["location"] == {
        "country": "US",
        "region": "Texas",
        "city": "Austin",
    }
    assert contact["email_source"] == {
        "provider": "harvestapi",
        "tool": "harvestapi_get_profile",
        "record_id": "allen-profile-1",
    }
    assert lookup.status(company) == "found"
    assert lookup.role_options(company) == []
    assert [tool for tool, _payload in provider.calls] == [
        "harvestapi_search_leads",
        "harvestapi_get_profile",
    ]


def test_role_handoff_options_are_bounded_and_exclude_wrong_identity_or_seniority() -> None:
    company, base_position, profile = _semantic_role_fixture()
    titles = [
        "VP Manufacturing",
        "VP Manufacturing",
        "VP Logistics",
        "VP Supply Chain",
        "VP Production",
    ]
    elements = []
    for index, title in enumerate(titles):
        position = {**base_position, "title": title}
        elements.append(
            {
                "linkedinUrl": f"https://www.linkedin.com/in/candidate-{index}/",
                "currentPositions": [position],
            }
        )
    elements.extend(
        [
            {
                "linkedinUrl": "https://www.linkedin.com/in/wrong-company/",
                "currentPositions": [
                    {
                        **base_position,
                        "title": "VP Other Function",
                        "companyName": "Other Company",
                    }
                ],
            },
            {
                "linkedinUrl": "https://www.linkedin.com/in/wrong-seniority/",
                "currentPositions": [
                    {**base_position, "title": "Director Manufacturing"}
                ],
            },
        ]
    )
    provider = SemanticRoleProvider(profile, [base_position], elements=elements)

    lookup = ContactLookup(_semantic_role_icp(), allow_role_selection=True)

    assert lookup.find(company, provider) is None
    assert lookup.role_options(company) == [
        "VP Manufacturing",
        "VP Logistics",
        "VP Supply Chain",
    ]
    assert [tool for tool, _payload in provider.calls] == [
        "harvestapi_search_leads"
    ]


def test_role_handoff_infers_seniority_from_roles_when_explicit_field_is_empty() -> None:
    company, base_position, profile = _semantic_role_fixture()
    titles = [
        "Manufacturing Manager",
        "Chief Manufacturing Officer",
        "VP Manufacturing",
        "Head of Manufacturing",
    ]
    elements = [
        {
            "linkedinUrl": f"https://www.linkedin.com/in/candidate-{index}/",
            "currentPositions": [{**base_position, "title": title}],
        }
        for index, title in enumerate(titles)
    ]
    provider = SemanticRoleProvider(profile, [base_position], elements=elements)
    icp = _semantic_role_icp()
    icp["target_seniority"] = ""

    lookup = ContactLookup(icp, allow_role_selection=True)

    assert lookup.find(company, provider) is None
    assert lookup.role_options(company) == [
        "VP Manufacturing",
        "Head of Manufacturing",
    ]
    assert [tool for tool, _payload in provider.calls] == [
        "harvestapi_search_leads"
    ]


@pytest.mark.parametrize(
    "failure",
    ["stale_title", "wrong_company", "wrong_location", "wrong_email", "wrong_source"],
)
def test_selected_observed_role_keeps_full_profile_gates(failure: str) -> None:
    company, position, profile = _semantic_role_fixture()
    if failure == "stale_title":
        profile["currentPosition"][0]["title"] = "VP Operations"
    elif failure == "wrong_company":
        profile["currentPosition"][0].update(
            {
                "companyName": "Other Company",
                "companyDomain": "other.example",
                "companyLinkedinUrl": "https://www.linkedin.com/company/other/",
            }
        )
    elif failure == "wrong_location":
        profile["location"] = {
            "countryCode": "US",
            "parsed": {"countryFull": "United States", "state": "California"},
        }
    elif failure == "wrong_email":
        profile["workEmail"] = "support@example.com"
    elif failure == "wrong_source":
        profile["id"] = ""
    provider = SemanticRoleProvider(profile, [position])

    lookup = ContactLookup(_semantic_role_icp(), allow_role_selection=True)
    assert lookup.find(company, provider) is None
    assert lookup.find(
        company, provider, selected_observed_role="VP of Manufacturing"
    ) is None
    assert lookup.status(company) == "not_found"
    assert [tool for tool, _payload in provider.calls] == [
        "harvestapi_search_leads",
        "harvestapi_get_profile",
    ]


def test_pending_role_handoff_never_auto_selects_or_repeats_search_at_final() -> None:
    company, position, profile = _semantic_role_fixture()
    provider = SemanticRoleProvider(profile, [position])

    lookup = ContactLookup(_semantic_role_icp(), allow_role_selection=True)
    assert lookup.find(company, provider) is None
    assert lookup.enrich([company], provider) == [company]
    assert lookup.status(company) == "role_selection_required"
    assert [tool for tool, _payload in provider.calls] == [
        "harvestapi_search_leads"
    ]


def test_selected_role_transient_profile_failure_retries_profile_without_search() -> None:
    company, position, profile = _semantic_role_fixture()
    provider = SemanticRoleProvider(
        profile, [position], transient_profile_failures=1
    )

    lookup = ContactLookup(_semantic_role_icp(), allow_role_selection=True)
    assert lookup.find(company, provider) is None
    assert lookup.find(
        company, provider, selected_observed_role="VP of Manufacturing"
    ) is None
    assert lookup.status(company) == "unavailable"
    rows = lookup.enrich([company], provider)
    assert rows[0]["contact"]["role"] == "VP of Manufacturing"
    assert [tool for tool, _payload in provider.calls] == [
        "harvestapi_search_leads",
        "harvestapi_get_profile",
        "harvestapi_get_profile",
    ]


def test_standalone_contact_lookup_does_not_expose_semantic_role_options() -> None:
    company, position, profile = _semantic_role_fixture()
    provider = SemanticRoleProvider(profile, [position])

    lookup = ContactLookup(_semantic_role_icp())
    assert lookup.find(company, provider) is None
    assert lookup.status(company) == "not_found"
    assert lookup.role_options(company) == []
    with pytest.raises(ValueError, match="unavailable"):
        lookup.find(
            company, provider, selected_observed_role="VP of Manufacturing"
        )
    assert [tool for tool, _payload in provider.calls] == [
        "harvestapi_search_leads"
    ]


def test_selected_role_profile_lookup_cannot_overrun_provider_budget() -> None:
    from experiments.harness_bakeoff.adapters import pydantic_ai

    company, position, profile = _semantic_role_fixture()
    provider = SemanticRoleProvider(profile, [position])
    budget = pydantic_ai._ToolBudget(
        SimpleNamespace(call=provider), maximum=1
    )
    lookup = ContactLookup(_semantic_role_icp(), allow_role_selection=True)

    assert lookup.find(company, budget.call) is None
    assert lookup.find(
        company,
        budget.call,
        selected_observed_role="VP of Manufacturing",
    ) is None
    assert lookup.status(company) == "unavailable"
    assert budget.calls == 1
    assert [tool for tool, _payload in provider.calls] == [
        "harvestapi_search_leads"
    ]


def test_arena_model_can_select_one_observed_role_and_reuse_verified_contact(
    monkeypatch,
) -> None:
    import httpx
    import arena_transport
    from experiments.harness_bakeoff.adapters import pydantic_ai

    company, position, profile = _semantic_role_fixture()
    scripted = SemanticRoleProvider(profile, [position])
    model_requests: list[dict] = []

    def provider_response(request: httpx.Request) -> httpx.Response:
        tool = request.url.path.split("/")[-2]
        payload = json.loads(request.content)
        return httpx.Response(200, request=request, json=scripted(tool, payload))

    tools = arena_transport.ArenaToolClient(
        client=httpx.Client(transport=httpx.MockTransport(provider_response))
    )

    async def model_response(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model_requests.append(body)
        generation = len(model_requests)
        if generation == 1:
            arguments = {
                key: company[key]
                for key in (
                    "company_name",
                    "company_website",
                    "company_linkedin",
                )
            }
            name = "get_company_contact"
        elif generation in {2, 3}:
            arguments = {
                key: company[key]
                for key in (
                    "company_name",
                    "company_website",
                    "company_linkedin",
                )
            }
            arguments["selected_observed_role"] = (
                "VP Operations" if generation == 2 else "VP of Manufacturing"
            )
            name = "get_company_contact"
        else:
            arguments = {"companies": [company]}
            name = "submit_companies"
        return httpx.Response(
            200,
            request=request,
            json={
                "id": f"generation-{generation}",
                "object": "chat.completion",
                "created": generation,
                "model": "openai/gpt-5.5",
                "provider": "OpenAI",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call-{generation}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", "/tmp/unused-worker.sock")
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "1")
    monkeypatch.setenv("BAKEOFF_OPENROUTER_MODEL", "openai/gpt-5.5")
    monkeypatch.setenv("BAKEOFF_RUN_TIMEOUT_SECONDS", "285")
    monkeypatch.setattr(arena_transport, "ArenaToolClient", lambda timeout: tools)
    monkeypatch.setattr(
        arena_transport,
        "arena_openrouter_http_client",
        lambda timeout: httpx.AsyncClient(
            transport=arena_transport.ArenaOpenRouterTransport(
                inner=httpx.MockTransport(model_response)
            )
        ),
    )

    rows = pydantic_ai.run_icp(
        {**_semantic_role_icp(), "icp_id": "today"}
    )

    assert rows[0]["contact"]["role"] == "VP of Manufacturing"
    assert [tool for tool, _payload in scripted.calls] == [
        "harvestapi_search_leads",
        "harvestapi_get_profile",
    ]
    first_contact_schema = next(
        tool["function"]["parameters"]
        for tool in model_requests[0]["tools"]
        if tool["function"]["name"] == "get_company_contact"
    )
    assert first_contact_schema["properties"]["selected_observed_role"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
    }
    first_result = next(
        json.loads(message["content"])
        for message in model_requests[1]["messages"]
        if message.get("role") == "tool"
    )
    assert first_result == {
        "contact_found": None,
        "lookup_status": "role_selection_required",
        "role": None,
        "location": None,
        "observed_role_options": ["VP of Manufacturing"],
    }
    invalid_selection_result = next(
        message["content"]
        for message in reversed(model_requests[2]["messages"])
        if message.get("role") == "tool"
    )
    assert invalid_selection_result.startswith(
        "selected_observed_role must exactly match an offered role"
    )
    assert [tool for tool, _payload in scripted.calls] == [
        "harvestapi_search_leads",
        "harvestapi_get_profile",
    ]
    selected_result = next(
        json.loads(message["content"])
        for message in reversed(model_requests[3]["messages"])
        if message.get("role") == "tool"
    )
    assert selected_result["contact_found"] is True
    assert selected_result["role"] == "VP of Manufacturing"
    assert profile["workEmail"] not in json.dumps(model_requests)
    assert pydantic_ai.get_last_usage()["provider_calls"] == 2


def test_final_fallback_cannot_overrun_four_reserved_provider_calls():
    from experiments.harness_bakeoff.adapters import pydantic_ai

    calls = []

    def provider(tool, payload):
        calls.append((tool, deepcopy(payload)))
        if tool == "harvestapi_search_leads" and len(calls) == 1:
            return {"result": {"data": {"status": "ok", "elements": []}}}
        if tool == "harvestapi_search_leads":
            return {
                "result": {
                    "data": {
                        "elements": [
                            {
                                "id": f"profile-{index}",
                                "linkedinUrl": f"https://www.linkedin.com/in/candidate-{index}/",
                                "currentPositions": _profile()["currentPosition"],
                            }
                            for index in range(3)
                        ]
                    }
                }
            }
        if tool == "harvestapi_get_profile":
            return {
                "result": {
                    "data": {
                        "element": _profile(
                            linkedinUrl=payload["url"], workEmail=""
                        )
                    }
                }
            }
        raise AssertionError(f"unexpected provider tool: {tool}")

    budget = pydantic_ai._ToolBudget(
        SimpleNamespace(call=provider), maximum=60, contact_reserve=4
    )
    budget.calls = budget.research_maximum

    assert ContactLookup(_icp()).enrich([_company()], budget.call) == [_company()]
    assert [tool for tool, _payload in calls] == [
        "harvestapi_search_leads",
        "harvestapi_search_leads",
        "harvestapi_get_profile",
        "harvestapi_get_profile",
    ]
    assert budget.calls == 60


@pytest.mark.parametrize("starting_calls", [0, 24, 25, 29])
def test_early_contact_tool_preserves_caps_and_reuses_final_contact(monkeypatch, starting_calls):
    import httpx
    import arena_transport
    from experiments.harness_bakeoff.adapters import pydantic_ai

    scripted = ScriptedProvider()
    model_requests = []
    cap_during_research = []

    def provider_response(request):
        tool = request.url.path.split("/")[-2]
        return httpx.Response(200, request=request, json=scripted(tool, json.loads(request.content)))

    tools = arena_transport.ArenaToolClient(
        client=httpx.Client(transport=httpx.MockTransport(provider_response))
    )
    tools.deepline_calls = starting_calls

    async def model_response(request):
        body = json.loads(request.content)
        model_requests.append(body)
        cap_during_research.append(tools.deepline_call_limit)
        if len(model_requests) == 1:
            functions = [
                {"name": "get_company_contact", "arguments": json.dumps({k: _company()[k] for k in ("company_name", "company_website", "company_linkedin")})}
            ] * 2
        else:
            functions = [{"name": "submit_companies", "arguments": json.dumps({"companies": [_company()]})}]
        return httpx.Response(200, request=request, json={
            "id": f"generation-{len(model_requests)}", "object": "chat.completion", "created": 1,
            "model": "openai/gpt-5.5", "provider": "OpenAI",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call-{len(model_requests)}-{i}", "type": "function", "function": f} for i, f in enumerate(functions)
            ]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", "/tmp/unused-worker.sock")
    monkeypatch.setenv("BAKEOFF_OPENROUTER_MODEL", "openai/gpt-5.5")
    monkeypatch.setenv("BAKEOFF_RUN_TIMEOUT_SECONDS", "285")
    monkeypatch.setattr(arena_transport, "ArenaToolClient", lambda timeout: tools)
    monkeypatch.setattr(arena_transport, "arena_openrouter_http_client", lambda timeout: httpx.AsyncClient(
        transport=arena_transport.ArenaOpenRouterTransport(inner=httpx.MockTransport(model_response))
    ))
    rows = pydantic_ai.run_icp(_icp(icp_id="today"))
    actual_calls = {0: 2, 24: 2, 25: 3, 29: 1}[starting_calls]
    assert len(scripted.calls) == actual_calls
    assert tools.deepline_calls == starting_calls + actual_calls <= 30
    expected_model_calls = 3 if starting_calls == 0 else 2
    assert cap_during_research == [26] * expected_model_calls
    assert tools.deepline_call_limit == 30
    assert ("contact" in rows[0]) is (starting_calls < 29)
    assert json.loads(json.dumps(rows)) == validate_companies(rows, 5, allow_contacts=True)
    assert "ada@acme.com" not in json.dumps(model_requests)
    first_tools = {
        tool["function"]["name"] for tool in model_requests[0].get("tools", [])
    }
    if starting_calls >= 26:
        assert "get_company_contact" not in first_tools
    else:
        assert "get_company_contact" in first_tools
        contact_returns = [
            json.loads(message["content"])
            for message in model_requests[1]["messages"]
            if message.get("role") == "tool"
            and "lookup_status" in message.get("content", "")
        ]
        expected_status = "unavailable" if starting_calls == 25 else "found"
        assert {row["lookup_status"] for row in contact_returns} == {
            expected_status
        }
        expected_found = None if starting_calls == 25 else True
        assert {row["contact_found"] for row in contact_returns} == {expected_found}
    assert pydantic_ai.get_last_usage()["provider_calls"] == {
        0: 2,
        24: 2,
        25: 4,
        29: 2,
    }[starting_calls]


def _install_completion_retry_failure(monkeypatch, failure: BaseException):
    import httpx
    import arena_transport
    from experiments.harness_bakeoff.adapters import pydantic_ai

    scripted = ScriptedProvider()

    def provider_response(request):
        tool = request.url.path.split("/")[-2]
        return httpx.Response(
            200,
            request=request,
            json=scripted(tool, json.loads(request.content)),
        )

    tools = arena_transport.ArenaToolClient(
        client=httpx.Client(transport=httpx.MockTransport(provider_response))
    )

    class FailingRetryAgent:
        def __init__(self, *_args, **_kwargs):
            self.validator = None

        def output_validator(self, validator):
            self.validator = validator
            return validator

        async def run(self, _prompt, *, usage_limits, usage):
            assert usage_limits.output_tokens_limit == 15_000
            usage.requests = 1
            usage.output_tokens = 100
            context = SimpleNamespace(usage=usage, retry=0, max_retries=2)
            assert self.validator is not None
            with pytest.raises(ModelRetry):
                self.validator(
                    context,
                    CompaniesResult.model_validate({"companies": [_company()]}),
                )
            raise failure

    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", "/tmp/unused-worker.sock")
    monkeypatch.setenv("BAKEOFF_OPENROUTER_MODEL", "openai/gpt-5.5")
    monkeypatch.setenv("BAKEOFF_RUN_TIMEOUT_SECONDS", "285")
    monkeypatch.setattr(pydantic_ai.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(arena_transport, "ArenaToolClient", lambda timeout: tools)
    monkeypatch.setattr(
        arena_transport,
        "arena_openrouter_http_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)),
    )
    monkeypatch.setattr(pydantic_ai, "OpenRouterModel", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(pydantic_ai, "Agent", FailingRetryAgent)
    return pydantic_ai, scripted


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("retry timed out"), UsageLimitExceeded("output limit reached")],
)
def test_completion_retry_limit_failure_keeps_partial_and_runs_final_contact(
    monkeypatch, failure
):
    pydantic_ai, scripted = _install_completion_retry_failure(monkeypatch, failure)

    rows = pydantic_ai.run_icp(_icp(icp_id="today"))

    assert rows[0]["company_name"] == "Acme"
    assert rows[0]["contact"]["email"] == "ada@acme.com"
    assert [tool for tool, _payload in scripted.calls] == [
        "harvestapi_search_leads",
        "harvestapi_get_profile",
    ]
    assert pydantic_ai.get_last_usage()["provider_calls"] == 2


def test_completion_retry_does_not_hide_unrelated_failure(monkeypatch):
    pydantic_ai, scripted = _install_completion_retry_failure(
        monkeypatch, RuntimeError("model transport failed")
    )

    with pytest.raises(RuntimeError, match="model transport failed"):
        pydantic_ai.run_icp(_icp(icp_id="today"))

    assert scripted.calls == []


def test_discarded_early_candidates_cannot_spend_final_contact_reserve(monkeypatch):
    import httpx
    import arena_transport
    from experiments.harness_bakeoff.adapters import pydantic_ai

    scripted = ScriptedProvider()
    requests = []
    generations = []
    deepline_calls_before_generation = []

    def provider_response(request):
        tool = request.url.path.split('/')[-2]
        requests.append(tool)
        payload = {'result': {'data': {'data': []}}} if tool == 'hunter_discover' else scripted(tool, json.loads(request.content))
        return httpx.Response(200, request=request, json=payload)

    tools = arena_transport.ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(provider_response)))

    async def model_response(request):
        deepline_calls_before_generation.append(tools.deepline_calls)
        generations.append(json.loads(request.content))
        if len(generations) == 1:
            functions = [{'name': 'get_company_contact', 'arguments': json.dumps({
                'company_name': 'Acme', 'company_website': f'https://discarded-{i}.com/',
                'company_linkedin': 'https://www.linkedin.com/company/acme/',
            })} for i in range(5)]
        elif len(generations) == 2:
            functions = [{'name': 'search_companies', 'arguments': json.dumps({'query': f'candidate {i}'})} for i in range(20)]
        else:
            functions = [{'name': 'submit_companies', 'arguments': json.dumps({'companies': [_company()]})}]
        return httpx.Response(200, request=request, json={
            'id': f'g-{len(generations)}', 'object': 'chat.completion', 'created': 1,
            'model': 'openai/gpt-5.5', 'provider': 'OpenAI',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'call-{len(generations)}-{i}', 'type': 'function', 'function': f} for i,f in enumerate(functions)
            ]}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
        })

    monkeypatch.setenv('LAB_ARENA_WORKER_SOCKET', '/tmp/unused-worker.sock')
    monkeypatch.setenv('BAKEOFF_OPENROUTER_MODEL', 'openai/gpt-5.5')
    monkeypatch.setenv('BAKEOFF_RUN_TIMEOUT_SECONDS', '285')
    monkeypatch.setattr(arena_transport, 'ArenaToolClient', lambda timeout: tools)
    monkeypatch.setattr(arena_transport, 'arena_openrouter_http_client', lambda timeout: httpx.AsyncClient(
        transport=arena_transport.ArenaOpenRouterTransport(inner=httpx.MockTransport(model_response))
    ))
    rows = pydantic_ai.run_icp(_icp(icp_id='today'))
    assert rows[0]['contact']['email'] == 'ada@acme.com'
    assert requests[:10] == ['harvestapi_search_leads', 'harvestapi_get_profile'] * 5
    assert requests[10:26] == ['hunter_discover'] * 16
    assert requests[26:] == ['harvestapi_search_leads', 'harvestapi_get_profile']
    assert deepline_calls_before_generation == [0, 10, 26]
    assert tools.deepline_calls == 28 <= 30
    assert pydantic_ai.get_last_usage()['provider_calls'] <= 60


@pytest.fixture
def harvestapi_west_coast_provider() -> ScriptedProvider:
    class HarvestApiWestCoastProvider(ScriptedProvider):
        def __call__(self, tool: str, payload: dict) -> object:
            if tool == "harvestapi_search_leads" and payload.get("locations") == (
                "US-WA,US-CA,US-OR"
            ):
                self.calls.append((tool, deepcopy(payload)))
                return {
                    "status": "error",
                    "error": {
                        "statusCode": 400,
                        "message": "locations must use HarvestAPI location names",
                    },
                }
            return super().__call__(tool, payload)

    profile = _profile()
    profile["location"]["parsed"]["state"] = "Washington"
    profile["location"]["parsed"]["city"] = "Seattle"
    return HarvestApiWestCoastProvider(profile)


def test_us_region_codes_avoid_provider_400_and_match_returned_region_name(
    harvestapi_west_coast_provider: ScriptedProvider,
) -> None:
    icp = _icp(
        contact_geography={
            "countries": ["US"],
            "regions": ["US-WA", "US-CA", "US-OR"],
            "cities": [],
        }
    )

    companies = enrich_contacts(icp, [_company()], harvestapi_west_coast_provider)

    assert harvestapi_west_coast_provider.calls[0][1]["locations"] == (
        "Washington,California,Oregon"
    )
    assert companies[0]["contact"]["location"]["region"] == "Washington"


@pytest.mark.parametrize(
    ("country", "region", "expected_contact"),
    [
        ("US", "WA", True),
        ("US", "Oregon", True),
        ("US", "New York", False),
        ("GB", "Washington", False),
    ],
)
def test_us_region_equivalence_keeps_wrong_state_and_country_out(
    country: str, region: str, expected_contact: bool
) -> None:
    profile = _profile()
    profile["location"]["countryCode"] = country
    profile["location"]["parsed"]["countryFull"] = (
        "United States" if country == "US" else "United Kingdom"
    )
    profile["location"]["parsed"]["state"] = region
    icp = _icp(
        contact_geography={
            "countries": ["US"],
            "regions": ["US-WA", "US-OR"],
            "cities": [],
        }
    )

    companies = enrich_contacts(icp, [_company()], ScriptedProvider(profile))

    assert ("contact" in companies[0]) is expected_contact


def test_explicit_us_region_rejects_wrong_country_without_country_list() -> None:
    profile = _profile()
    profile["location"]["countryCode"] = "CA"
    profile["location"]["parsed"].update(
        {"countryFull": "Canada", "state": "US-WA"}
    )
    icp = _icp(
        contact_geography={"countries": [], "regions": ["US-WA"], "cities": []}
    )

    companies = enrich_contacts(icp, [_company()], ScriptedProvider(profile))

    assert companies == [_company()]


def test_contact_round_uses_search_then_email_profile_and_attaches_provenance() -> None:
    provider = ScriptedProvider()

    companies = enrich_contacts(_icp(), [_company()], provider)

    assert provider.calls == [
        (
            "harvestapi_search_leads",
            {
                "currentJobTitles": "Vice President of Sales",
                "page": 1,
                "currentCompanies": "https://www.linkedin.com/company/acme/",
                "locations": "San Francisco",
            },
        ),
        (
            "harvestapi_get_profile",
            {
                "url": "https://www.linkedin.com/in/ACoOpaqueToken/",
                "findEmail": "true",
            },
        ),
    ]
    assert companies[0]["contact"] == {
        "full_name": "Ada Lovelace",
        "role": "VP Sales",
        "linkedin_url": "https://www.linkedin.com/in/ada-lovelace/",
        "location": {
            "country": "US",
            "region": "California",
            "city": "San Francisco",
        },
        "email": "ada@acme.com",
        "email_source": {
            "provider": "harvestapi",
            "tool": "harvestapi_get_profile",
            "record_id": "profile-1",
        },
    }
    assert (
        validate_companies(companies, allow_contacts=True)[0]["contact"]
        == companies[0]["contact"]
    )


def test_search_metadata_skips_explicitly_wrong_candidates_before_paid_profile() -> (
    None
):
    provider = RankedProvider()

    companies = enrich_contacts(_icp(), [_company()], provider)

    assert companies[0]["contact"]["email"] == "ada@acme.com"
    assert [name for name, _ in provider.calls] == [
        "harvestapi_search_leads",
        "harvestapi_get_profile",
    ]


def test_numeric_search_company_reference_allows_full_profile_identity_check() -> None:
    calls: list[tuple[str, dict]] = []

    def provider(tool: str, payload: dict) -> object:
        calls.append((tool, deepcopy(payload)))
        if tool == "harvestapi_search_leads":
            return {
                "data": {
                    "elements": [
                        {
                            "linkedinUrl": "https://www.linkedin.com/in/candidate/",
                            "currentPositions": [
                                {
                                    "title": "VP Sales",
                                    "companyName": "Acme Software",
                                    "companyLinkedinUrl": (
                                        "https://www.linkedin.com/company/12345"
                                    ),
                                    "current": True,
                                }
                            ],
                        }
                    ]
                }
            }
        if tool == "harvestapi_get_profile":
            return {"data": {"element": _profile()}}
        raise AssertionError(f"unexpected provider tool: {tool}")

    companies = enrich_contacts(_icp(), [_company()], provider)

    assert companies[0]["contact"]["email"] == "ada@acme.com"
    assert [tool for tool, _payload in calls] == [
        "harvestapi_search_leads",
        "harvestapi_get_profile",
    ]


def test_exact_search_company_match_precedes_numeric_company_fallback() -> None:
    calls: list[tuple[str, dict]] = []

    def provider(tool: str, payload: dict) -> object:
        calls.append((tool, deepcopy(payload)))
        if tool == "harvestapi_search_leads":
            return {
                "data": {
                    "elements": [
                        {
                            "linkedinUrl": "https://www.linkedin.com/in/fallback/",
                            "currentPositions": [
                                {
                                    "title": "VP Sales",
                                    "companyName": "Acme Software",
                                    "companyLinkedinUrl": (
                                        "https://www.linkedin.com/company/12345"
                                    ),
                                    "current": True,
                                }
                            ],
                        },
                        {
                            "linkedinUrl": "https://www.linkedin.com/in/exact/",
                            "currentPositions": _profile()["currentPosition"],
                        },
                    ]
                }
            }
        if tool == "harvestapi_get_profile":
            assert payload["url"].endswith("/exact/")
            return {"data": {"element": _profile()}}
        raise AssertionError(f"unexpected provider tool: {tool}")

    companies = enrich_contacts(_icp(), [_company()], provider)

    assert companies[0]["contact"]["email"] == "ada@acme.com"
    assert [payload["url"] for tool, payload in calls if tool.endswith("get_profile")] == [
        "https://www.linkedin.com/in/exact/"
    ]


def test_numeric_company_fallback_is_bounded_and_full_profile_rejects_wrong_company() -> (
    None
):
    calls: list[tuple[str, dict]] = []

    def provider(tool: str, payload: dict) -> object:
        calls.append((tool, deepcopy(payload)))
        if tool == "harvestapi_search_leads":
            return {
                "data": {
                    "elements": [
                        {
                            "linkedinUrl": f"https://www.linkedin.com/in/candidate-{index}/",
                            "currentPositions": [
                                {
                                    "title": "VP Sales",
                                    "companyName": "Other Company",
                                    "companyLinkedinUrl": (
                                        f"https://www.linkedin.com/company/{index}"
                                    ),
                                    "current": True,
                                }
                            ],
                        }
                        for index in range(4)
                    ]
                }
            }
        if tool == "harvestapi_get_profile":
            wrong_position = {
                **_profile()["currentPosition"][0],
                "companyName": "Other Company",
                "companyDomain": "other.example",
                "companyLinkedinUrl": "https://www.linkedin.com/company/other/",
            }
            return {
                "data": {
                    "element": _profile(currentPosition=[wrong_position]),
                }
            }
        raise AssertionError(f"unexpected provider tool: {tool}")

    original = _company()
    assert enrich_contacts(_icp(), [original], provider) == [original]
    assert len([tool for tool, _payload in calls if tool.endswith("get_profile")]) == 3


def test_country_name_alias_matches_provider_iso_code() -> None:
    profile = _profile()
    profile["location"]["parsed"]["countryFull"] = "USA"

    companies = enrich_contacts(_icp(), [_company()], ScriptedProvider(profile))

    assert companies[0]["contact"]["location"]["country"] == "US"


def test_country_full_is_normalized_when_provider_omits_country_code() -> None:
    profile = _profile()
    del profile["location"]["countryCode"]

    companies = enrich_contacts(_icp(), [_company()], ScriptedProvider(profile))

    assert companies[0]["contact"]["location"]["country"] == "US"


def test_exact_company_name_accepts_provider_subdomain() -> None:
    position = {
        **_profile()["currentPosition"][0],
        "companyDomain": "careers.acme.com",
    }

    companies = enrich_contacts(
        _icp(), [_company()], ScriptedProvider(_profile(currentPosition=[position]))
    )

    assert companies[0]["contact"]["email"] == "ada@acme.com"


def test_company_subdomain_match_requires_exact_company_name() -> None:
    position = {
        **_profile()["currentPosition"][0],
        "companyName": "Other Company",
        "companyDomain": "careers.acme.com",
        "companyLinkedinUrl": "",
    }
    original = _company()

    companies = enrich_contacts(
        _icp(), [original], ScriptedProvider(_profile(currentPosition=[position]))
    )

    assert companies == [original]


def test_linkedin_person_slug_case_is_preserved_in_claim() -> None:
    profile = _profile(linkedinUrl="https://www.linkedin.com/in/Ada-Lovelace/")

    companies = enrich_contacts(_icp(), [_company()], ScriptedProvider(profile))

    assert companies[0]["contact"]["linkedin_url"].endswith("/Ada-Lovelace/")


def test_profile_canonical_redirect_uses_verified_returned_profile() -> None:
    profile = _profile(
        linkedinUrl="https://www.linkedin.com/in/ada-lovelace-canonical/",
        publicIdentifier="ada-lovelace-canonical",
    )

    companies = enrich_contacts(_icp(), [_company()], ScriptedProvider(profile))

    assert companies[0]["contact"]["linkedin_url"].endswith("/ada-lovelace-canonical/")


def test_unicode_company_normalization_preserves_letters_and_accents() -> None:
    assert _company_name("München Software GmbH") == _company_name("Munchen Software")


def test_profile_plural_current_positions_are_supported() -> None:
    profile = _profile()
    profile["currentPositions"] = profile.pop("currentPosition")

    companies = enrich_contacts(_icp(), [_company()], ScriptedProvider(profile))

    assert companies[0]["contact"]["role"] == "VP Sales"


def test_generic_email_is_skipped_in_favor_of_person_email() -> None:
    profile = _profile(
        workEmail="billing@acme.com",
        emails=["ada@acme.com"],
    )

    companies = enrich_contacts(_icp(), [_company()], ScriptedProvider(profile))

    assert companies[0]["contact"]["email"] == "ada@acme.com"


def test_search_without_same_position_match_does_not_buy_profile() -> None:
    calls: list[str] = []

    def provider(tool: str, _payload: dict) -> object:
        calls.append(tool)
        if tool != "harvestapi_search_leads":
            raise AssertionError("mismatched search result reached profile lookup")
        return {
            "elements": [
                {
                    "linkedinUrl": "https://www.linkedin.com/in/wrong-profile/",
                    "currentPositions": [
                        {
                            "position": "Vice President of Sales",
                            "companyName": "Other Company",
                            "companyLinkedinUrl": "https://www.linkedin.com/company/other/",
                        }
                    ],
                }
            ]
        }

    original = _company()
    assert enrich_contacts(_icp(), [original], provider) == [original]
    assert calls == ["harvestapi_search_leads"]


@pytest.mark.parametrize(
    "profile",
    [
        _profile(workEmail=""),
        _profile(
            currentPosition=[
                {
                    **_profile()["currentPosition"][0],
                    "companyDomain": "other.com",
                }
            ]
        ),
        _profile(
            currentPosition=[
                {
                    **_profile()["currentPosition"][0],
                    "title": "Sales Manager",
                }
            ]
        ),
        _profile(
            location={
                "countryCode": "GB",
                "parsed": {
                    "countryFull": "United Kingdom",
                    "state": "England",
                    "city": "London",
                },
            }
        ),
    ],
)
def test_missing_or_mismatched_profile_keeps_company_row_without_contact(
    profile: dict,
) -> None:
    original = _company()
    companies = enrich_contacts(_icp(), [original], ScriptedProvider(profile))

    assert companies == [original]


def test_provider_failure_keeps_company_row_without_contact() -> None:
    original = _company()

    def fail(_tool: str, _payload: dict) -> object:
        raise RuntimeError("provider unavailable")

    assert enrich_contacts(_icp(), [original], fail) == [original]


def test_legacy_round_makes_no_provider_call_and_keeps_legacy_shape() -> None:
    original = {**_company(), "contact": {"email": "invented@example.com"}}

    def unexpected(_tool: str, _payload: dict) -> object:
        raise AssertionError("legacy round called contact provider")

    companies = enrich_contacts({}, [original], unexpected)
    assert companies == [_company()]
    assert "contact" not in validate_companies(companies)[0]


@pytest.mark.parametrize(
    "email",
    [
        ".ada@acme.com",
        "ada.@acme.com",
        "ada..lovelace@acme.com",
        f"{'a' * 65}@acme.com",
        "support@acme.com",
    ],
)
def test_malformed_or_generic_provider_email_keeps_company_without_contact(
    email: str,
) -> None:
    original = _company()
    companies = enrich_contacts(
        _icp(), [original], ScriptedProvider(_profile(workEmail=email))
    )

    assert companies == [original]


def test_contact_model_rejects_unattributed_email() -> None:
    provider = ScriptedProvider()
    company = enrich_contacts(_icp(), [_company()], provider)[0]
    del company["contact"]["email_source"]["record_id"]

    with pytest.raises(ValidationError, match="broker_call_id or record_id"):
        validate_companies([company], allow_contacts=True)


@pytest.mark.parametrize(
    "profile",
    [
        _profile(id="bad record id"),
        _profile(
            location={
                "countryCode": "ZZ",
                "parsed": {"countryFull": "Unknown"},
            }
        ),
    ],
)
def test_invalid_constructed_contact_is_omitted_without_losing_company(
    profile: dict,
) -> None:
    original = _company()
    unconstrained_icp = _icp(
        contact_geography={"countries": [], "regions": [], "cities": []}
    )

    assert enrich_contacts(
        unconstrained_icp, [original], ScriptedProvider(profile)
    ) == [original]


def test_contact_validation_requires_explicit_contact_mode() -> None:
    company = enrich_contacts(_icp(), [_company()], ScriptedProvider())[0]

    with pytest.raises(ValidationError, match="contact"):
        validate_companies([company])
    assert validate_companies([company], allow_contacts=True)[0]["contact"]


def test_company_generation_schema_does_not_allow_model_supplied_contact() -> None:
    with pytest.raises(ValidationError, match="contact"):
        CompaniesResult.model_validate({"companies": [{**_company(), "contact": {}}]})


def test_explicit_false_current_flag_wins_over_true_flag() -> None:
    position = {
        **_profile()["currentPosition"][0],
        "current": True,
        "isCurrent": False,
    }
    original = _company()

    companies = enrich_contacts(
        _icp(),
        [original],
        ScriptedProvider(_profile(currentPosition=[position])),
    )

    assert companies == [original]


@pytest.mark.parametrize(
    "title",
    [
        "Vice President of Software Engineering",
        "Vice President, Forward Deployed Engineering",
    ],
)
def test_target_role_allows_modifiers_without_changing_the_claimed_title(title) -> None:
    position = {**_profile()["currentPosition"][0], "title": title}
    provider = ScriptedProvider(_profile(currentPosition=[position]))

    def call(tool, payload):
        response = provider(tool, payload)
        if tool == "harvestapi_search_leads":
            response["result"]["data"]["elements"][0]["currentPositions"] = [position]
        return response

    companies = enrich_contacts(
        _icp(target_roles=["VP Engineering"], target_seniority="VP+"),
        [_company()],
        call,
    )

    assert companies[0]["contact"]["role"] == title


def test_target_role_words_must_remain_in_order() -> None:
    position = {
        **_profile()["currentPosition"][0],
        "title": "Chief Nursing Innovation Officer and Industry Executive",
    }
    original = _company()
    icp = _icp(
        target_roles=["Chief Executive Officer"],
        target_seniority="C-level",
    )
    provider = ScriptedProvider(_profile(currentPosition=[position]))

    def call(tool, payload):
        response = provider(tool, payload)
        if tool == "harvestapi_search_leads":
            response["result"]["data"]["elements"][0]["currentPositions"] = [position]
        return response

    companies = enrich_contacts(icp, [original], call)

    assert companies == [original]


@pytest.mark.parametrize("exact_profile_valid", [True, False])
def test_exact_target_role_precedes_broader_match_and_keeps_profile_checks(
    exact_profile_valid: bool,
) -> None:
    base = _profile()["currentPosition"][0]
    broad_title = "VP, Head of Global Product Strategy and Execution, Merchant Solutions"
    broad = _profile(currentPosition=[{**base, "title": broad_title}])
    exact = _profile(
        id="profile-2",
        publicIdentifier="grace-hopper",
        linkedinUrl="https://www.linkedin.com/in/grace-hopper/",
        firstName="Grace",
        lastName="Hopper",
        workEmail="grace@acme.com",
        currentPosition=[{**base, "title": "Head of Product"}],
    )
    calls = []

    def provider(tool, payload):
        calls.append((tool, payload))
        if tool == "harvestapi_search_leads":
            return {"data": {"elements": [broad, exact], "status": "OK"}}
        assert tool == "harvestapi_get_profile"
        assert payload["findEmail"] == "true"
        profile = deepcopy(exact if "grace-hopper" in payload["url"] else broad)
        if profile["id"] == "profile-2" and not exact_profile_valid:
            profile["currentPosition"][0]["companyDomain"] = "unrelated.com"
        return {"data": {"element": profile}}

    output = enrich_contacts(
        _icp(target_roles=["Head of Product"], target_seniority=""),
        [_company()],
        provider,
    )

    assert calls[1][1]["url"] == exact["linkedinUrl"]
    expected = "Head of Product" if exact_profile_valid else broad_title
    assert output[0]["contact"]["role"] == expected
    assert len(calls) == (2 if exact_profile_valid else 3)


def test_empty_search_retries_once_with_function_title_and_accepts_valid_contact() -> (
    None
):
    calls: list[tuple[str, dict]] = []
    search_calls = 0

    def provider(tool: str, payload: dict) -> object:
        nonlocal search_calls
        calls.append((tool, deepcopy(payload)))
        if tool == "harvestapi_search_leads":
            search_calls += 1
            if search_calls == 1:
                return {"result": {"data": {"elements": [], "status": "OK"}}}
            return {
                "result": {
                    "data": {
                        "elements": [
                            {
                                "linkedinUrl": "https://www.linkedin.com/in/ada-lovelace/",
                                "currentPositions": _profile()["currentPosition"],
                            }
                        ],
                        "status": "OK",
                    }
                }
            }
        if tool == "harvestapi_get_profile":
            return {"result": {"data": {"element": _profile()}}}
        raise AssertionError(f"unexpected provider tool: {tool}")

    companies = enrich_contacts(_icp(), [_company()], provider)

    assert companies[0]["contact"]["email"] == "ada@acme.com"
    search_payloads = [
        payload for tool, payload in calls if tool == "harvestapi_search_leads"
    ]
    assert search_payloads == [
        {
            "currentJobTitles": "Vice President of Sales",
            "page": 1,
            "currentCompanies": "https://www.linkedin.com/company/acme/",
            "locations": "San Francisco",
        },
        {
            "currentJobTitles": "sales",
            "page": 1,
            "currentCompanies": "https://www.linkedin.com/company/acme/",
            "locations": "San Francisco",
        },
    ]
    assert [tool for tool, _payload in calls] == [
        "harvestapi_search_leads",
        "harvestapi_search_leads",
        "harvestapi_get_profile",
    ]


@pytest.mark.parametrize(
    ("failure_kind", "expected_profile_calls"),
    [
        ("wrong_company", 0),
        ("wrong_role", 0),
        ("wrong_location", 1),
        ("wrong_email", 1),
    ],
)
def test_fallback_never_attaches_mismatched_contact(
    failure_kind: str, expected_profile_calls: int
) -> None:
    calls: list[str] = []
    search_calls = 0
    position = deepcopy(_profile()["currentPosition"][0])
    profile = _profile()
    if failure_kind == "wrong_company":
        position.update(
            {
                "companyName": "Other Company",
                "companyDomain": "other.example",
                "companyLinkedinUrl": "https://www.linkedin.com/company/other/",
            }
        )
    elif failure_kind == "wrong_role":
        position["title"] = "Sales Manager"
    elif failure_kind == "wrong_location":
        profile["location"] = {
            "countryCode": "GB",
            "parsed": {"countryFull": "United Kingdom", "city": "London"},
        }
    elif failure_kind == "wrong_email":
        profile["workEmail"] = ""

    def provider(tool: str, _payload: dict) -> object:
        nonlocal search_calls
        calls.append(tool)
        if tool == "harvestapi_search_leads":
            search_calls += 1
            if search_calls == 1:
                return {"data": {"elements": [], "status": "OK"}}
            return {
                "data": {
                    "elements": [
                        {
                            "linkedinUrl": "https://www.linkedin.com/in/candidate/",
                            "currentPositions": [position],
                        }
                    ],
                    "status": "OK",
                }
            }
        if tool == "harvestapi_get_profile":
            return {"data": {"element": profile}}
        raise AssertionError(f"unexpected provider tool: {tool}")

    original = _company()
    assert enrich_contacts(_icp(), [original], provider) == [original]
    assert calls.count("harvestapi_search_leads") == 2
    assert calls.count("harvestapi_get_profile") == expected_profile_calls


def test_empty_search_without_safe_function_terms_does_not_retry() -> None:
    calls: list[tuple[str, dict]] = []

    def provider(tool: str, payload: dict) -> object:
        calls.append((tool, deepcopy(payload)))
        return {"data": {"elements": [], "status": "OK"}}

    original = _company()
    icp = _icp(
        target_roles=["Chief Executive Officer"], target_seniority="C-level"
    )

    assert enrich_contacts(icp, [original], provider) == [original]
    assert len(calls) == 1


def test_fallback_deduplicates_function_terms_from_known_seniority_titles() -> None:
    calls: list[tuple[str, dict]] = []

    def provider(tool: str, payload: dict) -> object:
        calls.append((tool, deepcopy(payload)))
        return {"data": {"elements": [], "status": "OK"}}

    original = _company()
    icp = _icp(
        target_roles=["Chief Revenue Officer", "Vice President of Revenue"],
        target_seniority="VP+",
    )

    assert enrich_contacts(icp, [original], provider) == [original]
    assert [payload["currentJobTitles"] for _tool, payload in calls] == [
        "Chief Revenue Officer,Vice President of Revenue",
        "revenue",
    ]


def test_nonempty_first_search_does_not_retry() -> None:
    provider = ScriptedProvider()

    companies = enrich_contacts(_icp(), [_company()], provider)

    assert companies[0]["contact"]["email"] == "ada@acme.com"
    assert [tool for tool, _payload in provider.calls].count(
        "harvestapi_search_leads"
    ) == 1


def test_empty_fallback_search_runs_at_most_once() -> None:
    calls: list[tuple[str, dict]] = []

    def provider(tool: str, payload: dict) -> object:
        calls.append((tool, deepcopy(payload)))
        return {"data": {"elements": [], "status": "OK"}}

    original = _company()
    assert enrich_contacts(_icp(), [original], provider) == [original]
    assert [payload["currentJobTitles"] for _tool, payload in calls] == [
        "Vice President of Sales",
        "sales",
    ]


@pytest.mark.parametrize(
    "response",
    [
        {
            "ok": False,
            "error": "provider unavailable",
            "data": {"elements": []},
        },
        {"data": {"elements": [], "status": 500}},
        {"data": {"elements": [], "status": []}},
        {"data": {"elements": [], "status": {}}},
        {
            "ok": False,
            "error": "provider unavailable",
            "data": {"elements": [], "status": "OK"},
        },
    ],
)
def test_error_response_with_empty_collection_does_not_retry(response: dict) -> None:
    calls = 0

    def provider(_tool: str, _payload: dict) -> object:
        nonlocal calls
        calls += 1
        return response

    original = _company()
    assert enrich_contacts(_icp(), [original], provider) == [original]
    assert calls == 1


@pytest.mark.parametrize(
    "message",
    [
        "provider call limit exhausted",
        "provider cost limit exhausted",
        "contact provider deadline reached",
    ],
)
def test_fallback_limit_exception_keeps_company_without_contact(message: str) -> None:
    calls = 0

    def provider(_tool: str, _payload: dict) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"data": {"elements": [], "status": "OK"}}
        raise RuntimeError(message)

    original = _company()
    assert enrich_contacts(_icp(), [original], provider) == [original]
    assert calls == 2

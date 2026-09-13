from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from experiments.harness_bakeoff.contacts import (
    _company_name,
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

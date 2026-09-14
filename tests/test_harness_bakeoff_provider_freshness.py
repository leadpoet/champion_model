from __future__ import annotations

from datetime import date
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from experiments.harness_bakeoff import providers


class ProviderFreshnessTests(unittest.TestCase):
    def _tools(self, **kwargs) -> providers.LiveProviderTools:
        return providers.LiveProviderTools(
            deepline_api_key="deepline-test",
            scrapingdog_api_key="scrapingdog-test",
            **kwargs,
        )

    def test_environment_date_anchors_provider_recency(self) -> None:
        with patch.dict(
            os.environ,
            {"BAKEOFF_EVALUATION_DATE": "2026-09-02"},
            clear=False,
        ):
            tools = self._tools()

        with patch.object(
            tools, "_scrapingdog_search", return_value=[]
        ) as scrapingdog_search:
            with patch.object(tools, "_exa_search", return_value=[]) as exa_search:
                tools.search_web(
                    {
                        "query": "product launch",
                        "mode": "news",
                        "limit": 5,
                        "recency_days": 30,
                    }
                )

        scrapingdog_search.assert_called_once_with(
            "product launch after:2026-08-03", 5, "news"
        )
        exa_search.assert_called_once_with("product launch", 5, "news", "2026-08-03")

    def test_explicit_run_date_overrides_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"BAKEOFF_EVALUATION_DATE": "2025-01-01"},
            clear=False,
        ):
            tools = self._tools(evaluation_date="2026-09-02")

        self.assertEqual(tools.evaluation_date, date(2026, 9, 2))

    def test_news_default_window_uses_the_evaluation_date(self) -> None:
        tools = self._tools(evaluation_date="2026-09-02")
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"news_results": []},
        )

        with patch.object(providers.httpx, "get", return_value=response) as request:
            tools._scrapingdog_search("product launch", 1, "news")

        self.assertEqual(
            request.call_args.kwargs["params"]["query"],
            "product launch after:2025-09-02",
        )

    def test_invalid_environment_date_falls_back_to_utc(self) -> None:
        fixed_utc_day = date(2027, 4, 9)

        class FixedDateTime:
            @staticmethod
            def now(_timezone):
                class Current:
                    @staticmethod
                    def date():
                        return fixed_utc_day

                return Current()

        with patch.dict(
            os.environ,
            {"BAKEOFF_EVALUATION_DATE": "not-a-date"},
            clear=False,
        ):
            with patch.object(providers, "datetime", FixedDateTime):
                self.assertEqual(providers._evaluation_day(), fixed_utc_day)

    def test_standalone_discovery_uses_hunter_headcount_bands(self) -> None:
        tools = self._tools()

        with patch.object(
            tools,
            "_deepline",
            return_value={"data": {"data": []}},
        ) as deepline:
            tools.search_companies(
                {
                    "query": "software",
                    "employee_count": [
                        "2-10",
                        "501–1,000",
                        "not-a-band",
                    ],
                }
            )

        self.assertEqual(
            deepline.call_args.args[1]["headcount"],
            ["1-10", "501-1000"],
        )

    def test_standalone_discovery_labels_only_numeric_counts_as_estimates(self) -> None:
        tools = self._tools()
        source_rows = [
            {
                "organization": "Numeric",
                "domain": "numeric.example",
                "headcount": 13.0,
            },
            {
                "organization": "Numeric string",
                "domain": "string.example",
                "employee_count": "200",
            },
            {
                "organization": "Comma",
                "domain": "comma.example",
                "employee_count": "1,453",
            },
            {
                "organization": "Decimal",
                "domain": "decimal.example",
                "employee_count": "45.0",
            },
            {
                "organization": "Band",
                "domain": "band.example",
                "employee_count": "11-50",
            },
            {
                "organization": "Unknown",
                "domain": "unknown.example",
                "employee_count": None,
            },
        ]

        with patch.object(
            tools,
            "_deepline",
            return_value={"data": {"data": source_rows}},
        ):
            result = tools.search_companies({"query": "software", "limit": 6})

        companies = result["companies"]
        self.assertEqual(companies[0]["employee_count_estimate"], 13.0)
        self.assertEqual(companies[1]["employee_count_estimate"], "200")
        self.assertEqual(companies[2]["employee_count_estimate"], "1,453")
        self.assertEqual(companies[3]["employee_count_estimate"], "45.0")
        self.assertEqual(companies[4]["employee_count"], "11-50")
        self.assertNotIn("employee_count", companies[0])
        self.assertNotIn("employee_count_estimate", companies[4])
        self.assertNotIn("employee_count", companies[5])
        self.assertNotIn("employee_count_estimate", companies[5])
        self.assertEqual(
            [
                row["employee_count"]
                if "employee_count" in row
                else row["headcount"]
                for row in source_rows
            ],
            [13.0, "200", "1,453", "45.0", "11-50", None],
        )

    def test_standalone_profile_uses_one_lookup_and_leaves_funding_explicit(self) -> None:
        tools = self._tools()

        def execute(tool, _payload, **_kwargs):
            if tool == "free_simple_company_search":
                return {
                    "data": {
                        "rows": [
                            {
                                "domain": "example.com",
                                "company_name": "Example",
                                "employee_count": "11-50",
                            }
                        ]
                    }
                }
            self.assertEqual(tool, "predictleads_company_financing_events")
            return {
                "data": {
                    "data": [
                        {
                            "type": "financing_event",
                            "attributes": {
                                "financing_type": "Series B",
                                "found_at": "2026-06-23T11:00:00+02:00",
                            },
                        },
                        {
                            "type": "financing_event",
                            "attributes": {
                                "financing_type": "Series A",
                                "found_at": "2024-10-24T08:36:02+02:00",
                            },
                        },
                    ]
                }
            }

        with patch.object(tools, "_deepline", side_effect=execute) as deepline:
            profile = tools.get_company_profile({"domain": "example.com"})
            self.assertEqual(profile["company"]["employee_count"], "11-50")
            self.assertNotIn("latest_financing_events", profile)
            self.assertNotIn("linkedin_profile_evidence", profile)
            self.assertEqual(profile["errors"], [])
            self.assertEqual(deepline.call_count, 1)

            events = tools.get_company_events(
                {"domain": "example.com", "categories": ["FUNDING"], "limit": 3}
            )
        self.assertEqual(
            [
                item["attributes"]["financing_type"]
                for item in events["events"][0]["data"]["items"]
            ],
            ["Series B", "Series A"],
        )
        self.assertEqual(deepline.call_count, 2)

    def test_standalone_profile_bounds_lookup_failure(self) -> None:
        for status_code in (429, 502):
            with self.subTest(status_code=status_code):
                tools = self._tools()

                def execute(tool, _payload, **_kwargs):
                    if tool == "free_simple_company_search":
                        raise RuntimeError(
                            f"HTTP {status_code} token=secret-provider-detail"
                        )
                    self.fail(f"unexpected tool: {tool}")

                with patch.object(tools, "_deepline", side_effect=execute) as deepline:
                    profile = tools.get_company_profile({"domain": "example.com"})

                self.assertEqual(deepline.call_count, 1)
                self.assertEqual(profile["company"], {})
                self.assertEqual(
                    profile["errors"],
                    [
                        {
                            "source": "free_simple_company_search",
                            "error": f"profile lookup failed: HTTP {status_code}",
                        }
                    ],
                )
                self.assertNotIn("secret-provider-detail", json.dumps(profile))

    def test_standalone_profile_rejects_malformed_lookup(self) -> None:
        malformed_payloads = (
            {"result": {"error": "provider failure", "data": {"rows": []}}},
            {"data": {"rows": "not-a-list"}},
            {"data": {"rows": [{"domain": "example.com"}, "bad"]}},
        )
        for malformed_payload in malformed_payloads:
            with self.subTest(payload=malformed_payload):
                tools = self._tools()
                with patch.object(
                    tools, "_deepline", return_value=malformed_payload
                ) as deepline:
                    profile = tools.get_company_profile({"domain": "example.com"})

                self.assertEqual(deepline.call_count, 1)
                self.assertEqual(profile["company"], {})
                self.assertEqual(profile["errors"], [
                    {
                        "source": "free_simple_company_search",
                        "error": "profile lookup failed: ValueError",
                    }
                ])

    def test_standalone_profile_bounds_lookup_error(self) -> None:
        tools = self._tools()

        with patch.object(
            tools,
            "_deepline",
            side_effect=RuntimeError("HTTP 502 token=secret-provider-detail"),
        ) as deepline:
            profile = tools.get_company_profile({"domain": "example.com"})

        self.assertEqual(deepline.call_count, 1)
        self.assertEqual(profile["company"], {})
        self.assertEqual(
            profile["errors"],
            [
                {
                    "source": "free_simple_company_search",
                    "error": "profile lookup failed: HTTP 502",
                },
            ],
        )
        self.assertNotIn("secret-provider-detail", json.dumps(profile))

    def test_standalone_profile_preserves_provider_run_limits(self) -> None:
        for error in (
            TimeoutError("attempt deadline exhausted"),
            RuntimeError("provider call limit exhausted"),
            RuntimeError("provider cost limit exhausted"),
        ):
            with self.subTest(error=error):
                tools = self._tools()
                with patch.object(tools, "_deepline", side_effect=error) as deepline:
                    with self.assertRaises(type(error)):
                        tools.get_company_profile({"domain": "example.com"})
                self.assertEqual(deepline.call_count, 1)

    def test_standalone_profile_falls_back_when_structured_size_is_missing(self) -> None:
        tools = self._tools()
        source_row = {
            "domain": "example.com",
            "company_name": "Example",
            "linkedin_url": "linkedin.com/company/example",
            "employee_count": 89,
        }

        def execute(tool, payload, **kwargs):
            if tool == "free_simple_company_search":
                return {"data": {"rows": [source_row]}}
            if tool == "predictleads_company_financing_events":
                return {"data": {"data": []}}
            if tool == "harvestapi_get_company":
                self.assertEqual(
                    payload, {"url": "https://linkedin.com/company/example"}
                )
                self.assertEqual(kwargs["fallback_cost"], 0.003)
                return {
                    "status": "completed",
                    "data": {
                        "status": 200,
                        "element": {
                            "website": "example.com",
                            "linkedinUrl": "linkedin.com/company/example",
                            "locations": [
                                {
                                    "headquarter": True,
                                    "parsed": {"text": "Boston, Massachusetts"},
                                }
                            ],
                        },
                    },
                }
            self.assertEqual(tool, "exa_contents")
            self.assertEqual(
                payload,
                {
                    "urls": ["https://linkedin.com/company/example"],
                    "text": {"maxCharacters": 4_000},
                    "maxAgeHours": 0,
                    "livecrawlTimeout": 20_000,
                },
            )
            self.assertEqual(kwargs["fallback_cost"], 0.002)
            return {
                "data": {
                    "results": [
                        {
                            "url": "https://linkedin.com/company/example/",
                            "title": "Another display name | LinkedIn",
                            "text": (
                                "## About\nCompany size 51-200 employees\n"
                                "Headquarters Boston, Massachusetts\n"
                                "94 associated members\n## Updates"
                            ),
                        }
                    ]
                }
            }

        with patch.object(tools, "_deepline", side_effect=execute) as deepline:
            profile = tools.get_company_profile({"domain": "example.com"})

        self.assertEqual(deepline.call_count, 3)
        self.assertEqual(profile["company"]["employee_count_estimate"], 89)
        self.assertEqual(
            profile["company"]["linkedin_url"], "linkedin.com/company/example"
        )
        self.assertNotIn("employee_count", profile["company"])
        self.assertEqual(
            profile["linkedin_profile_evidence"],
            {
                "url": "https://linkedin.com/company/example/",
                "title": "Another display name | LinkedIn",
                "employee_count": "51-200",
                "quote": "Company size 51-200 employees",
                "listed_headquarters": "Boston, Massachusetts",
                "headquarters_quote": "Headquarters Boston, Massachusetts",
            },
        )
        self.assertEqual(
            profile["linkedin_structured_evidence"],
            {
                "provider": "harvestapi_get_company",
                "linkedin_url": "https://linkedin.com/company/example",
                "website": "https://example.com/",
                "headquarters": "Boston, Massachusetts",
                "headquarters_source_field": (
                    "locations[headquarter=true].parsed.text"
                ),
            },
        )
        self.assertEqual(profile["errors"], [])
        self.assertEqual(source_row["employee_count"], 89)

    def test_standalone_profile_skips_exa_for_complete_structured_proof(self) -> None:
        tools = self._tools()
        calls: list[str] = []

        def execute(tool, payload, **kwargs):
            calls.append(tool)
            if tool == "free_simple_company_search":
                return {
                    "data": {
                        "rows": [
                            {
                                "domain": "example.com",
                                "company_name": "Example",
                                "linkedin_url": "linkedin.com/company/example",
                            }
                        ]
                    }
                }
            if tool == "predictleads_company_financing_events":
                return {"data": {"data": []}}
            self.assertEqual(tool, "harvestapi_get_company")
            self.assertEqual(
                payload, {"url": "https://linkedin.com/company/example"}
            )
            self.assertEqual(kwargs["fallback_cost"], 0.003)
            return {
                "status": "completed",
                "data": {
                    "status": 200,
                    "element": {
                        "name": "Example",
                        "website": "example.com",
                        "linkedinUrl": "linkedin.com/company/example",
                        "employeeCountRange": {"start": 11, "end": 50},
                        "locations": [
                            {
                                "headquarter": True,
                                "parsed": {"text": "Austin, Texas"},
                            }
                        ],
                    },
                },
            }

        with patch.object(tools, "_deepline", side_effect=execute):
            profile = tools.get_company_profile({"domain": "example.com"})

        self.assertEqual(
            calls,
            [
                "free_simple_company_search",
                "harvestapi_get_company",
            ],
        )
        self.assertNotIn("linkedin_profile_evidence", profile)
        self.assertEqual(
            profile["linkedin_structured_evidence"]["employee_count"], "11-50"
        )
        self.assertEqual(
            profile["linkedin_structured_evidence"]["headquarters"],
            "Austin, Texas",
        )
        self.assertEqual(profile["errors"], [])

    def test_standalone_profile_uses_structured_fallback_when_page_fails(self) -> None:
        tools = self._tools()

        def execute(tool, payload, **kwargs):
            if tool == "free_simple_company_search":
                return {
                    "data": {
                        "rows": [
                            {
                                "domain": "example.com",
                                "company_name": "Example",
                                "linkedin_url": (
                                    "https://www.linkedin.com/company/example"
                                ),
                            }
                        ]
                    }
                }
            if tool == "predictleads_company_financing_events":
                return {
                    "data": {
                        "data": [
                            {
                                "type": "financing_event",
                                "attributes": {"financing_type": "Series A"},
                            }
                        ]
                    }
                }
            if tool == "exa_contents":
                raise RuntimeError("provider unavailable")
            self.assertEqual(tool, "harvestapi_get_company")
            self.assertEqual(
                payload, {"url": "https://www.linkedin.com/company/example"}
            )
            self.assertEqual(kwargs["fallback_cost"], 0.003)
            return {
                "status": "completed",
                "data": {
                    "status": 200,
                    "element": {
                        "name": "Example",
                        "website": "example.com",
                        "linkedinUrl": "linkedin.com/company/example",
                        "employeeCount": 6,
                        "employeeCountRange": {"start": 2, "end": 10},
                        "followerCount": 168,
                    }
                },
            }

        with patch.object(tools, "_deepline", side_effect=execute) as deepline:
            profile = tools.get_company_profile({"domain": "example.com"})

        self.assertEqual(deepline.call_count, 3)
        self.assertEqual(profile["company"]["company_name"], "Example")
        self.assertNotIn("latest_financing_events", profile)
        self.assertNotIn("linkedin_profile_evidence", profile)
        self.assertEqual(
            profile["linkedin_structured_evidence"],
            {
                "provider": "harvestapi_get_company",
                "linkedin_url": "https://linkedin.com/company/example",
                "website": "https://example.com/",
                "company_name": "Example",
                "employee_count": "2-10",
                "employee_count_source_field": "employeeCountRange",
            },
        )
        self.assertEqual(
            profile["errors"],
            [
                {
                    "source": "linkedin_profile_evidence",
                    "error": "profile fetch failed: RuntimeError",
                }
            ],
        )

    def test_standalone_profile_rejects_invalid_linkedin_without_fetch(self) -> None:
        tools = self._tools()

        def execute(tool, _payload, **_kwargs):
            if tool == "free_simple_company_search":
                return {
                    "data": {
                        "rows": [
                            {
                                "domain": "example.com",
                                "company_name": "Example",
                                "linkedin_url": "https://linkedin.com/in/example",
                            }
                        ]
                    }
                }
            self.fail(f"unexpected tool: {tool}")

        with patch.object(tools, "_deepline", side_effect=execute) as deepline:
            profile = tools.get_company_profile({"domain": "example.com"})

        self.assertEqual(deepline.call_count, 1)
        self.assertNotIn("linkedin_profile_evidence", profile)
        self.assertEqual(
            profile["errors"],
            [
                {
                    "source": "linkedin_profile_evidence",
                    "error": "stored LinkedIn profile URL is invalid",
                }
            ],
        )

    def test_standalone_profile_labels_numeric_count_and_ignores_boolean(self) -> None:
        for value, expected in ((45, 45), (True, None)):
            with self.subTest(value=value):
                tools = self._tools()
                source_row = {
                    "domain": "example.com",
                    "company_name": "Example",
                    "employee_count": value,
                }

                def execute(tool, _payload, **_kwargs):
                    if tool == "free_simple_company_search":
                        return {"data": {"rows": [source_row]}}
                    return {"data": {"data": []}}

                with patch.object(tools, "_deepline", side_effect=execute):
                    profile = tools.get_company_profile({"domain": "example.com"})

                if expected is None:
                    self.assertNotIn("employee_count_estimate", profile["company"])
                else:
                    self.assertEqual(
                        profile["company"]["employee_count_estimate"], expected
                    )
                self.assertNotIn("employee_count", profile["company"])
                self.assertIs(source_row["employee_count"], value)

    def test_standalone_profile_does_not_fetch_financing(self) -> None:
        tools = self._tools()

        def execute(tool, _payload, **_kwargs):
            if tool == "free_simple_company_search":
                return {
                    "data": {
                        "rows": [
                            {"domain": "example.com", "company_name": "Example"}
                        ]
                    }
                }
            raise RuntimeError("provider unavailable")

        with patch.object(tools, "_deepline", side_effect=execute):
            profile = tools.get_company_profile({"domain": "example.com"})

        self.assertEqual(profile["company"]["company_name"], "Example")
        self.assertNotIn("latest_financing_events", profile)
        self.assertEqual(profile["errors"], [])

    def test_standalone_profile_accounts_for_one_lookup(self) -> None:
        tools = self._tools()
        responses = [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"data": {"rows": [{"domain": "example.com"}]}}
                ),
                stderr="",
            ),
        ]

        with patch.object(providers.subprocess, "run", side_effect=responses):
            tools.get_company_profile({"domain": "example.com"})

        self.assertEqual(
            [call["tool"] for call in tools.stats.calls],
            ["free_simple_company_search"],
        )
        self.assertEqual(tools.stats.estimated_cost_usd, 0.0)

    def test_standalone_profile_uses_supplied_linkedin_without_lookup(self) -> None:
        tools = self._tools()

        def execute(tool, payload, **kwargs):
            self.assertEqual(tool, "harvestapi_get_company")
            self.assertEqual(
                payload, {"url": "https://www.linkedin.com/company/example/"}
            )
            self.assertEqual(kwargs["fallback_cost"], 0.003)
            return {
                "status": "completed",
                "data": {
                    "status": 200,
                    "element": {
                        "name": "Example",
                        "website": "example.com",
                        "linkedinUrl": "linkedin.com/company/example",
                        "employeeCountRange": {"start": 11, "end": 50},
                        "locations": [
                            {
                                "headquarter": True,
                                "parsed": {"text": "Austin, Texas"},
                            }
                        ],
                    }
                }
            }

        with patch.object(tools, "_deepline", side_effect=execute) as deepline:
            profile = tools.get_company_profile(
                {
                    "domain": "example.com",
                    "company_linkedin": "https://www.linkedin.com/company/example/",
                }
            )

        self.assertEqual(deepline.call_count, 1)
        self.assertEqual(profile["company"], {})
        self.assertEqual(
            profile["linkedin_structured_evidence"]["employee_count"], "11-50"
        )
        self.assertEqual(
            profile["linkedin_structured_evidence"]["headquarters"], "Austin, Texas"
        )

    def test_standalone_company_events_matches_job_filter_and_description_contract(self) -> None:
        tools = self._tools()
        event = {
            "type": "job_opening",
            "attributes": {
                "title": "Revenue Operations Lead",
                "description": "<b>Own</b> revenue systems &amp; reporting." + (" x" * 600),
                "url": "https://jobs.example.com/revenue-operations",
                "posted_at": "2026-09-02T10:00:00Z",
                "first_seen_at": "2026-09-02T10:01:00Z",
                "last_seen_at": "2026-09-05T10:00:00Z",
                "status": "open",
            },
        }

        with patch.object(
            tools, "_deepline", return_value={"data": {"data": [event]}}
        ) as deepline:
            result = tools.get_company_events(
                {
                    "domain": "example.com",
                    "categories": ["HIRING"],
                    "job_category": "operations",
                }
            )

        self.assertEqual(
            deepline.call_args.args,
            (
                "predictleads_company_job_openings",
                {
                    "company_id_or_domain": "example.com",
                    "page": 1,
                    "limit": 5,
                    "active_only": True,
                    "not_closed": True,
                    "categories": ["operations"],
                },
            ),
        )
        attributes = result["events"][0]["data"]["items"][0]["attributes"]
        self.assertEqual(len(attributes["description"]), 1_000)
        self.assertTrue(attributes["description"].startswith("Own revenue systems"))
        self.assertNotIn("<", attributes["description"])
        self.assertEqual(attributes["url"], event["attributes"]["url"])
        self.assertEqual(attributes["posted_at"], event["attributes"]["posted_at"])
        self.assertEqual(
            attributes["first_seen_at"], event["attributes"]["first_seen_at"]
        )
        self.assertEqual(attributes["last_seen_at"], event["attributes"]["last_seen_at"])
        self.assertEqual(attributes["status"], "open")

    def test_standalone_company_events_omits_and_validates_job_filter(self) -> None:
        tools = self._tools()
        with patch.object(
            tools, "_deepline", return_value={"data": {"data": []}}
        ) as deepline:
            tools.get_company_events(
                {"domain": "example.com", "categories": ["HIRING"]}
            )
        self.assertNotIn("categories", deepline.call_args.args[1])

        for malformed in (["sales"], "not_a_provider_category", 42):
            with self.subTest(malformed=malformed):
                with patch.object(tools, "_deepline") as deepline:
                    with self.assertRaisesRegex(ValueError, "job_category"):
                        tools.get_company_events(
                            {
                                "domain": "example.com",
                                "categories": ["HIRING"],
                                "job_category": malformed,
                            }
                        )
                    deepline.assert_not_called()


if __name__ == "__main__":
    unittest.main()

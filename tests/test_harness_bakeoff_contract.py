from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import importlib
import inspect
from io import StringIO
import json
import unittest
from unittest.mock import patch

import harness
from pydantic import ValidationError

from experiments.harness_bakeoff.models import (
    CompanyResult,
    IntentDetailsCompanyResult,
    companies_result_model,
    company_list_json_schema,
    normalize_icp,
    validate_companies,
)
from experiments.harness_bakeoff.prompt import build_prompt
from experiments.harness_bakeoff.tool_contract import (
    COMPANY_EVENT_CATEGORIES,
    PREDICTLEADS_JOB_CATEGORIES,
    tool_input_schema,
)
from experiments.harness_bakeoff import worker
from experiments.harness_bakeoff.worker import MODULES


class HarnessContractTests(unittest.TestCase):
    @staticmethod
    def _intent_details_company() -> dict:
        return {
            "company_name": "Example",
            "company_website": "https://example.com/",
            "company_linkedin": "https://www.linkedin.com/company/example/",
            "industry": "Software",
            "employee_count": "51-200",
            "company_stage": "Series A",
            "country": "United States",
            "state": "California",
            "intent_details": (
                "Example launched its payments API on 20 August 2026, which could increase "
                "the operational work needed to support integrations. It also announced a "
                "bank partnership, although the announcement did not give an effective date; "
                "that relationship could broaden settlement coverage. Together, this activity "
                "suggests Example is expanding the infrastructure behind its merchant payments service."
            ),
            "intent_signals": [
                {
                    "matched_icp_signal": 0,
                    "description": "Example launched its payments API.",
                    "date": "2026-08-20",
                    "url": "https://example.com/news/api-launch",
                },
                {
                    "matched_icp_signal": 1,
                    "description": "Example announced a bank partnership.",
                    "date": None,
                    "url": "https://example.com/news/bank-partnership",
                },
            ],
            "required_attribute": None,
        }


    def test_company_events_exposes_only_provider_native_job_filter(self) -> None:
        schema = tool_input_schema("get_company_events")

        self.assertNotIn("query", schema["properties"])
        self.assertEqual(
            schema["properties"]["job_category"]["enum"],
            list(PREDICTLEADS_JOB_CATEGORIES),
        )
        self.assertFalse(schema["additionalProperties"])

    def test_company_event_categories_match_both_provider_mappings(self) -> None:
        from arena_transport import _EVENT_TOOLS as arena_event_tools
        from experiments.harness_bakeoff.providers import (
            _EVENT_TOOLS as local_event_tools,
        )

        event_enum = tool_input_schema("get_company_events")["properties"][
            "categories"
        ]["items"]["enum"]

        self.assertEqual(event_enum, list(COMPANY_EVENT_CATEGORIES))
        self.assertEqual(set(event_enum), set(arena_event_tools))
        self.assertEqual(set(event_enum), set(local_event_tools))
        self.assertNotIn("PRODUCT_LAUNCHES", event_enum)

    def test_selected_harness_exposes_run_icp(self) -> None:
        for arm, module_name in MODULES.items():
            with self.subTest(arm=arm):
                module = importlib.import_module(module_name)
                self.assertEqual(
                    list(inspect.signature(module.run_icp).parameters), ["icp"]
                )

    def test_top_level_entrypoint_exports_selected_harness(self) -> None:
        selected = importlib.import_module(MODULES["pydantic_ai"])

        self.assertEqual(MODULES["pydantic_ai"], "harness")
        self.assertIs(harness.run_icp, selected.run_icp)
        self.assertEqual(list(inspect.signature(harness.run_icp).parameters), ["icp"])
        self.assertIsInstance(harness.get_last_usage(), dict)

    def test_worker_calls_the_public_run_icp_boundary(self) -> None:
        icp = {"icp_id": "icp_today"}
        output = StringIO()

        with patch.object(harness, "run_icp", return_value=[]) as run_icp:
            with patch("sys.stdin", StringIO(json.dumps(icp))):
                with redirect_stdout(output):
                    returncode = worker.main(["pydantic_ai"])

        self.assertEqual(returncode, 0)
        run_icp.assert_called_once_with(icp)
        payload = json.loads(output.getvalue().removeprefix(worker.SENTINEL))
        self.assertEqual(payload["companies"], [])

    def test_company_output_round_trip(self) -> None:
        company = CompanyResult.model_validate(
            {
                "company_name": "Example",
                "company_website": "https://example.com",
                "company_linkedin": "",
                "industry": "Software",
                "employee_count": "51-200",
                "company_stage": "Series A",
                "country": "United States",
                "state": "California",
                "fit_summary": "Matches the example ICP.",
                "fit_evidence_urls": ["https://example.com/about"],
                "intent_signals": [
                    {
                        "matched_icp_signal": 0,
                        "description": "A current product event.",
                        "date": "2026-08-20",
                        "why_now": "The event gives a timely contact reason.",
                        "url": "https://example.com/news/event",
                        "snippet": "Example launched the product.",
                    }
                ],
                "required_attribute": {
                    "text": "Uses a product-led sales motion",
                    "passed": True,
                    "evidence_url": "https://example.com/about",
                    "evidence_quote": "Customers can start with a self-service plan.",
                    "explanation": "The self-service plan supports the required motion.",
                },
            }
        )
        dumped = company.model_dump(mode="json")

        self.assertEqual(
            CompanyResult.model_validate(dumped).model_dump(mode="json"), dumped
        )
        self.assertEqual(validate_companies([dumped]), [dumped])

    def test_intent_details_policy_uses_only_the_new_authored_fields(self) -> None:
        policy = "intent_details_v1"
        company = self._intent_details_company()

        validated = validate_companies(
            [company], intent_details_policy=policy
        )[0]
        schema = company_list_json_schema(policy)["items"]

        self.assertEqual(validated, company)
        self.assertEqual(validated["intent_details"], company["intent_details"])
        self.assertIsNone(validated["intent_signals"][1]["date"])
        self.assertNotIn("fit_summary", schema["properties"])
        self.assertNotIn("fit_evidence_urls", schema["properties"])
        signal_properties = schema["properties"]["intent_signals"]["items"][
            "properties"
        ]
        self.assertNotIn("snippet", signal_properties)
        self.assertNotIn("why_now", signal_properties)
        self.assertEqual(
            companies_result_model(policy).model_fields["companies"].annotation,
            list[IntentDetailsCompanyResult],
        )

    def test_intent_details_policy_rejects_blank_or_nonparagraph_prose(self) -> None:
        for invalid in ("", "   ", "First paragraph.\n\nSecond paragraph.", ["text"],
                        "- A signal\n- Another signal", "# Heading", "```prose```",
                        "Hidden\u200bword", "x" * 2001):
            company = {**self._intent_details_company(), "intent_details": invalid}
            with self.subTest(intent_details=invalid):
                with self.assertRaises(ValidationError):
                    validate_companies(
                        [company], intent_details_policy="intent_details_v1"
                    )

    def test_intent_details_normalizes_wrapping_without_rewriting(self) -> None:
        company = {**self._intent_details_company(), "intent_details": " An event occurred.\nIt could support expansion. "}
        validated = validate_companies([company], intent_details_policy="intent_details_v1")[0]
        self.assertEqual(validated["intent_details"], "An event occurred. It could support expansion.")

    def test_legacy_policy_keeps_the_exact_historical_shape(self) -> None:
        legacy = {
            "company_name": "Example",
            "company_website": "https://example.com",
            "company_linkedin": "",
            "industry": "Software",
            "employee_count": "51-200",
            "company_stage": "Series A",
            "country": "United States",
            "state": "California",
            "fit_summary": "Matches the example ICP.",
            "fit_evidence_urls": ["https://example.com/about"],
            "intent_signals": [
                {
                    "matched_icp_signal": 0,
                    "description": "A current product event.",
                    "date": "2026-08-20",
                    "why_now": "The event gives a timely contact reason.",
                    "url": "https://example.com/news/event",
                    "snippet": "Example launched the product.",
                }
            ],
            "required_attribute": None,
        }

        dumped = CompanyResult.model_validate(legacy).model_dump(mode="json")
        self.assertEqual(validate_companies([legacy]), [dumped])
        self.assertEqual(
            list(dumped),
            [
                "company_name",
                "company_website",
                "company_linkedin",
                "industry",
                "employee_count",
                "company_stage",
                "country",
                "state",
                "fit_summary",
                "fit_evidence_urls",
                "intent_signals",
                "required_attribute",
            ],
        )
        with self.assertRaises(ValidationError):
            validate_companies(
                [self._intent_details_company()],
                intent_details_policy="unknown_policy",
            )

    def test_intent_details_policy_keeps_contact_enrichment_shape(self) -> None:
        company = {
            **self._intent_details_company(),
            "contact": {
                "full_name": "Avery Lee",
                "role": "Chief Financial Officer",
                "linkedin_url": "https://www.linkedin.com/in/avery-lee/",
                "location": {"country": "US", "region": "California"},
                "email": "avery@example.com",
                "email_source": {
                    "provider": "harvestapi",
                    "tool": "harvestapi_get_profile",
                    "record_id": "profile-1",
                },
            },
        }

        validated = validate_companies(
            [company],
            allow_contacts=True,
            intent_details_policy="intent_details_v1",
        )[0]

        self.assertEqual(validated, company)

    def test_intent_details_prompt_requires_authored_grounded_synthesis(self) -> None:
        prompt = build_prompt(
            {
                "intent_details_policy": "intent_details_v1",
                "product_service": "merchant payments service",
                "intent_signals": ["Product launch", "Partnership"],
            }
        )

        self.assertIn("one concise, natural paragraph", prompt)
        self.assertIn("every distinct supported signal", prompt)
        self.assertIn("Use null for a signal date", prompt)
        self.assertIn("often the target company's own offering", prompt)
        self.assertIn("Do not invent buying intent, urgency, budget", prompt)
        self.assertNotIn("Include the size source URL in fit_evidence_urls", prompt)

    def test_arena_date_anchors_the_prompt(self) -> None:
        with patch.dict(
            "os.environ",
            {"LAB_ARENA_EVALUATION_DATE": "2026-09-04"},
            clear=True,
        ):
            prompt = build_prompt({"icp_id": "today"}, max_companies=1)

        self.assertTrue(prompt.startswith("Evaluation date: 2026-09-04\n"))

    def test_current_multi_intent_shape_preserves_primary_and_bonus_metadata(self) -> None:
        raw = {
            "icp_id": "today",
            "employee_count": ["51-200", "201-500"],
            "intent_signal": "Raised a growth round",
            "intent_category": "funding",
            "intent_max_age_days": 365,
            "intent_signals": [
                "Raised a growth round",
                "Announced a strategic partnership",
            ],
            "bonus_intents": [
                {
                    "intent_signal": "Announced a strategic partnership",
                    "intent_category": "partnership",
                    "intent_max_age_days": 180,
                }
            ],
        }

        normalized = normalize_icp(raw)

        self.assertEqual(
            normalized["intent_contract"],
            [
                {
                    "index": 0,
                    "signal": "Raised a growth round",
                    "category": "FUNDING",
                    "max_age_days": 365,
                    "required": True,
                },
                {
                    "index": 1,
                    "signal": "Announced a strategic partnership",
                    "category": "PARTNERSHIP",
                    "max_age_days": 180,
                    "required": False,
                },
            ],
        )
        self.assertEqual(
            normalized["required_intents"],
            [
                {
                    "signal": "Raised a growth round",
                    "category": "FUNDING",
                    "max_age_days": 365,
                }
            ],
        )
        self.assertEqual(normalized["intent_category"], "FUNDING")
        self.assertEqual(normalized["intent_max_age_days"], 365)
        self.assertEqual(normalize_icp(normalized), normalized)

    def test_explicit_required_intents_do_not_promote_bonus(self) -> None:
        normalized = normalize_icp(
            {
                "icp_id": "today",
                "employee_count": "51-200|201-500",
                "required_intents": [
                    {
                        "signal": "Raised capital",
                        "category": "funding",
                        "max_age_days": 90,
                    }
                ],
                "bonus_intents": [
                    {
                        "signal": "Opened an office",
                        "category": "market_expansion",
                        "max_age_days": 180,
                    }
                ],
            }
        )

        self.assertEqual(
            normalized["intent_signals"], ["Raised capital", "Opened an office"]
        )
        self.assertTrue(normalized["intent_contract"][0]["required"])
        self.assertFalse(normalized["intent_contract"][1]["required"])
        self.assertEqual(normalized["intent_contract"][1]["index"], 1)

    def test_prompt_projects_intents_without_mutating_or_dropping_constraints(
        self,
    ) -> None:
        raw = {
            "icp_id": "synthetic-efficiency",
            "industry": "Industrial software for regulated operators",
            "geography": "United States",
            "employee_count": ["51-200", "201-500"],
            "company_stage": "Series C+",
            "product_service": ["workflow automation", "operations analytics"],
            "required_attribute": "Supports regulated field operations",
            "custom_constraint": {
                "certifications": ["SOC 2"],
                "exclude": ["consultancies"],
            },
            "intent_signal": "Launched a generally available operations product",
            "intent_category": "PRODUCT_LAUNCH",
            "intent_max_age_days": 180,
            "intent_signals": [
                "Launched a generally available operations product",
                "Entered a new customer market",
            ],
            "intent_signal_evidence_types": [
                "PRODUCT_LAUNCH",
                "MARKET_EXPANSION",
            ],
            "intent_signal_max_age_days": [180, 365],
            "required_intents": [
                {
                    "signal": "Launched a generally available operations product",
                    "category": "PRODUCT_LAUNCH",
                    "max_age_days": 180,
                }
            ],
            "bonus_intents": [
                {
                    "signal": "Entered a new customer market",
                    "category": "MARKET_EXPANSION",
                    "max_age_days": 365,
                }
            ],
            "intent_source": "public_web",
        }
        original = deepcopy(raw)

        with patch.dict(
            "os.environ", {"BAKEOFF_EVALUATION_DATE": "2026-09-06"}, clear=True
        ):
            prompt = build_prompt(raw)
        displayed = json.loads(
            next(line for line in prompt.splitlines() if line.startswith("{"))
        )

        self.assertEqual(raw, original)
        self.assertEqual(displayed["custom_constraint"], raw["custom_constraint"])
        self.assertEqual(displayed["intent_source"], "public_web")
        self.assertEqual(
            displayed["intent_contract"],
            [
                {
                    "index": 0,
                    "signal": "Launched a generally available operations product",
                    "category": "PRODUCT_LAUNCH",
                    "max_age_days": 180,
                    "required": True,
                },
                {
                    "index": 1,
                    "signal": "Entered a new customer market",
                    "category": "MARKET_EXPANSION",
                    "max_age_days": 365,
                    "required": False,
                },
            ],
        )
        for duplicate in (
            "intent_signal",
            "intent_category",
            "intent_max_age_days",
            "intent_signals",
            "intent_signal_evidence_types",
            "intent_signal_max_age_days",
            "required_intents",
            "bonus_intents",
        ):
            self.assertNotIn(duplicate, displayed)
        self.assertLessEqual(len(prompt), 7_600)

    def test_prompt_prioritizes_primary_and_requires_event_grounding(self) -> None:
        prompt = build_prompt(
            {
                "icp_id": "today",
                "industry": "Software",
                "employee_count": ["51-200"],
                "company_stage": "Series A",
                "country": "United States",
                "product_service": "A workflow platform",
                "intent_signal": "Raised funding",
                "intent_category": "FUNDING",
                "intent_max_age_days": 365,
                "intent_signals": ["Raised funding", "Opened an office"],
                "bonus_intents": [
                    {"signal": "Opened an office", "category": "MARKET_EXPANSION"}
                ],
            }
        )

        self.assertIn("Index 0 is the required primary", prompt)
        self.assertIn("verify every required=true row before any bonus", prompt)
        self.assertIn("required=false is optional and never replaces required evidence", prompt)
        self.assertIn("Keep fit discovery separate from event verification", prompt)
        self.assertIn("one focused search_web news/jobs query", prompt)
        self.assertIn("at most two search_companies and three total candidate-finding", prompt)
        self.assertIn("loosen one discovery filter", prompt)
        self.assertIn("never loosen final fit", prompt)
        self.assertIn("before verifying a plausible candidate", prompt)
        self.assertIn("best two or three independent candidates", prompt)
        self.assertIn("Never batch a call that depends on another call's result", prompt)
        self.assertIn("Series C, Series D, or later", prompt)
        self.assertIn("Quote the fetched article body", prompt)
        self.assertIn("not snippets, navigation, or related cards", prompt)
        self.assertIn("A linked event uses its own page, URL, and date", prompt)
        self.assertIn("never crawl, update, or index dates", prompt)
        self.assertIn("state the verified event", prompt)
        self.assertIn("implication as possible", prompt)
        self.assertIn("separate sourced fact from inference", prompt)

    def test_prompt_prioritizes_untested_hits_over_rejected_domains(self) -> None:
        prompt = build_prompt({"icp_id": "candidate-priority"})

        self.assertIn("Queue distinct dated hits", prompt)
        self.assertIn("required stage and primary event", prompt)
        self.assertIn("strongest untested queued hit", prompt)
        self.assertIn("revisit only with new direct evidence", prompt)

    def test_prompt_preserves_event_status_and_avoids_sales_fabrication(self) -> None:
        prompt = build_prompt(
            {
                "icp_id": "today",
                "industry": "Data and Analytics",
                "company_stage": "Series C+",
                "product_service": ["analytics", "workflow", "security"],
                "intent_signal": "Launched a product or appointed an executive",
                "intent_category": "PRODUCT_LAUNCH",
                "intent_max_age_days": 365,
            }
        )

        displayed = json.loads(
            next(line for line in prompt.splitlines() if line.startswith("{"))
        )
        self.assertEqual(displayed["company_stage"], "Series C+")
        self.assertIn(
            "beta, preview, pilot, planned, future, or merely announced",
            prompt,
        )
        self.assertIn("distinguish announcement, effective, and start dates", prompt)
        self.assertIn("a future start is not completed", prompt)
        self.assertIn("Never copy unrelated offerings", prompt)
        self.assertIn(
            "invent procurement, budget, demand, evaluation, or purchase plans",
            prompt,
        )
        self.assertIn("avoid benchmark/scoring jargon", prompt)
        self.assertIn("product_service is what the target sells", prompt)
        self.assertIn("not the seller's pitch or a target purchase need", prompt)
        self.assertIn("event's effect on the target's operations/growth", prompt)
        self.assertIn("current majority/controlling PE ownership", prompt)
        self.assertIn("not an investment", prompt)

    def test_named_candidate_empty_site_search_uses_existing_alternate(self) -> None:
        prompt = build_prompt({"icp_id": "named-candidate-fallback"})

        self.assertIn("named candidate whose site: search is empty", prompt)
        self.assertIn("one allowed non-site alternate", prompt)
        self.assertIn("instead of a near-repeat", prompt)
        self.assertIn("not an extra call", prompt)
        self.assertIn("Verify a resulting dated hit before abandoning", prompt)
        self.assertIn("planned, future, or merely announced is not completed", prompt)

    def test_prompt_requires_current_size_fallback_linkedin_and_us_hq_state(self) -> None:
        prompt = build_prompt({"icp_id": "today"})

        self.assertNotIn("leave company_linkedin empty", prompt)
        self.assertIn(
            "Every submitted company needs a verified canonical company_linkedin",
            prompt,
        )
        self.assertIn("U.S. HQ needs a proven state", prompt)
        self.assertIn("Employee estimates from discovery/profile are shortlist clues", prompt)
        self.assertIn("never current proof", prompt)
        self.assertIn("public band or an exact current integer", prompt)
        self.assertIn("approximate, qualified, projected, historical", prompt)
        self.assertIn("snippet-only counts are insufficient", prompt)
        for exact_mapping in (
            "0 or 1 -> 0-1",
            "2-10 -> 2-10",
            "11-50 -> 11-50",
            "51-200 -> 51-200",
            "201-500 -> 201-500",
            "501-1,000 -> 501-1,000",
            "1,001-5,000 -> 1,001-5,000",
            "5,001-10,000 -> 5,001-10,000",
            "10,001 or more -> 10,001+",
        ):
            with self.subTest(exact_mapping=exact_mapping):
                self.assertIn(exact_mapping, prompt)
        self.assertIn("mapped canonical employee_count when it is listed", prompt)
        self.assertIn("never copy the requested bucket as proof", prompt)
        self.assertIn("size source URL in fit_evidence_urls", prompt)
        self.assertIn("one targeted search_web(mode='search')", prompt)
        self.assertIn("then fetch_page the best non-LinkedIn identity-bound result", prompt)
        self.assertIn("Search snippets select a URL only", prompt)
        self.assertIn("fetched page body must prove the count", prompt)
        self.assertIn("linkedin_profile_evidence fields stand alone", prompt)
        self.assertIn("never generic login/sign-up", prompt)
        self.assertIn(
            "missing employee_count/quote needs other current proof",
            prompt,
        )
        self.assertIn("listed_headquarters proves only its literal label", prompt)

    def test_expansion_uses_original_event_and_separates_planned_entry(self) -> None:
        icp = {
            "icp_id": "test-expansion",
            "intent_signal": "Entered a new operating market",
            "intent_category": "MARKET_EXPANSION",
            "intent_max_age_days": 365,
        }
        prompt = build_prompt(icp)
        self.assertIn("annual report or announcement index", prompt)
        self.assertIn("fetch the original dated announcement", prompt)
        self.assertIn("completed entry into a new geography", prompt)
        self.assertIn("non-binding MoU, plan, or added facility, asset", prompt)
        self.assertIn("capacity in an existing market is insufficient", prompt)
        self.assertIn("Verify each country separately", prompt)
        self.assertIn("unless the source explicitly connects it", prompt)
        self.assertIn("investor market alone is financing, not commercial market entry", prompt)
        self.assertIn("unless the ICP explicitly requests financing-market access", prompt)
        self.assertIn("financial-services company entering a new customer market", prompt)
        icp["intent_category"] = "FUNDING"
        self.assertNotIn("Verify each country separately", build_prompt(icp))
        self.assertNotIn("investor market alone", build_prompt(icp))
        icp["intent_category"] = "FACILITY_OPENING"
        self.assertNotIn("another facility, asset, or capacity increase", build_prompt(icp))

    def test_output_schema_guides_stage_without_narrowing_host_contract(self) -> None:
        schema = company_list_json_schema()["items"]

        self.assertNotIn("company_stage", schema["required"])
        self.assertNotIn("enum", schema["properties"]["company_stage"])
        self.assertIn(
            "Series C+",
            schema["properties"]["company_stage"]["description"],
        )

    def test_certification_guidance_requires_a_real_granted_event(self) -> None:
        icp = {
            "icp_id": "test-certification",
            "intent_signal": "Announced a certification milestone",
            "intent_category": "REGULATORY_CLEARANCE",
            "intent_max_age_days": 365,
        }
        prompt = build_prompt(icp)
        self.assertIn("actually granted to this company or product", prompt)
        self.assertIn("do not invent or require an undisclosed auditor", prompt)
        self.assertIn("A marketplace listing, partner badge", prompt)
        self.assertIn("latest funding/ownership", prompt)
        icp["intent_category"] = "FUNDING"
        self.assertNotIn("A marketplace listing, partner badge", build_prompt(icp))

    def test_funding_guidance_distinguishes_company_and_investment_vehicle(self) -> None:
        icp = {
            "icp_id": "test-funding",
            "intent_signal": "Announced a funding round",
            "intent_category": "FUNDING",
            "intent_max_age_days": 365,
        }
        prompt = build_prompt(icp)
        self.assertIn("capital raised by the target company itself", prompt)
        self.assertIn("LP commitments, assets under management", prompt)
        self.assertIn("unless the ICP explicitly requests those events", prompt)
        self.assertIn("Corporate debt or equity financing can qualify", prompt)
        self.assertIn("when the ICP does not restrict the financing type", prompt)
        icp["intent_category"] = "HIRING"
        self.assertNotIn("capital raised by the target company itself", build_prompt(icp))

    def test_event_date_and_current_hiring_source_guidance(self) -> None:
        icp = {
            "icp_id": "test-current-hiring",
            "company_stage": "Series A",
            "intent_signal": "Actively hiring integration engineers",
            "intent_category": "HIRING",
            "intent_max_age_days": 365,
        }
        prompt = build_prompt(icp)
        self.assertIn("Check current employer/ATS listings", prompt)
        self.assertIn("quote current canonical duties", prompt)
        self.assertIn("Aggregator text or a retained Apply now page", prompt)
        self.assertIn("use its stated event date", prompt)
        icp["intent_category"] = "PRODUCT_LAUNCH"
        prompt = build_prompt(icp)
        self.assertNotIn("Check current employer/ATS listings", prompt)
        self.assertIn("not a later article publication date", prompt)
        self.assertIn("neutral company-name latest-funding/ownership lookup", prompt)
        self.assertIn("without the requested stage", prompt)
        icp["company_stage"] = ""
        self.assertNotIn("neutral company-name", build_prompt(icp))

    def test_seed_requirement_does_not_silently_include_pre_seed(self) -> None:
        icp = {
            "icp_id": "test-seed-stage",
            "company_stage": "Seed",
            "intent_signal": "Announced a funding round",
            "intent_category": "FUNDING",
            "intent_max_age_days": 365,
        }
        prompt = build_prompt(icp)
        self.assertIn("Do not relabel pre-seed as Seed", prompt)
        self.assertIn("unless the ICP explicitly includes pre-seed", prompt)
        icp["company_stage"] = "Series A"
        self.assertNotIn("Do not relabel pre-seed", build_prompt(icp))
        icp["company_stage"] = ""
        self.assertNotIn("Do not relabel pre-seed", build_prompt(icp))

    def test_hiring_guidance_requires_direct_function_match(self) -> None:
        icp = {
            "icp_id": "test-hiring",
            "intent_signal": "Hiring for integration roles",
            "intent_category": "HIRING",
            "intent_max_age_days": 365,
            "required_attribute": "Supports customer adoption programs",
        }
        prompt = build_prompt(icp)

        self.assertIn(
            "source quote of actual job responsibilities",
            prompt,
        )
        self.assertIn(
            "function named in the required intent text",
            prompt,
        )
        self.assertIn(
            "not merely a broader required_attribute",
            prompt,
        )
        self.assertIn(
            "Generic sales, renewal, or adoption targets alone do not prove platform, integration, or RevOps ownership",
            prompt,
        )
        self.assertIn(
            "Shared words such as systems or platform",
            prompt,
        )
        icp["intent_category"] = "FUNDING"
        self.assertNotIn(
            "function named in the required intent text",
            build_prompt(icp),
        )

    def test_output_canonicalizes_true_stage_and_employee_format_synonyms(self) -> None:
        base = {
            "company_name": "Example",
            "company_website": "https://example.com",
            "industry": "Software",
            "employee_count": "1,001–5,000 employees",
            "company_stage": "PE-backed",
            "country": "United States",
            "fit_summary": "Matches the example ICP.",
            "fit_evidence_urls": ["https://example.com/about"],
            "intent_signals": [
                {
                    "matched_icp_signal": 0,
                    "description": "Example raised capital.",
                    "date": "2026-08-20",
                    "why_now": "The capital supports growth.",
                    "url": "https://example.com/news/event",
                    "snippet": "Example announced the transaction.",
                }
            ],
        }

        dumped = CompanyResult.model_validate(base).model_dump(mode="json")

        self.assertEqual(dumped["employee_count"], "1,001-5,000")
        self.assertEqual(dumped["company_stage"], "Private Equity")
        self.assertEqual(
            CompanyResult.model_validate(
                {**base, "company_stage": "bootstrapped"}
            ).company_stage,
            "Bootstrapped",
        )
        self.assertEqual(
            CompanyResult.model_validate_json(json.dumps(dumped)).model_dump(mode="json"),
            dumped,
        )

    def test_output_does_not_relabel_ambiguous_stage_or_employee_range(self) -> None:
        base = {
            "company_name": "Example",
            "company_website": "https://example.com",
            "industry": "Software",
            "employee_count": "51-500",
            "company_stage": "growth stage",
            "country": "United States",
            "fit_summary": "Matches the example ICP.",
            "fit_evidence_urls": ["https://example.com/about"],
            "intent_signals": [
                {
                    "matched_icp_signal": 0,
                    "description": "Example raised capital.",
                    "date": "2026-08-20",
                    "why_now": "The capital supports growth.",
                    "url": "https://example.com/news/event",
                    "snippet": "Example announced the transaction.",
                }
            ],
        }

        dumped = CompanyResult.model_validate(base).model_dump(mode="json")

        self.assertEqual(dumped["employee_count"], "51-500")
        self.assertEqual(dumped["company_stage"], "growth stage")

    def test_output_rejects_non_public_and_invalid_port_urls(self) -> None:
        base = {
            "company_name": "Example",
            "company_website": "https://example.com",
            "industry": "Software",
            "employee_count": "51-200",
            "company_stage": "Series A",
            "country": "United States",
            "fit_summary": "Matches the example ICP.",
            "fit_evidence_urls": ["https://example.com/about"],
            "intent_signals": [
                {
                    "matched_icp_signal": 0,
                    "description": "A current product event.",
                    "date": "2026-08-20",
                    "why_now": "The event gives a timely contact reason.",
                    "url": "https://example.com/news/event",
                    "snippet": "Example launched the product.",
                }
            ],
        }
        for bad_url in ("https://internal.test/event", "https://example.com:bad/event"):
            value = dict(base)
            value["company_website"] = bad_url
            with self.subTest(url=bad_url):
                with self.assertRaises(ValidationError):
                    CompanyResult.model_validate(value)

    def test_output_rejects_non_company_linkedin_url_and_hidden_scorer_overflow(
        self,
    ) -> None:
        base = {
            "company_name": "Example",
            "company_website": "https://example.com",
            "industry": "Software",
            "employee_count": "51-200",
            "company_stage": "Series A",
            "country": "United States",
            "fit_summary": "Matches the example ICP.",
            "fit_evidence_urls": ["https://example.com/about"],
            "intent_signals": [
                {
                    "matched_icp_signal": 0,
                    "description": "A current product event.",
                    "date": "2026-08-20",
                    "why_now": "The event gives a timely contact reason.",
                    "url": "https://example.com/news/event",
                    "snippet": "Example launched the product.",
                }
            ],
        }
        for field, value in (
            ("company_linkedin", "https://example.com/company/example"),
            ("company_name", "x" * 201),
            ("fit_summary", "x" * 501),
        ):
            candidate = dict(base)
            candidate[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    CompanyResult.model_validate(candidate)


if __name__ == "__main__":
    unittest.main()

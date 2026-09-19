"""Native tool journeys use fixture provider responses, never paid services."""
import copy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import budget_guard as budget
import deepline
import email_receipts
import research_input
import research_tools
from research_tools import ResearchTools
import run_attempt as runner
from test_research_interface import setup_request
from tyche_tools import SandboxedTools, serve


class FixtureProvider:
    def __init__(self):
        self.requests = []
        self.raw = {"status": "ok", "element": {"name": "ExamplePay", "website": "https://example.test",
            "linkedinUrl": "https://www.linkedin.com/company/examplepay/",
            "employeeCountRange": {"start": 51, "end": 200}}}
        self.rate = .2
        self.delay = 0
        self.peak = self.active = 0
        self.lock = threading.Lock()

    def __call__(self, request, capture):
        self.requests.append(copy.deepcopy(request))
        tool = request.get("tool", "fixture-search")
        if request["operation"] != "execute":
            key = "email" if tool in {"zerobounce_validate", "bounceban_verify_single"} else "url" if (tool.startswith("harvestapi") or tool in {"firecrawl_scrape", "contextdev_post_web_crawl"}) else "website" if tool == "aviato_get_company_funding_rounds" else "query"
            fields = ["first_name", "last_name", "domain"] if tool in {"fixture_email_finder", "hunter_email_finder"} else [key]
            if tool in {"hunter_domain_search", "findymail_find_from_domain", "search_contact"}:
                fields = ["domain"]
            properties = {field: {"type": "string"} for field in fields}
            if tool == "harvestapi_get_profile":
                properties["findEmail"] = {"type": "string", "enum": ["true", "false"]}
            return {"provider": "deepline", "operation": request["operation"], "status": "ok", "results": [{
                "toolId": tool, "callable": True, "connected": True,
                "inputSchema": {"fields": [{"name": field, "required": True, "type": "string"} for field in fields],
                    "jsonSchema": {"properties": properties, "additionalProperties": False}},
                "pricing": {"creditsPerUnit": self.rate, "unit": "call"}}]}, 0
        def dispatch():
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(self.delay)
                raw = {"exit_code": 0, "body": copy.deepcopy(self.raw), "stderr": ""}
                raw["body"]["billing"] = {"credits_charged": self.rate, "cost_usd": round(self.rate * .1, 8)}
                capture(raw)
                return deepline.normalize_response(request, raw)
            finally:
                with self.lock:
                    self.active -= 1
        return budget.guarded_call(request, "deepline", dispatch)


def check(target="example.test", **options):
    return {"target": target, "phase": "account_verification", "purpose": "Check company fit",
            "tool": "harvestapi_get_company", "inputs": {"url": "https://www.linkedin.com/company/" + target}, **options}


def captured_page(tools, provider, *, target="example.test", url="https://example.test/news",
                  text="Example announced a planned partnership.", date="2026-01-01"):
    metadata = {"sourceURL": url, "statusCode": 200}
    if date:
        metadata["article:published_time"] = date
    provider.raw = {"status": "completed", "toolResponse": {"rawV2": {"data": {
        "metadata": metadata, "markdown": text}}}}
    return tools.lookup([check(target, purpose="Read captured page " + url, tool="firecrawl_scrape", inputs={"url": url})])["lookups"][0]["results"][0]["ref"]


def review_findings(packet):
    # Fixture judgments exercise persistence/gates, not model factual accuracy.
    return [{"target": company["company"]["domain"], "source_refs": list(company["sources"]),
             "finding": "The captured manufacturing and completed integration passages support the fixture fit and factual prose; potential coordination benefits remain qualified analysis."}
            for company in packet["companies"]]


class ResearchToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "run/results.json"
        self.provider = FixtureProvider()
        self.tools = ResearchTools(self.path, execute=self.provider)
        self.request = setup_request()["request"]

    def start(self, **options):
        return self.tools.call("tyche_start", {"request": self.request, **options})

    def lookup(self, *checks):
        return self.tools.call("tyche_lookup", {"checks": list(checks or [check()])})

    def test_paid_post_rows_remain_selectable_beyond_preview_and_in_old_receipts(self):
        self.start()
        for count in (40, 50, 100):
            with self.subTest(count=count):
                self.provider.raw = {"toolResponse": {"rawV2": {"elements": [
                    {"id": str(i), "content": f"Appointment announcement {i}",
                     "linkedinUrl": f"https://www.linkedin.com/posts/example-{i}",
                     "postedAt": {"date": "2026-08-03"}} for i in range(count)]}}}
                view = self.lookup(check(f"posts-{count}.test", tool="harvestapi_company_posts"))["lookups"][0]
                self.assertEqual((len(view["results"]), view["result_count"], view["next_offset"]), (10, count, 10))
                rid = view["route"]
                receipt_path = self.path.parent / "receipts" / (rid + ".json")
                saved = json.loads(receipt_path.read_text())
                self.assertEqual(len(saved["results"]), count)
                first_ten = saved["results"][:10]
                # Simulate historical preview-only normalization while retaining
                # the immutable full provider response and original request.
                saved["results"] = first_ten
                receipt_path.write_text(json.dumps(saved))
                before = receipt_path.read_bytes()
                calls = len(self.provider.requests)
                page = self.tools.inspect(ref=rid, offset=10)
                self.assertEqual(page["result_count"], count)
                self.assertEqual(page["results"][3]["ref"], rid + ":13")
                row, _, _ = self.tools._resolve(rid + ":13")
                self.assertEqual(row["evidence_text"], "Appointment announcement 13")
                self.assertEqual(self.tools.inspect(ref=rid, offset=count - 1)["next_offset"], None)
                for index, expected in enumerate(first_ten):
                    actual, _, _ = self.tools._resolve(f"{rid}:{index}")
                    self.assertEqual(actual, expected)
                self.assertEqual((receipt_path.read_bytes(), len(self.provider.requests)), (before, calls))

    def qualifying_signal(self, ref):
        return [{"criterion": "partnership", "signal": "PARTNERSHIP", "status": "pass",
                 "claim": "Fixture partnership reviewed", "evidence": [{"ref": ref,
                     "text": "Fixture company announced the requested partnership.", "event_date": "2026-09-01"}]}] + [
            {"requirement_ref": r["ref"], "claim": "Fixture company matches this filter", "status": "pass",
             "evidence": [{"ref": ref, "text": "Fixture company profile"}]}
            for r in research_tools.request_requirements(self.request) if r["ref"].startswith("icp:")]

    def saved_funding(self, target="example.test"):
        rows = [{"id": "round-c", "name": "Series C - ExamplePay", "stage": "Series C",
                 "announcedOn": "2024-11-28T00:00:00.000Z", "moneyRaised": 45000000}]
        self.provider.raw = {"toolResponse": {"rawV2": {"fundingRounds": rows}},
                             "output_preview": {"kind": "list", "rowCount": len(rows), "preview": rows}}
        return self.lookup(check(target, tool="aviato_get_company_funding_rounds",
            inputs={"website": "https://" + target}))["lookups"][0]["results"][0]["ref"]

    def selected_contact(self, target="example.test", first="Ada", last="Example", position=None, email="ada@example.test", profile_fields=None):
        """A reviewed account and current person, all from local provider fixtures."""
        ref = self.lookup(check(target))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": target, "decision": "qualify_account", "reason": "Verified fit",
            "company": {"ref": ref}, "account_fit": {"ref": ref, "text": "Provides payments infrastructure"},
            "qualification_checks": self.qualifying_signal(ref)}])
        self.provider.raw = {"status": "ok", "element": {"linkedinUrl": "https://www.linkedin.com/in/ada-example/",
            "firstName": first, "lastName": last, "email": email, "currentPosition": [{"companyName": "ExamplePay",
                "companyLinkedinUrl": "https://www.linkedin.com/company/examplepay/", "title": "Head of Payments", **(position or {})}],
            "location": {"parsed": {"countryFull": "Singapore"}}, **(profile_fields or {})}}
        profile = self.lookup(check(target, phase="contact_verification", tool="harvestapi_get_profile",
            inputs={"url": "https://www.linkedin.com/in/ada-example/"}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": target, "decision": "hold_contact", "reason": "Selected current buyer",
            "primary_contact": {"ref": profile, "requested_role": "Head of Payments", "role_match": "exact"}}])
        return profile

    def test_new_custom_criteria_require_explicit_evidence_fields_before_any_call(self):
        self.request["icp"]["custom_criteria"] = ["Current Series A"]
        with self.assertRaisesRegex(ValueError, "required_attributes.*buying_signals"):
            self.start()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".budget.json").exists())
        self.assertEqual(self.provider.requests, [])
        self.request["icp"]["required_attributes"] = self.request["icp"].pop("custom_criteria")
        self.start()
        self.assertIn({"ref": "attribute:0", "label": "Current Series A", "importance": "required"},
                      self.tools.inspect(field="requirements")["requirements"])

    def test_reject_reuses_saved_outside_size_without_extra_judgment_or_spend(self):
        self.request["icp"]["company_size"] = {"min_employees": 201, "max_employees": 10000}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Reviewing fit",
            "company": {"ref": ref, "description": "Existing business description."}}])
        held = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(held["qualification_checks"], [])  # Code does not choose rejection.
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Too small"}])
        row = json.loads(self.path.read_text())["rejected"][0]
        self.assertEqual(row["candidate"], held["candidate"])
        check = row["qualification_checks"][0]
        self.assertEqual((check["criterion"], check["status"], check["importance"]), ("company_size", "fail", "required"))
        self.assertIn("51-200", check["claim"])
        self.assertEqual(check["evidence"][0]["source"]["route_id"], ref.split(":")[0])
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_reject_can_select_new_receipt_above_size_limit(self):
        self.request["icp"]["company_size"] = {"min_employees": 1, "max_employees": 50}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Too large",
            "company": {"ref": ref}}])
        self.assertEqual(len(json.loads(self.path.read_text())["rejected"]), 1)

    def test_matching_overlapping_and_missing_ranges_do_not_prove_rejection(self):
        for label, band, size in [
            ("matching", {"min_employees": 51, "max_employees": 200}, {"start": 51, "end": 200}),
            ("overlapping", {"min_employees": 100, "max_employees": 500}, {"start": 51, "end": 200}),
            ("missing", {"min_employees": 201}, None),
        ]:
            with self.subTest(label=label):
                path = self.path.parent / label / "results.json"
                provider = FixtureProvider()
                provider.raw["element"]["employeeCountRange"] = size
                tools = ResearchTools(path, execute=provider)
                request = copy.deepcopy(self.request)
                request["icp"]["company_size"] = band
                tools.start(request)
                ref = tools.lookup([check()])["lookups"][0]["results"][0]["ref"]
                before = path.read_bytes(), budget.ledger_path(path).read_bytes(), len(provider.requests)
                with self.assertRaisesRegex(ValueError, "requires an evidenced required failure"):
                    tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Size uncertain",
                        "company": {"ref": ref}}])
                self.assertEqual((path.read_bytes(), budget.ledger_path(path).read_bytes(), len(provider.requests)), before)

    def test_size_rejection_requires_matching_saved_receipt(self):
        self.request["icp"]["company_size"] = {"min_employees": 201}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Size review",
            "company": {"ref": ref}}])
        original = json.loads(self.path.read_text())
        for key, value, error in [("employee_range", "1-10", "conflicts with its Harvest receipt"),
                                  ("linkedin_url", "https://www.linkedin.com/company/other/", "conflicts with its Harvest receipt"),
                                  ("employee_range_evidence", {"source": {"route_id": "another-run"}}, "Unknown saved result reference")]:
            with self.subTest(key=key, value=value):
                document = copy.deepcopy(original)
                document["unresolved"][0]["candidate"][key] = value
                self.path.write_text(json.dumps(document))
                before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
                with self.assertRaisesRegex(ValueError, error):
                    self.tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Too small"}])
                self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_size_rejection_keeps_explicit_check_consistency_validation(self):
        self.request["icp"]["company_size"] = {"min_employees": 201}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        with self.assertRaisesRegex(ValueError, "company_size decision contradicts"):
            self.tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Review size",
                "company": {"ref": ref}, "qualification_checks": [{"criterion": "company_size", "importance": "required",
                    "status": "pass", "claim": "An explicit contradictory judgment", "evidence": [{"ref": ref}]}]}])

    def test_company_review_reuses_unique_domain_matched_getter_and_closes_it(self):
        self.request["icp"]["company_size"] = {"min_employees": 51, "max_employees": 200}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Verified fit",
            "company": {"description": "Reviewed description."}, "qualification_checks": self.qualifying_signal(ref)}])
        document = json.loads(self.path.read_text())
        company = document["unresolved"][0]["candidate"]
        self.assertEqual(company["employee_range"], "51-200")
        self.assertEqual(company["description"], "Reviewed description.")
        self.assertEqual(company["employee_range_evidence"]["source"]["route_id"], ref.split(":")[0])
        frontier = next(x for x in document["stop_audit"]["route_frontier"] if x["route_id"] == ref.split(":")[0])
        self.assertEqual(frontier["state"], "exhausted")
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_company_reuse_skips_wrong_domain_and_coalesces_identical_getters(self):
        self.start()
        self.provider.raw["element"]["website"] = "https://wrong.test"
        wrong = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.provider.raw["element"]["website"] = "https://example.test"
        for suffix in ("correct", "duplicate"):
            correct = self.lookup(check(approach="receipt-fixture-" + suffix, inputs={"url": "https://www.linkedin.com/company/" + suffix}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Research remaining facts"}])
        company = json.loads(self.path.read_text())["unresolved"][0]["candidate"]
        self.assertEqual(company["employee_range_evidence"]["source"]["route_id"], correct.split(":")[0])
        self.assertNotEqual(company["employee_range_evidence"]["source"]["route_id"], wrong.split(":")[0])

    def test_conflicting_company_getters_require_selection_and_preserve_it(self):
        self.start()
        first = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.provider.raw["element"]["linkedinUrl"] = "https://www.linkedin.com/company/another-company/"
        second = self.lookup(check(inputs={"url": "https://www.linkedin.com/company/another-company/"}))["lookups"][0]["results"][0]["ref"]
        review = {"target": "example.test", "decision": "qualify_account", "reason": "Selected company",
                  "qualification_checks": self.qualifying_signal(first)}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "saved company getters disagree") as error:
            self.tools.review(companies=[review])
        self.assertIn(first, str(error.exception))
        self.assertIn(second, str(error.exception))
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.tools.review(companies=[{**review, "company": {"ref": first}}])
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Finding buyer",
                                     "company": {"description": "Updated prose."}}])
        company = json.loads(self.path.read_text())["unresolved"][0]["candidate"]
        self.assertEqual(company["linkedin_url"], "https://www.linkedin.com/company/examplepay/")

    def test_requested_size_requires_saved_range_before_contact_work(self):
        self.request["icp"]["company_size"] = {"min_employees": 11, "max_employees": 1000}
        self.start()
        self.provider.raw["element"]["employeeCountRange"] = None
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        with self.assertRaisesRegex(ValueError, "requested company_size needs a saved LinkedIn employee_range"):
            self.tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Still missing size",
                "qualification_checks": self.qualifying_signal(ref)}])
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Size remains unknown"}])
        document = json.loads(self.path.read_text())
        document["unresolved"][0]["stage"] = "contact"
        document["unresolved"][0]["qualification_checks"] = [{"criterion": "partnership", "signal": "PARTNERSHIP", "importance": "required",
            "status": "pass", "claim": "Fixture partnership", "evidence": [self.tools._evidence({"ref": ref})]}]
        document["unresolved"][0]["candidate"]["employee_range"] = "51-200"
        self.path.write_text(json.dumps(document))
        before = sum(x["operation"] == "execute" for x in self.provider.requests)
        with self.assertRaisesRegex(ValueError, "must match the saved HarvestAPI"):
            self.lookup(check(phase="contact_discovery", tool="fixture-search", inputs={"query": "senior buyer"}))
        self.assertEqual(sum(x["operation"] == "execute" for x in self.provider.requests), before)

    def test_company_selection_without_size_is_not_replaced_by_later_getter(self):
        self.start()
        self.provider.raw["element"]["employeeCountRange"] = None
        first = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Selected identity",
            "company": {"ref": first}}])
        self.provider.raw["element"]["employeeCountRange"] = {"start": 51, "end": 200}
        self.lookup(check(inputs={"url": "https://www.linkedin.com/company/later-observation/"}))
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Review other facts",
            "company": {"description": "Preserved identity."}}])
        company = json.loads(self.path.read_text())["unresolved"][0]["candidate"]
        self.assertEqual(company["employee_range_evidence"]["source"]["route_id"], first.split(":")[0])
        self.assertIsNone(company["employee_range"])

    def test_legacy_custom_criteria_resume_without_rewriting_request_or_budget(self):
        document, options = research_tools.research_input.start_document(self.path, {"request": self.request})
        document["request"]["icp"]["custom_criteria"] = ["Original legacy condition"]
        research_tools.runner.refresh(document)
        self.path.parent.mkdir(parents=True)
        budget.create_run(self.path, document, **options)
        self.lookup()  # A saved paid receipt must also survive the legacy resume.
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        self.tools.start(document["request"])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)
        self.assertEqual(len(self.provider.requests), calls)

    def test_original_request_is_bound_once_and_available_on_resume(self):
        source = self.path.parent.parent / "original.txt"
        source.write_text("Find multi-site businesses; hiring is preferred.")
        self.tools.environment["TYCHE_REQUEST_FILE"] = str(source)
        self.assertEqual(self.start()["request"]["original_text"], source.read_text())
        before = self.path.read_bytes()
        source.write_text("Changed outside the saved run")
        self.start()
        self.assertEqual(before, self.path.read_bytes())
        self.assertIn("multi-site", self.tools.inspect()["request"]["original_text"])

    def test_target_offering_context_is_consistent_at_start_company_and_final_review(self):
        self.request['product_service'] = {'description': 'A platform that governs business data.', 'perspective': 'target'}
        started = self.start()
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        company = self.tools.inspect(target='example.test', field='evidence_review')
        final = self.tools.review_delivery(json.loads(self.path.read_text()))
        for packet in (started, company, final):
            guidance = packet['writing_requirements']
            self.assertEqual(guidance['product_service'], self.request['product_service'])
            self.assertIn("target company's own offering", guidance['offering_context'])
            self.assertIn('Do not invent an external seller', guidance['offering_context'])
            self.assertEqual(guidance['intent_details'], research_tools.WRITING_REQUIREMENTS['intent_details'])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_seller_and_missing_offering_context_do_not_inherit_target_interpretation(self):
        offering = {'description': 'Specialized staffing services.', 'perspective': 'seller'}
        guidance = research_tools.writing_requirements({'product_service': offering})
        self.assertIn("user's offering", guidance['offering_context'])
        self.assertIn('do not assert confirmed demand', guidance['offering_context'])
        guidance['product_service']['perspective'] = 'target'
        self.assertEqual(offering['perspective'], 'seller')
        for request in ({}, {'product_service': None}):
            unknown = research_tools.writing_requirements(request)
            self.assertEqual(unknown['product_service'], {})
            self.assertIn('do not invent an offering', unknown['offering_context'])

    def test_research_review_handoff_preserves_state_and_cannot_approve(self):
        self.start()
        document = json.loads(self.path.read_text())
        ref = runner.review_fingerprint(document)
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.environment['TYCHE_FINALIZATION_ONLY'] = '0'
        for supplied in (None, ref):
            result = self.tools.review_delivery(document, supplied)
            self.assertEqual(result['status'], 'review_handoff')
            self.assertFalse(result['delivery_allowed'])
            self.assertNotIn('review_ref', result)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        final = ResearchTools(self.path, execute=self.provider, environment={'TYCHE_FINALIZATION_ONLY': '1'})
        packet = final.review_delivery(document)
        self.assertEqual(packet['status'], 'review_required')
        self.assertEqual(packet['review_ref'], ref)
        self.assertIn('reopen the exact saved source URL once', packet['instructions'])
        self.assertIn('preserve the captured qualification ref', packet['instructions'])
        self.assertIn('No new searches, new source URLs or provider lookups', packet['instructions'])
        self.assertIsNone(final.review_delivery(document, ref, []))
        self.assertEqual(json.loads(self.path.read_text())['final_review']['review_ref'], ref)
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before[1:])

    def test_required_attribute_cannot_be_omitted_before_contact_spend(self):
        self.request["icp"]["required_attributes"] = ["Operates multiple sites"]
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        row = {"target": "example.test", "decision": "qualify_account", "reason": "Reviewed company",
               "company": {"ref": ref}, "account_fit": {"ref": ref},
               "qualification_checks": self.qualifying_signal(ref)}
        with self.assertRaisesRegex(ValueError, "required attribute"):
            self.tools.review(companies=[row])
        row["qualification_checks"].append({"requirement_ref": "attribute:0", "status": "pass",
            "claim": "The source identifies two operating sites", "evidence": [{"ref": ref, "text": "Two operating sites are listed"}]})
        self.tools.call("tyche_review", {"companies": [row]})
        saved = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(saved["qualification_checks"][-1]["criterion"], "operates multiple sites")
        self.assertNotIn("requirement_ref", saved["qualification_checks"][-1])

    def test_review_shows_saved_source_not_only_the_rewritten_claim(self):
        self.start()
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Review source strength",
            "account_fit": {"ref": "web:0:0"},
            "qualification_checks": [{"criterion": "hiring", "importance": "preferred", "status": "unknown",
                "claim": "Persistence is unverified", "evidence": [{"ref": "web:0:0", "text": "Unproven persistence claim"}]}]}],
            web=[{"target": "example.test", "purpose": "Inspect hiring evidence", "query": "careers",
                "operation": "open", "response": {"status": "ok", "results": [
                    {"url": "https://example.test/jobs", "text": "One current vacancy. No posting date is shown."}]}}])
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        proof = next(iter(packet["sources"].values()))
        self.assertEqual(proof["text"], "One current vacancy. No posting date is shown.")
        self.assertEqual(proof["capture_method"], "agent_recorded_web")
        self.assertIsNone(proof["date"])
        self.assertNotIn("text", packet["company"]["account_fit"])
        self.assertEqual(len(packet["company"]["account_fit"]["source_refs"]), 1)
        self.assertEqual(packet["company"]["qualification_checks"][0]["evidence"][0]["text"], "Unproven persistence claim")
        self.assertEqual(json.loads(self.path.read_text())["unresolved"][0]["qualification_checks"][0]["status"], "unknown")

    def test_review_identifies_adapter_saved_provider_text_without_dispatch(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Review source",
            "account_fit": {"ref": ref, "text": "Research interpretation"}}])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        proof = next(iter(packet["sources"].values()))
        self.assertEqual(proof["capture_method"], "provider_response")
        self.assertNotEqual(proof["text"], "Research interpretation")
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_selected_point_lookups_close_without_closing_searches(self):
        self.start()
        profile = self.selected_contact()
        search = self.lookup(check(tool="fixture_search", inputs={"query": "more companies"}))["lookups"][0]
        frontier = {r["route_id"]: r for r in json.loads(self.path.read_text())["stop_audit"]["route_frontier"]}
        self.assertEqual(frontier[profile.split(":")[0]]["state"], "exhausted")
        self.assertEqual(frontier[search["route"]]["state"], "continuable")

    def test_review_labels_search_snippets_and_reuses_identical_excerpts(self):
        self.start()
        snippet = "ExamplePay announced a planned capacity expansion."
        self.provider.raw = {"status": "ok", "results": [{"url": "https://example.test/announcement", "snippet": snippet}]}
        ref = self.lookup(check(tool="fixture_search", inputs={"query": "expansion"}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Read full source next",
            "account_fit": {"ref": ref}}])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        proof = packet["sources"][ref]
        self.assertEqual(proof["capture_method"], "provider_response")
        self.assertEqual(proof["content_kind"], "search_excerpt")
        self.assertEqual(proof["text"], snippet)
        self.assertEqual(packet["company"]["account_fit"]["source_refs"], [ref])
        self.assertNotIn("text", packet["company"]["account_fit"])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_review_keeps_distinct_evidence_even_when_compact_prefixes_match(self):
        self.start()
        prefix = "Current source context. " * 100
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Resolve source conflict",
            "account_fit": {"ref": "web:0:0", "text": prefix + "The expansion is completed."}}],
            web=[{"target": "example.test", "purpose": "Read source", "query": "https://example.test/announcement",
                "operation": "open", "response": {"status": "ok", "results": [
                    {"url": "https://example.test/announcement", "text": prefix + "The expansion is planned."}]}}])
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        self.assertIn("text", packet["company"]["account_fit"])
        self.assertEqual(next(iter(packet["sources"].values()))["content_kind"], "unverified")

    def test_review_preserves_backup_contacts_and_fallback_verdicts(self):
        row = {"primary_contact": {"full_name": "Primary Buyer", "email_validation": {"status": "valid"}},
               "backup_contacts": [{"full_name": "Secondary Buyer", "requested_role": "COO",
                   "email_validation": {"status": "unknown", "fallback": {"status": "success", "result": "deliverable"}}}]}
        view = self.tools._company_review(row, {})
        self.assertEqual(view["primary_contact"]["email_validation"]["status"], "valid")
        self.assertEqual(view["backup_contacts"][0]["requested_role"], "COO")
        self.assertEqual(view["backup_contacts"][0]["email_validation"]["fallback"]["result"], "deliverable")

    def test_review_exposes_current_responsibilities_without_prior_role_judgment(self):
        self.start()
        duties = "Oversees operations and sales with full P&L responsibility."
        ref = self.selected_contact(position={"title": "President", "description": duties,
                                              "startDate": {"year": 2025, "month": 3}},
                                    profile_fields={"headline": "Operating leader", "about": "Leads plant operations. " * 100})
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        buyer = packet["company"]["primary_contact"]
        self.assertEqual(buyer["current_title"], "President")
        self.assertNotIn("role_match", buyer)
        self.assertEqual(buyer["profile_evidence"]["source_refs"], [ref])
        source = packet["sources"][ref]
        self.assertEqual(source["detail_ref"], ref)
        self.assertEqual(source["record"]["headline"], "Operating leader")
        self.assertTrue(source["record"]["about"].startswith("Leads plant operations."))
        self.assertIn("truncated", source["record"]["about"])
        self.assertEqual(self.tools.inspect(ref=ref, field="about")["text"], "Leads plant operations. " * 100)
        self.assertEqual(source["record"]["current_positions"][0]["description"], duties)
        self.assertEqual(source["record"]["current_positions"][0]["start_date"], {"year": 2025, "month": 3})
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_review_allows_missing_responsibilities_but_reports_missing_profile_receipt(self):
        self.start()
        ref = self.selected_contact()
        document = json.loads(self.path.read_text())
        row = document["unresolved"][0]
        packet = self.tools._evidence_packet(dict(document, accepted=[row]), "review:fixture")
        self.assertEqual(packet["status"], "review_required")
        self.assertNotIn("about", packet["companies"][0]["sources"][ref]["record"])
        self.assertIsNone(packet["companies"][0]["sources"][ref]["record"]["current_positions"][0]["description"])
        row["primary_contact"]["location_evidence"]["source"]["route_id"] = "missing-profile"
        packet = self.tools._evidence_packet(dict(document, accepted=[row]), "review:changed")
        self.assertEqual(packet["status"], "needs_repair")
        self.assertTrue(any("Unknown" in error or "receipt" in error for error in packet["errors"]))

    def test_review_preserves_independent_legacy_signal_beside_other_checks(self):
        row = {"signal_evidence": {"signal": "Expansion", "evidence_text": "Opened a new location"},
               "qualification_checks": [{"signal": "Hiring", "status": "unknown", "evidence": []}]}
        view = self.tools._company_review(row, {})
        self.assertEqual(view["signal_evidence"]["signal"], "Expansion")
        self.assertEqual(view["signal_evidence"]["text"], "Opened a new location")

    def test_email_phase_is_derived_and_initial_rejected_before_paid_lookup(self):
        self.start()
        ref = self.selected_contact(last="H.")
        self.tools.inspect(tool="hunter_email_finder")
        before = budget.ledger_path(self.path).read_bytes()
        attempted = len([r for r in self.provider.requests if r.get("operation") == "execute"])
        item = check(tool="hunter_email_finder", contact_ref=ref, inputs={})
        item.pop("phase")
        with self.assertRaisesRegex(ValueError, "surname letters"):
            self.lookup(item)
        self.assertEqual(before, budget.ledger_path(self.path).read_bytes())
        self.assertEqual(attempted, len([r for r in self.provider.requests if r.get("operation") == "execute"]))

    def test_profile_email_addon_uses_account_gate_without_an_explicit_phase(self):
        self.start()
        item = check(tool="harvestapi_get_profile", contact_ref="unverified:0",
                     inputs={"url": "https://www.linkedin.com/in/unknown/", "findEmail": True})
        item.pop("phase")
        before = budget.ledger_path(self.path).read_bytes()
        attempted = len([r for r in self.provider.requests if r.get("operation") == "execute"])
        with self.assertRaisesRegex(ValueError, "account-qualified company"):
            self.lookup(item)
        self.assertEqual(before, budget.ledger_path(self.path).read_bytes())
        self.assertEqual(attempted, len([r for r in self.provider.requests if r.get("operation") == "execute"]))

    def profile_email_result(self, fields, **response_fields):
        self.start()
        ref = self.selected_contact(email=None)
        self.provider.raw["element"].update(fields)
        self.provider.rate = .14
        def execute(request, capture):
            body, code = self.provider(request, capture)
            if request.get("operation") == "execute":
                body.update(response_fields)
            return body, code
        self.tools.execute = execute
        result = self.lookup(check(tool="harvestapi_get_profile", contact_ref=ref,
                                   inputs={"findEmail": "true"}))
        rid = result["lookups"][0]["route"]
        doc = json.loads(self.path.read_text())
        frontier = next(r for r in doc["stop_audit"]["route_frontier"] if r["route_id"] == rid)
        return rid, doc, frontier

    def test_completed_profile_email_miss_needs_no_manual_source_closure(self):
        rid, doc, frontier = self.profile_email_result({"emails": []})
        self.assertEqual(frontier["state"], "exhausted")
        self.assertIn("another email source", frontier["reason"])
        self.assertNotIn(rid, {r["ref"] for r in runner.pending_source_reviews(doc)})
        route = next(r for r in doc["routes"] if r["route_id"] == rid)
        self.assertEqual((route["provider_status"], route["rows_returned"]), ("ok", 1))
        self.assertEqual(route["cost_credits"], .14)
        self.assertEqual(doc["accepted"], [])
        self.assertEqual(len(doc["unresolved"]), 1)
        ledger = budget.load_ledger(self.path)
        receipt = runner.read_receipt(self.path, rid)["result"]
        runner.finish_attempt(self.path, rid, receipt)
        self.assertEqual(budget.load_ledger(self.path), ledger)
        self.assertEqual(budget.audit_ledger(self.path, doc), [])

    def test_profile_email_result_with_address_still_requires_review(self):
        _, _, frontier = self.profile_email_result({"emails": [{"email": "ada@example.test"}]})
        self.assertEqual(frontier["state"], "continuable")

    def test_profile_without_email_field_is_not_an_explicit_miss(self):
        _, _, frontier = self.profile_email_result({})
        self.assertEqual(frontier["state"], "continuable")

    def test_pending_profile_email_lookup_remains_open(self):
        _, _, frontier = self.profile_email_result({"emails": []}, pending_verification={"job_id": "pending"})
        self.assertEqual(frontier["state"], "continuable")

    def test_partial_profile_email_lookup_remains_open(self):
        _, _, frontier = self.profile_email_result({"emails": []}, status="partial")
        self.assertEqual(frontier["state"], "continuable")

    def test_malformed_saved_requirements_return_an_actionable_error(self):
        self.start()
        document = json.loads(self.path.read_text())
        for fields in ({"icp": None}, {"buying_signals": [None]}, {"buying_signals": [{}]},
                       {"icp": {"required_attributes": "not a list"}}):
            damaged = copy.deepcopy(document)
            damaged["request"].update(fields)
            with self.subTest(fields=fields), patch.object(self.tools, "_document", return_value=damaged):
                with self.assertRaisesRegex(ValueError, "Restore the original saved request"):
                    self.tools.call("tyche_inspect", {"field": "requirements"})

    def test_unknown_reference_and_cost_inspection_are_actionable(self):
        self.start()
        current = self.lookup()["lookups"][0]["route"]
        with self.assertRaisesRegex(ValueError, current):
            self.tools.inspect(ref="mistyped-reference:0")
        self.assertIn("costs", self.tools.inspect(field="costs"))

    def test_start_without_age_window_preserves_unspecified_policy_on_resume(self):
        self.request.pop("time_window")
        for signal in self.request["buying_signals"]:
            signal.pop("max_age_days", None)
        self.start()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["request"]["time_window"], {})
        self.lookup()
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.start()
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        changed = copy.deepcopy(self.request)
        changed["time_window"] = {"max_age_days": 30}
        with self.assertRaises(ValueError):
            self.tools.call("tyche_start", {"request": changed})
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_email_discovery_ref_correction_preserves_spend_and_reuses_profile(self):
        self.start()
        profile = self.selected_contact()
        self.provider.raw = {"status": "completed", "toolResponse": {"rawV2": {"email": "ada@example.test"}}}
        finder = self.lookup(check(tool="fixture_email_finder", phase="contact_discovery",
            contact_ref=profile, inputs={}))['lookups'][0]['results'][0]['ref']
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for ref, tool in ((profile, "harvestapi_get_profile"), (finder, "fixture_email_finder")):
            with self.subTest(ref=ref), self.assertRaises(ValueError) as error:
                self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact",
                    "reason": "Select discovered email", "primary_contact": {"email_ref": ref}}])
            message = str(error.exception)
            for detail in ("example.test", "contact.email_ref", ref, tool, "validation ref", "do not repeat"):
                self.assertIn(detail, message)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.provider.raw = {"status": "ok", "data": {"address": "ada@example.test", "status": "valid"}}
        validation = self.lookup(check(tool="zerobounce_validate", contact_ref=profile,
            inputs={"email": "ada@example.test"}))['lookups'][0]['results'][0]['ref']
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact",
            "reason": "Save exact email verdict", "primary_contact": {"email_ref": validation, "email_source": {"ref": finder}}}])
        person = json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]
        self.assertEqual(person["email"], "ada@example.test")
        self.assertEqual(person["email_validation"]["status"], "valid")
        self.assertEqual(person["profile_ref"], profile)
        self.assertEqual(person["email_source"]["source"], {"provider": "deepline", "operation": "execute",
            "tool": "fixture_email_finder", "route_id": finder.split(":")[0], "result_index": 0})
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for change in ({"email_source": {"ref": validation}},
                       {"email": "other@example.test", "email_source": {"ref": finder}}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "email_source"):
                self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact",
                    "reason": "Invalid attribution", "primary_contact": change}])
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact",
            "reason": "Retain selected buyer", "primary_contact": {"requested_role": "Head of Payments"}}])
        self.assertEqual(json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]["email_source"], person["email_source"])
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact",
            "reason": "Change address for later validation", "primary_contact": {"email": "other@example.test"}}])
        changed = json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]
        self.assertNotIn("email_source", changed)
        self.assertNotIn("email_validation", changed)
        for tool in ("harvestapi_get_profile", "fixture_email_finder", "zerobounce_validate"):
            self.assertEqual(sum(r.get("operation") == "execute" and r.get("tool") == tool
                for r in self.provider.requests), 1)

    def test_guessed_email_is_blocked_before_spend_then_saved_page_is_reused(self):
        self.start()
        profile = self.selected_contact(email=None)
        self.tools.inspect(tool="zerobounce_validate")
        check_email = check(tool="zerobounce_validate", contact_ref=profile, inputs={"email": "ada@example.test"})
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "exact address.*saved finder/page"):
            self.lookup(check_email)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        ref = captured_page(self.tools, self.provider, text="Email Ada at **Ada@Example.Test**.")
        self.provider.raw = {"status": "ok", "data": {"address": "ada@example.test", "status": "valid"}}
        verdict = self.lookup(check_email)["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Select discovered email",
            "primary_contact": {"email_ref": verdict}}])
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc["unresolved"][0]["primary_contact"]["email_source"]["source"]["route_id"], ref.split(':')[0])
        doc["accepted"] = doc["unresolved"]
        self.assertEqual(email_receipts.email_receipt_errors(doc, self.path), [])

    def test_old_valid_verdict_and_later_discovery_do_not_prove_prior_discovery(self):
        self.start()
        profile = self.selected_contact(email=None)
        self.provider.raw = {"status": "ok", "data": {"address": "ada@example.test", "status": "valid"}}
        # Reproduce a historical submission before the discovery gate existed.
        with patch.object(runner, "discovery_source", return_value={"source": {}}):
            verdict = self.lookup(check(tool="zerobounce_validate", contact_ref=profile,
                inputs={"email": "ada@example.test"}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Historical valid verdict",
            "primary_contact": {"email_ref": verdict}}])
        captured_page(self.tools, self.provider, text="ada@example.test")
        doc = json.loads(self.path.read_text()); doc["accepted"] = doc["unresolved"]
        self.assertIn("before validation", str(email_receipts.email_receipt_errors(doc, self.path)))
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        self.assertIsNone(email_receipts.discovery_source(self.path, doc["routes"], "ada@example.test", before=verdict.split(':')[0]))
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)

    def test_nested_finder_discovery_uses_raw_response_not_request_echo_or_edited_results(self):
        self.start()
        profile = self.selected_contact(email=None)
        self.provider.raw = {"status": "completed", "toolResponse": {"rawV2": {
            "output": {"company": None, "persons": [{"professional_email": "ada@example.test"}]},
            "input": {"email": "guessed@example.test"}}}}
        found = self.lookup(check(tool="prospector", phase="contact_discovery", contact_ref=profile,
            inputs={"query": "Company buyers"}))["lookups"][0]["route"]
        path = self.path.parent / "receipts" / (found + ".json")
        saved = json.loads(path.read_text()); saved["results"] = [{"email": "forged@example.test"}]
        path.write_text(json.dumps(saved)); routes = json.loads(self.path.read_text())["routes"]
        self.assertIsNotNone(email_receipts.discovery_source(self.path, routes, "ADA@example.test"))
        for email in ("guessed@example.test", "forged@example.test", "aada@example.test"):
            self.assertIsNone(email_receipts.discovery_source(self.path, routes, email))
        saved["run_fingerprint"] = "another run"; path.write_text(json.dumps(saved))
        self.assertIsNone(email_receipts.discovery_source(self.path, routes, "ada@example.test"))

    def test_discovery_attribution_preserves_selected_index_and_report_link(self):
        self.start()
        company = copy.deepcopy(self.provider.raw['element'])
        self.provider.raw = {'results': [{'name': 'Unrelated', 'website': 'https://unrelated.test'}, company]}
        discovery = self.lookup(check('discovery', phase='account_discovery', tool='fixture_company_search',
            inputs={'query': 'payments companies'}))['lookups'][0]['results'][1]['ref']
        self.provider.raw = {'status': 'ok', 'element': company}
        verified = self.lookup(check())['lookups'][0]['results'][0]['ref']
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.review(companies=[{'target': 'example.test', 'decision': 'hold_account', 'reason': 'Selected account',
            'company': {'ref': verified, 'discovery_source': {'ref': discovery}}}])
        row = json.loads(self.path.read_text())['unresolved'][0]
        saved = row['candidate']['discovery_source']
        self.assertEqual(saved['source']['tool'], 'fixture_company_search')
        self.assertEqual(saved['source']['result_index'], 1)
        self.assertEqual(saved['source']['route_id'], discovery.split(':')[0])
        self.tools.review(companies=[{'target': 'example.test', 'decision': 'hold_account', 'reason': 'Keep researching',
                                    'company': {'description': 'Selected business.'}}])
        self.assertEqual(json.loads(self.path.read_text())['unresolved'][0]['candidate']['discovery_source'], saved)
        with self.assertRaisesRegex(ValueError, 'account-discovery'):
            self.tools.review(companies=[{'target': 'example.test', 'decision': 'hold_account', 'reason': 'Wrong stage',
                'company': {'discovery_source': {'ref': verified}}}])
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'scripts'))
        from run_costs import write_research_report
        report = {'request': {'target_count': 1, 'contact_fields': []}, 'accepted': [{'company': row['candidate']}]}
        write_research_report(self.path.parent, report, {}, 'Fixture report')
        text = (self.path.parent / 'report.md').read_text()
        self.assertIn('deepline/fixture_company_search', text)
        self.assertIn('receipts/' + discovery.split(':')[0] + '.json', text)

    def test_published_email_attribution_keeps_page_url_and_rejects_forged_source(self):
        self.start()
        self.selected_contact()
        ref = captured_page(self.tools, self.provider, text='Contact Ada at ada@example.test.')
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, 'cannot replace'):
            self.tools.review(companies=[{'target': 'example.test', 'decision': 'hold_contact', 'reason': 'Wrong source',
                'primary_contact': {'email': 'ada@example.test', 'email_source': {'ref': ref, 'url': 'https://fake.test'}}}])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.tools.review(companies=[{'target': 'example.test', 'decision': 'hold_contact', 'reason': 'Published email',
            'primary_contact': {'email': 'ada@example.test', 'email_source': {'ref': ref}}}])
        saved = json.loads(self.path.read_text())['unresolved'][0]['primary_contact']['email_source']
        self.assertEqual(saved['url'], 'https://example.test/news')
        self.assertEqual(saved['source']['tool'], 'firecrawl_scrape')

    def test_email_reference_supplies_receipt_names_and_rejects_conflicting_identity(self):
        self.start()
        ref = self.selected_contact(first="Aaron", last="Blocher-Rubin, PhD, BCBA")
        before = budget.ledger_path(self.path).read_bytes()
        for fields in ({"first_name": "Other"}, {"domain": "another.test"}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, "conflicts with the selected profile"):
                self.lookup(check(tool="fixture_email_finder", phase="contact_discovery", contact_ref=ref, inputs=fields))
        self.assertEqual(before, budget.ledger_path(self.path).read_bytes())
        self.provider.raw = {"status": "ok", "data": {"email": "aaron@example.test"}}
        self.lookup(check(tool="fixture_email_finder", phase="contact_discovery", contact_ref=ref,
                          inputs={"full_name": "Aaron Blocher-Rubin", "linkedin_url": "https://www.linkedin.com/in/ada-example/"}))
        sent = [r for r in self.provider.requests if r.get("operation") == "execute" and r.get("tool") == "fixture_email_finder"]
        self.assertEqual(sent[0]["payload"], {"first_name": "Aaron", "last_name": "Blocher-Rubin", "domain": "example.test"})
        self.assertNotIn("contact_ref", sent[0]["payload"])
        self.assertEqual(len([r for r in self.provider.requests if r.get("operation") == "execute" and r.get("tool") == "harvestapi_get_profile"]), 1)

    def test_email_miss_shows_sent_domain_without_weakening_identity_gate(self):
        self.start()
        ref = self.selected_contact()
        original = copy.deepcopy(json.loads(self.path.read_text())["unresolved"][0])
        self.provider.raw = {"status": "ok", "data": []}
        output = self.lookup(check(tool="fixture_email_finder", contact_ref=ref, inputs={}))
        self.assertEqual(self.provider.requests[-1]["payload"],
                         {"first_name": "Ada", "last_name": "Example", "domain": "example.test"})
        view = output["lookups"][0]
        self.assertEqual(view["email_search_domain"], "example.test")
        self.assertIn("profile-based", view["email_search_guidance"])
        for email in (None, "ada@example.test"):
            body = {"attempt": {"action": {"contact_ref": ref},
                                "request": {"payload": {"company_domain": "example.test"}}},
                    "results": [{"email": email}], "status": "ok"}
            replay = self.tools._lookup_view({"result": body})
            self.assertEqual(replay["email_search_domain"], "example.test")
            self.assertEqual("email_search_guidance" in replay, email is None)
        saved = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(saved, original)
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "conflicts with the selected profile"):
            self.lookup(check(tool="fixture_email_finder", contact_ref=ref,
                inputs={"domain": "another.test"}))
        self.assertEqual(before, (budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)))
        with self.assertRaisesRegex(ValueError, "profile|identity"):
            self.lookup(check(tool="fixture_email_finder", contact_ref="unverified:0", inputs={"domain": "employer.test"}))
        self.assertEqual(before, (budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)))

    def test_role_group_is_derived_without_changing_requested_roles(self):
        self.request["contact_role_groups"] = {"primary": ["Chief Executive Officer"], "secondary": ["Head of Payments"]}
        self.request["requested_roles"] = ["Chief Executive Officer", "Head of Payments"]
        self.start()
        self.selected_contact()
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Reviewed equivalent senior role",
            "primary_contact": {"requested_role": "CEO", "role_match": "approved_family"}}])
        contact = json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]
        self.assertEqual(contact["requested_role"], "Chief Executive Officer")
        self.assertEqual(contact["role_group"], "primary")
        self.assertEqual(json.loads(self.path.read_text())["request"]["requested_roles"], self.request["requested_roles"])

    def test_selected_contact_identifies_email_work_without_provider_name_heuristics(self):
        self.start()
        ref = self.selected_contact()
        self.provider.raw = {"status": "ok", "data": {"email": "ada@example.test"}}
        for tool in ("hunter_domain_search", "findymail_find_from_domain", "search_contact"):
            spec = check(tool=tool, contact_ref=ref, inputs={})
            spec.pop("phase")
            self.lookup(spec)
            sent = self.provider.requests[-1]
            self.assertEqual(sent["payload"], {"domain": "example.test"})
            route = json.loads(self.path.read_text())["routes"][-1]
            self.assertEqual(route["phase"], "contact_discovery")
        before = budget.ledger_path(self.path).read_bytes()
        with self.assertRaisesRegex(ValueError, "profile|contact_ref|identity"):
            self.lookup(check(tool="search_contact", contact_ref="not-a-profile:0", inputs={}))
        self.assertEqual(before, budget.ledger_path(self.path).read_bytes())
        profiles = [r for r in self.provider.requests if r.get("operation") == "execute" and r.get("tool") == "harvestapi_get_profile"]
        self.assertEqual(len(profiles), 1)

    def test_saved_employer_selects_current_role_without_refetching_profile(self):
        self.start()
        self.selected_contact()
        person = self.provider.raw["element"]
        person["linkedinUrl"] = "https://www.linkedin.com/in/another-buyer/"
        person["currentPosition"].append({"companyName": "OtherCo", "title": "Advisor",
            "companyLinkedinUrl": "https://www.linkedin.com/company/otherco/"})
        result = self.lookup(check(phase="contact_verification", tool="harvestapi_get_profile",
            inputs={"url": person["linkedinUrl"]}))["lookups"][0]
        ref = result["results"][0]["ref"]
        calls = len(self.provider.requests)
        # Replaying an older request with no employer context must work too.
        raw, source, receipt = self.tools._resolve(ref)
        request = dict(receipt["attempt"]["request"])
        request.pop("target_company_linkedin_url", None)
        old, _ = deepline.normalize_response(request, receipt["provider_response"])
        self.assertEqual(old["results"][0]["position_review"], "ambiguous")
        self.assertEqual(raw["contact_title"], "Head of Payments")
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Employer matched",
            "primary_contact": {"ref": ref, "requested_role": "Head of Payments", "role_match": "exact"}}])
        saved = json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]
        self.assertEqual((saved["company"], saved["current_title"]), ("ExamplePay", "Head of Payments"))
        self.assertEqual(len(self.provider.requests), calls)

    def test_saved_profile_ref_selects_identity_and_closes_receipt_without_spending(self):
        self.start()
        old_ref = self.selected_contact()
        self.provider.raw["element"].update(firstName="Grace", linkedinUrl="https://www.linkedin.com/in/grace-example/")
        new_ref = self.lookup(check(phase="contact_verification", tool="harvestapi_get_profile",
            inputs={"url": "https://www.linkedin.com/in/grace-example/"}))["lookups"][0]["results"][0]["ref"]
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        result = self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Select another verified buyer",
            "primary_contact": {"profile_ref": new_ref, "requested_role": "Head of Payments", "role_match": "exact"}}])
        doc = json.loads(self.path.read_text())
        person = doc["unresolved"][0]["primary_contact"]
        self.assertEqual((person["full_name"], person["profile_ref"]), ("Grace Example", new_ref))
        self.assertTrue(result["progress"]["completion_candidates"][0]["profile_verified"])
        self.assertNotIn(new_ref.split(":")[0], {r["ref"] for r in runner.pending_source_reviews(doc)})
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        saved = self.path.read_bytes()
        for selection in ({"profile_ref": new_ref.split(":")[0]}, {"ref": new_ref, "profile_ref": old_ref}):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Invalid selection",
                    "primary_contact": selection}])
            self.assertEqual(self.path.read_bytes(), saved)
            self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_company_and_profile_selected_together_resolve_current_employer(self):
        self.start()
        # Without an exact domain, company identity still needs the researcher's selection.
        self.provider.raw["element"].pop("website")
        company_ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Fit reviewed",
            "account_fit": {"ref": company_ref, "text": "Provides payments infrastructure"},
            "qualification_checks": self.qualifying_signal(company_ref)}])
        self.provider.raw = {"status": "ok", "element": {"linkedinUrl": "https://www.linkedin.com/in/ada-example/",
            "firstName": "Ada", "lastName": "Example", "experience": [
                {"companyName": "ExamplePay", "companyLinkedinUrl": "https://www.linkedin.com/company/examplepay/",
                 "position": "Head of Payments", "endDate": {"text": "Present"}},
                {"companyName": "OtherCo", "companyLinkedinUrl": "https://www.linkedin.com/company/otherco/",
                 "position": "Member", "endDate": {"text": "Present"}}],
            "location": {"parsed": {"countryFull": "Singapore"}}}}
        profile_ref = self.lookup(check(phase="contact_verification", tool="harvestapi_get_profile",
            inputs={"url": "https://www.linkedin.com/in/ada-example/"}))["lookups"][0]["results"][0]["ref"]
        self.assertEqual(self.tools._resolve(profile_ref)[0]["position_review"], "ambiguous")
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        result = self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Select company and buyer",
            "company": {"ref": company_ref},
            "primary_contact": {"ref": profile_ref, "requested_role": "Head of Payments", "role_match": "exact"}}])
        person = json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]
        self.assertEqual((person["company"], person["current_title"]), ("ExamplePay", "Head of Payments"))
        self.assertTrue(result["progress"]["completion_candidates"][0]["profile_verified"])
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_native_verified_buyer_clears_stall_but_later_email_gap_is_distinct(self):
        self.start()
        self.selected_contact()
        document = json.loads(self.path.read_text())
        person = document["unresolved"][0].pop("primary_contact")
        before = runner.progress_snapshot(document)
        document["routes"] = [dict(route_id=str(i), scope="example.test", phase="contact_discovery",
            tool="fixture-search", provider_status="ok", approach="people search", progress_before=before) for i in range(2)]
        document["stop_audit"]["route_frontier"] = [dict(route_id=str(i), state="exhausted", reason="Read results") for i in range(2)]
        self.assertEqual(runner.strategy_reminder(document)["count"], 1)
        document["unresolved"][0]["primary_contact"] = person
        self.assertEqual(runner.strategy_reminder(document)["count"], 0)
        self.assertTrue(any(":buyer:" in f for f in runner.progress_snapshot(document)))
        for route in document["routes"]:
            route["progress_before"] = runner.progress_snapshot(document)
        reminder = runner.strategy_reminder(document)
        self.assertEqual(reminder["items"][0]["remaining_work"], "verified buyer contact details")

    def test_source_group_saves_explicit_decisions_without_closing_unreviewed_work(self):
        self.start()
        results = [self.lookup(check(tool="fixture-search", inputs={"query": str(i)}, approach=f"independent source {i}"))["lookups"][0] for i in range(3)]
        refs = [r["route"] for r in results[:2]]
        self.tools.call("tyche_review", {"sources": [{"refs": refs, "state": "exhausted", "reason": "Read both; no further page"}]})
        pending = self.tools.inspect(field="pending_sources")["items"]
        self.assertEqual([r["ref"] for r in pending], [results[2]["route"]])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.tools.review(sources=[{"ref": refs[0], "refs": refs, "state": "exhausted", "reason": "Invalid selection"}])
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, r"input.sources\[0\].refs\[1\].*Saved choices"):
            self.tools.call("tyche_review", {"sources": [{"refs": [results[2]["route"], "missing-lookup"],
                "state": "exhausted", "reason": "Unknown receipt must not partially close the group"}]})
        self.assertEqual(before, self.path.read_bytes())

    def test_selected_open_page_closes_once_but_search_results_stay_open(self):
        self.start()
        for operation in ("open", "search_query"):
            result = self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Reviewed fit only",
                "account_fit": {"ref": "web:0:0"}}], web=[{"target": "example.test", "purpose": "Read source",
                "query": operation, "operation": operation, "response": {"status": "ok", "results": [
                    {"url": "https://example.test/about", "text": "Provides payments infrastructure"}]}}])
            rid = result["web_references"]["web:0"]
            pending = self.tools.inspect(field="pending_sources")["items"]
            self.assertEqual(rid in [r["ref"] for r in pending], operation == "search_query")

    def test_email_reference_removes_clinical_credentials_without_editing_profile(self):
        self.start()
        ref = self.selected_contact(first="Jaime", last="Scourfield, MS, BCBA, LBA")
        self.provider.raw = {"status": "ok", "data": {"email": "jaime@example.test"}}
        self.lookup(check(tool="fixture_email_finder", phase="contact_discovery", contact_ref=ref, inputs={}))
        sent = [r for r in self.provider.requests if r.get("operation") == "execute"
                and r.get("tool") == "fixture_email_finder"]
        self.assertEqual(sent[0]["payload"], {
            "first_name": "Jaime", "last_name": "Scourfield", "domain": "example.test"})
        saved = json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]
        self.assertEqual(saved["full_name"], "Jaime Scourfield, MS, BCBA, LBA")

    def test_signal_corrections_replace_derived_view_and_preserve_contact(self):
        source = {"url": "https://example.test/jobs", "date": "2026-06-01", "date_basis": "published", "text": "One nursing vacancy"}
        check = dict(criterion="hiring", importance="required", status="pass", claim="Rapid hiring", signal="HIRING", evidence=[source])
        row = {"candidate": {"domain": "example.test"}, "stage": "account", "reason_code": "missing_account_evidence", "reason_text": "Review",
               "qualification_checks": [check], "primary_contact": {"full_name": "Saved Buyer", "email": "saved@example.test"},
               "signal_evidence": {"signal": "HIRING", "evidence_text": "Different stale wording"}}
        doc = {"request": {}, "unresolved": [row]}
        update = {"scope": "example.test", "qualification_checks": [dict(check, claim="Source supports one vacancy")], "reason_text": "Correction"}
        saved = research_input.company_update(doc, update)["row"]
        self.assertEqual(saved["signal_evidence"]["evidence_text"], "One nursing vacancy")
        self.assertEqual(saved["primary_contact"], row["primary_contact"])

        legacy = copy.deepcopy(row)
        legacy["signal_evidence"].update(evidence_url="https://example.test/earlier", evidence_date="2026-05-01")
        result = research_input.company_update({"request": {}, "unresolved": [legacy]}, update)["row"]
        self.assertEqual(result["signal_evidence"], legacy["signal_evidence"])
        self.assertEqual(result["qualification_checks"][0]["evidence"], [source])
        corrected = dict(check, status="unknown", claim="A single vacancy does not demonstrate rapid hiring")
        corrected.pop("signal")
        update["qualification_checks"] = [corrected]
        doc["unresolved"] = [saved]
        saved = research_input.company_update(doc, update)["row"]
        self.assertNotIn("signal", saved["qualification_checks"][0])
        self.assertNotIn("signal_evidence", saved)
        self.assertEqual(saved["primary_contact"], row["primary_contact"])

    def test_source_review_changes_invalidate_approval_but_cost_updates_do_not(self):
        doc = {"request": {}, "accepted": [], "unresolved": [], "rejected": [],
               "routes": [{"cost_credits": None}], "stop_audit": {"route_frontier": [
                   {"route_id": "source-1", "state": "exhausted", "reason": "Reviewed"}]}}
        before = runner.review_fingerprint(doc)
        doc["routes"][0]["cost_credits"] = 1
        self.assertEqual(runner.review_fingerprint(doc), before)
        doc["stop_audit"]["route_frontier"][0]["reason"] = "Reopened after a contradictory source"
        self.assertNotEqual(runner.review_fingerprint(doc), before)

    def test_missing_source_excerpt_has_a_specific_diagnostic(self):
        from validate_run import source_evidence_error
        evidence = {"url": "https://example.test/news", "date": "2026-06-01", "date_basis": "published",
                    "source": {"provider": "public_web", "operation": "open", "route_id": "source-1"}}
        self.assertEqual(source_evidence_error(evidence, "qualification"),
                         "qualification requires dated source evidence; missing or invalid: text (supporting source excerpt)")

    def test_profile_gate_precedes_email_spend_and_reuses_current_profile(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Account passed",
            "company": {"ref": ref}, "account_fit": {"ref": ref, "text": "Provides payments"},
            "qualification_checks": self.qualifying_signal(ref)}])
        email = check(phase="email_validation", tool="zerobounce_validate", inputs={"email": "ada@example.test"})
        before = budget.ledger_path(self.path).read_bytes()
        with self.assertRaisesRegex(ValueError, "Email work requires verified identity"):
            self.lookup(email)
        for tool, phase, payload in (
                ("fixture_email_finder", "contact_discovery", {"first_name": "Ada", "last_name": "Example", "domain": "example.test"}),
                ("harvestapi_get_profile", "contact_verification", {"url": "https://www.linkedin.com/in/ada-example/", "findEmail": True})):
            spec = research_input.prepare_lookup({"scope": "example.test", "phase": phase,
                "purpose": "Email enrichment", "max_cost_credits": .2,
                "request": {"operation": "execute", "tool": tool, "payload": payload}})
            with self.subTest(tool=tool), self.assertRaisesRegex(ValueError, "Email work requires verified identity"):
                runner.run_attempt(self.path, spec, execute=lambda *_: self.fail("Provider must not run"))
        self.assertEqual(before, budget.ledger_path(self.path).read_bytes())
        self.assertFalse(any(r.get("tool") == "zerobounce_validate" and r["operation"] == "execute" for r in self.provider.requests))
        # Resolve the profile once and reuse it for both email finding and validation.
        self.provider.raw = {"status": "ok", "element": {"linkedinUrl": "https://www.linkedin.com/in/ada-example/",
            "firstName": "Ada", "lastName": "Example", "email": "ada@example.test", "currentPosition": [{"companyName": "ExamplePay", "title": "Head of Payments",
                "companyLinkedinUrl": "https://www.linkedin.com/company/examplepay/"}]}}
        profile = self.lookup(check(phase="contact_verification", tool="harvestapi_get_profile",
            inputs={"url": "https://www.linkedin.com/in/ada-example/"}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Current identity and role verified",
            "primary_contact": {"ref": profile, "requested_role": "Head of Payments", "role_match": "exact"}}])
        self.provider.raw = {"status": "ok", "data": {"address": "ada@example.test", "status": "valid", "domain_is_catch_all": True}}
        result = self.lookup(email)["lookups"][0]
        self.assertTrue(result["email_decisions"][0]["usable"])
        self.assertFalse(result["email_decisions"][0]["fallback_allowed"])
        saved = self.tools.inspect()["completion_candidates"][0]
        self.assertTrue(saved["profile_verified"])
        self.assertEqual(saved["saved_valid_emails"][0]["email"], "ada@example.test")
        self.assertTrue(saved["recent_email_decisions"][0]["usable"])
        self.assertNotIn("method keeps returning", saved["next"])
        self.assertEqual(sum(r["operation"] == "execute" and r.get("tool") == "harvestapi_get_profile" for r in self.provider.requests), 1)

    def test_missing_company_selection_does_not_look_like_a_wrong_person(self):
        self.start()
        self.provider.raw["element"].pop("website")  # No exact-domain automatic selection.
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Fit reviewed",
            "account_fit": {"ref": ref, "text": "Provides payments infrastructure"},
            "qualification_checks": self.qualifying_signal(ref)}])
        self.provider.raw = {"status": "ok", "element": {"linkedinUrl": "https://www.linkedin.com/in/ada-example/",
            "firstName": "Ada", "lastName": "Example", "currentPosition": [{"companyName": "ExamplePay",
                "companyLinkedinUrl": "https://www.linkedin.com/company/examplepay/", "title": "Head of Payments"}],
            "location": {"parsed": {"countryFull": "Singapore"}}}}
        profile = self.lookup(check(phase="contact_verification", tool="harvestapi_get_profile",
            inputs={"url": "https://www.linkedin.com/in/ada-example/"}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Selected current buyer",
            "primary_contact": {"ref": profile, "requested_role": "Head of Payments", "role_match": "exact"}}])
        paid_calls = lambda: sum(r["operation"] == "execute" for r in self.provider.requests)
        before = budget.ledger_path(self.path).read_bytes(), paid_calls()
        due = self.tools.inspect()["completion_candidates"][0]
        self.assertFalse(due["profile_verified"])
        self.assertTrue(any("company.ref" in message for message in due["missing"]))
        with self.assertRaisesRegex(ValueError, "company.ref"):
            self.lookup(check(tool="zerobounce_validate", contact_ref=profile, inputs={"email": "ada@example.test"}))
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), paid_calls()), before)
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Selected saved company identity",
            "company": {"ref": ref}}])
        self.assertTrue(self.tools.inspect()["completion_candidates"][0]["profile_verified"])
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), paid_calls()), before)

    def test_completion_advice_exposes_missing_fit_source_before_acceptance(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        reviewed = {"target": "example.test", "decision": "qualify_account", "reason": "Fit checks reviewed",
                    "company": {"ref": ref}, "qualification_checks": self.qualifying_signal(ref)}
        result = self.tools.review(companies=[reviewed])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        selected = self.tools.call("tyche_inspect", {"field": "completion_candidates", "limit": 1})
        self.assertEqual(selected["total"], 1)
        self.assertIsNone(selected["next_offset"])
        self.assertEqual(selected["value"], self.tools.inspect()["completion_candidates"])
        for packet in (result["progress"], self.tools.inspect(), {"completion_candidates": selected["value"]}):
            due = packet["completion_candidates"][0]
            self.assertTrue(any("account_fit" in message and "source evidence" in message for message in due["missing"]))
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        repaired = self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact",
            "reason": "Selected the existing fit source", "account_fit": {"ref": ref, "text": "Provides payments infrastructure"}}])
        self.assertFalse(any("account_fit" in message for message in repaired["progress"]["completion_candidates"][0]["missing"]))
        self.assertEqual(len(self.provider.requests), before[2])
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before[1])

    def test_completion_advice_surfaces_legacy_valid_email_with_unfinished_profile(self):
        self.start()
        self.selected_contact()
        self.provider.raw = {"status": "ok", "data": {"address": "ada@example.test", "status": "valid"}}
        self.lookup(check(phase="email_validation", tool="zerobounce_validate", inputs={"email": "ada@example.test"}))
        # Replay the older Arizona shape: paid email saved, profile still missing.
        document = json.loads(self.path.read_text())
        candidate = document["unresolved"][0]
        candidate.pop("primary_contact")
        other = copy.deepcopy(candidate)
        other["candidate"]["domain"] = "other.example"
        document["unresolved"].insert(0, other)
        self.path.write_text(json.dumps(document))
        before = budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        due = self.tools.inspect()["completion_candidates"]
        self.assertEqual(due[0]["target"], "example.test")
        self.assertFalse(due[0]["profile_verified"])
        self.assertEqual(due[0]["saved_valid_emails"][0]["email"], "ada@example.test")
        self.assertIn("Select and review", due[0]["missing"][0])
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)

    def test_banner_single_vacancy_review_keeps_rapid_hiring_unresolved(self):
        self.request["buying_signals"] = [{"kind": "RAPID_HIRING", "query": "Rapid clinical hiring"}]
        self.start()
        finding = {"target": "banner.example", "decision": "hold_account", "reason": "A single vacancy does not establish rapid hiring",
            "qualification_checks": [{"criterion": "rapid hiring", "importance": "required", "status": "unknown",
                "claim": "One current nursing vacancy is insufficient to infer rapid hiring", "signal": "RAPID_HIRING",
                "evidence": [{"ref": "web:0:0"}]}]}
        result = self.tools.review(companies=[finding], web=[{"target": "banner.example", "purpose": "Review hiring strength",
            "query": "https://banner.example/careers", "operation": "open", "response": {"status": "ok", "results": [
                {"url": "https://banner.example/careers", "text": "Banner Health lists one current nursing vacancy."}]}}])
        self.assertEqual(result["progress"]["summary"]["accepted_companies"], 0)
        with self.assertRaisesRegex(ValueError, "required signal coverage"):
            self.tools.review(companies=[{"target": "banner.example", "decision": "qualify_account", "reason": "Try to continue with the unresolved required claim"}])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["unresolved"][0]["qualification_checks"][0]["status"], "unknown")
        self.assertFalse(saved["rejected"])

    def test_wrong_identity_employer_role_or_foreign_profile_cannot_buy_email(self):
        self.start()
        ref = self.selected_contact()
        original = self.path.read_bytes()
        receipt = self.path.parent / "receipts" / (ref.split(":")[0] + ".json")
        raw = receipt.read_bytes()
        variants = [("full_name", "Someone Else"), ("current_title", "Sales Manager"),
                    ("requested_role", "Unrequested role"), ("role_match", "guessed")]
        for field, value in variants:
            doc = json.loads(original)
            doc["unresolved"][0]["primary_contact"][field] = value
            self.path.write_text(json.dumps(doc))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Email work requires verified identity"):
                self.lookup(check(phase="email_validation", tool="zerobounce_validate", inputs={"email": "ada@example.test"}))
        self.path.write_bytes(original)
        for foreign in (False, True):
            saved = json.loads(raw)
            if foreign:
                saved["run_fingerprint"] = "another-run"
            else:
                position = saved["provider_response"]["body"]["element"]["currentPosition"][0]
                position.update(companyName="Other employer", companyLinkedinUrl="https://www.linkedin.com/company/other/")
            receipt.write_text(json.dumps(saved))
            before = budget.ledger_path(self.path).read_bytes()
            with self.assertRaisesRegex(ValueError, "Email work requires verified identity"):
                self.lookup(check(phase="email_validation", tool="zerobounce_validate", inputs={"email": "ada@example.test"}))
            self.assertEqual(before, budget.ledger_path(self.path).read_bytes())

    def test_verified_backup_cannot_unlock_email_work_for_unverified_primary(self):
        self.start()
        self.selected_contact()
        document = json.loads(self.path.read_text())
        row = document["unresolved"][0]
        row["backup_contacts"] = [row.pop("primary_contact")]
        row["primary_contact"] = {"full_name": "Unverified Person"}
        self.path.write_text(json.dumps(document))
        before = budget.ledger_path(self.path).read_bytes()
        with self.assertRaisesRegex(ValueError, "Email work requires verified identity"):
            self.lookup(check(phase="email_validation", tool="zerobounce_validate", inputs={"email": "unverified@example.test"}))
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)

    def test_invalid_saved_role_returns_target_field_and_choices_without_paid_work(self):
        self.start()
        ref = self.selected_contact()
        response = self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact",
            "reason": "Review the selected role", "primary_contact": {
                "requested_role": "Head of Payment Operations", "role_match": "exact"}}])
        missing = response["progress"]["completion_candidates"][0]["missing"]
        self.assertTrue(any("requested_role 'Head of Payment Operations'" in e and '"Head of Payments"' in e for e in missing))
        self.tools.inspect(tool="zerobounce_validate")
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaises(ValueError) as caught:
            self.lookup(check(tool="zerobounce_validate", phase="email_validation", contact_ref=ref,
                inputs={"email": "ada@example.test"}))
        message = str(caught.exception)
        for expected in ("target='example.test'", ref, "requested_role", "Head of Payment Operations", '"Head of Payments"', "Reuse this profile receipt"):
            self.assertIn(expected, message)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_native_contact_review_rejects_role_prose_before_saving(self):
        self.start()
        self.selected_contact()
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for field, value in (("primary_contact", {"role_match": "matched"}),
                             ("backup_contacts", [{"role_match": "Current operations leadership matches the buyer family."}])):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, r"input\.companies\[0\].*role_match.*exact.*normalized.*approved_family"):
                self.tools.call("tyche_review", {"companies": [{"target": "example.test",
                    "decision": "hold_contact", "reason": "Review the selected role", field: value}]})
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_native_contact_role_patch_preserves_saved_profile(self):
        self.start()
        ref = self.selected_contact()
        original = json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]
        for match in ("exact", "normalized", "approved_family"):
            self.tools.call("tyche_review", {"companies": [{"target": "example.test",
                "decision": "hold_contact", "reason": "Review the selected role",
                "primary_contact": {"role_match": match}}]})
            saved = json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]
            self.assertEqual(saved, {**original, "role_match": match})
            self.assertEqual(saved["profile_ref"], ref)

    def test_finish_without_completion_returns_actionable_work_without_export(self):
        self.start()
        self.selected_contact()
        before = budget.ledger_path(self.path).read_bytes()
        with patch("research_tools.subprocess.run") as exported:
            result = self.tools.finish()
        self.assertEqual(result["status"], "needs_research")
        self.assertEqual(result["progress"]["completion_candidates"][0]["target"], "example.test")
        self.assertFalse(result["delivery_allowed"])
        exported.assert_not_called()
        self.assertEqual(before, budget.ledger_path(self.path).read_bytes())

    def test_incomplete_finish_exposes_saved_discovery_reviews_without_more_spending(self):
        self.start()
        lookup = self.lookup(check(target="discovery", phase="account_discovery",
            tool="fixture-search", inputs={"query": "companies"}))["lookups"][0]
        before = budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        with patch("research_tools.subprocess.run") as exported:
            result = self.tools.finish()
        self.assertEqual(result["status"], "needs_research")
        self.assertFalse(result["delivery_allowed"])
        self.assertEqual(result["progress"]["review_due"]["count"], 1)
        self.assertEqual(result["progress"]["review_due"]["sources"], result["pending_sources"])
        self.assertEqual(self.tools.inspect(field="pending_sources")["items"], result["pending_sources"])
        self.assertEqual([s["ref"] for s in result["pending_sources"]], [lookup["route"]])
        self.assertEqual(result["pending_sources"][0]["target"], "discovery")
        self.tools.review(sources=[{"ref": lookup["route"], "state": "exhausted",
            "reason": "Fixture results reviewed; no further page or qualifying evidence."}])
        self.assertNotIn(lookup["route"], [s["ref"] for s in self.tools.finish().get("pending_sources", [])])
        self.assertEqual(self.tools.inspect()["review_due"]["count"], 0)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(before, budget.ledger_path(self.path).read_bytes())
        exported.assert_not_called()

    def test_incomplete_finish_without_candidates_preserves_continue_and_accounting(self):
        self.start()
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        calls = list(self.provider.requests)
        with patch("research_tools.subprocess.run") as exported:
            for _ in range(2):
                result = self.tools.finish()
                self.assertEqual(result["status"], "needs_research")
                self.assertEqual(result["progress"]["stop"], "continue")
                self.assertFalse(result["progress"]["completion_candidates"])
                self.assertFalse(result["pending_sources"])
                self.assertFalse(result["delivery_allowed"])
        exported.assert_not_called()
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)
        self.assertEqual(self.provider.requests, calls)

    def test_source_reminders_ignore_closed_aliases_and_page_all_open_sources(self):
        self.start()
        first = self.lookup(check(target="old-alias"))["lookups"][0]["route"]
        self.tools.review(sources=[{"ref": first, "state": "exhausted", "reason": "Wrong identity; source reviewed"}])
        for i in range(4):
            self.lookup(check(target="discovery", phase="account_discovery", approach=f"source family {i}",
                              tool="fixture-search", inputs={"query": str(i)}))
        progress = self.tools.inspect()["review_due"]
        self.assertEqual((progress["count"], progress["scopes"]), (4, ["discovery"]))
        self.assertEqual(len(progress["sources"]), 3)
        a = self.tools.inspect(field="pending_sources", limit=2)
        b = self.tools.inspect(field="pending_sources", offset=a["next_offset"], limit=2)
        self.assertIsNone(b["next_offset"])
        self.assertEqual(a["items"] + b["items"], self.tools.finish()["pending_sources"])

    def test_strategy_feedback_requires_review_and_allows_another_method(self):
        self.start()
        lookups = [self.lookup(check(tool="fixture-search", inputs={"query": f"variant {i}"},
            approach=f"search wording {i}"))["lookups"][0] for i in range(2)]
        self.assertEqual(self.tools.inspect()["strategy_review"]["count"], 0)
        result = self.tools.review(sources=[{"ref": r["route"], "state": "exhausted",
            "reason": "Read the results; they do not resolve the requested fact."} for r in lookups])
        feedback = result["progress"]["strategy_review"]
        self.assertEqual(feedback["count"], 1)
        self.assertEqual(feedback["items"][0]["sources"], [r["route"] for r in lookups])
        self.assertEqual(feedback["items"][0]["tools"], ["fixture-search"])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.assertEqual(self.tools.inspect(field="strategy_review")["value"], feedback)
        self.assertEqual(before, (self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)))
        # Advisory feedback does not add a dispatch veto or change the ledger.
        self.lookup(check(tool="other-search", inputs={"query": "different evidence source"}, approach="different source"))

    def test_strategy_feedback_clears_when_review_adds_evidence(self):
        self.start()
        lookups = [self.lookup(check(inputs={"url": f"https://example.test/source-{i}"},
            approach=f"company lookup {i}"))["lookups"][0] for i in range(2)]
        self.tools.review(sources=[{"ref": r["route"], "state": "exhausted", "reason": "Reviewed source"}
                                   for r in lookups])
        self.assertEqual(self.tools.inspect()["strategy_review"]["count"], 1)
        ref = lookups[0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Fit verified",
            "company": {"ref": ref}, "account_fit": {"ref": ref, "text": "Provides payments infrastructure"},
            "qualification_checks": self.qualifying_signal(ref)}])
        self.assertEqual(self.tools.inspect()["strategy_review"]["count"], 0)

    def test_strategy_feedback_stays_scoped_and_ignores_pending_or_failed_work(self):
        document = {"request": {"target_count": 5}, "accepted": [], "unresolved": [], "rejected": [],
                    "routes": [], "stop_audit": {"route_frontier": []}}
        def attempt(scope, phase, *, status="no_results", reviewed=True, catalog=False):
            rid = str(len(document["routes"]))
            document["routes"].append(dict(route_id=rid, scope=scope, phase=phase, tool="fixture-search",
                entity_type="tool_catalog" if catalog else "research", provider_status=status,
                approach="query " + rid, progress_before=[]))
            document["stop_audit"]["route_frontier"].append(dict(route_id=rid,
                state="exhausted" if reviewed else "continuable", reason="Reviewed response"))
        attempt("one.test", "account_verification")
        attempt("two.test", "account_verification")
        attempt("one.test", "account_discovery")
        attempt("one.test", "account_verification", catalog=True)
        attempt("one.test", "account_verification", status="timeout")
        self.assertEqual(runner.strategy_reminder(document)["count"], 0)
        attempt("one.test", "account_verification")
        self.assertEqual(runner.strategy_reminder(document)["count"], 1)
        attempt("one.test", "account_verification", reviewed=False)
        self.assertEqual(runner.strategy_reminder(document)["count"], 0)
        for phase in ("contact_verification", "email_validation"):
            attempt("two.test", phase)
            attempt("two.test", phase)
        self.assertEqual(runner.strategy_reminder(document)["count"], 0)

    def test_strategy_feedback_covers_discovery_and_contacts_without_extra_state(self):
        for scope, phase in (("discovery", "account_discovery"), ("one.test", "contact_discovery")):
            with self.subTest(phase=phase):
                document = {"request": {"target_count": 5}, "accepted": [], "unresolved": [], "rejected": [],
                    "routes": [dict(route_id=str(i), scope=scope, phase=phase, tool=f"tool-{i}",
                        approach=f"method {i}", progress_before=[], provider_status="no_results") for i in range(2)],
                    "stop_audit": {"route_frontier": [dict(route_id=str(i), state="exhausted", reason="No fit") for i in range(2)]}}
                original = copy.deepcopy(document)
                feedback = runner.strategy_reminder(document)
                self.assertEqual(feedback["count"], 1)
                self.assertEqual(feedback["items"][0]["target"], scope)
                self.assertEqual(document, original)
                if scope != "discovery":
                    document["rejected"] = [{"company": {"domain": scope}}]
                    self.assertEqual(runner.strategy_reminder(document)["count"], 0)

    def test_completed_email_checks_need_no_manual_source_closure(self):
        self.start()
        self.selected_contact()
        captured_page(self.tools, self.provider, text="valid@example.test invalid@example.test catch-all@example.test unknown@example.test")
        for status in ("valid", "invalid", "catch-all", "unknown"):
            with self.subTest(status=status):
                email = status + "@example.test"
                self.provider.raw = {"status": "ok", "data": {"address": email, "status": status}}
                lookup = self.lookup(check(phase="email_validation", tool="zerobounce_validate",
                    inputs={"email": email}))["lookups"][0]
                document = json.loads(self.path.read_text())
                route = next(r for r in document['stop_audit']['route_frontier'] if r['route_id'] == lookup['route'])
                self.assertEqual(route['state'], 'exhausted')
                self.assertNotIn(lookup['route'], [r['ref'] for r in runner.pending_source_reviews(document)])
                verdict = lookup['email_decisions'][0]
                self.assertEqual(verdict['usable'], status == 'valid')
                self.assertEqual(verdict['fallback_allowed'], status in ('catch-all', 'unknown'))
                self.assertEqual(document['accepted'], [])
                ledger_before = budget.ledger_path(self.path).read_bytes()
                receipt_path = self.path.parent / 'receipts' / (lookup['route'] + '.json')
                receipt_before = receipt_path.read_bytes()
                calls_before = len(self.provider.requests)
                runner.finish_attempt(self.path, lookup['route'], json.loads(receipt_before))
                self.tools.inspect(ref=lookup['route'])
                self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger_before)
                self.assertEqual(receipt_path.read_bytes(), receipt_before)
                self.assertEqual(len(self.provider.requests), calls_before)

    def test_completed_email_check_keeps_uncertain_charge_reserved(self):
        self.start()
        self.selected_contact()
        def execute(request, capture):
            if request["operation"] != "execute":
                return self.provider(request, capture)
            def dispatch():
                raw = {"exit_code": 0, "body": {"status": "ok", "data": {
                    "address": "ada@example.test", "status": "invalid"}}, "stderr": ""}
                capture(raw)
                return deepline.normalize_response(request, raw)
            return budget.guarded_call(request, "deepline", dispatch)
        self.tools.execute = execute
        lookup = self.lookup(check(phase="email_validation", tool="zerobounce_validate",
            inputs={"email": "ada@example.test"}))["lookups"][0]
        document = json.loads(self.path.read_text())
        route = next(r for r in document['stop_audit']['route_frontier'] if r['route_id'] == lookup['route'])
        self.assertEqual(route['state'], 'exhausted')
        charge = list(budget.load_ledger(self.path)['calls'].values())[-1]
        self.assertIsNone(charge.get('actual_credits'))
        self.assertGreater(float(charge['maximum_credits']), 0)
        self.assertFalse(lookup['email_decisions'][0]['usable'])
        self.assertFalse(lookup['email_decisions'][0]['fallback_allowed'])
        self.assertEqual(document['accepted'], [])
        self.assertEqual(budget.audit_ledger(self.path, document), [])

    def test_completion_advice_reuses_recent_failed_email_receipts(self):
        self.start()
        self.selected_contact()
        captured_page(self.tools, self.provider, text=" ".join(f"candidate{i}@example.test" for i in range(4)))
        decisions = []
        for i, status in enumerate(("invalid", "invalid", "invalid", "unknown")):
            email = f"candidate{i}@example.test"
            self.provider.raw = {"status": "ok", "data": {"address": email, "status": status}}
            result = self.lookup(check(phase="email_validation", tool="zerobounce_validate",
                inputs={"email": email}))
            decisions.extend(result["lookups"][0]["email_decisions"])
        due = result["progress"]["completion_candidates"][0]
        self.assertEqual(due["recent_email_decisions"], decisions[-3:])
        self.assertFalse(due["saved_valid_emails"])
        self.assertFalse(due["recent_email_decisions"][0]["fallback_allowed"])
        self.assertTrue(due["recent_email_decisions"][-1]["fallback_allowed"])
        self.assertIn("consult tools.md for another source or method", due["next"])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.assertEqual(self.tools.inspect(field="completion_candidates")["value"][0], due)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_pending_or_wrong_address_email_checks_are_not_auto_closed(self):
        self.start()
        self.selected_contact()
        captured_page(self.tools, self.provider, text="pending@example.test asked@example.test")
        for raw, email in (({"id": "job-1", "status": "pending"}, "pending@example.test"),
                           ({"address": "other@example.test", "status": "valid"}, "asked@example.test")):
            with self.subTest(email=email):
                self.provider.raw = {"status": "ok", "data": raw}
                lookup = self.lookup(check(phase="email_validation", tool="zerobounce_validate",
                    inputs={"email": email}))["lookups"][0]
                document = json.loads(self.path.read_text())
                route = next(r for r in document['stop_audit']['route_frontier'] if r['route_id'] == lookup['route'])
                self.assertNotEqual(route['state'], 'exhausted')
                self.assertEqual(document['accepted'], [])

    def test_native_fallback_reuses_both_receipts_and_exposes_no_repeat_decision(self):
        self.start()
        self.selected_contact()
        self.provider.raw = {"status": "ok", "data": {"address": "ada@example.test", "status": "unknown"}}
        original = self.lookup(check(phase="email_validation", tool="zerobounce_validate", inputs={"email": "ada@example.test"}))["lookups"][0]
        self.assertTrue(original["email_decisions"][0]["fallback_allowed"])
        self.provider.raw = {"status": "ok", "data": {"email": "ada@example.test", "status": "success", "result": "deliverable"}}
        fallback = self.lookup(check(phase="email_validation", tool="bounceban_verify_single", inputs={"email": "ada@example.test"}))["lookups"][0]
        self.assertTrue(fallback["email_decisions"][0]["usable"])
        closed = {r['route_id'] for r in json.loads(self.path.read_text())['stop_audit']['route_frontier'] if r['state'] == 'exhausted'}
        self.assertTrue({original['route'], fallback['route']} <= closed)
        before = budget.ledger_path(self.path).read_bytes()
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact", "reason": "Validated fallback selected",
            "primary_contact": {"email_ref": fallback["results"][0]["ref"]}}])
        saved = json.loads(self.path.read_text())["unresolved"][0]["primary_contact"]["email_validation"]
        self.assertEqual(saved["source"]["route_id"], original["route"])
        self.assertEqual(saved["fallback"]["source"]["route_id"], fallback["route"])
        self.assertEqual(saved["fallback"]["result"], "deliverable")
        reread = self.tools.inspect(ref=original["route"])
        self.assertFalse(reread["email_decisions"][0]["fallback_allowed"])
        self.assertIn("already attempted", reread["email_decisions"][0]["next"])
        self.assertTrue(self.tools.inspect()["completion_candidates"][0]["email_usable"])
        recent = self.tools.inspect()["completion_candidates"][0]["recent_email_decisions"]
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["ref"], fallback["route"])
        self.assertTrue(recent[0]["usable"])
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)

    def test_start_resume_and_cached_describe_need_no_manual_bookkeeping(self):
        self.start()
        result = self.lookup()
        ref = result["lookups"][0]["results"][0]["ref"]
        self.assertEqual(self.tools.inspect(ref=ref)["facts"]["employee_range"], "51-200")
        self.lookup(check("another.test"))
        self.assertEqual(len([r for r in self.provider.requests if r["operation"] == "describe"]), 2)
        ledger = budget.ledger_path(self.path).read_bytes()
        self.start()
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger)
        with self.assertRaisesRegex(ValueError, "already attempted"):
            self.lookup()
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_start_prices_verification_and_reuses_saved_description(self):
        self.request["contact_fields"] = ["email"]
        self.start()
        self.assertEqual(float(budget.load_ledger(self.path)["verification_reserve_credits"]), 1)
        self.tools.inspect(tool="zerobounce_validate")
        self.start()
        self.assertEqual(len(self.provider.requests), 3)

    def test_free_startup_prerequisites_overlap_and_expose_reusable_descriptions(self):
        self.request["contact_fields"] = ["email"]
        ready = threading.Barrier(3)
        def concurrent_catalog(request, capture):
            self.assertEqual(request["operation"], "describe")
            ready.wait(timeout=3)
            return self.provider(request, capture)
        self.tools.execute = concurrent_catalog
        result = self.start()
        expected = ["harvestapi_get_company", "harvestapi_get_profile", "zerobounce_validate"]
        self.assertEqual(result["cached_descriptions"], expected)
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        self.assertEqual(float(budget.load_ledger(self.path)["verification_reserve_credits"]), 1)
        self.tools.execute = lambda *args: self.fail("Saved contracts must not be requested again")
        for tool in expected:
            self.tools.inspect(tool=tool)
        self.assertEqual(self.start()["cached_descriptions"], expected)
        self.assertFalse(budget.load_ledger(self.path)["calls"])

    def test_explicit_verification_reserve_keeps_prerequisite_check(self):
        self.request["contact_fields"] = ["email"]
        self.start(verification_reserve_credits=.4)
        self.assertEqual(float(budget.load_ledger(self.path)["verification_reserve_credits"]), .4)
        self.assertEqual({r["tool"] for r in self.provider.requests},
                         {"zerobounce_validate", "harvestapi_get_company", "harvestapi_get_profile"})

    def test_transient_startup_timeouts_recover_once_without_agent_or_paid_work(self):
        self.request["contact_fields"] = ["email"]
        attempts = {}
        def catalog(request, capture):
            self.assertEqual(request["operation"], "describe")
            tool = request["tool"]
            attempts[tool] = attempts.get(tool, 0) + 1
            if attempts[tool] == 1:
                capture({"timed_out": True, "body": "", "stderr": ""})
                return {"provider": "deepline", "operation": "describe", "tool": tool,
                        "status": "timeout", "results": []}, 2
            return self.provider(request, capture)
        self.tools.execute = catalog
        result = self.start()
        self.assertEqual(sorted(attempts), result["cached_descriptions"])
        self.assertEqual(list(attempts.values()), [2, 2, 2])
        started = json.loads(self.path.read_text())["stop_check"]["started_at"]
        for prefix in ("verification", "company", "profile"):
            archived = list(self.path.parent.glob(prefix + "-tool-*.json"))
            self.assertEqual(len(archived), 1)
            failed = json.loads(archived[0].read_text())
            self.assertEqual(failed["status"], "timeout")
            self.assertTrue(failed["provider_response"]["timed_out"])
            self.assertEqual(failed["started_at"], started)
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        self.tools.execute = lambda *args: self.fail("Successful prerequisites should be reused")
        self.start()

    def test_startup_retry_is_bounded_and_does_not_retry_access_or_schema_failures(self):
        for status, count in (("timeout", 2), ("provider_error", 2), ("auth_failed", 1),
                              ("quota_exceeded", 1), ("rate_limited", 1), ("schema_error", 1)):
            with self.subTest(status=status):
                calls = []
                def catalog(request, capture):
                    calls.append(request)
                    return {"provider": "deepline", "operation": "describe", "tool": request["tool"],
                            "status": status, "results": []}, 2
                self.tools.execute = catalog
                with self.assertRaises(research_tools.OperationalBlock) as error:
                    self.tools._startup_price("zerobounce_validate", status + ".json", {}, "2026-09-01T00:00:00+00:00")
                self.assertIn(status, str(error.exception))
                self.assertEqual(len(calls), count)
                self.assertTrue(all(c["operation"] == "describe" for c in calls))
                self.assertFalse(self.path.exists())
                self.assertFalse(self.path.with_name(self.path.name + ".budget.json").exists())

    def test_unpriced_mandatory_profile_stops_before_research_and_recovers_free(self):
        available = False
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request.get("tool") == "harvestapi_get_profile" and not available:
                body["results"][0]["pricing"] = {"unit": "usage", "creditsPerUnit": None,
                    "displayText": "Calculated after execution from returned usage."}
            return body, code
        self.tools.execute = catalog
        with patch("provider_pricing.profile_price", return_value=None):
            result = self.start()
        self.assertEqual(result["status"], "operationally_blocked")
        self.assertIn("harvestapi_get_profile", result["reason"])
        self.assertFalse(result["delivery_allowed"])
        self.assertEqual(self.tools.inspect()["status"], "operationally_blocked")
        self.assertEqual(self.tools.finish()["status"], "operationally_blocked")
        self.assertEqual(self.lookup()["status"], "operationally_blocked")
        original = json.loads((self.path.parent / "profile-tool.json").read_text())
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".budget.json").exists())
        self.assertTrue(all(r["operation"] == "describe" for r in self.provider.requests))
        available = True
        with patch("provider_pricing.profile_price", return_value=None):
            self.start()
        self.assertEqual(json.loads(self.path.read_text())["stop_check"]["started_at"], original["started_at"])
        self.assertEqual(len(list(self.path.parent.glob("profile-tool-*.json"))), 1)
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        before = len(self.provider.requests)
        self.tools.inspect(tool="harvestapi_get_profile")
        self.start()
        self.assertEqual(len(self.provider.requests), before)

    def test_unavailable_required_company_tool_stops_before_research(self):
        def unavailable(request, capture):
            body, code = self.provider(request, capture)
            body["results"][0]["connected"] = False
            return body, code
        self.tools.execute = unavailable
        result = self.start()
        self.assertEqual(result["status"], "operationally_blocked")
        self.assertIn("required tool is unavailable", result["reason"])
        self.assertFalse(self.path.exists())
        self.assertTrue(all(r["operation"] == "describe" for r in self.provider.requests))

    def test_stored_profile_price_starts_and_records_actual_billing_and_provenance(self):
        self.provider.rate = .03
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request.get("tool") == "harvestapi_get_profile" and request["operation"] == "describe":
                contract = body["results"][0]
                contract["pricing"] = {"unit": "usage", "creditsPerUnit": None}
                contract["inputSchema"]["jsonSchema"]["properties"]["main"] = {"type": "string"}
            return body, code
        self.tools.execute = catalog
        self.start()
        profile = check(tool="harvestapi_get_profile", inputs={"url": "https://www.linkedin.com/in/example", "main": "true"})
        result = self.lookup(profile)
        rid = result["lookups"][0]["route"]
        receipt = runner.read_receipt(self.path, rid)["result"]
        self.assertEqual(receipt["attempt"]["action"]["pricing_basis"]["basis"], "measured_planning_price")
        charge = budget.load_ledger(self.path)["calls"][rid]
        self.assertEqual(float(charge["maximum_credits"]), .03)
        self.assertEqual(float(charge["actual_credits"]), .03)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])
        # A changed actual price must be preserved, block new spending and
        # report the failure without trying to export or reset its ledger.
        self.provider.rate = .04
        result = self.lookup(check("second.test", tool="harvestapi_get_profile",
            inputs={"url": "https://www.linkedin.com/in/second", "main": "true"}))
        self.assertEqual(result["status"], "operationally_blocked")
        charge = budget.load_ledger(self.path)["calls"][result["lookups"][0]["route"]]
        self.assertEqual(float(charge["actual_credits"]), .04)
        before = len(self.provider.requests)
        self.assertEqual(self.lookup(check("third.test"))["status"], "operationally_blocked")
        self.assertEqual(len(self.provider.requests), before)

    def test_profile_planning_rates_are_option_specific_and_catalog_wins(self):
        contract = {"toolId": "harvestapi_get_profile", "pricing": {"unit": "usage", "creditsPerUnit": None}}
        inputs = {"url": "https://www.linkedin.com/in/example"}
        self.assertEqual(self.tools._price(contract, inputs), .05)
        self.assertEqual(self.tools._price(contract, dict(inputs, main="true")), .03)
        self.assertEqual(self.tools._price(contract, dict(inputs, findEmail="true")), .14)
        with self.assertRaisesRegex(ValueError, "below"):
            self.tools._price(contract, dict(inputs, findEmail="true"), .05)
        for extra in ({"findEmail": "false"}, {"main": "true", "findEmail": "true"}, {"skipSmtp": "true"},
                      {"includeAboutProfile": "true"}, {"main": "false"}, {"main": True}, {"newAddon": "true"}):
            for override in (None, .05, 1):
                with self.subTest(extra=extra, override=override), self.assertRaisesRegex(ValueError, "No whole-call price"):
                    self.tools._price(contract, dict(inputs, **extra), override)
        contract["pricing"] = {"unit": "call", "creditsPerUnit": .08}
        self.assertEqual(self.tools._price(contract, dict(inputs, main="true")), .08)
        contract["pricing"] = {"unit": "usage", "creditsPerUnit": .08}
        with self.assertRaisesRegex(ValueError, "No whole-call price"):
            self.tools._price(contract, inputs)

    def test_profile_description_exposes_literal_priced_options(self):
        contract = {"toolId": "harvestapi_get_profile", "pricing": {"unit": "usage", "creditsPerUnit": None}}
        prices = self.tools._description_view(contract)["stored_planning_prices"]
        self.assertEqual([p["inputs"] for p in prices], [{"main": "true"}, {}, {"findEmail": "true"}])
        for price in prices:
            inputs = {"url": "https://www.linkedin.com/in/example", **price["inputs"]}
            self.assertEqual(self.tools._price(contract, inputs), price["credits"])
        contract["pricing"] = {"unit": "usage", "creditsPerUnit": .08}
        with self.assertRaisesRegex(ValueError, "published catalog rate takes precedence") as error:
            self.tools._price(contract, {"findEmail": "true"})
        self.assertNotIn("Stored-price optional input sets", str(error.exception))

    def test_unpriced_email_options_return_fix_without_spend_or_identity_rework(self):
        self.provider.rate = .03
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request.get("tool") == "harvestapi_get_profile" and request["operation"] == "describe":
                contract = body["results"][0]
                contract["pricing"] = {"unit": "usage", "creditsPerUnit": None}
                contract["inputSchema"]["jsonSchema"]["properties"]["main"] = {"type": "string"}
            return body, code
        self.tools.execute = catalog
        self.start()
        profile = self.selected_contact()
        ledger = budget.ledger_path(self.path).read_bytes()
        requests = len(self.provider.requests)
        email = check(tool="harvestapi_get_profile", contact_ref=profile,
                      inputs={"findEmail": "true", "main": "full_email"})
        with self.assertRaisesRegex(ValueError, r"input.checks\[0\].inputs.*No whole-call price") as error:
            self.lookup(email)
        self.assertIn('{"findEmail": "true"}', str(error.exception))
        self.assertIn("omit other options", str(error.exception))
        self.assertIn("No paid call was made", str(error.exception))
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger)
        self.assertEqual(len(self.provider.requests), requests)
        email["inputs"] = {"findEmail": "true"}
        self.lookup(email)
        sent = self.provider.requests[-1]
        self.assertEqual(sent["payload"], {"findEmail": "true", "url": "https://www.linkedin.com/in/ada-example/"})
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_required_auth_failure_blocks_discovery_without_rejecting_companies(self):
        self.start()
        self.provider.raw = {"error": "unauthorized API key"}
        result = self.lookup()
        self.assertEqual(result["status"], "operationally_blocked")
        self.assertIn("auth_failed", result["reason"])
        self.assertEqual(json.loads(self.path.read_text())["rejected"], [])
        calls = len(self.provider.requests)
        self.assertEqual(self.lookup(check("second.test"))["status"], "operationally_blocked")
        self.assertEqual(len(self.provider.requests), calls)
        ledger = budget.ledger_path(self.path).read_bytes()
        self.tools.inspect(tool="harvestapi_get_company", refresh=True)
        self.assertIsNone(self.tools.inspect()["operational_block"])
        self.assertEqual(json.loads((self.path.parent / "operational-status.json").read_text())["status"], "ready")
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger)
        with self.assertRaisesRegex(ValueError, "already attempted"):
            self.lookup()

    def test_no_result_is_a_research_gap_not_an_operational_block(self):
        self.start()
        self.provider.raw = {"status": "completed", "toolResponse": {
            "rawV2": {"error": None, "status": 200, "element": None}}}
        result = self.lookup()
        self.assertNotIn("status", result)
        self.assertIsNone(result["progress"]["operational_block"])
        route = result["lookups"][0]["route"]
        saved = self.tools._receipt(route)["result"]
        self.assertEqual(saved["status"], "no_results")
        self.assertEqual(float(budget.load_ledger(self.path)["calls"][route]["actual_credits"]), self.provider.rate)

    def test_complete_failed_response_does_not_request_receipt_recovery(self):
        self.start()
        self.provider.raw = {"unrecognized_payload": True}
        route = self.lookup()["lookups"][0]["route"]
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "complete.*schema_error") as error:
            self.tools.inspect(ref=route + ":0")
        self.assertIn(route, str(error.exception))
        self.assertNotIn("recover its receipt", str(error.exception))
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_launcher_clock_includes_setup_and_is_preserved_on_resume(self):
        from datetime import datetime, timedelta, timezone
        started = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        with patch.dict(os.environ, {"TYCHE_RUN_STARTED_AT": started}):
            self.start()
        self.assertEqual(json.loads(self.path.read_text())["stop_check"]["started_at"], started)
        self.assertGreaterEqual(self.tools.inspect()["elapsed_seconds"], 90)
        with patch.dict(os.environ, {"TYCHE_RUN_STARTED_AT": datetime.now(timezone.utc).isoformat()}):
            self.start()
        self.assertEqual(json.loads(self.path.read_text())["stop_check"]["started_at"], started)

    def test_research_budget_refusal_explains_protected_verification_reserve(self):
        self.request["contact_fields"] = ["email"]
        self.start(max_usd=.05, verification_reserve_credits=.4)
        with self.assertRaisesRegex(ValueError, "reserved for email verification") as error:
            self.lookup()
        self.assertIn("cap $0.05", str(error.exception))
        self.assertIn("Verification can use its reserve", str(error.exception))
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        document = json.loads(self.path.read_text())
        action = document["stop_check"]["next_actions"][0]
        self.assertEqual(action["scope"], "example.test")
        self.assertIn(action["id"], self.tools.inspect()["blocked_actions"])
        self.assertNotIn(action["id"], {r["route_id"] for r in document["routes"]})
        self.assertFalse((self.path.parent / "receipts" / (action["id"] + ".json")).exists())
        budget.check_allowance(budget.load_ledger(self.path), "deepline", .2, 0, verification=True)

    def test_email_finder_is_discovery_and_cannot_claim_verification_reserve(self):
        spec = research_input.prepare_lookup({"provider": "deepline", "scope": "example.test", "phase": "contact_discovery",
            "purpose": "Find the selected person's work email", "max_cost_credits": 1,
            "request": {"operation": "execute", "tool": "zerobounce_email_finder", "payload": {"first_name": "Alex", "last_name": "Buyer", "domain": "example.test"}}})
        _, action, request = runner._validate_spec(copy.deepcopy(spec))
        self.assertEqual(action["phase"], "contact_discovery")
        self.assertNotEqual(request.get("entity_type"), "email_validation")
        spec["action"]["phase"] = "email_validation"
        with self.assertRaisesRegex(ValueError, "cannot spend its protected reserve"):
            runner._validate_spec(spec)
        for tool in ("zerobounce_email_finder", "zerobounce_get_credits", "zerobounce_score"):
            self.assertIsNone(email_receipts.validator_for_tool(tool))
        for tool, family in (("zerobounce_validate", "zerobounce"), ("bounceban_verify_single", "bounceban"),
                             ("bounceban_get_verification", "bounceban"), ("bounceban_verify_single_result", "bounceban")):
            self.assertEqual(email_receipts.validator_for_tool(tool), family)

    def test_single_verification_cannot_be_resubmitted_as_a_free_status_read(self):
        contract = {"toolId": "bounceban_verify_single", "inputSchema": {"jsonSchema": {"properties": {"email": {"type": "string"}}}},
                    "pricing": {"unit": "result", "creditsPerUnit": .06}}
        self.assertEqual(self.tools._price(contract, {"email": "buyer@example.test"}), .06)
        with self.assertRaisesRegex(ValueError, "below the catalog-derived"):
            self.tools._price(contract, {"email": "buyer@example.test"}, 0)
        spec = research_input.prepare_lookup({"provider": "deepline", "scope": "example.test", "phase": "email_validation",
            "purpose": "Read pending verification", "max_cost_credits": 0, "status_read": True,
            "request": {"operation": "execute", "tool": "bounceban_verify_single", "payload": {"email": "buyer@example.test"}}})
        with self.assertRaisesRegex(ValueError, "do not resubmit"):
            runner._validate_spec(spec)

    def test_missing_current_title_or_employer_cannot_pass_delivery(self):
        from test_client_output import client_document
        document = client_document()
        document["accepted"][0]["primary_contact"].update(current_title=None, company=None)
        errors = runner.accepted_errors(document)
        for field in ("current_title", "company"):
            self.assertTrue(any("primary_contact." + field in error and "verified current value" in error for error in errors))

    def test_spending_pause_preserves_review_without_clearing_ledger_or_allowing_calls(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        reason = "provider billed above its reserved bound; reconcile pricing before further paid calls"
        with budget.transaction(budget.ledger_path(self.path)) as ledger:
            ledger["blocked"] = reason
        before = budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        result = self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Funding remains unverified", "company": {"ref": ref}}])
        self.assertEqual(result["progress"]["budget"]["blocked"], reason)
        self.assertEqual(json.loads(self.path.read_text())["unresolved"][0]["reason_text"], "Funding remains unverified")
        blocked = self.lookup(check("another.test"))
        self.assertEqual(blocked["status"], "operationally_blocked")
        with patch.object(research_tools.subprocess, "run") as export:
            self.assertEqual(self.tools.finish()["status"], "operationally_blocked")
            export.assert_not_called()
        self.assertFalse(blocked["delivery_allowed"])
        self.assertEqual(json.loads(Path(blocked["status_file"]).read_text())["reason"], reason)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)

    def test_all_selected_field_conflicts_are_reported_before_web_is_saved(self):
        self.start()
        self.provider.raw["element"]["locations"] = [{"headquarter": True, "country": "Canada"}]
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        before = self.path.read_bytes()
        receipts = sorted((self.path.parent / "receipts").glob("*.json"))
        web = [{"target": "example.test", "purpose": "Review the announcement", "query": "example.test news",
                "response": {"status": "ok", "results": [{"url": "https://example.test/news", "text": "An observed company announcement"}]}}]
        company = {"target": "example.test", "decision": "hold_account", "reason": "Funding still needs research",
                   "company": {"ref": ref, "website": "https://www.example.test/", "hq_country": "United States",
                               "employee_range_evidence": {}}}
        with self.assertRaises(ValueError) as error:
            self.tools.review(companies=[company], web=web)
        for field in ("website", "hq_country", "employee_range_evidence"):
            self.assertIn(field, str(error.exception))
        self.assertIn("Keep the selected ref", str(error.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted((self.path.parent / "receipts").glob("*.json")), receipts)
        company["company"] = {"ref": ref}
        self.tools.review(companies=[company], web=web)
        self.assertEqual(len(json.loads(self.path.read_text())["unresolved"]), 1)

    def test_missing_company_hq_does_not_discard_reviewed_fields(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        facts = self.tools._harvest({"ref": ref, "hq_country": "United States", "hq_state": "Minnesota"}, "example.test")
        self.assertEqual(facts["hq_country"], "United States")
        self.assertEqual(facts["hq_state"], "Minnesota")
        self.assertEqual(facts["employee_range"], "51-200")

    def test_publication_date_alias_is_retained_and_missing_date_is_not_invented(self):
        self.start()
        result = self.tools.review(web=[{"target": "example.test", "purpose": "Review publication", "query": "example.test news",
            "response": {"status": "ok", "results": [
                {"url": "https://example.test/news", "text": "A dated announcement", "published_date": "2026-02-09"},
                {"url": "https://example.test/about", "text": "Current company information"}]}}])
        route = result["web_references"]["web:0"]
        evidence = self.tools._evidence({"ref": route + ":0", "date_basis": "published"})
        self.assertEqual(evidence["date"], "2026-02-09")
        with self.assertRaisesRegex(ValueError, "cannot replace captured metadata"):
            self.tools._evidence({"ref": route + ":1", "date_basis": "published"})
        current = self.tools._evidence({"ref": route + ":1"})
        self.assertEqual(current["date_basis"], "observed_current")

    def test_undated_capture_accepts_only_its_effective_observation_date(self):
        self.start()
        ref = captured_page(self.tools, self.provider, date=None,
                            text="The annual results report group operating profit for 2025.")
        receipt = self.path.parent / "receipts" / (ref.split(":")[0] + ".json")
        original = receipt.read_bytes()
        implicit = self.tools._evidence({"ref": ref})
        explicit = self.tools._evidence({"ref": ref, "date": implicit["date"],
                                         "date_basis": "observed_current"})
        self.assertEqual(explicit, implicit)
        self.assertEqual(explicit["date"], self.tools._document()["request"]["as_of_date"])
        for override in ({"date": "2000-01-01"}, {"date_basis": "published"}):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, "cannot replace captured metadata"):
                self.tools._evidence({"ref": ref, **override})
        self.assertEqual(receipt.read_bytes(), original)

    def test_funding_reference_supplies_saved_date_and_text_without_manual_copy(self):
        self.start()
        rows = [
            {"name": "Series B - ExamplePay", "announcedOn": "2024-11-28T00:00:00.000Z"},
            {"name": "Undated round - ExamplePay"}]
        self.provider.raw = {"toolResponse": {"rawV2": {"fundingRounds": rows}},
                             "output_preview": {"kind": "list", "rowCount": len(rows), "preview": rows}}
        results = self.lookup(check(tool="aviato_get_company_funding_rounds", inputs={"website": "https://example.test"}))["lookups"][0]["results"]
        evidence = self.tools._evidence({"ref": results[0]["ref"]})
        self.assertEqual((evidence["date"], evidence["date_basis"], evidence["text"]),
                         ("2024-11-28", "published", "Series B - ExamplePay"))
        with self.assertRaisesRegex(ValueError, "no publication/event date"):
            self.tools._evidence({"ref": results[1]["ref"]})

    def test_url_free_funding_reuses_raw_receipt_for_company_review(self):
        self.request["icp"]["required_attributes"] = ["Funding stage is Series C or later"]
        self.start()
        company_ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        funding_ref = self.saved_funding()
        before = budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        self.tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Reviewed stage and fit",
            "company": {"ref": company_ref}, "account_fit": {"ref": company_ref},
            "qualification_checks": self.qualifying_signal(company_ref) + [{"requirement_ref": "attribute:0", "status": "pass",
                "claim": "Funding history records Series C; this is a stage check, not a recent event", "evidence": [{"ref": funding_ref}]}]}])
        saved = json.loads(self.path.read_text())
        evidence = saved["unresolved"][0]["qualification_checks"][-1]["evidence"][0]
        self.assertIsNone(evidence["url"])
        self.assertEqual(evidence["source"]["result_index"], 0)
        self.assertFalse(runner.qualification_errors(saved, run_file=self.path))
        self.assertTrue(runner.qualification_errors(saved))  # No receipt access cannot pass.
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        self.assertEqual(packet["sources"][funding_ref]["record"]["id"], "round-c")
        self.assertEqual(packet["sources"][funding_ref]["date"], "2024-11-28")
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)

    def test_url_free_funding_rejects_wrong_identity_tampering_and_signal_use(self):
        from source_receipts import funding_record
        from validate_run import qualification_evidence_error, source_evidence_error
        self.request["icp"]["required_attributes"] = ["Funding stage is Series C or later"]
        self.start()
        ref = self.saved_funding()
        evidence = self.tools._evidence({"ref": ref})
        document = json.loads(self.path.read_text())
        company = {"domain": "example.test"}
        check_value = {"criterion": self.request["icp"]["required_attributes"][0]}
        receipt_path = self.path.parent / "receipts" / (ref.split(":")[0] + ".json")
        receipt_bytes = receipt_path.read_bytes()
        original = json.loads(receipt_bytes)
        for label, replacement in (("foreign run", {"run_fingerprint": "other"}),
                ("wrong request", {"request_fingerprint": "other"}),
                ("incomplete", {"receipt_status": "pending"}),
                ("unknown outcome", {"status": "error"}),
                ("pending", {"pending_verification": {"id": "pending"}}),
                ("missing raw", {"provider_response": None})):
            with self.subTest(label=label):
                receipt_path.write_text(json.dumps({**original, **replacement}))
                with self.assertRaises(ValueError):
                    funding_record(self.path, document, company, evidence)
        receipt_path.write_bytes(receipt_bytes)
        for field, value in (("date", "2026-01-01"), ("text", "Different claim"), ("date_basis", "observed_current")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                funding_record(self.path, document, company, {**evidence, field: value})
        for field, value in (("result_index", 3), ("result_index", True), ("tool", "web_search")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                funding_record(self.path, document, company, {**evidence, "source": {**evidence["source"], field: value}})
        with self.assertRaisesRegex(ValueError, "identify this company"):
            funding_record(self.path, document, {"domain": "other.test"}, evidence)
        altered = copy.deepcopy(original)
        altered["attempt"]["request"]["payload"]["website"] = "https://other.test"
        receipt_path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            funding_record(self.path, document, {"domain": "other.test"}, evidence)
        altered = copy.deepcopy(original)
        altered["results"][0]["stage"] = "Invented Series D"
        receipt_path.write_text(json.dumps(altered))
        self.assertEqual(funding_record(self.path, document, company, evidence)["stage"], "Series C")
        altered["provider_response"]["body"]["toolResponse"]["rawV2"]["fundingRounds"][0]["website"] = "https://other.test"
        altered["provider_response"]["body"]["output_preview"]["preview"][0]["website"] = "https://other.test"
        receipt_path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(ValueError, "different company"):
            funding_record(self.path, document, company, evidence)
        receipt_path.write_bytes(receipt_bytes)
        self.assertIsNone(qualification_evidence_error(evidence, "stage", document, company, check_value, self.path))
        for disallowed in ({**check_value, "signal": "FUNDING"}, {"criterion": "unrequested stage"}):
            self.assertIn("signals require a source URL", qualification_evidence_error(evidence, "signal", document, company, disallowed, self.path))
        self.assertIn("HTTP/HTTPS", source_evidence_error(evidence, "account_fit"))
        receipt_path.unlink()
        self.assertIn("No such file", qualification_evidence_error(evidence, "stage", document, company, check_value, self.path))

    def test_compatible_result_reviews_merge_into_one_saved_route_decision(self):
        self.start()
        observed = self.tools.review(web=[{"target": "discovery", "purpose": "Read funding sources", "query": "funding sources",
            "response": {"status": "ok", "results": [{"text": "First source"}, {"text": "Second source"}]}}])
        rid = observed["web_references"]["web:0"]
        self.tools.review(sources=[{"ref": rid + ":0", "state": "exhausted", "reason": "First source reviewed"},
                                   {"ref": rid + ":1", "state": "exhausted", "reason": "Second source reviewed"}])
        saved = json.loads(self.path.read_text())
        route = next(r for r in saved["stop_audit"]["route_frontier"] if r["route_id"] == rid)
        self.assertEqual(route["state"], "exhausted")
        self.assertEqual(route["reason"], "First source reviewed\nSecond source reviewed")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "conflicting source decisions"):
            self.tools.review(sources=[{"ref": rid + ":0", "state": "exhausted", "reason": "Done"},
                                       {"ref": rid + ":1", "state": "blocked", "reason": "Not reviewed"}])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_attached_web_date_fails_before_saving_and_corrected_retry_works(self):
        self.start()
        web = [{"target": "example.test", "purpose": "Review event", "query": "example event",
                "response": {"status": "ok", "results": [{"url": "https://example.test/news", "text": "Opened a plant"}]}}]
        companies = [{"target": "example.test", "decision": "hold_account", "reason": "More evidence needed",
                      "signal_evidence": {"ref": "web:0:0", "signal": "FACILITY_OPENING", "date_basis": "published"}}]
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "cannot replace captured metadata"):
            self.tools.review(companies=companies, web=web)
        self.assertEqual(self.path.read_bytes(), before)
        web[0]["response"]["results"][0]["published_date"] = json.loads(self.path.read_text())["request"]["as_of_date"]
        result = self.tools.review(companies=companies, web=web)
        self.assertIn("web:0", result["web_references"])

    def test_crossed_company_web_indexes_fail_before_saving_and_can_be_corrected(self):
        self.start()
        web = [{"target": target, "purpose": "Review company news", "query": target,
                "response": {"status": "ok", "results": [{"url": f"https://{target}/news",
                    "text": f"Announcement about {target}"}]}}
               for target in ("first.test", "second.test")]
        company = {"target": "second.test", "decision": "hold_account", "reason": "Review fit",
                   "account_fit": {"ref": "web:0:0"}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "web:0:0.*first.test.*second.test.*web:1:0"):
            self.tools.review(companies=[company], web=web)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        company["account_fit"]["ref"] = "web:1:0"
        saved = self.tools.review(companies=[company], web=web)
        row = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(row["account_fit"]["evidence_url"], "https://second.test/news")
        # A deliberately shared saved source remains available; scope is not a semantic gate.
        company["target"] = "first.test"
        company["account_fit"]["ref"] = saved["web_references"]["web:1"] + ":0"
        self.tools.review(companies=[company])
        self.assertEqual(len(json.loads(self.path.read_text())["unresolved"]), 2)

    def test_shared_discovery_observation_remains_usable_with_company_observations(self):
        self.start()
        web = [{"target": target, "purpose": "Review source", "query": target,
                "response": {"status": "ok", "results": [{"url": "https://news.test/shared",
                    "text": "A shared announcement."}]}}
               for target in ("discovery", "example.test")]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Shared source reviewed", "account_fit": {"ref": "web:0:0"}}], web=web)
        row = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(row["account_fit"]["evidence_url"], "https://news.test/shared")

    def test_review_exposes_judgment_under_review_without_state_changes(self):
        self.request["icp"]["required_attributes"] = ["Operates multiple sites"]
        self.request["buying_signals"] = [{"kind": "EXPANSION", "importance": "preferred",
            "query": "Completed expansion into a new market", "max_age_days": 365}]
        self.start()
        row = {"qualification_checks": [
            {"criterion": "operates multiple sites", "importance": "required", "status": "pass", "evidence": []},
            {"criterion": "expansion", "signal": "EXPANSION", "importance": "preferred", "status": "pass",
             "claim": "An expansion was proposed", "evidence": []},
            {"criterion": "hiring", "signal": "HIRING", "importance": "preferred", "status": "unknown",
             "claim": "Hiring has not been established", "evidence": []}]}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        original = copy.deepcopy(row)
        view = self.tools._company_review(row, {})
        self.assertEqual(view["qualification_checks"][0]["requirement"]["label"], "Operates multiple sites")
        signal = view["signal_checks"][0]
        self.assertEqual(signal["requirement"]["query"], "Completed expansion into a new market")
        self.assertEqual(signal["requirement"]["ref"], "signal:0")
        self.assertEqual(signal["draft_claim"], "An expansion was proposed")
        self.assertEqual(len(view["qualification_checks"]), 1)
        self.assertEqual([c["recorded_status"] for c in view["signal_checks"]], ["pass", "unknown"])
        for check in view["signal_checks"] + view["qualification_checks"]:
            self.assertNotIn("status", check)
            self.assertNotIn("claim", check)
        self.assertEqual(row, original)
        self.assertNotIn("verified_signals", view)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_failed_judgment_returns_reusable_saved_web_reference(self):
        self.start()
        web = [{"target": "example.test", "purpose": "Read observed page", "query": "observed page",
                "response": {"status": "ok", "results": [{"url": "https://example.test", "text": "Verified page text"}]}}]
        with self.assertRaisesRegex(ValueError, "Web observations were saved as") as error:
            self.tools.review(web=web, companies=[{"target": "example.test", "decision": "accept", "reason": "Missing contact evidence"}])
        rid = json.loads(self.path.read_text())["routes"][-1]["route_id"]
        self.assertIn(rid, str(error.exception))
        self.assertEqual(self.tools.inspect(ref=rid + ":0")["facts"]["text"], "Verified page text")

    def test_native_review_contract_names_judgment_fields_before_saving_observations(self):
        self.start()
        payload = {"companies": [{"target": "example.test", "decision": "hold_account", "reason": "Review fit",
            "qualification_checks": [{"criterion": "product", "status": "unknown", "evidence": []}]}],
            "web": [{"target": "example.test", "purpose": "Review source", "query": "company information",
                "response": {"status": "ok", "results": [{"url": "https://example.test", "text": "Observed facts"}]}}]}
        before = self.path.read_bytes()
        receipt_count = len(list((self.path.parent / "receipts").glob("*.json"))) if (self.path.parent / "receipts").exists() else 0
        with self.assertRaisesRegex(ValueError, "missing fields: claim"):
            self.tools.call("tyche_review", payload)
        check = payload["companies"][0]["qualification_checks"][0]
        check.update(importance="required", claim="Product fit still needs review",
            evidence=[{"ref": "web:0:0", "date_basis": "2026-09-14"}])
        with self.assertRaisesRegex(ValueError, "date_basis must be one of"):
            self.tools.call("tyche_review", payload)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(list((self.path.parent / "receipts").glob("*.json"))), receipt_count)
        check["evidence"][0]["date_basis"] = "observed_current"
        result = self.tools.call("tyche_review", payload)
        self.assertEqual(result["saved_companies"], ["example.test"])

    def test_signal_age_is_checked_at_account_gate_and_delivery(self):
        request = {"as_of_date": "2026-09-14", "time_window": {"max_age_days": 365},
                   "buying_signals": [{"kind": "FACILITY_OPENING", "max_age_days": 365}, {"kind": "HIRING", "max_age_days": 90}]}
        row = {"stage": "contact", "candidate": {"domain": "example.test"},
               "signal_evidence": {"signal": "FACILITY_OPENING", "evidence_date": "2025-04-01"}}
        document = {"request": request, "unresolved": [row]}
        self.assertIn("event_date is required", ";".join(runner.qualification_errors(document)))
        row["stage"] = "account"
        self.assertEqual(runner.qualification_errors(document), [])
        row["stage"] = "contact"
        for date, valid in [("2025-09-14", True), ("2025-09-13", False), ("2026-09-15", False)]:
            row["signal_evidence"]["evidence_date"] = date
            row["signal_evidence"]["event_date"] = date
            self.assertEqual(not runner.qualification_errors(document), valid)
        row["signal_evidence"] = {"signal": "HIRING", "evidence_date": "2026-06-15", "event_date": "2026-06-15"}
        self.assertIn("2026-06-16 through 2026-09-14", ";".join(runner.qualification_errors(document)))
        document["accepted"], document["unresolved"] = [row], []
        self.assertIn("2026-06-16 through 2026-09-14", ";".join(runner.qualification_errors(document)))

    def test_signal_date_preflight_leaves_rejected_web_batch_unsaved(self):
        self.start()
        date = json.loads(self.path.read_text())["request"]["as_of_date"]
        web = [{"target": target, "purpose": "Review source", "query": target, "operation": "open",
                "response": {"status": "ok", "results": [{"url": "https://" + target,
                    "date": date, "text": "Observed company announcement."}]}}
               for target in ("one.test", "two.test")]
        companies = [{"target": w["target"], "decision": "qualify_account", "reason": "Reviewed company",
            "qualification_checks": [{"requirement_ref": "signal:0", "status": "pass", "claim": "Partnership confirmed",
                "evidence": [{"ref": f"web:{i}:0", "event_date": date}]}]}
            for i, w in enumerate(web)]
        hiring = {"requirement_ref": "signal:1", "status": "pass", "claim": "Hiring observed",
                  "evidence": [{"ref": "web:1:0", "event_date": "2020-01-01"}]}
        companies[1]["qualification_checks"].append(hiring)
        def snapshot():
            return (self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(),
                    {p.name: p.read_bytes() for p in (self.path.parent / "receipts").glob("*.json")},
                    len(self.provider.requests))
        before = snapshot()
        for invalid, message in [("2020-01-01", "not wholly within the requested window"), (None, "event_date is required")]:
            if invalid:
                hiring["evidence"][0]["event_date"] = invalid
            else:
                hiring["evidence"][0].pop("event_date")
            with self.assertRaisesRegex(ValueError, message) as error:
                self.tools.review(companies=companies, web=web)
            self.assertIn("input.companies[1] (two.test)", str(error.exception))
            self.assertIn("No attached web observations", str(error.exception))
            self.assertEqual(snapshot(), before)
        hiring["status"] = "unknown"
        # Date corrections alone cannot make model-transcribed text qualify.
        with self.assertRaisesRegex(ValueError, "tool-captured"):
            self.tools.review(companies=companies, web=web)
        for item in companies:
            item["decision"] = "hold_account"
        result = self.tools.review(companies=companies, web=web)
        self.assertEqual(result["saved_companies"], ["one.test", "two.test"])
        self.assertEqual(len(result["web_references"]), 2)
        receipts = snapshot()[2]
        self.tools.review(companies=companies, web=web)
        self.assertEqual(snapshot()[2], receipts)

    def test_schema_and_unknown_price_fail_before_paid_dispatch(self):
        self.start()
        with self.assertRaises(ValueError):
            self.lookup(check(inputs={"wrong": "field"}))
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        self.provider.rate = None
        with self.assertRaisesRegex(ValueError, "whole-call price"):
            self.lookup(check(tool="unknown-priced-tool", inputs={"query": "fit"}))
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        for bad in ({"checks": []}, {"checks": [check()] * 4}, {"checks": [dict(check(), command="echo")]}, {"checks": [check(max_cost_credits=float("nan"))]}):
            with self.assertRaises(ValueError):
                self.tools.call("tyche_lookup", bad)

    def test_failed_free_pricing_read_can_resume_without_resetting_clock(self):
        self.request["contact_fields"] = ["email"]
        self.tools.execute = lambda request, capture: ({"provider":"deepline", "operation":"describe", "status":"provider_error", "results":[]}, 2)
        blocked = self.start()
        self.assertEqual(blocked["status"], "operationally_blocked")
        self.assertIn("price unavailable", blocked["reason"])
        original = json.loads((self.path.parent / "verification-tool.json").read_text())
        self.tools.execute = self.provider
        self.start()
        self.assertEqual(json.loads(self.path.read_text())["stop_check"]["started_at"], original["started_at"])
        self.assertEqual(len(list(self.path.parent.glob("verification-tool-*.json"))), 2)
        self.assertFalse(budget.load_ledger(self.path)["calls"])

    def test_multiple_batches_share_three_dispatch_slots_and_lose_no_writes(self):
        self.start()
        self.provider.delay = .1
        batches = [[check(f"company-{i}-{j}.test") for j in range(3)] for i in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda batch: self.lookup(*batch), batches))
        self.assertEqual(sum(len(r["lookups"]) for r in results), 9)
        self.assertEqual(self.provider.peak, 3)
        self.assertEqual(len(budget.load_ledger(self.path)["calls"]), 9)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_receipt_recovery_uses_saved_response_without_redispatch(self):
        self.start()
        self.tools.inspect(tool="harvestapi_get_company")
        with patch.object(runner, "finish_attempt", side_effect=OSError("interrupted after capture")), self.assertRaises(OSError):
            self.lookup()
        rid = next(iter(budget.load_ledger(self.path)["calls"]))
        self.assertEqual(self.tools.inspect()["pending"][0]["ref"], rid)
        calls = len(self.provider.requests)
        self.tools.inspect(recover=rid)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])
        with self.assertRaisesRegex(ValueError, "already attempted"):
            self.lookup()

    def test_cap_and_uncertain_charges_survive_native_retry(self):
        self.start(max_usd=.025)
        result = self.lookup(*[check(f"company-{i}.test") for i in range(3)])
        self.assertEqual(len(result["lookups"]), 3)
        self.assertEqual(len(budget.load_ledger(self.path)["calls"]), 1)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

        other = ResearchTools(self.path.parent.parent / "uncertain/results.json", execute=lambda request, capture:
            self.provider(request, capture) if request["operation"] != "execute" else budget.guarded_call(request, "deepline", lambda: (
                {"provider": "deepline", "operation": "execute", "tool": request["tool"], "status": "timeout", "results": []}, 2)))
        other.start(self.request)
        outcome = other.lookup([check()])["lookups"][0]
        ledger = budget.load_ledger(other.path)
        self.assertIsNone(ledger["calls"][outcome["route"]]["actual_credits"])
        self.assertEqual(ledger["calls"][outcome["route"]]["maximum_credits"], "0.2")
        with self.assertRaisesRegex(ValueError, "already attempted"):
            other.lookup([check()])

    def test_unqualified_contacts_and_incomplete_acceptance_are_rejected(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "account-qualified"):
            self.lookup(check(phase="contact_discovery"))
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.tools.review(companies=[{"target": "example.test", "decision": "accept", "reason": "No evidence"}])
        self.assertEqual(self.path.read_bytes(), before)

    def test_inspection_pages_long_sources_without_losing_saved_text(self):
        self.start()
        text = "verified text " * 2400
        observed = self.tools.review(web=[{"target": "discovery", "purpose": "Read long page", "query": "long source",
            "response": {"status": "ok", "results": [{"url": "https://example.test", "text": text}]}}])
        ref = observed["web_references"]["web:0"] + ":0"
        found, offset = "", 0
        while offset is not None:
            page = self.tools.call("tyche_inspect", {"ref": ref, "field": "text", "offset": offset, "limit": 200})
            self.assertEqual(page["text"], text[offset:offset + 12000])
            found += page["text"]
            offset = page["next_offset"]
        self.assertEqual(found, text)

    def test_saved_result_pages_keep_absolute_references_without_new_calls(self):
        self.start()
        rows = [{"url": f"https://example.test/{i}", "text": f"Observed result {i}"} for i in range(13)]
        observed = self.tools.review(web=[{"target": "discovery", "purpose": "Read result list", "query": "company signals",
            "response": {"status": "ok", "results": rows}}])
        rid = observed["web_references"]["web:0"]
        calls = len(self.provider.requests)
        page = self.tools.inspect(ref=rid)
        self.assertEqual(page["next_offset"], 10)
        page = self.tools.inspect(ref=rid, offset=page["next_offset"])
        self.assertEqual([r["ref"] for r in page["results"]], [f"{rid}:{i}" for i in range(10, 13)])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(self.tools.inspect(ref=page["results"][2]["ref"])["facts"], rows[12])
        selected = self.tools.inspect(ref=rid, field="results", offset=10)
        self.assertEqual(selected, {"value": rows[10:], "total": 13, "next_offset": None})
        self.assertEqual(len(self.provider.requests), calls)

    def test_oversized_inspection_limits_keep_pages_bounded_and_read_only(self):
        self.start()
        rows = [{"url": f"https://example.test/{i}", "tags": list(range(23))} for i in range(23)]
        observed = self.tools.review(web=[{"target": "discovery", "purpose": "Saved results", "query": "signals",
            "response": {"status": "ok", "results": rows}}])
        rid = observed["web_references"]["web:0"]
        before = {p: p.read_bytes() for p in self.path.parent.rglob("*") if p.is_file()}
        calls = len(self.provider.requests)
        for requested in (20, 100, 200, 1000000):
            with self.subTest(limit=requested):
                for options, key in (({"ref": rid}, "results"),
                                     ({"ref": rid, "field": "results"}, "value"),
                                     ({"ref": rid + ":0", "field": "tags"}, "items")):
                    page = self.tools.call("tyche_inspect", {**options, "limit": requested})
                    self.assertEqual(len(page[key]), 10)
                    self.assertEqual(page["next_offset"], 10)
                    tail = self.tools.call("tyche_inspect", {**options, "limit": requested, "offset": 20})
                    self.assertEqual(len(tail[key]), 3)
                    self.assertIsNone(tail["next_offset"])
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual({p: p.read_bytes() for p in self.path.parent.rglob("*") if p.is_file()}, before)

    def test_inspect_own_tool_reads_authoritative_schema_without_provider_or_state_changes(self):
        before = self.tools.call("tyche_inspect", {"tool": "tyche_review"})
        self.assertEqual(before["tool"]["toolId"], "tyche_review")
        self.assertEqual(before["tool"]["inputSchema"], research_tools.contract_view(research_tools.TOOLS["tyche_review"][1], "inputSchema"))
        self.assertFalse(self.path.exists())
        self.start()
        saved = self.path.read_bytes()
        calls = len(self.provider.requests)
        block = self.path.parent / "operational-status.json"
        block.write_text('{"status":"blocked"}')
        result = self.tools.call("tyche_inspect", {"tool": "tyche_review", "refresh": True,
            "field": "inputSchema.properties.companies.items.properties.qualification_checks.items.properties.status"})
        self.assertEqual(result["tool"]["enum"], ["pass", "fail", "unknown"])
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertTrue(block.exists())

    def test_tool_view_omits_sdk_help_but_preserves_cached_native_contract(self):
        self.start()
        def annotated(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                body["results"][0].update(usageGuidance={"sdk_help": "irrelevant SDK help " * 1000},
                    outputSchema={"fields": [{"name": "element", "type": "object"}],
                                  "jsonSchema": {"properties": {"element": {"type": "object"}}}})
            return body, code
        self.tools.execute = annotated
        view = self.tools.inspect(tool="harvestapi_get_company", refresh=True)["tool"]
        self.assertNotIn("usageGuidance", view)
        self.assertEqual(view["inputSchema"]["fields"][0]["name"], "url")
        self.assertEqual(view["pricing"]["creditsPerUnit"], .2)
        self.assertEqual(view["output_fields"][0]["name"], "element")
        self.assertEqual(self.tools._description_view({"outputSchema": None})["output_fields"], [])
        self.assertNotIn("reservation_preview", self.tools._description_view({"pricing": None}))
        calls = len(self.provider.requests)
        schema = self.tools.inspect(tool="harvestapi_get_company", field="outputSchema.jsonSchema")["tool"]
        self.assertEqual(schema["properties"]["element"]["type"], "object")
        self.lookup()
        self.assertEqual(len([r for r in self.provider.requests if r["operation"] == "describe"]), 3)
        self.assertEqual(len(self.provider.requests), calls + 1)

    def test_tool_description_exposes_unbounded_result_pricing_before_dispatch(self):
        self.start()
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                body["results"][0]["pricing"] = {"creditsPerUnit": .26, "unit": "result"}
            return body, code
        self.tools.execute = catalog
        ledger = budget.ledger_path(self.path).read_bytes()
        view = self.tools.inspect(tool="fixture_lookup")["tool"]
        self.assertEqual(view["pricing"]["creditsPerUnit"], .26)
        self.assertEqual(view["reservation_preview"]["status"], "unavailable_for_default_options")
        self.assertNotIn("maximum_credits", view["reservation_preview"])
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger)
        calls = len(self.provider.requests)
        self.assertEqual(self.tools.inspect(tool="fixture_lookup")["tool"], view)
        with self.assertRaisesRegex(ValueError, "No whole-call price"):
            self.lookup(check(tool="fixture_lookup", inputs={"query": "company"}))
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger)

    def test_reservation_preview_is_not_an_execution_gate_or_final_quote(self):
        self.start()
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                contract = body["results"][0]
                contract["pricing"] = {"creditsPerUnit": .2, "unit": "result"}
                contract["inputSchema"]["fields"].append({"name": "limit", "type": "integer"})
                contract["inputSchema"]["jsonSchema"]["properties"]["limit"] = {"type": "integer", "minimum": 1}
            return body, code
        self.tools.execute = catalog
        view = self.tools.inspect(tool="fixture_search")["tool"]
        self.assertEqual(view["reservation_preview"]["status"], "unavailable_for_default_options")
        result = self.lookup(check(tool="fixture_search", inputs={"query": "company", "limit": 2}))
        rid = result["lookups"][0]["route"]
        self.assertEqual(float(budget.load_ledger(self.path)["calls"][rid]["maximum_credits"]), .4)
        priced = self.tools.inspect(tool="harvestapi_get_company")["tool"]["reservation_preview"]
        self.assertEqual(priced["status"], "available_for_default_options")
        self.assertEqual(priced["maximum_credits"], .2)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_result_pricing_uses_catalog_page_size_without_agent_arithmetic(self):
        self.start()
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                contract = body["results"][0]
                contract["pricing"] = {"creditsPerUnit": .56, "unit": "result"}
                contract["inputSchema"]["fields"].append({"name": "page_size", "type": "integer"})
                contract["inputSchema"]["jsonSchema"]["properties"]["page_size"] = {"type": "integer", "minimum": 1}
            return body, code
        self.tools.execute = catalog
        inputs = {"query": "company", "page_size": 3}
        with self.assertRaisesRegex(ValueError, "below the catalog-derived"):
            self.lookup(check(tool="fixture_search", inputs=inputs, max_cost_credits=1))
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        with self.assertRaisesRegex(ValueError, "No whole-call price"):
            self.lookup(check(tool="fixture_search", inputs={"query": "company"}))
        result = self.lookup(check(tool="fixture_search", inputs=inputs))
        rid = result["lookups"][0]["route"]
        call = budget.load_ledger(self.path)["calls"][rid]
        self.assertEqual(float(call["maximum_credits"]), 1.68)
        self.assertEqual(float(call["actual_credits"]), self.provider.rate)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_ordinary_input_guidance_is_complete_and_general_help_remains_compact(self):
        self.start()
        description = "Provider input context. " * 35 + "SQL must include LIMIT <= 100000."
        general_help = "General tool background. " * 1200
        enum = [f"category-{i}" for i in range(60)]
        full = {}
        def described(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                contract = body["results"][0]
                contract["description"] = general_help
                contract["inputSchema"]["description"] = description
                contract["inputSchema"]["fields"][0]["description"] = description
                contract["inputSchema"]["jsonSchema"].update(required=["url"], properties={
                    "url": {"type": "string", "description": description},
                    "category": {"type": "string", "enum": enum},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1}})
                full.update(copy.deepcopy(contract))
            return body, code
        self.tools.execute = described
        view = self.tools.inspect(tool="harvestapi_get_company", refresh=True)["tool"]
        self.assertLess(len(view["description"]), len(general_help) // 10)
        self.assertEqual(view["inputSchema"]["description"], description)
        self.assertEqual(view["inputSchema"]["fields"][0]["description"], description)
        self.assertEqual(view["inputSchema"]["jsonSchema"]["properties"]["url"]["description"], description)
        schema = view["inputSchema"]["jsonSchema"]
        self.assertEqual(schema["required"], ["url"])
        self.assertEqual(schema["properties"]["limit"], full["inputSchema"]["jsonSchema"]["properties"]["limit"])
        field = schema["properties"]["category"]["enum"]["detail_field"]
        self.assertEqual(self.tools.inspect(tool="harvestapi_get_company", field=field, offset=20)["tool"], enum[20:30])
        calls = len(self.provider.requests)
        # An explicit subtree request must expose all its constraints in one
        # response instead of forcing another inspection for every child field.
        whole = self.tools.inspect(tool="harvestapi_get_company", field="inputSchema")["tool"]
        self.assertEqual(whole, full["inputSchema"])
        fields = self.tools.inspect(tool="harvestapi_get_company", field="inputSchema.fields")["tool"]
        self.assertEqual(fields[0]["description"], description)
        whole["jsonSchema"]["required"].append("not_a_real_input")
        self.assertEqual(self.tools._description("harvestapi_get_company"), full)
        restored, offset = "", 0
        while offset is not None:
            page = self.tools.inspect(tool="harvestapi_get_company", field="inputSchema.fields.0.description", offset=offset)
            restored += page["tool"]
            offset = page["next_offset"]
        self.assertEqual(restored, description)
        self.assertEqual(self.tools._description("harvestapi_get_company"), full)
        with self.assertRaisesRegex(ValueError, "input.checks\\[0\\].inputs.*missing required fields: url"):
            self.lookup(check(inputs={"category": "category-50"}))
        self.assertEqual(len(self.provider.requests), calls)
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        self.lookup()
        self.assertEqual(len(self.provider.requests), calls + 1)

    def test_large_input_descriptions_are_bounded_without_changing_saved_contract(self):
        self.start()
        description = "Category taxonomy. " * 1500 + "Use only the provider's documented categories."
        full = {}
        def described(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                contract = body["results"][0]
                contract["inputSchema"]["fields"][0]["description"] = description
                contract["inputSchema"]["jsonSchema"].update(required=["url"], properties={
                    "url": {"type": "string", "description": description},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1}})
                full.update(copy.deepcopy(contract))
            return body, code
        self.tools.execute = described
        view = self.tools.inspect(tool="harvestapi_get_company", refresh=True)["tool"]
        schema = view["inputSchema"]["jsonSchema"]
        self.assertLess(len(json.dumps(view)), len(json.dumps(full)) / 3)
        self.assertIn("abridged guidance", schema["properties"]["url"]["description"])
        self.assertIn("inputSchema.jsonSchema.properties.url.description", schema["properties"]["url"]["description"])
        self.assertEqual(schema["required"], ["url"])
        self.assertEqual(schema["properties"]["limit"], full["inputSchema"]["jsonSchema"]["properties"]["limit"])
        calls = len(self.provider.requests)
        detail = self.tools.inspect(tool="harvestapi_get_company", field="inputSchema.jsonSchema.properties.url")["tool"]
        self.assertEqual(detail["description"], description)
        self.assertEqual(self.tools._description("harvestapi_get_company"), full)
        self.assertEqual(len(self.provider.requests), calls)
        with self.assertRaisesRegex(ValueError, "missing required fields: url"):
            self.lookup(check(inputs={"limit": 1}))
        self.assertEqual(len(self.provider.requests), calls)
        self.assertFalse(budget.load_ledger(self.path)["calls"])

    def test_reference_corrections_include_exact_input_path_and_saved_choices(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        row = {"target": "example.test", "decision": "hold_account", "reason": "Review fit",
               "account_fit": {"ref": "mistyped:0"}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for bad in ("mistyped:0", ref.rsplit(":", 1)[0] + ":999"):
            row["account_fit"]["ref"] = bad
            with self.assertRaises(ValueError) as error:
                self.tools.call("tyche_review", {"companies": [row]})
            self.assertIn("input.companies[0].account_fit.ref", str(error.exception))
            self.assertIn(ref, str(error.exception))
            self.assertIn("example.test", str(error.exception))
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        row["account_fit"]["ref"] = ref
        self.tools.call("tyche_review", {"companies": [row]})
        self.assertEqual(len(self.provider.requests), before[2])

    def test_reference_typo_suggests_older_receipt_across_scope_alias_without_resolving(self):
        self.start()
        ref = self.lookup(check("ExamplePay"))["lookups"][0]["results"][0]["ref"]
        for index in range(4):
            self.lookup(check(f"other-{index}.test"))
        rid = ref.split(":")[0]
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for bad in (rid[:-1] + ":0", rid[:-2] + "xx:0"):
            with self.subTest(reference=bad), self.assertRaises(ValueError) as error:
                self.tools.call("tyche_review", {"companies": [{"target": "example.test",
                    "decision": "hold_account", "reason": "Check saved evidence", "account_fit": {"ref": bad}}]})
            self.assertIn(ref, str(error.exception))
            self.assertIn('"target": "examplepay"', str(error.exception))
            self.assertIn("no replacement was selected", str(error.exception))
            with self.assertRaises(ValueError) as inspection:
                self.tools.call("tyche_inspect", {"ref": bad})
            self.assertIn(ref, str(inspection.exception))
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.assertEqual(self.tools._reference_choices("unrelated-run:0", "missing.test"), [])

    def test_stale_web_alias_returns_saved_choices_without_resaving_or_spending(self):
        self.start()
        web = {"target": "example.test", "purpose": "Read company source", "query": "company source",
               "operation": "find", "response": {"status": "ok", "results": [
                   {"url": "https://example.test/about", "text": "The company provides verified business data services."}]}}
        saved = self.tools.review(web=[web])["web_references"]["web:0"] + ":0"
        # Later provider calls must not bury the source behind irrelevant profiles.
        for index in range(4):
            self.lookup(check(purpose=f"Inspect company observation {index}",
                              inputs={"url": f"https://www.linkedin.com/company/observation-{index}/"}))
        row = {"target": "example.test", "decision": "hold_account", "reason": "Review saved company source",
               "account_fit": {"ref": "web:0:0"}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        receipts = sorted(self.path.parent.joinpath("receipts").iterdir())
        for bad, attached in (("web:0:0", []), ("web:0:99", [web])):
            row["account_fit"]["ref"] = bad
            with self.assertRaises(ValueError) as error:
                self.tools.call("tyche_review", {"companies": [row], "web": attached})
            self.assertIn("input.companies[0].account_fit.ref", str(error.exception))
            self.assertIn(saved, str(error.exception))
            self.assertIn("https://example.test/about", str(error.exception))
            self.assertIn("no replacement was selected", str(error.exception))
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
            self.assertEqual(sorted(self.path.parent.joinpath("receipts").iterdir()), receipts)
        row["account_fit"]["ref"] = saved
        result = self.tools.call("tyche_review", {"companies": [row]})
        self.assertEqual(result["saved_companies"], ["example.test"])
        self.assertEqual(len(self.provider.requests), before[2])

    def test_input_corrections_show_valid_fields_and_leave_paid_work_untouched(self):
        self.start()
        before = self.path.read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, r"input.companies\[0\].*input.sources"):
            self.tools.call("tyche_review", {"companies": [{"target": "example.test", "decision": "hold_account",
                "reason": "Needs evidence", "sources": []}]})
        for invalid_limit in (0, -1, 1.5, True):
            with self.subTest(limit=invalid_limit), self.assertRaises(ValueError):
                self.tools.call("tyche_inspect", {"limit": invalid_limit})
        with self.assertRaisesRegex(ValueError, r"input.checks has 4 items; allowed count: 1–3"):
            self.lookup(*[check() for _ in range(4)])
        with self.assertRaisesRegex(ValueError, r"input.checks\[0\].inputs.*wrong.*allowed fields: \['url'\]"):
            self.lookup(check(inputs={"url": "https://example.test", "wrong": "value"}))
        with self.assertRaisesRegex(ValueError, r"input.field.*stop_audit.route_frontier"):
            self.tools.call("tyche_inspect", {"field": "route_frontier"})
        self.assertEqual((self.path.read_bytes(), len(self.provider.requests)), before)
        self.assertFalse(budget.load_ledger(self.path)["calls"])

    def test_web_observation_shape_errors_are_precise_and_do_not_save_partial_work(self):
        self.start()
        before = self.path.read_bytes()
        receipts = sorted(self.path.parent.joinpath("receipts").iterdir())
        observation = {"target": "example.test", "purpose": "Review source", "query": "source query",
                       "status": "ok", "results": [{"url": "https://example.test/news", "text": "Observed source"}]}
        with self.assertRaisesRegex(ValueError, r"input.web\[0\].response.results"):
            self.tools.call("tyche_review", {"web": [observation]})
        observation["response"] = {"status": observation.pop("status"), "results": observation.pop("results")}
        for bad_results, expected in (("Pasted tool transcript", r"input.web\[0\].response.results requires array"),
                                      (["Pasted source"], r"input.web\[0\].response.results\[0\] requires object")):
            observation["response"]["results"] = bad_results
            with self.assertRaisesRegex(ValueError, expected):
                self.tools.review(web=[observation])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(self.path.parent.joinpath("receipts").iterdir()), receipts)
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        observation["response"]["results"] = [{"url": "https://example.test/news", "text": "Observed source"}]
        result = self.tools.review(web=[observation])
        self.assertIn("web:0", result["web_references"])
        self.assertEqual(self.tools.inspect(ref=result["web_references"]["web:0"] + ":0")["facts"]["text"], "Observed source")

    def test_invalid_web_status_reports_allowed_values_before_saving_any_observation(self):
        self.start()
        web = {"target": "example.test", "purpose": "Review source", "query": "https://example.test/news",
               "operation": "open", "response": {"status": "ok", "results": [
                   {"url": "https://example.test/news", "text": "Observed source"}]}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        receipts = sorted(self.path.parent.joinpath("receipts").iterdir())
        for invalid in ("success", "pending", "unknown"):
            other = copy.deepcopy(web)
            other["response"]["status"] = invalid
            with self.subTest(status=invalid), self.assertRaisesRegex(ValueError, r"input.web\[1\].response.status.*ok"):
                self.tools.call("tyche_review", {"web": [web, other]})
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)
            self.assertEqual(sorted(self.path.parent.joinpath("receipts").iterdir()), receipts)
        self.assertEqual(len(self.tools.review(web=[web])["web_references"]), 1)

    def test_catalog_pages_show_usable_tools_and_keep_original_evidence_indices(self):
        self.start()
        rows = [{"toolId": "monitor", "callable": False, "deployCommand": "monitor setup"}] * 10
        rows += [{"toolId": f"research_{i}", "callable": True, "description": f"Research capability {i}",
                  "usageGuidance": "SDK material"} for i in range(12)]
        def search(request, capture):
            body, code = self.provider(request, capture)
            body["results"] = copy.deepcopy(rows)
            return body, code
        self.tools.execute = search
        view = self.tools.inspect(query="research")
        rid = view["route"]
        self.assertEqual(view["result_count"], 12)
        self.assertEqual(view["non_callable_count"], 10)
        self.assertEqual(view["results"][0]["ref"], f"{rid}:10")
        self.assertNotIn("usageGuidance", view["results"][0]["facts"])
        calls = len(self.provider.requests)
        page = self.tools.inspect(ref=rid, offset=view["next_offset"])
        self.assertEqual([r["ref"] for r in page["results"]], [f"{rid}:20", f"{rid}:21"])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(self.tools.inspect(ref=f"{rid}:21")["facts"], rows[21])
        self.assertEqual(runner.read_receipt(self.path, rid)["result"]["results"], rows)
        self.assertEqual(len(self.provider.requests), calls)
        rows[:] = rows[:10]
        view = self.tools.inspect(query="no match")
        self.assertEqual(view["results"], [])
        self.assertIn("No callable tools matched", view["catalog_note"])

    def test_inspection_selects_company_and_run_fields_instead_of_ignoring_them(self):
        self.start()
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Needs funding evidence",
                                     "company": {"canonical_name": "ExamplePay"}, "intent_details": "Saved research paragraph."}])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        selected = self.tools.inspect(target="example.test", field="company.candidate.canonical_name")
        self.assertEqual(selected, {"value": "ExamplePay"})
        self.assertEqual(self.tools.inspect(target="example.test", field="candidate.canonical_name"), selected)
        self.assertEqual(self.tools.call("tyche_inspect", {"target": "example.test", "field": "intent_details"}),
                         {"value": "Saved research paragraph."})
        self.assertEqual(self.tools.inspect(target="example.test", field="route_count"), {"value": 0})
        with self.assertRaisesRegex(ValueError, "Unknown company field.*saved fields"):
            self.tools.inspect(target="example.test", field="missing")
        with self.assertRaisesRegex(ValueError, "Unknown company field"):
            self.tools.inspect(target="missing.test", field="intent_details")
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.assertEqual(self.tools.inspect(field="stop_check.started_at")["value"],
                         json.loads(self.path.read_text())["stop_check"]["started_at"])
        with self.assertRaisesRegex(ValueError, "available fields"):
            self.tools.inspect(field="nonexistent")

    def test_progress_reports_required_gaps_without_promoting_optional_hiring(self):
        self.start()
        checks = [{"criterion": name, "importance": importance, "status": "unknown", "claim": "Needs research", "evidence": []}
                  for name, importance in [("current funding stage", "required"), ("hiring bonus", "preferred")]]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Funding is unresolved",
                                     "company": {"canonical_name": "ExamplePay"}, "qualification_checks": checks}])
        missing = self.tools.inspect()["companies"][0]["missing"]
        self.assertIn("current funding stage", missing)
        self.assertFalse(any("HIRING" in item for item in missing))
        self.assertTrue(any("geographies" in item for item in missing))
        self.assertEqual(self.tools.inspect(target="example.test", field="qualification_checks")["value"], checks)

    def test_selected_run_fields_page_without_repeating_or_changing_state(self):
        self.start()
        document = json.loads(self.path.read_text())
        roles = [f"Requested role {i}" for i in range(22)]
        document["request"]["requested_roles"] = roles
        self.path.write_text(json.dumps(document))
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        found, offset = [], 0
        while offset is not None:
            page = self.tools.call("tyche_inspect", {"field": "request.requested_roles", "offset": offset})
            self.assertEqual(page["total"], len(roles))
            found.extend(page["value"])
            offset = page["next_offset"]
        self.assertEqual(found, roles)
        self.assertEqual(self.tools.inspect(field="request.requested_roles", offset=99),
                         {"value": [], "total": 22, "next_offset": None})
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_company_fields_page_full_text_and_source_history(self):
        self.start()
        narrative = "Supported company context. " * 160
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Research pending", "intent_details": narrative}])
        self.tools.review(web=[{"target": "example.test", "purpose": f"Review company fact {i}", "query": str(i),
            "response": {"status": "ok", "results": [{"url": f"https://example.test/{i}", "text": f"Source {i}"}]}}
            for i in range(12)])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        found, offset = "", 0
        while offset is not None:
            page = self.tools.inspect(target="example.test", field="intent_details", offset=offset)
            found += page["value"]
            offset = page["next_offset"]
        self.assertEqual(found, narrative)
        first = self.tools.inspect(target="example.test", field="recent_sources")
        last = self.tools.inspect(target="example.test", field="recent_sources", offset=first["next_offset"])
        self.assertEqual(first["total"], 12)
        self.assertEqual(len(last["value"]), 2)
        self.assertIsNone(last["next_offset"])
        self.assertFalse({r["ref"] for r in first["value"]} & {r["ref"] for r in last["value"]})
        self.assertEqual(self.tools.inspect(target="example.test")["recent_sources"],
                         (first["value"] + last["value"])[-10:])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_recorded_provider_error_explains_recovery_without_releasing_unknown_cost(self):
        self.start()
        calls = []
        def failing(request, capture):
            if request["operation"] != "execute":
                return self.provider(request, capture)
            calls.append(request)
            def dispatch():
                raw = {"exit_code": 1, "body": {"error": "Bad request"}, "stderr": ""}
                capture(raw)
                return deepline.normalize_response(request, raw)
            return budget.guarded_call(request, "deepline", dispatch)
        self.tools.execute = failing
        found = self.lookup()["lookups"][0]
        self.assertTrue(found["recorded"])
        self.assertEqual(runner.read_receipt(self.path, found["route"])["result"]["receipt_status"], "complete")
        self.assertIn("cannot resolve unknown billing", found["recovery_note"])
        before = budget.ledger_path(self.path).read_bytes()
        self.tools.inspect(recover=found["route"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)
        self.assertIsNone(budget.load_ledger(self.path)["calls"][found["route"]]["actual_credits"])

    def test_sandbox_relay_requires_host_metadata_and_refuses_policy_drift(self):
        relay = SandboxedTools(self.path)
        with patch("tyche_tools.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "sandbox metadata"):
                relay.call("tyche_inspect", {}, {})
            state = {"permissionProfile": {"mode": "read-only"}, "sandboxCwd": self.path.parent.as_uri()}
            with self.assertRaisesRegex(ValueError, "differs"):
                relay.call("tyche_inspect", {}, {"codex/sandbox-state-meta": state})
            state["sandboxCwd"] = Path(__file__).resolve().parents[4].as_uri()
            relay.state = {**state, "permissionProfile": {"mode": "restricted"}}
            with self.assertRaisesRegex(ValueError, "Sandbox changed"):
                relay.call("tyche_inspect", {}, {"codex/sandbox-state-meta": state})
            launch.assert_not_called()

    def test_web_review_is_one_handoff_and_retry_preserves_receipt(self):
        self.start()
        web = {"target": "example.test", "purpose": "Read product", "query": "https://example.test/product",
            "operation": "open", "response": {"status": "ok", "results": [{"url": "https://example.test/product",
                "text": "ExamplePay provides merchant payment processing."}]}}
        company = {"target": "example.test", "decision": "hold_account", "reason": "Funding still needs review",
                   "company": {"canonical_name": "ExamplePay"},
                   "account_fit": {"ref": "web:0:0", "date_basis": "observed_current"}}
        result = self.tools.review(companies=[company], web=[web])
        rid = result["web_references"]["web:0"]
        before = (self.path.parent / "receipts" / (rid + ".json")).read_bytes()
        self.tools.review(companies=[company], web=[web], sources=[{"ref": "web:0", "state": "exhausted", "reason": "Page reviewed"}])
        self.assertEqual((self.path.parent / "receipts" / (rid + ".json")).read_bytes(), before)
        self.assertEqual(len([r for r in json.loads(self.path.read_text())["routes"] if r["provider"] == "public_web"]), 1)
        web["response"]["results"][0]["text"] += " Its customers include retailers."
        later = self.tools.review(web=[web])["web_references"]["web:0"]
        self.assertNotEqual(later, rid)
        self.assertEqual((self.path.parent / "receipts" / (rid + ".json")).read_bytes(), before)
        self.assertEqual(self.tools.review(web=[web])["web_references"]["web:0"], later)
        self.assertEqual(len([r for r in json.loads(self.path.read_text())["routes"] if r["provider"] == "public_web"]), 2)

    def test_raw_receipt_authority_and_foreign_reference_rejection(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        receipt = self.path.parent / "receipts" / (ref.split(":")[0] + ".json")
        saved = json.loads(receipt.read_text())
        saved["results"][0]["employee_range"] = "10,001+"
        receipt.write_text(json.dumps(saved))
        self.assertEqual(self.tools.inspect(ref=ref)["facts"]["employee_range"], "51-200")
        saved["run_fingerprint"] = "foreign-run"
        receipt.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, "another run"):
            self.tools.inspect(ref=ref)

    def test_conflicting_evidence_source_returns_the_selected_receipt_and_url(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        row, source, _ = self.tools._resolve(ref)
        company = {"target": "example.test", "decision": "hold_account", "reason": "Funding needs research",
                   "company": {"canonical_name": "ExamplePay"},
                   "account_fit": {"ref": ref, "source": {"provider": "public_web", "route_id": "wrong"},
                                   "evidence_url": "https://example.test/wrong-page"}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaises(ValueError) as error:
            self.tools.review(companies=[company])
        message = str(error.exception)
        for expected in (ref, ".source", source["tool"], source["route_id"], row["company_linkedin_url"], "Omit source"):
            self.assertIn(expected, message)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        company["account_fit"] = {"ref": ref}
        self.tools.review(companies=[company])
        self.assertEqual(len(self.provider.requests), before[2])

    def test_readonly_transport_lists_tools_and_refuses_mutation(self):
        stream = io.StringIO('\n'.join(json.dumps(m) for m in [
            {"id": 1, "method": "initialize"}, {"id": 2, "method": "tools/list"}]) + '\n')
        output = io.StringIO()
        serve(ResearchTools(self.path, readonly=True), stream, output)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(messages[1]["result"]["tools"]), 5)
        session = ResearchTools(self.path, readonly=True)
        self.assertEqual(session.call("tyche_inspect", {})["status"], "not_started")
        with self.assertRaisesRegex(ValueError, "read-only"):
            session.call("tyche_start", {"request": self.request})

    def test_complete_native_journey_saves_and_checks_the_workbook(self):
        if not os.environ.get("TYCHE_WORKSPACE_NODE_MODULES"):
            self.skipTest("Bundled workbook runtime not configured")
        if os.environ.get("TYCHE_TEST_ARTIFACTS"):
            directory = Path(os.environ["TYCHE_TEST_ARTIFACTS"])
            directory.mkdir(parents=True, exist_ok=False)
            self.path = directory / "results.json"
            self.tools = ResearchTools(self.path, execute=self.provider)
        from test_client_output import client_document
        from test_export_xlsx import read_first_sheet_rows
        template = client_document()
        row = template["accepted"][0]
        company, person = row["company"], row["primary_contact"]
        self.request = template["request"]
        self.request["target_count"] = 1
        self.request["buying_signals"] = [{"kind": row["signal_evidence"]["signal"], "query": "Recent warehouse integration"}]
        self.request["icp"]["required_attributes"] = ["Funding stage is Series C or later"]
        original = self.path.parent.parent / "request.txt"
        original.write_text("Find one company matching the supplied fixture criteria; verify one current buyer.")
        self.tools.environment["TYCHE_REQUEST_FILE"] = str(original)
        self.start()
        self.provider.raw = {"status": "ok", "element": {"name": company["canonical_name"],
            "website": company["website"], "linkedinUrl": company["linkedin_url"],
            "employeeCountRange": {"start": 201, "end": 500},
            "locations": [{"headquarter": True, "country": "United States", "geographicArea": "Ohio"}]}}
        selected = self.lookup(check("example.com", inputs={"url": company["linkedin_url"]}))["lookups"][0]["results"][0]["ref"]
        funding_ref = self.saved_funding("example.com")
        fit_ref = captured_page(self.tools, self.provider, target="example.com",
            url=row["account_fit"]["evidence_url"], text=row["account_fit"]["evidence_text"], date=row["account_fit"]["evidence_date"])
        signal_ref = captured_page(self.tools, self.provider, target="example.com",
            url=row["signal_evidence"]["evidence_url"], text=row["signal_evidence"]["evidence_text"], date=row["signal_evidence"]["evidence_date"])
        research = {"target": "example.com", "decision": "qualify_account", "reason": "Product and recent integration verified",
            "company": {"ref": selected, **{k: company[k] for k in ("industry", "sub_industry", "description", "classification_note")}},
            "account_fit": {"ref": fit_ref, "fit_claim": row["account_fit"]["fit_claim"]},
            "qualification_checks": [{"criterion": "recent integration", "signal": row["signal_evidence"]["signal"],
                "status": "pass", "claim": "Recent integration verified", "evidence": [{"ref": signal_ref, "event_date": "2026-08-12"}]},
                {"requirement_ref": "attribute:0", "status": "pass", "claim": "Captured funding history records Series C",
                 "evidence": [{"ref": funding_ref}]}] + [
                {"requirement_ref": r["ref"], "status": "pass", "claim": "Fixture matches requested company filter",
                 "evidence": [{"ref": selected, "text": "Fixture company profile"}]}
                for r in research_tools.request_requirements(self.request) if r["ref"].startswith("icp:")],
            "intent_details": row["intent_details"]}
        self.tools.call("tyche_review", {"companies": [research],
            "sources": [{"refs": [fit_ref, signal_ref, funding_ref], "state": "exhausted", "reason": "Captured sources reviewed"}]})
        self.provider.raw = {"status": "ok", "element": {"linkedinUrl": person["linkedin_url"], "firstName": "Ada", "lastName": "Example", "email": person["email"],
            "currentPosition": [{"companyName": company["canonical_name"], "title": person["current_title"], "companyLinkedinUrl": company["linkedin_url"]}],
            "location": {"linkedinText": "Columbus, Ohio, United States", "parsed": {"city": "Columbus", "state": "Ohio", "countryFull": "United States"}}}}
        profile = self.lookup(check("example.com", phase="contact_verification", tool="harvestapi_get_profile", inputs={"url": person["linkedin_url"]}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.com", "decision": "hold_contact", "reason": "Validate selected work email",
            "primary_contact": {"ref": profile, "requested_role": person["requested_role"], "role_match": "exact"}}])
        self.provider.raw = {"status": "ok", "data": {"address": person["email"], "status": "valid", "sub_status": "", "domain_is_catch_all": True}}
        verifier = self.lookup(check("example.com", phase="email_validation", tool="zerobounce_validate", inputs={"email": person["email"]}))["lookups"][0]["results"][0]["ref"]
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "conflicts with the contact email"):
            self.tools.review(companies=[{"target": "example.com", "decision": "accept", "reason": "Conflicting address is refused",
                "primary_contact": {"email_ref": verifier, "email": "other@example.com"}}])
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.lookup(check("example.com", phase="email_validation", tool="bounceban_verify_single", inputs={"email": person["email"]}))
        self.tools.review(companies=[{"target": "example.com", "decision": "accept", "reason": "Reviewed account and buyer are complete",
                                      "primary_contact": {"email_ref": verifier}}],
                          sources=[{"ref": ref, "state": "exhausted", "reason": "Selected returned evidence reviewed"}
                                   for ref in (selected, profile, verifier)])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["accepted"][0]["primary_contact"]["email"], person["email"])
        self.assertEqual(saved["accepted"][0]["primary_contact"]["email_validation"]["status"], "valid")
        self.assertEqual(saved["accepted"][0]["primary_contact"]["country"], "United States")
        self.assertIn("leads_ready_at", saved["stop_check"])
        original_check = saved["accepted"][0]["qualification_checks"][0]
        wrong_source = copy.deepcopy(original_check)
        wrong_source["evidence"][0]["url"] = "https://example.com/not-in-this-receipt"
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "required web evidence must quote captured source text at the selected URL"):
            self.tools.review(companies=[{"target": "example.com", "decision": "accept", "reason": "Source URL needs review",
                "qualification_checks": [wrong_source]}])
        self.assertEqual(self.path.read_bytes(), before)
        self.tools.environment['TYCHE_FINALIZATION_ONLY'] = '0'
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.assertEqual(self.tools.finish()['status'], 'review_handoff')
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.assertFalse((self.path.parent / 'leads.xlsx').exists())
        self.tools = ResearchTools(self.path, execute=self.provider,
                                  environment=dict(self.tools.environment, TYCHE_FINALIZATION_ONLY='1'))
        packet = self.tools.finish()
        self.assertEqual(packet["status"], "review_required")
        self.assertEqual(packet["request"], saved["request"])
        self.assertEqual(packet["request"]["original_text"], original.read_text())
        self.assertFalse((self.path.parent / "leads.xlsx").exists())
        with patch.object(self.tools, "_company_review", side_effect=AssertionError("Do not rebuild an unchanged packet")):
            repeated = self.tools.finish()
        self.assertEqual(repeated["review_ref"], packet["review_ref"])
        self.assertTrue(repeated["unchanged"])
        self.assertFalse(repeated["delivery_allowed"])
        self.assertNotIn("companies", repeated)
        # Production sets the environment, not just ResearchTools.environment.
        # Final review can reread a cited page, but cannot add discovery or spend.
        with patch.dict(os.environ, {'TYCHE_FINALIZATION_ONLY': '1'}):
            for target, operation, query in [
                ('example.com', 'search_query', row['signal_evidence']['evidence_url']),
                ('example.com', 'open', 'https://example.com/new-source'),
                ('other.example', 'open', row['signal_evidence']['evidence_url']),
            ]:
                before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
                with self.assertRaisesRegex(ValueError, 'Research is closed'):
                    self.tools.review(web=[{'target': target, 'purpose': 'Check review boundary',
                        'operation': operation, 'query': query, 'response': {'status': 'ok',
                            'results': [{'url': query, 'text': 'New observation'}]}}])
                self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        revised_signal = {"criterion": "recent integration", "importance": "required", "status": "pass",
            "claim": "The integration announcement is supported", "signal": row["signal_evidence"]["signal"],
            "evidence": [{"ref": "web:0:0", "event_date": "2026-08-12"}]}
        with patch.dict(os.environ, {'TYCHE_FINALIZATION_ONLY': '1'}):
            self.tools.review(companies=[{"target": "example.com", "decision": "hold_account", "reason": "Final source interpretation needs correction",
                "qualification_checks": [dict(revised_signal, status="unknown", claim="Review the release wording")]}],
                web=[{"target": "example.com", "purpose": "Final source review", "query": row["signal_evidence"]["evidence_url"],
                    "operation": "open", "response": {"status": "ok", "results": [{"url": row["signal_evidence"]["evidence_url"],
                        "date": row["signal_evidence"]["evidence_date"], "text": "Released its warehouse integration."}]}}],
                sources=[{"ref": "web:0", "state": "exhausted", "reason": "Reviewed release meaning and date"}])
        corrected = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(corrected["primary_contact"], saved["accepted"][0]["primary_contact"])
        self.assertEqual(len([r for r in self.provider.requests if r.get("tool") == "zerobounce_validate" and r.get("operation") == "execute"]), 1)
        revised_signal["evidence"] = [{"ref": signal_ref, "event_date": "2026-08-12"}]  # reuse the captured body, not the corroborating note
        self.tools.review(companies=[{"target": "example.com", "decision": "accept", "reason": "Final writing and source meaning reviewed",
            "qualification_checks": [revised_signal],
            "intent_details": row["intent_details"] + " Its manufacturing business depends on coordinated fulfillment."}])
        stale = self.tools.finish(review_ref=packet["review_ref"])
        self.assertEqual(stale["status"], "review_required")
        self.assertNotEqual(stale["review_ref"], packet["review_ref"])
        with patch("research_tools.subprocess.run", return_value=type("FailedExport", (), {"returncode": 1, "stderr": "Temporary export failure", "stdout": ""})()):
            failed = self.tools.call("tyche_finish", {"review_ref": stale["review_ref"], "review_findings": review_findings(stale), "commentary": "Offline fixture. Required evidence and the selected buyer were reviewed; no live research was performed."})
        self.assertEqual(failed["status"], "needs_repair")
        before = self.path.read_bytes()
        ledger_before = budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        with patch("research_tools.subprocess.run", return_value=type("FailedExport", (), {
                "returncode": 2, "stderr": json.dumps({"exported": False, "failure_kind": "workbook_verification",
                    "error": "Saved Sources!I4 differs from validated evidence"}), "stdout": ""})()):
            failed = self.tools.finish()
        self.assertEqual(failed["status"], "export_failed")
        self.assertFalse(failed["delivery_allowed"])
        self.assertIn("Do not rewrite research", failed["next"])
        self.assertIn("Sources!I4", failed["errors"][0])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger_before)
        self.assertEqual(len(self.provider.requests), calls)
        # A new process can finish the already reviewed snapshot without
        # another review token, email request or reconstructed company record.
        resumed = ResearchTools(self.path, execute=self.provider)
        result = resumed.finish()
        self.assertTrue(result.get("delivery_allowed"), result)
        validation = json.loads((self.path.parent / "validation.json").read_text())
        self.assertTrue(validation["delivery_allowed"])
        self.assertIn("completed_at", validation)
        import hashlib
        self.assertEqual(validation["results_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        exported = result["export"]
        self.assertTrue(exported["saved_workbook_values_verified"])
        self.assertEqual(exported["results_sha256"], validation["results_sha256"])
        self.assertEqual(exported["workbook_sha256"], hashlib.sha256(Path(exported["path"]).read_bytes()).hexdigest())
        self.assertEqual(exported["workbook_sha256"], validation["workbook_sha256"])
        cells = read_first_sheet_rows(Path(result["export"]["path"]))[1]
        self.assertEqual(cells[9:12], ["Columbus", "Ohio", "United States"])
        self.assertEqual(cells[14], "201-500")
        self.assertEqual(cells[16].count("The integration announcement is supported"), 1)
        report = Path(result["report"]).read_text()
        self.assertIn("Offline fixture.", report)
        self.assertIn("Accepted 1 of 1", report)
        self.assertIn("Standard API equivalent", report)
        self.assertTrue(Path(result["preview"]).is_file())
        import zipfile
        with zipfile.ZipFile(result["export"]["path"]) as workbook:
            xml = "\n".join(workbook.read(name).decode() for name in workbook.namelist() if name.endswith(".xml"))
        self.assertIn("Saved receipt: " + funding_ref, xml)
        self.assertIn("aviato_get_company_funding_rounds", xml)
        self.assertNotIn("Released its warehouse integration.", xml)
        self.assertIn("the company connected its acquired warehouse to one WMS.", xml)


if __name__ == "__main__":
    unittest.main()

"""Company finalization through native tools and real workbook export, using fixture providers."""

import copy
import json
import os
import unittest
from unittest.mock import patch

import test_confirmed_leads as fixtures
import test_client_output as client_fixtures
from test_export_xlsx import read_first_sheet_rows
from test_research_tools import captured_page, check, review_findings
from research_tools import ResearchTools
import budget_guard
import validate_run


PASSAGE = ("Example Products supplies 240 retail branches from two warehouses. "
           "In August 2026 it announced an automated replenishment pilot for both warehouses, "
           "scheduled for October 2026.")
NARRATIVE = ("Example Products connected its acquired warehouse to a shared WMS on August 12, 2026. "
             "Its two warehouses supply 240 retail branches, and an automated replenishment pilot "
             "announced in August is planned for October 2026. The combined operation and planned "
             "pilot may increase its need to coordinate stock and orders; the pilot is not yet complete.")


def findings(ref):
    return [
        {"kind": "context", "label": "Distribution footprint", "claim": "Two warehouses supply 240 retail branches.",
         "evidence": [{"ref": ref}]},
        {"kind": "signal", "label": "Replenishment pilot", "claim": "An automated replenishment pilot was announced in August 2026 for October 2026.",
         "evidence": [{"ref": ref, "event_date": "2026-08"}]},
    ]


class CompanyFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ConfirmedLeadTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path, self.tools, self.provider = self.fixture.path, self.fixture.tools, self.fixture.provider
        self.provider.rate = .1  # Keep the larger fixture journey inside its original allowance.

    def enrich(self):
        self.fixture.add(1, accept=False)
        ref = captured_page(self.tools, self.provider, target="example1.com",
            url="https://example1.com/roadmap", text=PASSAGE, date="2026-08-25")
        return ref

    def finalize(self, ref):
        return self.tools.call("tyche_review", {"companies": [{"target": "example1.com", "decision": "accept",
            "reason": "Finalized supported context and planned activity; checked all contacts and client prose.",
            "supporting_findings": findings(ref), "intent_details": NARRATIVE}]})

    def add_contact(self, number):
        company = "https://www.linkedin.com/company/example-products-1/"
        profile = f"https://www.linkedin.com/in/buyer-{number}/"
        email = f"buyer{number}@example1.com"
        self.provider.raw = {"status": "ok", "element": {"linkedinUrl": profile,
            "firstName": "Buyer", "lastName": str(number), "email": email, "currentPosition": [{
                "companyName": "Example Products 1", "title": "Director of Supply Chain", "companyLinkedinUrl": company}],
            "location": {"parsed": {"city": "Columbus", "state": "Ohio", "countryFull": "United States"}}}}
        ref = self.tools.lookup([check("example1.com", phase="contact_verification", tool="harvestapi_get_profile",
            inputs={"url": profile})])["lookups"][0]["results"][0]["ref"]
        row = self.tools._document()["unresolved"][0]
        contacts = copy.deepcopy(row.get("backup_contacts", []))
        contacts.append({"ref": ref, "requested_role": "Director of Supply Chain", "role_match": "exact"})
        self.tools.review(companies=[{"target": "example1.com", "decision": "hold_contact", "reason": "Verified additional buyer",
                                     "backup_contacts": contacts}])
        self.provider.raw = {"status": "ok", "data": {"address": email, "status": "valid", "sub_status": ""}}
        email_ref = self.tools.lookup([check("example1.com", phase="email_validation", tool="zerobounce_validate",
            contact_ref=ref, inputs={"email": email})])["lookups"][0]["results"][0]["ref"]
        contacts[-1]["email_ref"] = email_ref
        self.tools.review(companies=[{"target": "example1.com", "decision": "hold_contact", "reason": "Exact email verified",
                                     "backup_contacts": contacts}])

    def test_company_and_all_contacts_finalize_before_run_completion_and_resume_without_lookup(self):
        ref = self.enrich()
        self.add_contact(2)
        self.add_contact(3)
        before = copy.deepcopy(self.tools._document())
        calls = len(self.provider.requests)
        packet = self.finalize(ref)
        self.assertEqual(packet["expected_targets"], ["example1.com"])
        frontier = self.tools._document()["stop_audit"]["route_frontier"]
        self.assertEqual(next(r for r in frontier if r["route_id"] == ref.split(":")[0])["state"], "exhausted")
        review = packet["companies"][0]
        self.assertEqual(len(review["backup_contacts"]), 2)
        self.assertEqual(review["company"]["hq_state"], "Ohio")
        self.assertTrue(review["company_evidence"]["source_refs"])
        self.assertEqual(review["sources"][ref]["text"], PASSAGE)
        self.assertEqual([f["kind"] for f in review["supporting_findings"]], ["context", "signal"])
        self.assertEqual(review["supporting_findings"][1]["evidence"][0]["event_date"], "2026-08")
        self.fixture.approve(packet)
        saved = self.fixture.file()
        self.assertEqual((saved["confirmed_count"], saved["target_count"]), (1, 5))
        self.assertEqual(saved["leads"][0]["intent_details"], NARRATIVE)
        self.assertEqual(saved["leads"][0]["company"], before["unresolved"][0]["candidate"])
        self.assertEqual(saved["leads"][0]["primary_contact"], before["unresolved"][0]["primary_contact"])
        self.assertEqual(saved["leads"][0]["backup_contacts"], before["unresolved"][0]["backup_contacts"])
        self.assertEqual(self.tools._document()["request"], before["request"])
        output_before = self.path.with_name("leads.json").read_bytes()
        ledger_before = budget_guard.ledger_path(self.path).read_bytes()
        resumed = ResearchTools(self.path, execute=self.provider)
        self.assertEqual(resumed.review()["status"], "confirmed_leads_saved")
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(self.path.with_name("leads.json").read_bytes(), output_before)
        self.assertEqual(budget_guard.ledger_path(self.path).read_bytes(), ledger_before)
        self.assertEqual(resumed.finish()["status"], "needs_research")

    def test_parallel_finalization_preserves_peer_save_and_rechecks_changed_support(self):
        import run_coordination as coordination
        coordination.configure(self.path, 2)
        workers = []
        for number in (1, 2):
            worker = f"worker-{number}"
            coordination.register(self.path, worker, worker)
            tools = ResearchTools(self.path, execute=self.provider, environment={
                "TYCHE_WORKER_ID": worker, "TYCHE_WORKER_GENERATION": worker})
            tools.claim(f"example{number}.com", f"https://linkedin.com/company/example-products-{number}/")
            workers.append(tools)
        self.tools = self.fixture.tools = workers[0]
        ref = self.enrich()
        self.add_contact(2)
        first = self.finalize(ref)
        self.assertEqual(first["expected_targets"], ["example1.com"])
        self.assertEqual(len(first["companies"][0]["backup_contacts"]), 1)

        self.fixture.tools = workers[1]
        second = self.fixture.add(2)
        self.assertEqual(second["expected_targets"], ["example2.com"])
        self.fixture.approve(second)
        peer = copy.deepcopy(self.fixture.file()["leads"][0])
        self.assertEqual(peer["company"]["domain"], "example2.com")

        self.fixture.tools = workers[0]
        corrected = findings(ref)
        corrected[0]["label"] = "Retail footprint"
        current = self.tools.review(companies=[{"target": "example1.com", "decision": "accept",
            "reason": "Clarified the label of captured supporting context.", "supporting_findings": corrected}])
        self.assertNotEqual(first["review_ref"], current["review_ref"])
        stale = self.tools.review(review_ref=first["review_ref"], review_findings=review_findings(first))
        self.assertEqual(stale["review_ref"], current["review_ref"])
        self.assertEqual(self.fixture.file()["leads"], [peer])
        self.fixture.approve(current)
        saved = {row["company"]["domain"]: row for row in self.fixture.file()["leads"]}
        self.assertEqual(saved["example2.com"], peer)
        self.assertEqual(saved["example1.com"]["supporting_findings"][0]["label"], "Retail footprint")
        self.assertEqual(saved["example1.com"]["intent_details"], NARRATIVE)
        self.assertEqual(len(saved["example1.com"]["backup_contacts"]), 1)
        self.assertTrue(all(worker["current_company"] is None
                            for worker in coordination.snapshot(self.path)["workers"].values()))

    def test_no_new_findings_does_not_change_qualification_or_require_extra_spending(self):
        self.fixture.add(1, accept=False)
        before = copy.deepcopy(self.tools._document()["unresolved"][0])
        calls = len(self.provider.requests)
        packet = self.tools.review(companies=[{"target": "example1.com", "decision": "accept",
            "reason": "Saved sources suffice; no useful additional finding.", "supporting_findings": []}])
        self.fixture.approve(packet)
        row = self.fixture.file()["leads"][0]
        self.assertEqual(row["qualification_checks"], before["qualification_checks"])
        self.assertEqual(row["intent_details"], before["intent_details"])
        self.assertEqual(len(self.provider.requests), calls)

    def assert_optional_lookup_preserves_qualified_company(self, response, status, number=1):
        target = f"example{number}.com"
        self.fixture.add(number, accept=False)
        before = copy.deepcopy(next(row for row in self.tools._document()["unresolved"]
                                    if row["candidate"]["domain"] == target))
        self.provider.raw = response
        lookup = self.tools.lookup([check(target, tool="fixture_optional_search",
            purpose="Look for useful optional operating context",
            inputs={"query": f"{target} additional operating context"})])["lookups"][0]
        self.assertEqual(self.tools._receipt(lookup["route"])["result"]["status"], status)
        self.assertIsNotNone(budget_guard.load_ledger(self.path)["calls"][lookup["route"]]["actual_usd"])
        calls = len(self.provider.requests)
        ledger = budget_guard.ledger_path(self.path).read_bytes()
        packet = self.tools.review(companies=[{"target": target, "decision": "accept",
            "reason": "Optional research added no usable facts; original company and contact evidence remains sufficient.",
            "supporting_findings": []}], sources=[{"ref": lookup["route"],
            "state": "blocked" if status == "provider_error" else "exhausted",
            "reason": "Optional lookup reviewed; no usable new fact and no new required evidence gap."}])
        self.fixture.approve(packet)
        saved = next(row for row in self.fixture.file()["leads"] if row["company"]["domain"] == target)
        for field in ("qualification_checks", "primary_contact", "intent_details"):
            self.assertEqual(saved[field], before[field])
        self.assertEqual(saved["company"], before["candidate"])
        self.assertEqual(saved["supporting_findings"], [])
        self.assertEqual(self.tools._document()["rejected"], [])
        self.assertNotIn(lookup["route"], [row["ref"] for row in self.tools.inspect(field="pending_sources")["items"]])
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget_guard.ledger_path(self.path).read_bytes(), ledger)

    def test_empty_optional_research_does_not_disqualify_or_repeat_verified_work(self):
        self.assert_optional_lookup_preserves_qualified_company({"status": "ok", "data": []}, "no_results")

    def test_settled_optional_provider_failure_does_not_disqualify_or_repeat_verified_work(self):
        self.assert_optional_lookup_preserves_qualified_company(
            {"status": "error", "error": "Optional page fetch failed"}, "provider_error")

    def test_supporting_findings_cannot_replace_an_unknown_or_failed_required_signal(self):
        ref = self.enrich()
        for status in ("unknown", "fail"):
            with self.subTest(status=status):
                document = self.tools._document()
                row = copy.deepcopy(document["unresolved"][0])
                row["supporting_findings"] = [{**f, "evidence": [self.tools._evidence(e) for e in f["evidence"]]}
                                             for f in findings(ref)]
                check_row = next(c for c in row["qualification_checks"] if c.get("signal"))
                check_row["status"] = status
                row.pop("signal_evidence", None)
                errors = validate_run.qualification_errors(dict(document, accepted=[row], unresolved=[]), run_file=self.path)
                self.assertTrue(errors, "Optional findings cannot satisfy the original required signal")

    def test_optional_finding_corrections_require_review_and_preserve_verified_contacts(self):
        ref = self.enrich()
        first = self.finalize(ref)
        self.fixture.approve(first)
        original = self.fixture.file()["leads"][0]
        corrected = self.tools.review(companies=[{"target": "example1.com", "decision": "accept",
            "reason": "Omit optional detail; retain established integration facts.", "supporting_findings": [],
            "intent_details": self.fixture.template["accepted"][0]["intent_details"]}])
        self.assertEqual(self.fixture.file()["leads"], [])
        self.assertNotEqual(first["review_ref"], corrected["review_ref"])
        stale = self.tools.review(review_ref=first["review_ref"], review_findings=review_findings(first))
        self.assertEqual(stale["review_ref"], corrected["review_ref"])
        self.fixture.approve(corrected)
        saved = self.fixture.file()["leads"][0]
        self.assertEqual(saved["primary_contact"], original["primary_contact"])
        self.assertEqual(saved["qualification_checks"], original["qualification_checks"])

    def test_unsupported_optional_evidence_is_repairable_without_rejecting_company(self):
        ref = self.enrich()
        original = self.tools._document()
        for evidence in ({"ref": ref, "text": "Invented statement absent from source"},
                         {"ref": ref, "date": "2026-09-01"}):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.tools.review(companies=[{"target": "example1.com", "decision": "accept", "reason": "Fixture",
                    "supporting_findings": [{**findings(ref)[0], "evidence": [evidence]}]}])
            self.assertEqual(self.tools._document()["unresolved"], original["unresolved"])
            self.assertEqual(self.tools._document()["rejected"], [])
        packet = self.tools.review(companies=[{"target": "example1.com", "decision": "accept", "reason": "Omit unsupported optional fact",
                                             "supporting_findings": []}])
        self.fixture.approve(packet)

    def test_structured_support_reuses_capture_and_rejects_a_fabricated_excerpt(self):
        self.fixture.add(1, accept=False)
        company = self.tools._document()["unresolved"][0]["candidate"]
        ref = company["employee_range_evidence"]["source"]["route_id"] + ":0"
        finding = {"kind": "context", "label": "Operating scale", "claim": "LinkedIn reports 201-500 employees.",
                   "evidence": [{"ref": ref, "text": "Invented passage absent from captured record."}]}
        with self.assertRaisesRegex(ValueError, "quote captured source text"):
            self.tools.review(companies=[{"target": "example1.com", "decision": "accept", "reason": "Fixture",
                                         "supporting_findings": [finding]}])
        finding["evidence"] = [{"ref": ref}]
        packet = self.tools.review(companies=[{"target": "example1.com", "decision": "accept", "reason": "Use captured record",
                                             "supporting_findings": [finding]}])
        self.fixture.approve(packet)
        evidence = self.fixture.file()["leads"][0]["supporting_findings"][0]["evidence"][0]
        self.assertEqual(json.loads(evidence["text"]), self.tools._resolve(ref)[0])

    def test_search_snippet_cannot_be_promoted_to_a_supporting_fact(self):
        self.fixture.add(1, accept=False)
        self.provider.raw = {"status": "ok", "data": [{"url": "https://example1.com/roadmap", "snippet": PASSAGE}]}
        ref = self.tools.lookup([check("example1.com", tool="fixture_search", inputs={"query": "roadmap"})])["lookups"][0]["results"][0]["ref"]
        with self.assertRaisesRegex(ValueError, "captured source body"):
            self.finalize(ref)
        self.assertEqual(self.tools._document()["accepted"], [])

    def test_source_only_final_review_can_reopen_supporting_url_but_cannot_add_a_new_url(self):
        ref = self.enrich()
        self.fixture.approve(self.finalize(ref))
        calls = len(self.provider.requests)
        def observation(url):
            return {"target": "example1.com", "purpose": "Reread saved source", "query": url, "operation": "open",
                    "response": {"status": "ok", "results": [{"url": url, "text": PASSAGE}]}}
        with patch.dict(os.environ, TYCHE_FINALIZATION_ONLY="1"):
            self.tools.review(web=[observation("https://example1.com/roadmap")])
            with self.assertRaisesRegex(ValueError, "exact saved source URL"):
                self.tools.review(web=[observation("https://example1.com/new-source")])
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(self.fixture.file()["confirmed_count"], 1)

    @unittest.skipUnless(os.environ.get("TYCHE_WORKSPACE_NODE_MODULES"), "bundled workbook runtime required")
    def test_finalized_records_reach_verified_workbook_with_shared_prose_and_matching_sources(self):
        ref = self.enrich()
        self.add_contact(2)
        self.add_contact(3)
        self.fixture.approve(self.finalize(ref))
        self.assert_optional_lookup_preserves_qualified_company(
            {"status": "error", "error": "Optional page fetch failed"}, "provider_error", number=2)
        for number in range(3, 6):
            self.fixture.approve(self.fixture.add(number))
        self.tools.environment = dict(os.environ, TYCHE_FINALIZATION_ONLY="1")
        packet = self.tools.finish()
        self.assertEqual(packet["status"], "review_required", packet)
        result = self.tools.finish(review_ref=packet["review_ref"], review_findings=review_findings(packet))
        self.assertTrue(result.get("delivery_allowed"), result)
        self.assertTrue(result["export"]["saved_workbook_values_verified"])
        workbook = self.path.with_name("leads.xlsx")
        leads = read_first_sheet_rows(workbook)
        self.assertEqual(len(leads), 8)  # Five companies, seven complete contacts, plus header.
        row = dict(zip(leads[0], leads[1]))
        self.assertEqual(row["Intent Details"], NARRATIVE)
        self.assertIn("Context: Distribution footprint", row["Signals"])
        self.assertIn("Signal: Replenishment pilot", row["Signals"])
        self.assertIn("Activity date: 2026-08", row["Signals"])
        self.assertIn("Source: https://example1.com/roadmap", row["Signals"])
        # All complete contacts share the Leads sheet; Sources retains their evidence.
        sources = read_first_sheet_rows(workbook, 2)
        source_rows = [dict(zip(sources[0], r)) for r in sources[1:]]
        roadmap = [r for r in source_rows if r["Source URL"] == "https://example1.com/roadmap"]
        self.assertEqual(len(roadmap), 2)
        self.assertTrue(all(r["Field"] == "Signals" and PASSAGE in r["Evidence Text"] for r in roadmap))
        company_contacts = [dict(zip(leads[0], r)) for r in leads[1:] if "Example Products 1" in r]
        self.assertEqual(len(company_contacts), 3)
        self.assertTrue(all(r["Intent Details"] == NARRATIVE and r["Signals"] == row["Signals"] for r in company_contacts))
        self.assertEqual(json.loads(self.path.with_name("validation.json").read_text())["errors"], [])


class SupportingFindingOutputTests(unittest.TestCase):
    def test_client_formatting_preserves_raw_values_and_exact_urls(self):
        document = client_fixtures.client_document()
        row = document["accepted"][0]
        row["company"]["canonical_name"] = "Example Products — US  "
        row["intent_details"] = "The integration is complete — stock coordination may be useful.  "
        url = "https://example.com/context—original"
        row["supporting_findings"] = [{"kind": "context", "label": "Retail — operations ", "claim": "Serves retailers — from two sites.  ",
            "evidence": [{"url": url, "date": "2026-08-25", "date_basis": "published", "text": PASSAGE,
                          "source": row["account_fit"]["source"]}]}]
        helper = client_fixtures.ClientOutputTests()
        helper.setUpClass()
        result = helper.run_rows_json(document)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        output = payload["rows"][0]
        self.assertEqual(output["Company"], "Example Products - US")
        self.assertEqual(output["Intent Details"], "The integration is complete - stock coordination may be useful.")
        self.assertIn("Context: Retail - operations", output["Signals"])
        self.assertIn(url, output["Signals"])
        self.assertTrue(any(r["Source URL"] == url for r in payload["sources"]))
        self.assertTrue(payload["unchanged"])

    def test_malformed_optional_findings_have_actionable_errors(self):
        for finding in (None, {}, [None], [{"kind": [], "evidence": []}], [{"kind": "context", "label": "X", "claim": "Y", "evidence": [{}]}]):
            with self.subTest(finding=finding):
                self.assertTrue(validate_run.supporting_finding_errors(finding, "supporting_findings"))
        self.assertEqual(validate_run.supporting_finding_errors([], "supporting_findings"), [])


if __name__ == "__main__":
    unittest.main()

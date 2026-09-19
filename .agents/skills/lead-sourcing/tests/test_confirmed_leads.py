"""Native tool journeys verify the continuously saved file without provider calls."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from test_client_output import client_document
from test_research_tools import FixtureProvider, captured_page, check, review_findings
import budget_guard
import confirmed_leads
from research_tools import ResearchTools
from test_export_xlsx import read_first_sheet_rows


class ConfirmedLeadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name).resolve() / "results.json"
        self.provider = FixtureProvider()
        self.tools = ResearchTools(self.path, execute=self.provider, environment={"TYCHE_FINALIZATION_ONLY": "0"})
        self.template = client_document()
        request = copy.deepcopy(self.template["request"])
        request.update(target_count=5, as_of_date="2026-09-01", icp={"industries": ["Manufacturing"]})
        request["buying_signals"] = [{"kind": self.template["accepted"][0]["signal_evidence"]["signal"],
                                      "query": "Recent warehouse integration", "importance": "required"}]
        self.tools.call("tyche_start", {"request": request})

    def file(self):
        return json.loads(self.path.with_name("leads.json").read_text())

    def test_partial_projection_keeps_only_unchanged_confirmed_rows(self):
        self.approve(self.add(1))
        self.approve(self.add(2))
        self.add(3)  # Accepted, but not yet reviewed.
        document = self.tools._document()
        document["accepted"][1]["intent_details"] += " Changed after review."
        before = self.path.with_name("leads.json").read_bytes()
        projected, metadata = confirmed_leads.export_view(self.path, document)
        self.assertEqual([r["company"]["domain"] for r in projected["accepted"]], ["example1.com"])
        self.assertEqual((metadata["confirmed_count"], metadata["target_count"], metadata["shortfall"]), (1, 5, 4))
        self.assertFalse(metadata["delivery_allowed"])
        self.assertEqual(self.path.with_name("leads.json").read_bytes(), before)
        document["accepted"] = []
        with self.assertRaisesRegex(ValueError, "No unchanged confirmed"):
            confirmed_leads.export_view(self.path, document)

    def test_partial_projection_rechecks_saved_receipt_evidence(self):
        self.approve(self.add(1))
        document = self.tools._document()
        rid = document["accepted"][0]["primary_contact"]["source"]["route_id"]
        (self.path.parent / "receipts" / (rid + ".json")).unlink()
        with self.assertRaises(ValueError):
            confirmed_leads.export_view(self.path, document)

    @unittest.skipUnless(os.environ.get("TYCHE_WORKSPACE_NODE_MODULES"), "bundled workbook runtime required")
    def test_blocked_finish_exports_verified_partial_workbook_without_mutating_research(self):
        self.approve(self.add(1))
        self.add(2)  # Unreviewed accepted rows must not leak into the workbook.
        ledger = budget_guard.ledger_path(self.path)
        with budget_guard.transaction(ledger) as state:
            state["blocked"] = "Later provider call is uncertain"
        full_validation = self.path.with_name("validation.json")
        full_validation.write_text('{"delivery_allowed":false,"fixture":"preserve"}')
        before = {p: p.read_bytes() for p in self.path.parent.rglob("*.json")}
        calls = len(self.provider.requests)
        self.tools.environment = dict(os.environ)
        self.tools.execute = None  # Exercise the production finalization path.
        with patch("billing_reconciliation.reconcile", side_effect=AssertionError("No network reconciliation")) as reconcile:
            result = self.tools.finish()
        reconcile.assert_not_called()
        self.assertEqual(result["status"], "operationally_blocked")
        self.assertFalse(result["delivery_allowed"])
        exported = result["partial_export"]
        self.assertTrue(exported["exported"], exported)
        self.assertEqual((exported["rows"], exported["shortfall"]), (1, 4))
        self.assertTrue(exported["saved_workbook_values_verified"])
        workbook = self.path.with_name("leads-partial.xlsx")
        rows = read_first_sheet_rows(workbook)
        self.assertEqual(len(rows), 2)
        self.assertIn("Example Products 1", rows[1])
        with zipfile.ZipFile(workbook) as archive:
            self.assertIn(b'name="Status"', archive.read("xl/workbook.xml"))
            self.assertIn(b'ref="A1:S2"', archive.read("xl/tables/table1.xml"))
        validation = json.loads(self.path.with_name("validation-partial.json").read_text())
        self.assertTrue(validation["partial"])
        self.assertFalse(validation["delivery_allowed"])
        self.assertEqual((validation["confirmed_count"], validation["target_count"]), (1, 5))
        self.assertFalse(self.path.with_name("leads.xlsx").exists())
        self.assertEqual(len(self.provider.requests), calls)
        for p, data in before.items():
            self.assertEqual(p.read_bytes(), data, str(p))

    def add(self, number, *, signal_text=None, intent_details=None):
        row = copy.deepcopy(self.template["accepted"][0])
        company, person = row["company"], row["primary_contact"]
        target = f"example{number}.com"
        company_url = f"https://www.linkedin.com/company/example-products-{number}/"
        person_url = f"https://www.linkedin.com/in/ada-example-{number}/"
        name = f"Example Products {number}"
        email = "ada@" + target

        def lookup(**options):
            return self.tools.call("tyche_lookup", {"checks": [check(target, **options)]})["lookups"][0]["results"][0]["ref"]

        self.provider.raw = {"status": "ok", "element": {"name": name,
            "website": "https://" + target, "linkedinUrl": company_url,
            "employeeCountRange": {"start": 201, "end": 500},
            "locations": [{"headquarter": True, "country": "United States", "geographicArea": "Ohio"}]}}
        selected = lookup(inputs={"url": company_url})
        fit = captured_page(self.tools, self.provider, target=target, url=f"https://{target}/about",
            text=row["account_fit"]["evidence_text"], date=row["account_fit"]["evidence_date"])
        signal = captured_page(self.tools, self.provider, target=target, url=f"https://{target}/integration",
            text=signal_text or row["signal_evidence"]["evidence_text"], date=row["signal_evidence"]["evidence_date"])
        self.tools.call("tyche_review", {"companies": [{"target": target, "decision": "qualify_account",
            "reason": "Captured business and integration evidence reviewed",
            "company": {"ref": selected, **{k: company[k] for k in ("industry", "sub_industry", "description", "classification_note")}},
            "account_fit": {"ref": fit, "fit_claim": row["account_fit"]["fit_claim"]},
            "qualification_checks": [{"requirement_ref": "icp:industries", "status": "pass",
                "claim": "Manufactures products", "evidence": [{"ref": fit}]},
                {"requirement_ref": "signal:0", "status": "pass",
                "claim": "Integrated an acquired warehouse", "evidence": [{"ref": signal, "event_date": "2026-08-12"}]}],
            "intent_details": intent_details or row["intent_details"]}],
            "sources": [{"refs": [fit, signal], "state": "exhausted", "reason": "Captured source passages reviewed"}]})
        self.provider.raw = {"status": "ok", "element": {"linkedinUrl": person_url,
            "firstName": "Ada", "lastName": "Example", "email": email, "currentPosition": [{"companyName": name,
                "title": person["current_title"], "companyLinkedinUrl": company_url}],
            "location": {"parsed": {"city": "Columbus", "state": "Ohio", "countryFull": "United States"}}}}
        profile = lookup(phase="contact_verification", tool="harvestapi_get_profile", inputs={"url": person_url})
        self.tools.call("tyche_review", {"companies": [{"target": target, "decision": "hold_contact",
            "reason": "Current buyer verified; email verification remains",
            "primary_contact": {"ref": profile, "requested_role": person["requested_role"], "role_match": "exact"}}]})
        self.provider.raw = {"status": "ok", "data": {"address": email, "status": "valid", "sub_status": ""}}
        email_ref = lookup(phase="email_validation", tool="zerobounce_validate", inputs={"email": email})
        return self.tools.call("tyche_review", {"companies": [{"target": target, "decision": "accept",
            "reason": "Company, signal, buyer and exact email verified", "primary_contact": {"email_ref": email_ref}}]})

    def approve(self, packet):
        self.assertEqual(packet["status"], "review_required", packet)
        self.assertEqual(packet["review_scope"], "confirmed_leads")
        self.assertEqual(packet["approval_tool"], "tyche_review")
        return self.tools.call("tyche_review", {"review_ref": packet["review_ref"], "review_findings": review_findings(packet)})

    def test_changed_lead_and_final_packet_keep_their_scope_when_repeated(self):
        for number in range(1, 6):
            self.approve(self.add(number))
        row = self.tools._document()['accepted'][-1]
        packet = self.tools.review(companies=[{'target': 'example5.com', 'decision': 'accept',
            'reason': 'Clarified output', 'intent_details': row['intent_details'] + ' Coordination may be useful.'}])
        before = self.path.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        repeated = self.tools.review()
        self.assertTrue(repeated['unchanged'])
        for item in (packet, repeated):
            self.assertEqual(item['expected_targets'], ['example5.com'])
            self.assertEqual(item['review_scope'], 'confirmed_leads')
            self.assertEqual(item['approval_tool'], 'tyche_review')
        self.assertEqual((self.path.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.approve(packet)
        self.tools.environment['TYCHE_FINALIZATION_ONLY'] = '1'
        document = self.tools._document()
        final = self.tools.review_delivery(document)
        repeated_final = self.tools.review_delivery(document)
        self.assertTrue(repeated_final['unchanged'])
        for item in (final, repeated_final):
            self.assertEqual(item['expected_targets'], [f'example{number}.com' for number in range(1, 6)])
            self.assertEqual(item['review_scope'], 'final_delivery')
            self.assertEqual(item['approval_tool'], 'tyche_finish')

    def test_approval_requires_complete_findings_with_company_sources(self):
        packet = self.add(1)
        valid = review_findings(packet)
        run_before = self.path.read_bytes()
        ledger_before = budget_guard.ledger_path(self.path).read_bytes()
        output_before = self.path.with_name("leads.json").read_bytes()
        invalid = [None, [], valid * 2,
            [dict(valid[0], target="another.example")],
            [dict(valid[0], source_refs=["another-receipt:0"])],
            [dict(valid[0], finding="   ")]]
        for findings in invalid:
            with self.subTest(findings=findings), self.assertRaises(ValueError) as failure:
                self.tools.review(review_ref=packet["review_ref"], review_findings=findings)
            if findings == [] or findings == valid * 2 or (findings and findings[0].get("target") == "another.example"):
                message = str(failure.exception)
                self.assertIn('Expected targets: ["example1.com"]', message)
                self.assertIn('Received targets: ' + json.dumps([f["target"] for f in findings]), message)
            if findings and findings[0].get("source_refs") == ["another-receipt:0"]:
                message = str(failure.exception)
                self.assertIn("example1.com", message)
                self.assertIn("another-receipt:0", message)
                for ref in packet["companies"][0]["sources"]:
                    self.assertIn(ref, message)
            self.assertEqual(self.path.read_bytes(), run_before)
            self.assertEqual(budget_guard.ledger_path(self.path).read_bytes(), ledger_before)
            self.assertEqual(self.path.with_name("leads.json").read_bytes(), output_before)
        self.approve(packet)
        self.assertEqual(self.file()["review_findings"], valid)
        self.tools = ResearchTools(self.path, execute=self.provider)
        self.assertEqual(self.file()["review_findings"], valid)

    def test_final_approval_requires_findings_and_invalidates_them_after_edit(self):
        packet = self.add(1)
        self.tools.environment["TYCHE_FINALIZATION_ONLY"] = "1"
        document = self.tools._document()
        final = self.tools.review_delivery(document)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.tools.review_delivery(document, final["review_ref"])
        self.assertEqual(self.path.read_bytes(), before)
        self.tools.review_delivery(document, final["review_ref"], review_findings(final))
        saved = self.tools._document()
        self.assertEqual(saved["final_review"]["findings"], review_findings(final))
        self.tools.review(companies=[{"target": "example1.com", "decision": "accept",
            "reason": "Clarified a qualified inference", "intent_details": saved["accepted"][0]["intent_details"] +
                " This may create integration needs."}])
        fresh = self.tools.review_delivery(self.tools._document(), final["review_ref"], review_findings(final))
        self.assertEqual(fresh["status"], "review_required")
        self.assertNotEqual(fresh["review_ref"], final["review_ref"])
        self.assertEqual(self.file()["review_findings"], [])

    def test_correcting_optional_status_keeps_lead_contact_and_receipts(self):
        passage = "Example completed warehouse integration on August 12, 2026. Additional equipment will be consolidated next year."
        original = self.template["accepted"][0]["intent_details"]
        draft = original + " Additional equipment has already been consolidated."
        packet = self.add(1, signal_text=passage, intent_details=draft)
        company = packet["companies"][0]
        self.assertEqual(company["intent_details"], draft)
        self.assertIn(passage, [source.get("text") for source in company["sources"].values()])
        self.assertNotIn("sources", packet)  # Source bodies live beside their own company.
        before = self.tools._document()["accepted"][0]
        ledger = budget_guard.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        corrected = original + " Additional equipment consolidation is planned. This may create integration needs."
        fresh = self.tools.review(companies=[{"target": "example1.com", "decision": "accept",
            "reason": "Completed integration does not establish completion of the additional equipment plan",
            "intent_details": corrected}])
        stale = self.tools.review(review_ref=packet["review_ref"], review_findings=review_findings(packet))
        self.assertEqual(stale["status"], "review_required")
        findings = review_findings(fresh)
        findings[0]["finding"] = "The passage confirms completed warehouse integration, while additional equipment consolidation is planned. Corrected prose preserves that distinction; integration needs are a qualified inference."
        self.tools.review(review_ref=fresh["review_ref"], review_findings=findings)
        saved = self.file()["leads"][0]
        self.assertEqual(saved["intent_details"], corrected)
        for field in ("company", "primary_contact", "qualification_checks"):
            self.assertEqual(saved[field], before[field])
        self.assertEqual(budget_guard.ledger_path(self.path).read_bytes(), ledger)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(self.file()["review_findings"], findings)

    def test_supported_completion_and_qualified_analysis_are_preserved(self):
        text = "Example completed warehouse integration and equipment consolidation on August 12, 2026."
        prose = self.template["accepted"][0]["intent_details"] + " Equipment consolidation is complete; it may improve coordination."
        packet = self.add(1, signal_text=text, intent_details=prose)
        self.approve(packet)
        self.assertEqual(self.file()["leads"][0]["intent_details"], prose)
        self.tools.review(companies=[{"target": "example1.com", "decision": "hold_account",
            "reason": "Required activity is not established by the reviewed source",
            "qualification_checks": [{"requirement_ref": "signal:0", "status": "unknown",
                "claim": "Required activity needs corroboration", "evidence": []}]}])
        self.assertEqual(self.file()["leads"], [])
        self.assertEqual(self.file()["review_findings"], [])

    def test_file_grows_during_research_and_survives_resume_with_unfinished_work(self):
        self.assertEqual(self.file()["leads"], [])
        first = self.add(1)
        self.assertEqual(self.file()["confirmed_count"], 0)
        calls = len(self.provider.requests)
        blocked = self.tools.call("tyche_lookup", {"checks": [check("next.example")]})
        self.assertEqual(blocked["review_ref"], first["review_ref"])
        self.assertEqual(len(self.provider.requests), calls)
        saved = self.approve(first)
        self.assertFalse(saved["delivery_allowed"])
        self.assertEqual(self.file()["confirmed_count"], 1)
        first_bytes = self.path.with_name("leads.json").read_bytes()
        self.tools = ResearchTools(self.path, execute=self.provider, environment={"TYCHE_FINALIZATION_ONLY": "0"})
        self.tools.call("tyche_review", {"review_ref": first["review_ref"]})
        self.assertEqual(self.path.with_name("leads.json").read_bytes(), first_bytes)
        second = self.add(2)
        self.assertEqual(len(second["companies"]), 1)  # Earlier confirmed lead is not reviewed again.
        self.approve(second)
        self.tools.call("tyche_review", {"companies": [{"target": "unfinished.example", "decision": "hold_account",
            "reason": "Still checking the required signal"}]})
        self.assertEqual([r["company"]["domain"] for r in self.file()["leads"]], ["example1.com", "example2.com"])
        self.assertEqual(self.file()["target_count"], 5)
        self.assertEqual(self.tools.call("tyche_finish", {})["status"], "needs_research")
        self.assertFalse(self.path.with_name("leads.xlsx").exists())
        self.assertNotIn("stop_reason", json.loads(self.path.read_text()))
        ledger = budget_guard.ledger_path(self.path)
        with budget_guard.transaction(ledger) as state:
            state["blocked"] = "Later provider call is uncertain"
        before = self.path.with_name("leads.json").read_bytes()
        resumed = ResearchTools(self.path, execute=self.provider)
        self.assertEqual(resumed.inspect()["confirmed_leads"]["confirmed_count"], 2)
        self.assertEqual(resumed.lookup([check()])["status"], "operationally_blocked")
        self.assertEqual(self.path.with_name("leads.json").read_bytes(), before)

    def test_stale_approval_and_changed_confirmed_lead_require_current_review(self):
        packet = self.add(1)
        changed = self.tools.call("tyche_review", {"companies": [{"target": "example1.com", "decision": "accept",
            "reason": "Corrected reviewed explanation", "intent_details": self.template["accepted"][0]["intent_details"] +
                " The integration may require closer coordination across warehouses."}]})
        stale = self.tools.call("tyche_review", {"review_ref": packet["review_ref"]})
        self.assertEqual(stale["status"], "review_required")
        self.assertEqual(stale["review_ref"], changed["review_ref"])
        self.assertEqual(self.file()["leads"], [])
        self.approve(changed)
        revised = self.tools.review(companies=[{"target": "example1.com", "decision": "accept",
            "reason": "Tighten the confirmed explanation", "intent_details": self.template["accepted"][0]["intent_details"]}])
        self.assertEqual(self.file()["leads"], [])
        self.assertEqual(self.tools.review(review_ref=changed["review_ref"])["status"], "review_required")
        self.approve(revised)
        self.tools.call("tyche_review", {"companies": [{"target": "example1.com", "decision": "hold_contact",
            "reason": "New evidence requires another buyer review"}]})
        self.assertEqual(self.file()["leads"], [])
        self.assertEqual(len(json.loads(self.path.read_text())["unresolved"]), 1)

    def test_interrupted_atomic_write_preserves_prior_file_and_retry_needs_no_lookup(self):
        self.approve(self.add(1))
        second = self.add(2)
        output = self.path.with_name("leads.json")
        before, calls = output.read_bytes(), len(self.provider.requests)
        replace = os.replace

        def fail_snapshot(source, destination):
            if Path(destination) == output:
                self.assertEqual(output.read_bytes(), before)
                self.assertEqual(json.loads(Path(source).read_text())["confirmed_count"], 2)
                raise OSError("Fixture disk write failed")
            return replace(source, destination)

        with patch.object(confirmed_leads.os, "replace", side_effect=fail_snapshot):
            with self.assertRaisesRegex(OSError, "disk write failed"):
                self.approve(second)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(list(output.parent.glob(".leads-*.tmp")), [])
        self.tools = ResearchTools(self.path, execute=self.provider)
        self.approve(second)
        self.assertEqual(self.file()["confirmed_count"], 2)
        self.assertEqual(len(self.provider.requests), calls)

    def test_unverified_email_never_enters_confirmed_file(self):
        self.approve(self.add(1))
        self.add(2)
        saved = json.loads(self.path.read_text())
        saved["accepted"][1]["primary_contact"]["email_validation"]["status"] = "invalid"
        self.path.write_text(json.dumps(saved))
        blocked = self.tools.call("tyche_review", {})
        self.assertEqual(blocked["status"], "needs_repair")
        self.assertTrue(blocked["errors"])
        self.assertEqual(self.file()["confirmed_count"], 1)

    def test_spending_pause_does_not_discard_a_completed_lead(self):
        packet = self.add(1)
        ledger = budget_guard.ledger_path(self.path)
        with budget_guard.transaction(ledger) as state:
            state["blocked"] = "Uncertain later provider billing"
        before = ledger.read_bytes()
        self.approve(packet)
        self.assertEqual(self.file()["confirmed_count"], 1)
        self.assertEqual(ledger.read_bytes(), before)
        calls = len(self.provider.requests)
        self.assertEqual(self.tools.lookup([check()])["status"], "operationally_blocked")
        self.assertEqual(len(self.provider.requests), calls)

    def test_owner_uniqueness_spans_previous_and_new_confirmations(self):
        self.add(1)
        saved = json.loads(self.path.read_text())
        saved["accepted"][0]["company"]["owner_group"] = "Shared Parent"
        self.path.write_text(json.dumps(saved))
        self.approve(self.tools.review())
        self.add(2)
        saved = json.loads(self.path.read_text())
        saved["accepted"][1]["company"]["owner_group"] = "Shared Parent"
        self.path.write_text(json.dumps(saved))
        blocked = self.tools.review()
        self.assertEqual(blocked["status"], "needs_repair")
        self.assertTrue(any("duplicate owner group" in error for error in blocked["errors"]))
        self.assertEqual(self.file()["confirmed_count"], 1)

    def test_accounting_inconsistency_still_blocks_approval(self):
        self.approve(self.add(1))
        packet = self.add(2)
        budget_guard.reserve({"run_file": str(self.path), "route_id": "missing-receipt", "max_cost_credits": 0.1}, "deepline")
        with self.assertRaises(ValueError):
            self.approve(packet)
        self.assertEqual(self.file()["confirmed_count"], 1)

    def test_approval_cannot_be_combined_with_changed_findings(self):
        packet = self.add(1)
        with self.assertRaisesRegex(ValueError, "separately"):
            self.tools.call("tyche_review", {"review_ref": packet["review_ref"], "companies": [
                {"target": "example1.com", "decision": "hold_contact", "reason": "Recheck"}]})
        self.assertEqual(self.file()["leads"], [])

    def test_foreign_or_corrupted_snapshot_is_preserved(self):
        self.approve(self.add(1))
        output = self.path.with_name("leads.json")
        saved = self.file()
        saved["request_fingerprint"] = "different-request"
        output.write_text(json.dumps(saved))
        before = output.read_bytes()
        with self.assertRaisesRegex(ValueError, "another run/request"):
            self.tools.call("tyche_review", {})
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

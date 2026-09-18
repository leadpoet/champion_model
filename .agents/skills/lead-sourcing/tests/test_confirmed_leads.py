"""Native tool journeys verify the continuously saved file without provider calls."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_client_output import client_document
from test_research_tools import FixtureProvider, captured_page, check
import budget_guard
import confirmed_leads
from research_tools import ResearchTools


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

    def add(self, number):
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
            text=row["signal_evidence"]["evidence_text"], date=row["signal_evidence"]["evidence_date"])
        self.tools.call("tyche_review", {"companies": [{"target": target, "decision": "qualify_account",
            "reason": "Captured business and integration evidence reviewed",
            "company": {"ref": selected, **{k: company[k] for k in ("industry", "sub_industry", "description", "classification_note")}},
            "account_fit": {"ref": fit, "fit_claim": row["account_fit"]["fit_claim"]},
            "qualification_checks": [{"requirement_ref": "icp:industries", "status": "pass",
                "claim": "Manufactures products", "evidence": [{"ref": fit}]},
                {"requirement_ref": "signal:0", "status": "pass",
                "claim": "Integrated an acquired warehouse", "evidence": [{"ref": signal, "event_date": "2026-08-12"}]}],
            "intent_details": row["intent_details"]}],
            "sources": [{"refs": [fit, signal], "state": "exhausted", "reason": "Captured source passages reviewed"}]})
        self.provider.raw = {"status": "ok", "element": {"linkedinUrl": person_url,
            "firstName": "Ada", "lastName": "Example", "currentPosition": [{"companyName": name,
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
        return self.tools.call("tyche_review", {"review_ref": packet["review_ref"]})

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

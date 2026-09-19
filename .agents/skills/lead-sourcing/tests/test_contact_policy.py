"""Contact minimums, targets and preserved company records; no provider calls."""
import copy
from pathlib import Path
import unittest

from test_export_xlsx import accepted_document
from test_research_interface import setup_request
from test_stop_policy import NOW, action, stop_document
from linkedin_fixtures import add_linkedin_fields
import research_input
import validate_run as validator


def contacts_document(count=3, minimum=3, target=5):
    document = accepted_document()
    document["request"].update(target_count=1, min_contacts_per_company=minimum,
                               target_contacts_per_company=target)
    row = document["accepted"][0]
    row["backup_contacts"] = []
    for index in range(1, count):
        person = copy.deepcopy(row["primary_contact"])
        person.update(full_name=f"Buyer {index}", email=f"buyer{index}@example.com",
                      linkedin_url=f"https://linkedin.com/in/buyer-{index}")
        person.pop("location_evidence")
        person["email_validation"]["email"] = person["email"]
        rid = f"email-validation-{index + 1}"
        person["email_validation"]["source"]["route_id"] = rid
        document["routes"].append(dict(document["routes"][0], route_id=rid))
        row["backup_contacts"].append(person)
    return add_linkedin_fields(document)


class ContactPolicyTests(unittest.TestCase):
    def normalize(self, **fields):
        request = setup_request()["request"]
        request.update(fields)
        return research_input.normalize_request(request, Path("/tmp/contact-policy/results.json"))

    def test_defaults_minimum_only_target_only_and_legacy_alias(self):
        for fields, expected in (({}, (1, 1)), ({"min_contacts_per_company": 3}, (3, 3)),
                ({"target_contacts_per_company": 5}, (1, 5)), ({"contacts_per_company": 5}, (1, 5))):
            with self.subTest(fields=fields):
                request = self.normalize(**fields)
                self.assertEqual(validator.contact_limits(request), expected)
                self.assertNotIn("contacts_per_company", request)

    def test_invalid_counts_and_conflicting_alias_fail(self):
        for fields in ({"min_contacts_per_company": 0}, {"target_contacts_per_company": True},
                       {"contacts_per_company": 2.5}, {"min_contacts_per_company": 5, "target_contacts_per_company": 3},
                       {"contacts_per_company": 3, "target_contacts_per_company": 5}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.normalize(**fields)

    def test_legacy_resume_preserves_request_and_completion(self):
        request = self.normalize()
        request.pop("min_contacts_per_company")
        request.pop("target_contacts_per_company")
        request["contacts_per_company"] = 3
        resumed = research_input.normalize_request(setup_request()["request"], Path("/tmp/contact-policy/results.json"), saved=request)
        self.assertEqual(resumed, request)
        document = dict(request=dict(request, target_count=1), accepted=[{"primary_contact": {"full_name": "Ada"}}])
        self.assertTrue(validator.sourcing_target_met(document))

    def test_minimum_requires_distinct_complete_contacts(self):
        document = contacts_document()
        self.assertEqual(validator.accepted_errors(document), [])
        for kind in ("too_few", "duplicate", "missing_email", "invalid_email"):
            with self.subTest(kind=kind):
                bad = copy.deepcopy(document)
                row = bad["accepted"][0]
                if kind == "too_few":
                    row["backup_contacts"].pop()
                elif kind == "duplicate":
                    row["backup_contacts"][0] = copy.deepcopy(row["primary_contact"])
                elif kind == "missing_email":
                    row["backup_contacts"][0].pop("email")
                    row["backup_contacts"][0].pop("email_validation")
                else:
                    row["backup_contacts"][0]["email_validation"]["status"] = "invalid"
                self.assertTrue(validator.accepted_errors(bad))

    def test_company_target_does_not_hide_contact_work_or_deadline(self):
        row = contacts_document()["accepted"][0]
        document = stop_document([action("more-buyers", scope="example.com")], accepted=[row])
        document["request"].update(min_contacts_per_company=3, target_contacts_per_company=5)
        result = validator.evaluate_stop(document, now=NOW)
        self.assertEqual(result["decision"], "continue")
        self.assertEqual(result["eligible_actions"], ["more-buyers"])
        self.assertEqual(result["contact_coverage"]["target_shortfall"], 2)
        self.assertNotIn("discovery", result["missing_scopes"])
        document["stop_check"]["next_actions"] = []
        self.assertEqual(validator.evaluate_stop(document, now=NOW)["missing_scopes"], ["example.com"])
        document["request"]["max_duration_seconds"] = 60
        self.assertEqual(validator.evaluate_stop(document, now=NOW)["decision"], "time_limit_reached")
        document["accepted"] = contacts_document(5)["accepted"]
        self.assertEqual(validator.evaluate_stop(document, now=NOW)["decision"], "target_met")

    def test_pending_extra_profile_keeps_minimum_but_does_not_count_toward_target(self):
        document = contacts_document(4)
        pending = document["accepted"][0]["backup_contacts"][-1]
        pending.pop("email")
        pending.pop("email_validation")
        self.assertEqual(validator.accepted_errors(document), [])
        self.assertEqual(validator.ready_contact_indexes(document["accepted"][0], document["request"]), [0, 1, 2])
        coverage = validator.contact_coverage(document)
        self.assertEqual((coverage["contacts"], coverage["companies_at_minimum"], coverage["target_shortfall"]), (3, 1, 2))

    def test_contact_only_review_preserves_company_details_and_prior_buyers(self):
        document = contacts_document()
        row = document["accepted"][0]
        row.update(account_fit={"fit_claim": "Existing verified fit"}, intent_details="Existing company intent")
        old = copy.deepcopy(row)
        extras = contacts_document(5)["accepted"][0]["backup_contacts"]
        # Use the same concise review translator as tyche_review.
        change = research_input.company_update(document, {"scope": "example.com", "state": "accepted", "backup_contacts": extras})
        result = change["row"]
        for key, value in old.items():
            if key != "backup_contacts":
                self.assertEqual(result[key], value)
        self.assertEqual(result["backup_contacts"][:2], old["backup_contacts"])
        self.assertEqual(result["contact_candidate_count"], 5)
        self.assertEqual(result["backup_shortfall"], 0)

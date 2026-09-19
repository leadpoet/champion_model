"""Request-specific evidence gates, exercised without provider calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import budget_guard
import research_input
from research_tools import ResearchTools
from test_research_tools import FixtureProvider, captured_page
import validate_run


def request(mode="all"):
    return {"target_count": 1, "icp": {"exclusions": ["excluded.test"]},
            "requested_roles": ["Operations leader"], "contact_fields": [],
            "buying_signals": [
                {"kind": "Expansion", "importance": "required", "min_age_days": 30, "max_age_days": 90},
                {"kind": "Partnership", "importance": "required", "max_age_days": 180},
                {"kind": "Hiring", "importance": "preferred", "max_age_days": 45}],
            "signal_match_mode": mode, "time_window": {"max_age_days": 365, "as_of_date": "2026-09-14"}}


def check(kind="Expansion", status="pass", importance="required", date="2026-08-01"):
    return {"criterion": kind, "signal": kind, "importance": importance, "status": status,
            "claim": "Reviewed event", "evidence": [] if status == "unknown" else [
                {"url": "https://example.test/event", "date": date, "date_basis": "published", "event_date": date,
                 "text": "Source facts", "source": {"provider": "public_web", "operation": "open", "route_id": "source"}}]}


def document(req, checks, state="accepted"):
    row = {"company": {"canonical_name": "Example", "domain": "example.test"}, "qualification_checks": checks}
    if state == "unresolved":
        row["stage"] = "contact"
    if state == "rejected":
        row["reason_code"] = "not_icp_fit"
    return {"request": req, state: [row]}


class SignalRequirementsTests(unittest.TestCase):
    def test_calendar_months_keep_the_original_boundary_and_precision(self):
        req = request('any')
        req['time_window'] = {'as_of_date': '2026-09-17', 'max_age_months': 6}
        req['buying_signals'] = [{'kind': 'Expansion', 'importance': 'required'}]
        for event, passes in [('2026-03-16', False), ('2026-03-17', True),
                              ('2026-03', False), ('2026-04', True), ('2026-09-18', False)]:
            with self.subTest(event=event):
                self.assertEqual(not validate_run.signal_age_errors(req,
                    {'qualification_checks': [check(date=event)]}, 'company'), passes)
        saved = research_input.normalize_request(req, Path('run/results.json'), started_at='2026-09-17T00:00:00Z')
        resumed = research_input.normalize_request(saved, Path('run/results.json'), saved=saved,
                                                  started_at='2026-10-17T00:00:00Z')
        self.assertEqual(resumed, saved)

    def test_calendar_month_ends_and_signal_unit_override(self):
        for as_of, months, expected in [('2024-03-31', 1, '2024-02-29'),
                                        ('2025-03-31', 1, '2025-02-28'),
                                        ('2026-01-31', 6, '2025-07-31')]:
            start = validate_run.signal_window_start(datetime.fromisoformat(as_of), {}, {'max_age_months': months})
            self.assertEqual(start.date().isoformat(), expected)
        as_of = datetime(2026, 9, 17)
        self.assertEqual(validate_run.signal_window_start(as_of, {'max_age_days': 90},
            {'max_age_months': 6}).date().isoformat(), '2026-06-19')
        self.assertEqual(validate_run.signal_window_start(as_of, {'max_age_months': 6},
            {'max_age_days': 90}).date().isoformat(), '2026-03-17')

    def test_calendar_limits_reject_ambiguous_or_malformed_units(self):
        for invalid in ({'max_age_months': None}, {'max_age_months': True}, {'max_age_months': 0},
                        {'max_age_months': 1.5}, {'max_age_months': -1},
                        {'max_age_months': 6, 'max_age_days': 183}):
            for scope in ('shared', 'signal'):
                req = request('any')
                req['time_window'] = invalid if scope == 'shared' else {}
                req['buying_signals'] = [{'kind': 'Expansion', **(invalid if scope == 'signal' else {})}]
                with self.subTest(invalid=invalid, scope=scope), self.assertRaises(ValueError):
                    research_input.normalize_request(req, Path('run/results.json'))
    def test_legacy_label_can_be_explicitly_mapped_without_changing_request_or_evidence(self):
        req = request("any")
        for signal in req["buying_signals"]:
            signal.pop("importance")
        old = check("Expansion")
        old.update(criterion="legacy event", signal="New location")
        doc = document(req, [old], "unresolved")
        before = json.dumps(doc, sort_keys=True)
        self.assertIn("explicit requirement_ref", " ".join(validate_run.qualification_errors(doc)))
        patch = {"scope": "example.test", "reason_text": "Explicitly mapped saved evidence to its requested kind", "qualification_checks": [
            {k: v for k, v in dict(old, requirement_ref="signal:0").items() if k != "signal"}]}
        result = research_input.company_update(doc, patch)["row"]
        self.assertEqual(result["qualification_checks"][0]["signal"], "Expansion")
        self.assertEqual(result["qualification_checks"][0]["evidence"], old["evidence"])
        self.assertEqual(json.dumps(doc, sort_keys=True), before)
        doc["unresolved"] = [result]
        self.assertEqual(validate_run.qualification_errors(doc), [])
        patch["qualification_checks"][0]["requirement_ref"] = "signal:100"
        with self.assertRaisesRegex(ValueError, "Unknown requirement_ref"):
            research_input.company_update(doc, patch)

    def test_missing_or_preferred_attribute_cannot_pass_as_required(self):
        req = request("any")
        req["icp"]["required_attributes"] = ["Operates multiple sites"]
        checks = [check()]
        self.assertTrue(validate_run.qualification_errors(document(req, checks)))
        fit = dict(criterion="Operates multiple sites", importance="preferred", status="pass", evidence=check()["evidence"])
        self.assertTrue(validate_run.qualification_errors(document(req, checks + [fit])))
        fit["importance"] = "required"
        self.assertEqual(validate_run.qualification_errors(document(req, checks + [fit])), [])
        self.assertTrue(validate_run.qualification_errors(document(req, checks + [fit, fit])))

    def test_insurance_scale_and_open_employee_range_survive_any_signal_match(self):
        req = request('any')
        scale = 'Financial scale of at least USD 50 million; no upper limit'
        req['icp']['required_attributes'] = [scale]
        req['icp']['company_size'] = {'min_employees': 50}
        normalized = research_input.normalize_request(req, Path('run/results.json'))
        self.assertEqual(normalized['icp']['company_size'], {'min_employees': 50})
        self.assertEqual(normalized['icp']['required_attributes'], [scale])
        checks = [check()]
        self.assertTrue(validate_run.qualification_errors(document(normalized, checks)))
        checks.append(dict(criterion=scale, importance='required', status='pass',
                           evidence=check()['evidence']))
        qualified = document(normalized, checks)
        qualified['accepted'][0]['company']['employee_range'] = '201-500'
        self.assertEqual(validate_run.qualification_errors(qualified), [])

    def test_all_requires_every_required_signal_and_any_accepts_one(self):
        checks = [check(), check("Partnership", "unknown"), check("Hiring", "unknown", "preferred")]
        self.assertTrue(validate_run.qualification_errors(document(request(), checks)))
        self.assertEqual(validate_run.qualification_errors(document(request("any"), checks)), [])
        checks[1] = check("Partnership")
        self.assertEqual(validate_run.qualification_errors(document(request(), checks)), [])

    def test_preferred_does_not_replace_required_or_become_a_must_have(self):
        self.assertTrue(validate_run.qualification_errors(document(request("any"), [check("Hiring", importance="preferred")])))
        req = request()
        req["buying_signals"] = [req["buying_signals"][2]]
        self.assertEqual(validate_run.qualification_errors(document(req, [])), [])

    def test_cannot_downgrade_a_required_signal_or_omit_it_before_contacts(self):
        self.assertTrue(validate_run.qualification_errors(document(request("any"), [check(importance="preferred")])))
        self.assertTrue(validate_run.qualification_errors(document(request(), [], "unresolved")))

    def test_failed_alternative_is_not_an_automatic_company_rejection(self):
        checks = [check(status="fail"), check("Partnership", "unknown")]
        self.assertTrue(validate_run.qualification_errors(document(request("any"), checks, "rejected")))
        self.assertEqual(validate_run.qualification_errors(document(request("all"), checks, "rejected")), [])
        checks[1] = check("Partnership")
        self.assertEqual(validate_run.qualification_errors(document(request("any"), checks)), [])

    def test_age_limits_include_both_boundaries(self):
        req = request()
        for date, passes in [("2026-06-15", False), ("2026-06-16", True),
                             ("2026-08-15", True), ("2026-08-16", False), ("2026-09-15", False)]:
            with self.subTest(date=date):
                row = {"qualification_checks": [check(date=date)]}
                self.assertEqual(not validate_run.signal_age_errors(req, row, "example"), passes)

    def test_renamed_kind_does_not_fall_back_to_a_looser_age_window(self):
        row = {"signal_evidence": {"signal": "Recent expansion", "evidence_date": "2026-01-01"}}
        self.assertIn("saved request kind", " ".join(validate_run.signal_age_errors(request(), row, "example")))

    def test_duplicate_signal_judgments_cannot_hide_a_conflict(self):
        self.assertTrue(validate_run.qualification_errors(document(request("any"), [check(), check(status="unknown")])))

    def test_new_requests_require_a_reviewed_check_not_only_legacy_primary(self):
        doc = document(request("any"), [])
        doc["accepted"][0]["signal_evidence"] = {"signal": "Expansion", "evidence_date": "2026-08-01", "event_date": "2026-08-01"}
        self.assertIn("required signal coverage", " ".join(validate_run.qualification_errors(doc)))

    def test_malformed_saved_policy_reports_errors_without_crashing_or_passing(self):
        for invalid in ({"signal_match_mode": "all_of"}, {"buying_signals": None},
                        {"buying_signals": "Expansion"}, {"buying_signals": ["Expansion"]},
                        {"buying_signals": [{"kind": "Expansion", "min_age_days": "30"}]},
                        {"buying_signals": [{"kind": "Expansion", "max_age_days": None}]},
                        {"time_window": {"max_age_days": None}},
                        {"time_window": None},
                        {"buying_signals": [{"kind": "Expansion", "importance": "optional"}]}):
            with self.subTest(invalid=invalid):
                req = {**request(), **invalid}
                self.assertTrue(validate_run.qualification_errors(document(req, [check()])))
                self.assertTrue(validate_run.signal_age_errors(req, {}, "example"))
                with self.assertRaises(ValueError):
                    research_input.normalize_request(request(), Path("run/results.json"), saved=req)

    def test_unspecified_age_window_stays_unspecified_and_required_coverage_remains(self):
        req = request("any")
        req.pop("time_window")
        req["buying_signals"] = [{"kind": "Hiring", "importance": "required"}]
        normalized = research_input.normalize_request(req, Path("run/results.json"), started_at="2026-09-14T00:00:00Z")
        self.assertEqual(normalized["time_window"], {})
        self.assertNotIn("max_age_days", normalized["buying_signals"][0])
        self.assertEqual(validate_run.qualification_errors(document(normalized, [check("Hiring")])), [])
        self.assertTrue(validate_run.qualification_errors(document(normalized, [check("Hiring", status="unknown")])))
        self.assertEqual(validate_run.request_requirements(normalized)[0]["importance"], "required")

    def test_age_limits_still_apply_without_a_shared_window(self):
        req = request("any")
        req["time_window"] = {"as_of_date": "2026-09-14"}
        req["buying_signals"] = [{"kind": "Hiring", "importance": "required", "max_age_days": 30}]
        self.assertTrue(validate_run.signal_age_errors(req, {"qualification_checks": [check("Hiring")]}, "company"))
        self.assertEqual(validate_run.signal_age_errors(req, {"qualification_checks": [check("Hiring", date="2026-09-10")]}, "company"), [])
        req["buying_signals"][0] = {"kind": "Hiring", "importance": "required", "min_age_days": 30}
        self.assertTrue(validate_run.signal_age_errors(req, {"qualification_checks": [check("Hiring", date="2026-09-10")]}, "company"))
        self.assertEqual(validate_run.signal_age_errors(req, {"qualification_checks": [check("Hiring")]}, "company"), [])

    def test_explicit_malformed_age_limits_are_not_treated_as_absent(self):
        for maximum in (None, 0, -1, True, "30", 1.5):
            for scope in ("global", "signal"):
                with self.subTest(maximum=maximum, scope=scope):
                    req = request("any")
                    req["time_window"] = {}
                    req["buying_signals"] = [{"kind": "Hiring", "importance": "required"}]
                    (req["time_window"] if scope == "global" else req["buying_signals"][0])["max_age_days"] = maximum
                    self.assertTrue(validate_run.signal_request_errors(req))
                    with self.assertRaises(ValueError):
                        research_input.normalize_request(req, Path("run/results.json"))

    def test_legacy_primary_evidence_and_saved_preferences_still_work(self):
        req = request("any")
        for signal in req["buying_signals"]:
            signal.pop("importance")
        doc = document(req, [check("Hiring", "unknown", "preferred")])
        doc["accepted"][0]["signal_evidence"] = {"signal": "Expansion", "evidence_date": "2026-08-01", "event_date": "2026-08-01"}
        self.assertEqual(validate_run.qualification_errors(doc), [])
        legacy = {"buying_signals": [{"kind": "Expansion"}]}
        self.assertEqual(validate_run.signal_request_errors(legacy), [])


class RequestNormalizationTests(unittest.TestCase):
    def test_offering_context_and_preferences_survive_resume(self):
        for perspective in ("seller", "target"):
            req = request()
            req["product_service"] = {"description": "The supplied offering", "perspective": perspective}
            saved = research_input.normalize_request(req, Path("run/results.json"))
            resumed = research_input.normalize_request(saved, Path("run/results.json"), saved=saved)
            self.assertEqual(saved, resumed)
            self.assertEqual(saved["product_service"], req["product_service"])
            self.assertEqual(saved["buying_signals"], req["buying_signals"])

    def test_legacy_resume_does_not_rewrite_signal_importance(self):
        req = request()
        for signal in req["buying_signals"]:
            signal.pop("importance")
        saved = research_input.normalize_request(req, Path("run/results.json"), saved=req)
        self.assertEqual(saved["buying_signals"], req["buying_signals"])

    def test_duplicate_kinds_and_invalid_context_are_rejected(self):
        req = request()
        req["buying_signals"].append({"kind": " expansion "})
        with self.assertRaises(ValueError):
            research_input.normalize_request(req, Path("run/results.json"))
        req = request()
        req["product_service"] = {"description": "Offering", "perspective": "guessed"}
        with self.assertRaises(ValueError):
            research_input.normalize_request(req, Path("run/results.json"))

    def test_sector_abbreviations_are_not_invented(self):
        self.assertEqual(research_input.canonical_requested_role("Chief Nursing Officer", ["CNO"]), "Chief Nursing Officer")
        self.assertEqual(research_input.canonical_requested_role("cno", ["CNO"]), "CNO")
        self.assertEqual(research_input.canonical_requested_role("CEO", ["Chief Executive Officer"]), "Chief Executive Officer")


class NativeRequirementJourneyTests(unittest.TestCase):
    def test_exclusion_variant_is_held_then_resolved_without_contact_spending(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = FixtureProvider()
            tools = ResearchTools(Path(directory) / 'results.json', execute=provider)
            req = request('any')
            req['icp']['exclusions'] = ['Pen Underwriting', 'Ancells Farm Dental Clinic']
            tools.start(req, max_usd=1)
            ref = captured_page(tools, provider, text='Pen Underwriting UK is the Pen Underwriting business.', date='2026-08-01')
            finding = {'target': 'example.test', 'decision': 'qualify_account', 'reason': 'Checking identity',
                'company': {'canonical_name': 'Pen Underwriting UK'}, 'account_fit': {'ref': ref},
                'qualification_checks': [dict(check(), evidence=[{'ref': ref, 'event_date': '2026-08-01'}])]}
            before = len(provider.requests), budget_guard.ledger_path(tools.path).read_bytes()
            with self.assertRaisesRegex(ValueError, 'resolve company identity'):
                tools.review(companies=[finding])
            finding.update(decision='reject', reason='Same company as explicit exclusion')
            finding['company']['aliases'] = ['Pen Underwriting']
            finding['qualification_checks'].append({'criterion': 'Explicit exclusion', 'importance': 'required',
                'status': 'fail', 'claim': 'Same company as excluded Pen Underwriting', 'evidence': [{'ref': ref}]})
            tools.review(companies=[finding])
            saved = tools._document()
            self.assertTrue(validate_run.excluded_company(saved['request'], saved['rejected'][0]))
            self.assertEqual(saved['request']['icp']['exclusions'], req['icp']['exclusions'])
            self.assertEqual((len(provider.requests), budget_guard.ledger_path(tools.path).read_bytes()), before)

    def test_name_similarity_requires_review_not_automatic_rejection(self):
        req = {'icp': {'exclusions': ['Pen Underwriting', 'Alpha', 'Acme Risk Limited']}}
        for name, held in [('Pen Underwriting UK', True), ('Acme Risk Ltd.', True),
                           ('Alpha UK', True), ('Alphabet Insurance', False), ('Pen Dental', False)]:
            row = {'company': {'canonical_name': name}}
            self.assertFalse(validate_run.excluded_company(req, row))
            self.assertEqual(bool(validate_run.exclusion_identity_errors(req, row, 'company')), held)
        row = {'company': {'canonical_name': 'Pen Underwriting Services'}, 'qualification_checks': [{
            'criterion': 'Distinct from excluded company: Pen Underwriting', 'importance': 'required',
            'status': 'pass', 'claim': 'Different registered entities', 'evidence': [{'url': 'https://example.test/identity'}]}]}
        self.assertEqual(validate_run.exclusion_identity_errors(req, row, 'company'), [])
        row['company']['aliases'] = ['Pen Underwriting']
        self.assertTrue(validate_run.excluded_company(req, row))  # A distinctness claim never overrides an exact alias.

    def test_review_copies_importance_and_blocks_contact_work_until_all_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run/results.json"
            provider = FixtureProvider()
            tools = ResearchTools(path, execute=provider)
            tools.start(request(), max_usd=1)
            ref = captured_page(tools, provider, url="https://example.test/event", text="Source facts", date="2026-08-01")
            evidence = [{"ref": ref, "event_date": "2026-08-01"}]
            finding = {"target": "example.test", "decision": "hold_account", "reason": "Reviewing requirements",
                       "company": {"canonical_name": "Example"}, "qualification_checks": [dict(check(), evidence=evidence)]}
            finding["qualification_checks"][0].pop("importance")
            tools.review(companies=[finding])
            saved = json.loads(path.read_text())
            self.assertEqual(saved["unresolved"][0]["qualification_checks"][0]["importance"], "required")
            before, ledger = path.read_bytes(), budget_guard.ledger_path(path).read_bytes()
            with self.assertRaisesRegex(ValueError, "coverage"):
                tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Missing partnership"}])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(budget_guard.ledger_path(path).read_bytes(), ledger)
            tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Both signals reviewed",
                                     "qualification_checks": [dict(check("Partnership"), evidence=evidence)]}])
            self.assertEqual(json.loads(path.read_text())["unresolved"][0]["stage"], "contact")
            self.assertEqual([r["tool"] for r in provider.requests if r["operation"] == "execute"], ["firecrawl_scrape"])


if __name__ == "__main__":
    unittest.main()

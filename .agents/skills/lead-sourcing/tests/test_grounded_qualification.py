"""Offline regressions for the manufacturing audit's evidence failures."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_research_tools import FixtureProvider, captured_page, check
from research_tools import ResearchTools
import budget_guard
import validate_run
import scrapingdog


class GroundedQualificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'results.json'
        self.provider = FixtureProvider()
        self.tools = ResearchTools(self.path, execute=self.provider)
        self.request = {
            'target_count': 1, 'as_of_date': '2026-09-17', 'contact_fields': [],
            'icp': {'company_types': ['Private Equity'],
                    'industries': ['Industrial manufacturing', 'Contract manufacturing'],
                    'geographies': ['United States, Midwest']},
            'requested_roles': ['Operations leader'],
            'buying_signals': [{'kind': 'FACILITY_OPENING', 'importance': 'required',
                               'max_age_days': 365, 'query': 'Opened, expanded capacity or completed an acquisition'}]}
        self.tools.start(self.request, max_usd=1)

    def evidence(self, **kwargs):
        ref = captured_page(self.tools, self.provider, **kwargs)
        return self.tools._evidence({'ref': ref}), ref

    def test_steele_publication_cannot_be_replaced_and_packet_keeps_date(self):
        evidence, ref = self.evidence(date='2026-04-20', text='April 20, 2026: Steele Solutions acquires Maysteel Industries.')
        receipt = self.path.parent / 'receipts' / (ref.split(':')[0] + '.json')
        before = receipt.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for override in ({'date': '2026-04-08'}, {'evidence_date': '2026-04-08'}, {'date_basis': 'observed_current'}):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, 'captured metadata'):
                self.tools._evidence({'ref': ref, **override})
        forged = dict(evidence, date='2026-04-08')
        self.assertIn('captured metadata', validate_run.qualification_evidence_error(
            forged, 'check', json.loads(self.path.read_text()), {}, {'status': 'pass'}, self.path))
        sources = {}
        self.tools._company_review({'company': {'domain': 'example.test'}, 'account_fit': evidence}, sources)
        self.assertEqual(sources[ref]['date'], '2026-04-20')
        self.assertEqual(sources[ref]['date_basis'], 'published')
        self.assertEqual((receipt.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_undated_expansion_does_not_gain_an_event_date_from_observation(self):
        evidence, ref = self.evidence(date=None, text='The Bloomington expansion is under construction. GMP readiness is expected in 2027.')
        self.assertEqual(evidence['date_basis'], 'observed_current')
        for override in ({'date': '2026-09-17'}, {'date_basis': 'published'}):
            with self.assertRaisesRegex(ValueError, 'captured metadata'):
                self.tools._evidence({'ref': ref, **override})
        row = {'qualification_checks': [{'signal': 'FACILITY_OPENING', 'status': 'pass', 'evidence': [evidence]}]}
        self.assertIn('event_date is required', ' '.join(validate_run.signal_age_errors(self.request, row, 'company')))

    def test_capture_retains_planned_status_and_review_does_not_rewrite_receipt(self):
        passage = 'October 3, 2025: The final phase has started. Completion is scheduled for late January 2026.'
        evidence, ref = self.evidence(date='2025-10-03', text=passage)
        original = (self.path.parent / 'receipts' / (ref.split(':')[0] + '.json')).read_bytes()
        finding = {'target': 'example.test', 'decision': 'hold_account', 'reason': 'Completion remains unverified',
            'qualification_checks': [{'requirement_ref': 'signal:0', 'status': 'unknown',
                'claim': 'The source announces a final phase; it does not establish completion.',
                'evidence': [{'ref': ref, 'event_date': '2025-10-03'}]}]}
        self.tools.review(companies=[finding])
        sources = {}
        packet = self.tools._company_review(json.loads(self.path.read_text())['unresolved'][0], sources)
        self.assertEqual(sources[ref]['text'], passage)
        self.assertIn('does not establish completion', packet['signal_checks'][0]['draft_claim'])
        self.assertEqual((self.path.parent / 'receipts' / (ref.split(':')[0] + '.json')).read_bytes(), original)
        bad_quote = dict(evidence, text='The final phase is complete.')
        self.assertIn('quote captured source', validate_run.qualification_evidence_error(
            bad_quote, 'check', json.loads(self.path.read_text()), {}, {'status': 'pass'}, self.path))
        # This verifies preserved context, not the model's semantic judgment.

    def test_all_company_filters_gate_contact_work_and_reuse_one_capture(self):
        body = 'The PE-backed manufacturer operates a machining plant in Ohio. It completed an acquisition on April 20, 2026.'
        evidence, ref = self.evidence(date='2026-04-20', text=body)
        requirements = self.tools.inspect(field='requirements')['requirements']
        filters = [r for r in requirements if r['ref'].startswith('icp:')]
        self.assertEqual({r['ref'] for r in filters}, {'icp:company_types', 'icp:industries', 'icp:geographies'})
        self.assertEqual(len([r for r in filters if r['ref'] == 'icp:industries']), 1)  # alternatives, not two must-haves
        checks = [{'requirement_ref': r['ref'], 'status': 'pass', 'claim': body,
                   'evidence': [{'ref': ref}]} for r in filters]
        checks.append({'requirement_ref': 'signal:0', 'status': 'pass', 'claim': 'Completed acquisition',
                       'evidence': [{'ref': ref, 'event_date': '2026-04-20'}]})
        finding = {'target': 'example.test', 'decision': 'qualify_account', 'reason': 'Reviewed criteria',
                   'qualification_checks': checks}
        for omitted in range(3):
            partial = copy.deepcopy(finding)
            partial['qualification_checks'].pop(omitted)
            before = self.path.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)
            with self.assertRaisesRegex(ValueError, filters[omitted]['label'].split(':')[0]):
                self.tools.review(companies=[partial])
            self.assertEqual((self.path.read_bytes(), budget_guard.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.tools.review(companies=[finding])
        document = json.loads(self.path.read_text())
        self.assertEqual(document['unresolved'][0]['stage'], 'contact')
        self.assertEqual(document['request']['icp'], self.request['icp'])
        self.assertEqual(len([r for r in self.provider.requests if r['operation'] == 'execute']), 1)

    def test_old_ownership_date_and_partial_event_precision_are_preserved(self):
        evidence, ref = self.evidence(date='2024-04-08', text='April 8, 2024: Lincoln announces the sale to a private equity portfolio company.')
        self.tools.review(companies=[{'target': 'example.test', 'decision': 'hold_account',
            'reason': 'Ownership has no recency requirement', 'qualification_checks': [
                {'requirement_ref': 'icp:company_types', 'status': 'pass', 'claim': 'PE ownership', 'evidence': [{'ref': ref}]}]}])
        self.assertEqual(json.loads(self.path.read_text())['unresolved'][0]['qualification_checks'][0]['evidence'][0]['date'], '2024-04-08')
        # Publication and activity may differ; month precision must not become a guessed day.
        ref = captured_page(self.tools, self.provider, url='https://example.test/recap', date='2026-09-01', text='The acquisition closed in July 2026.')
        evidence = self.tools._evidence({'ref': ref, 'event_date': '2026-07'})
        self.assertEqual((evidence['date'], evidence['event_date']), ('2026-09-01', '2026-07'))
        self.assertEqual(validate_run.signal_age_errors(self.request, {'qualification_checks': [
            {'signal': 'FACILITY_OPENING', 'status': 'pass', 'evidence': [evidence]}]}, 'company'), [])

    def test_scraped_page_does_not_promote_a_mentioned_date_to_publication(self):
        body = '<p>Read our 2026-09-01 update.</p><p>Completion is expected in 2027.</p>'
        row = scrapingdog.normalize_result({'url': 'https://example.test/expansion', 'html': body}, 'scrape')
        self.assertIsNone(row['evidence_date'])
        self.assertIn('Completion is expected in 2027.', row['evidence_text'])

    def test_publication_precision_and_timestamps_survive_capture(self):
        for index, (date, expected) in enumerate([('2026-04', '2026-04'), ('2026-04-20T11:00:00Z', '2026-04-20')]):
            evidence, ref = self.evidence(url=f'https://example.test/dates-{index}', date=date)
            self.assertEqual(evidence['date'], expected)
            self.assertIsNone(validate_run.source_evidence_error(evidence, 'source'))


if __name__ == '__main__':
    unittest.main()

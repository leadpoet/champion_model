"""Replay source/date/URL failures through existing review and delivery paths."""
import copy
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import research_input
import research_tools
import validate_run
from research_tools import ResearchTools
from test_client_output import client_document
import test_client_output as client_output
from test_research_tools import FixtureProvider, check as lookup_check
from test_request_requirements import request, check
from test_research_interface import setup_request
import budget_guard


class WebPassageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.provider = FixtureProvider()
        self.tools = ResearchTools(Path(directory.name) / 'results.json', execute=self.provider)
        self.tools.start(setup_request()['request'], max_usd=1)

    def observed(self, operation='open', field='text'):
        result = self.tools.review(web=[{'target': 'example.test', 'purpose': 'Read partnership source ' + operation + ' ' + field,
            'query': 'https://example.test/news ' + field, 'operation': operation, 'response': {'status': 'ok', 'results': [{
                'url': 'https://example.test/news', field: 'Example announced a planned partnership.',
                'date': '2026-01-01', 'date_basis': 'published'}]}}])
        return result['web_references']['web:0'] + ':0'

    def company(self, ref, **evidence):
        return {'target': 'example.test', 'decision': 'qualify_account', 'reason': 'Compare source with request',
                'account_fit': {'ref': ref}, 'qualification_checks': [{'requirement_ref': 'signal:0',
                    'status': 'pass', 'claim': 'The source announces a planned partnership.',
                    'evidence': [{'ref': ref, 'event_date': '2026-01-01', **evidence}]}]}

    def test_search_text_and_open_snippets_cannot_pass_before_contact_spend(self):
        for operation, field in [('search_query', 'text'), ('search_query', 'snippet'), ('open', 'snippet')]:
            with self.subTest(operation=operation, field=field):
                ref = self.observed(operation, field)
                before = budget_guard.ledger_path(self.tools.path).read_bytes(), len(self.provider.requests)
                with self.assertRaisesRegex(ValueError, 'required web evidence'):
                    self.tools.review(companies=[self.company(ref)])
                self.assertEqual((budget_guard.ledger_path(self.tools.path).read_bytes(), len(self.provider.requests)), before)

    def test_opened_passage_reused_without_calls_and_interpretation_stays_separate(self):
        ref = self.observed()
        before = len(self.provider.requests)
        self.tools.review(companies=[self.company(ref)])
        document = json.loads(self.tools.path.read_text())
        row = document['unresolved'][0]
        self.assertEqual(row['stage'], 'contact')
        self.assertEqual(row['qualification_checks'][0]['evidence'][0]['text'], 'Example announced a planned partnership.')
        self.assertFalse(validate_run.qualification_errors(document, run_file=self.tools.path))
        self.assertEqual(len(self.provider.requests), before)
        with self.assertRaisesRegex(ValueError, 'put interpretation in claim'):
            self.tools.review(companies=[self.company(ref, text='The partnership is complete.')])

    def test_unknown_or_preferred_claim_does_not_require_an_opened_body(self):
        ref = self.observed('search_query', 'snippet')
        evidence = self.tools._evidence({'ref': ref})
        document = json.loads(self.tools.path.read_text())
        for importance, status in [('required', 'unknown'), ('preferred', 'pass')]:
            self.assertIsNone(validate_run.qualification_evidence_error(evidence, 'check', document, {},
                {'importance': importance, 'status': status}, self.tools.path))

    def test_opened_passage_after_snippet_uses_same_url_and_preserves_old_receipt(self):
        web = {'target': 'example.test', 'purpose': 'Read announcement', 'query': 'https://example.test/news',
               'operation': 'open', 'response': {'status': 'ok', 'results': [{
                   'url': 'https://example.test/news', 'snippet': 'Example announced a partnership.',
                   'date': '2026-01-01', 'date_basis': 'published'}]}}
        old = self.tools.review(web=[web])['web_references']['web:0']
        receipt = self.tools.path.parent / 'receipts' / (old + '.json')
        before, ledger, calls = receipt.read_bytes(), budget_guard.ledger_path(self.tools.path).read_bytes(), len(self.provider.requests)
        web['response']['results'][0]['text'] = web['response']['results'][0].pop('snippet')
        new = self.tools.review(web=[web])['web_references']['web:0']
        self.assertNotEqual(old, new)
        self.tools.review(companies=[self.company(new + ':0')])
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(budget_guard.ledger_path(self.tools.path).read_bytes(), ledger)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(self.tools.review(web=[web])['web_references']['web:0'], new)

    def test_failed_open_text_cannot_qualify_and_successful_observation_can_reuse_url(self):
        web = {'target': 'example.test', 'purpose': 'Read announcement', 'query': 'https://example.test/news',
               'operation': 'open', 'response': {'status': 'ok', 'results': [{
                   'url': 'https://example.test/news', 'text': 'Internal Error ()\nSource: open(...)\nURL is not safe to open (non-retryable error)',
                   'date': '2026-01-01', 'date_basis': 'published'}]}}
        old = self.tools.review(web=[web])['web_references']['web:0']
        before = budget_guard.ledger_path(self.tools.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, 'tool error, not source text'):
            self.tools.review(companies=[self.company(old + ':0')])
        self.assertEqual((budget_guard.ledger_path(self.tools.path).read_bytes(), len(self.provider.requests)), before)
        web['response']['results'][0]['text'] = 'Example announced a partnership to address Internal Error () reports.'
        new = self.tools.review(web=[web])['web_references']['web:0']
        self.assertNotEqual(old, new)
        self.tools.review(companies=[self.company(new + ':0')])
        self.assertFalse(validate_run.qualification_errors(json.loads(self.tools.path.read_text()), run_file=self.tools.path))

    def test_missing_or_malformed_source_returns_feedback(self):
        for evidence in (None, {}, {'source': None}, {'source': 'not a reference'}):
            self.assertIn('requires dated source evidence', validate_run.qualification_evidence_error(
                evidence, 'check', {}, {}, {'importance': 'required', 'status': 'pass'}, self.tools.path))

    def test_wrong_url_operation_provider_and_foreign_receipt_do_not_pass(self):
        ref = self.observed()
        document = json.loads(self.tools.path.read_text())
        original = self.tools._evidence({'ref': ref})
        for field, value in [('url', 'https://example.test/other'), ('operation', 'search_query'), ('provider', 'deepline')]:
            evidence = copy.deepcopy(original)
            (evidence if field == 'url' else evidence['source'])[field] = value
            self.assertIsNotNone(validate_run.qualification_evidence_error(evidence, 'check', document, {},
                {'importance': 'required', 'status': 'pass'}, self.tools.path))
        path = self.tools.path.parent / 'receipts' / (ref.split(':')[0] + '.json')
        saved = json.loads(path.read_text())
        saved['run_fingerprint'] = 'another-run'
        path.write_text(json.dumps(saved))
        self.assertIn('another run', validate_run.qualification_evidence_error(original, 'check', document, {},
            {'importance': 'required', 'status': 'pass'}, self.tools.path))

    def test_old_snippet_qualification_is_blocked_at_contact_and_export(self):
        ref = self.observed('search_query', 'snippet')
        evidence = self.tools._evidence({'ref': ref})
        document = json.loads(self.tools.path.read_text())
        row = {'candidate': {'domain': 'example.test'}, 'stage': 'contact',
            'qualification_checks': [{'criterion': 'partnership', 'signal': 'PARTNERSHIP', 'importance': 'required',
                'status': 'pass', 'claim': 'Earlier judgment', 'evidence': [evidence]}]}
        document['unresolved'] = [row]
        self.tools.path.write_text(json.dumps(document))
        before = budget_guard.ledger_path(self.tools.path).read_bytes(), sum(r['operation'] == 'execute' for r in self.provider.requests)
        with self.assertRaisesRegex(ValueError, 'required web evidence'):
            self.tools.lookup([lookup_check(phase='contact_discovery', tool='fixture_search', inputs={'query': 'buyer'})])
        self.assertEqual((budget_guard.ledger_path(self.tools.path).read_bytes(), sum(r['operation'] == 'execute' for r in self.provider.requests)), before)
        document['accepted'], document['unresolved'] = [row], []
        self.assertIn('required web evidence', ' '.join(validate_run.source_evidence_errors(document, run_file=self.tools.path)))


class SignalTimingTests(unittest.TestCase):
    def errors(self, value):
        signal = check()
        signal['evidence'][0].update(value)
        return validate_run.signal_age_errors(request(), {'qualification_checks': [signal]}, 'example')

    def test_recent_recap_does_not_renew_old_event(self):
        self.assertTrue(self.errors({'date': '2026-09-01', 'event_date': '2025-01-01'}))
        self.assertFalse(self.errors({'date': '2026-09-01', 'event_date': '2026-07-02'}))

    def test_missing_event_timing_needs_evidence_not_publication_fallback(self):
        self.assertIn('event_date is required', ' '.join(self.errors({'event_date': None})))

    def test_month_precision_passes_only_when_entire_period_fits(self):
        self.assertFalse(self.errors({'event_date': '2026-07'}))
        self.assertTrue(self.errors({'event_date': '2026-06'}))
        self.assertTrue(self.errors({'event_date': '2026-08'}))
        self.assertTrue(self.errors({'event_date': '2026'}))

    def test_current_observation_and_historical_event_stay_distinct(self):
        self.assertFalse(self.errors({'date_basis': 'observed_current', 'event_date': None}))
        self.assertTrue(self.errors({'date_basis': 'observed_current', 'event_date': '2024-01-01'}))

    def test_invalid_dates_and_future_events_do_not_pass(self):
        for date in ['2026-02-30', '2026-13', '0000', '2026-7', 'today', 2026, '2027-01-01']:
            with self.subTest(date=date):
                self.assertTrue(self.errors({'event_date': date}))

    def test_unknown_optional_signal_is_not_forced_to_invent_a_date(self):
        row = {'qualification_checks': [check('Hiring', 'unknown', 'preferred')]}
        self.assertFalse(validate_run.signal_age_errors(request(), row, 'example'))


class WebsiteTests(unittest.TestCase):
    def test_saved_wrapper_unwraps_only_to_matching_company(self):
        for path in ['suspicious-page', 'redirect']:
            value = {'domain': 'example.com', 'website': 'https://www.linkedin.com/redir/' + path + '?url=example%2ecom'}
            original = copy.deepcopy(value)
            self.assertEqual(validate_run.company_website(value), 'https://example.com')
            self.assertEqual(value, original)
            update = research_input.company_update({'request': {}}, {'scope': 'example.com', 'company': value, 'reason_text': 'Source reviewed'})
            self.assertEqual(update['row']['candidate']['website'], 'https://example.com')

    def test_mismatch_and_unsafe_destinations_are_actionable(self):
        for value in ['https://linkedin.com/company/example', 'https://example.com.evil.test',
                      'https://linkedin.com/redir/suspicious-page?url=https%3A%2F%2Fother.test',
                      'https://linkedin.com/redir/redirect?url=example.com&url=other.test',
                      'https://user:pass@example.com', 'javascript:alert(1)', 'https://example.com\\@other.test']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_run.company_website({'domain': 'example.com', 'website': value})

    def test_direct_site_subdomains_and_verified_domain_fallback(self):
        for source, expected in [('example.com', 'https://example.com'),
                                 (' https://example.com/ ', 'https://example.com/'),
                                 ('https://docs.example.com/about', 'https://docs.example.com/about'),
                                 (None, 'https://example.com')]:
            self.assertEqual(validate_run.company_website({'domain': 'example.com', 'website': source}), expected)


class ReviewQualityTests(unittest.TestCase):
    def test_company_identity_error_names_the_target_and_selected_receipt_without_changing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = ResearchTools(Path(directory) / 'results.json', execute=FixtureProvider())
            tools.start(request(), max_usd=1)
            ref = tools.lookup([lookup_check()])['lookups'][0]['results'][0]['ref']
            before = tools.path.read_bytes()
            with self.assertRaises(ValueError) as caught:
                tools.review(companies=[{'target': 'different.test', 'decision': 'hold_account',
                                         'reason': 'Verify intended company', 'company': {'ref': ref}}])
            for value in ('different.test', 'example.test', ref, 'No identity was changed'):
                self.assertIn(value, str(caught.exception))
            self.assertEqual(tools.path.read_bytes(), before)

    def test_review_packet_preserves_structured_company_evidence_beside_description(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = FixtureProvider()
            facts = {'industries': ['Manufacturing'], 'specialities': ['Components'],
                     'locations': [{'city': 'Madison', 'geographicArea': 'Wisconsin', 'country': 'US'},
                                   {'city': 'Rockford', 'geographicArea': 'Illinois', 'country': 'US'}],
                     'companyType': 'Privately Held'}
            provider.raw['element'].update(facts)
            provider.raw['element']['description'] = 'Example makes industrial components.'
            tools = ResearchTools(Path(directory) / 'results.json', execute=provider)
            tools.start(request(), max_usd=1)
            ref = tools.lookup([lookup_check()])['lookups'][0]['results'][0]['ref']
            value, source, _ = tools._resolve(ref)
            row = client_document()['accepted'][0]
            row['account_fit'] = {'evidence_url': value['company_linkedin_url'], 'source': source}
            row['qualification_checks'] = []
            row.pop('signal_evidence', None)
            sources = {}
            before = tools.path.read_bytes()
            packet = tools._company_review(row, sources)
            selected = sources[packet['account_fit']['source_refs'][0]]
            for key, expected in facts.items():
                self.assertEqual(selected['record'][key], expected)
            self.assertEqual(selected['detail_ref'], ref)
            self.assertIn('Example makes industrial components.', selected['text'])
            self.assertEqual(tools.path.read_bytes(), before)

    def test_taxonomy_feedback_can_be_resolved_without_provider_calls_or_state_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = ResearchTools(Path(directory) / 'results.json', execute=FixtureProvider())
            tools.start(request(), max_usd=1)
            before = tools.path.read_bytes()
            row = client_document()['accepted'][0]
            row['company'].update(industry='Health Care', sub_industry='Behavioral medicine')
            errors = []
            validate_run._validate_client_output([row], errors)
            self.assertIn("tyche_inspect(field='taxonomy.Health Care')", ' '.join(errors))
            choices = tools.inspect(field='taxonomy.Health Care')
            self.assertIn('Behavioral Health', choices['sub_industries'])
            row['company']['sub_industry'] = 'Behavioral Health'
            errors = []
            validate_run._validate_client_output([row], errors)
            self.assertEqual(errors, [])
            self.assertEqual(tools.path.read_bytes(), before)
            # Every returned choice is accepted by the same canonical validator.
            for industry in tools.inspect(field='taxonomy')['industries']:
                for sub in tools.inspect(field='taxonomy.' + industry)['sub_industries']:
                    row['company'].update(industry=industry, sub_industry=sub)
                    errors = []
                    validate_run._validate_client_output([row], errors)
                    self.assertEqual(errors, [], (industry, sub))
            with self.assertRaisesRegex(ValueError, 'canonical industry'):
                tools.inspect(field='taxonomy.Invented')

    def test_taxonomy_mismatch_suggests_parents_without_silently_reclassifying(self):
        row = client_document()['accepted'][0]
        row['company'].update(industry='Software', sub_industry='Textiles')
        before = copy.deepcopy(row)
        errors = []
        validate_run._validate_client_output([row], errors)
        self.assertIn("Valid parents for 'Textiles': ['Manufacturing']", ' '.join(errors))
        self.assertEqual(row, before)

    def test_harvest_selection_normalizes_wrapper_without_changing_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = FixtureProvider()
            wrapper = "https://linkedin.com/redir/suspicious-page?url=example%2etest"
            provider.raw["element"]["website"] = wrapper
            tools = ResearchTools(Path(directory) / "results.json", execute=provider)
            tools.start(request(), max_usd=1)
            ref = tools.lookup([lookup_check()])["lookups"][0]["results"][0]["ref"]
            original = tools._receipt(ref)["result"]
            tools.review(companies=[{"target": "example.test", "decision": "hold_account",
                "reason": "Continue source review", "company": {"ref": ref}}])
            saved = json.loads(tools.path.read_text())["unresolved"][0]["candidate"]
            self.assertEqual(saved["website"], "https://example.test")
            self.assertEqual(tools._receipt(ref)["result"], original)

    def test_two_signals_reuse_original_passages_and_show_timing_and_website(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = ResearchTools(Path(directory) / 'run/results.json', execute=FixtureProvider())
            tools.start(request(), max_usd=1)
            observed = [{'url': 'https://example.com/recap', 'date': '2026-09-01',
                         'text': 'The company announced funding on July 2. Its platform launched in July.'}]
            result = tools.review(web=[{'target': 'example.com', 'purpose': 'Read recap', 'query': 'recap',
                                       'operation': 'open', 'response': {'status': 'ok', 'results': observed}}])
            ref = result['web_references']['web:0'] + ':0'
            evidence = tools._evidence({'ref': ref, 'event_date': '2026-07'})
            self.assertEqual(evidence['date'], '2026-09-01')
            self.assertEqual(evidence['event_date'], '2026-07')
            row = client_document()['accepted'][0]
            row['account_fit'] = dict(row['account_fit'], source=evidence['source'], evidence_url=observed[0]['url'])
            row['company']['website'] = 'https://linkedin.com/redir/suspicious-page?url=example.com'
            row.pop('signal_evidence')
            row['qualification_checks'] = [dict(check(kind), evidence=[copy.deepcopy(evidence)])
                                            for kind in ['Expansion', 'Partnership']]
            row['intent_details'] = 'The company is a timely account for a senior buyer.'
            sources = {}
            packet = tools._company_review(row, sources)
            self.assertEqual(len(packet['signal_checks']), 2)
            self.assertEqual(packet['qualification_checks'], [])
            self.assertEqual(packet['company']['website'], 'https://example.com')
            self.assertEqual(packet['intent_details'], row['intent_details'])
            self.assertEqual(len(sources), 1)
            for signal in packet['signal_checks']:
                value = signal['evidence'][0]
                self.assertEqual(value['event_date'], '2026-07')
                self.assertEqual(sources[value['source_refs'][0]]['text'], observed[0]['text'])
            # This test proves review context, not that the LLM rejects generic prose.
            before = research_tools.runner.review_fingerprint({'accepted': [row]})
            row['qualification_checks'][0]['evidence'][0]['event_date'] = '2026-07-02'
            self.assertNotEqual(before, research_tools.runner.review_fingerprint({'accepted': [row]}))

    def test_export_timeouts_preserve_approval_and_do_not_request_research_repairs(self):
        for failure in [subprocess.TimeoutExpired('export', 180),
                        subprocess.CompletedProcess('export', 1, '', 'Output validation failed: ETIMEDOUT')]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                tools = ResearchTools(Path(directory) / 'results.json', execute=FixtureProvider())
                tools.start(request(), max_usd=1)
                before = tools.path.read_bytes()
                kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with patch.object(tools, '_operational_block', return_value=None), \
                     patch.object(tools, '_overview', return_value={'stop': 'target_reached'}), \
                     patch('research_tools.runner.pending_source_reviews', return_value=[]), \
                     patch('research_tools.runner.delivery_preflight', return_value=(None, {'errors': []})), \
                     patch.object(tools, 'review_delivery', return_value=None), \
                     patch('research_tools.subprocess.run', **kwargs):
                    result = tools.finish()
                self.assertEqual(result['status'], 'export_retryable')
                self.assertFalse(result['delivery_allowed'])
                self.assertIn('Do not rewrite findings', result['next'])
                self.assertEqual(before, tools.path.read_bytes())


class SourceExportTests(unittest.TestCase):
    run_rows_json = client_output.ClientOutputTests.run_rows_json

    @classmethod
    def setUpClass(cls):
        cls.node = os.environ.get("TYCHE_WORKSPACE_NODE") or shutil.which("node")

    def test_recap_and_wrapper_export(self):
        document = client_document()
        signal = document['accepted'][0]['signal_evidence']
        signal.update(evidence_date='2026-08-30', event_date='2026-08', evidence_text='The August recap describes the integration earlier that month.')
        document['accepted'][0]['company']['website'] = 'https://linkedin.com/redir/suspicious-page?url=example.com'
        result = self.run_rows_json(document)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['rows'][0]['Website'], 'https://example.com')
        self.assertIn('Activity date: 2026-08', payload['rows'][0]['Signals'])
        self.assertIn('Source date: 2026-08-30', payload['rows'][0]['Signals'])
        self.assertIn('Activity date: 2026-08', next(row['Evidence Text'] for row in payload['sources'] if row['Field'] == 'Signals'))
        self.assertTrue(payload['unchanged'])


if __name__ == '__main__':
    unittest.main()

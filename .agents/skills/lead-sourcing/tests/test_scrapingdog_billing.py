"""Exercise real dispatch, saved accounting, continuation and audit; no network."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_provider_scripts import ROOT
import budget_guard as budget
import scrapingdog
import scrapingdog_billing as prices
from research_tools import ResearchTools
from test_research_tools import setup_request, FixtureProvider, check


class TariffTests(unittest.TestCase):
    def test_all_supported_operations_have_a_versioned_tariff(self):
        for operation in scrapingdog.OPERATIONS:
            with self.subTest(operation=operation):
                tariff = prices.quote(operation, {})
                self.assertEqual(tariff['version'], prices.VERSION)
                self.assertGreaterEqual(tariff['maximum_credits'], tariff['minimum_credits'])
                self.assertTrue(tariff['sources'])

    def test_pricing_options_and_provider_defaults(self):
        for operation, params, expected in [
            ('google_search', {}, 5), ('google_search', {'mob_search': 'true'}, 10),
            ('google_search', {'advance_search': True, 'mob_search': True}, 10),
            ('scrape', {}, 5), ('scrape', {'dynamic': 'false'}, 1),
            ('scrape', {'premium': True}, 25), ('scrape', {'premium': 'true', 'dynamic': False}, 10),
            ('scrape', {'dynamic': False, 'country': 'us'}, 10),
            ('linkedin_company', {}, 10), ('youtube_transcript', {}, 1),
        ]:
            with self.subTest(operation=operation, params=params):
                quote = prices.quote(operation, params)
                self.assertEqual((quote['minimum_credits'], quote['maximum_credits']), (expected, expected))
        self.assertIsNone(prices.quote('scrape', {'country': 'us'}))
        with self.assertRaises(ValueError):
            prices.quote('google_search', {'mob_search': 'sometimes'})
        self.assertEqual(prices.quote('linkedin_person', {})['maximum_credits'], 100)
        self.assertEqual(prices.quote('linkedin_post', {})['maximum_credits'], 25)


class BillingJourneyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'run/results.json'
        self.tools = ResearchTools(self.path, execute=FixtureProvider())
        self.tools.start(request=setup_request()['request'], max_usd=1,
                         provider_credit_limits={'deepline': 10, 'scrapingdog': 1000}, scrapingdog_usd_per_credit=.001)
        self.tools.execute = None
        self.key = patch.dict(os.environ, {'SCRAPINGDOG_API_KEY': 'fixture-never-sent'})
        self.key.start(); self.addCleanup(self.key.stop)
        self.transport = patch.object(scrapingdog, '_http_get', return_value=(200, '{"organic_results":[]}', {})).start()
        self.addCleanup(patch.stopall)

    def lookup(self, query='one', **inputs):
        return self.tools.lookup([{'provider': 'scrapingdog', 'target': 'fixture.test', 'phase': 'account_discovery', 'purpose': 'Find fixture companies',
                                  'inputs': {'operation': 'google_search', **(inputs or {'query': query})}}])

    def state(self):
        return budget.load_ledger(self.path)

    def receipt(self):
        rid = next(reversed(self.state()['calls']))
        path = self.path.parent / 'receipts' / (rid + '.json')
        return rid, path, json.loads(path.read_text())

    def audit(self):
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_successful_empty_calls_continue_and_saved_costs_audit(self):
        self.lookup('one'); self.lookup('two')
        summary = budget.actual_cost_summary(self.state())
        self.assertEqual(summary['provider_usd'], .01)
        self.assertEqual(summary['pending_provider_calls'], 0)
        self.assertIsNone(budget.spending_stop(self.state()))
        self.assertEqual(len(summary['providers']['scrapingdog']['documented_tariff_calls']), 2)
        self.audit()
        self.assertEqual(self.transport.call_count, 2)

    def test_complete_unknown_schema_is_still_billed(self):
        self.transport.return_value = (200, '{"changed_schema":true}', {})
        self.lookup()
        rid, _, receipt = self.receipt()
        self.assertEqual(receipt['status'], 'schema_error')
        self.assertEqual(self.state()['calls'][rid]['actual_credits'], '5')
        self.audit()

    def test_provider_failure_is_free_but_local_timeout_is_held_and_not_replayed(self):
        self.transport.return_value = (429, '{"error":"rate limited"}', {})
        self.lookup('failure')
        self.assertEqual(budget.actual_cost_summary(self.state())['provider_usd'], 0)
        self.transport.side_effect = scrapingdog.ProviderResponseError('timeout')
        self.lookup('timeout')
        summary = budget.actual_cost_summary(self.state())
        self.assertEqual(summary['held_provider_usd'], .005)
        self.assertEqual(summary['budget_total_usd'], .005)
        self.assertEqual(summary['pending_provider_calls'], 0)
        self.assertIsNone(budget.spending_stop(self.state()))
        self.audit()
        before = self.transport.call_count
        try:
            self.lookup('timeout')
        except ValueError:
            pass
        self.assertEqual(self.transport.call_count, before)
        # A changed request may continue; the original hold is never released.
        self.transport.side_effect = None
        self.transport.return_value = (200, '{"organic_results":[]}', {})
        self.lookup('next')
        self.assertEqual(budget.actual_cost_summary(self.state())['budget_total_usd'], .01)
        self.audit()

    def test_variable_tariff_keeps_ceiling_separate_from_actual(self):
        self.transport.return_value = (200, '{"full_name":"Fixture"}', {})
        self.lookup(operation='linkedin_person', id='fixture')
        summary = budget.actual_cost_summary(self.state())
        self.assertEqual(summary['provider_usd'], 0)
        self.assertEqual(summary['held_provider_usd'], .1)
        self.assertEqual(summary['providers']['scrapingdog']['held_credits'], 100)
        self.assertIsNone(budget.spending_stop(self.state()))
        self.audit()

    def test_unsupported_combined_price_stays_unknown_without_fake_ceiling(self):
        self.transport.return_value = (200, '<html>Fixture</html>', {})
        self.lookup(operation='scrape', url='https://fixture.test', country='us')
        self.assertEqual(budget.spending_stop(self.state()), 'billing_pending')
        self.assertNotIn('held_provider_usd', budget.actual_cost_summary(self.state()))
        self.audit()

    def test_forged_charge_request_or_response_fails_audit(self):
        self.lookup()
        rid, path, receipt = self.receipt()
        for field in ('billing', 'request', 'same-price-request', 'response', 'tariff', 'route', 'ledger', 'run', 'request-and-fingerprint'):
            changed = copy.deepcopy(receipt)
            if field == 'billing': changed['billing']['credits_charged'] = 0
            if field == 'request': changed['attempt']['request']['advance_search'] = True
            if field == 'same-price-request': changed['attempt']['request']['query'] = 'other query'
            if field == 'route': changed['spend_receipt']['route_id'] = 'other-route'
            if field == 'ledger': changed['spend_receipt']['ledger'] = '/other/ledger.json'
            if field == 'run': changed['run_fingerprint'] = 'another-run'
            if field == 'request-and-fingerprint':
                from source_receipts import request_fingerprint
                changed['attempt']['request']['query'] = 'other query'
                changed['request_fingerprint'] = request_fingerprint('scrapingdog', changed['attempt']['request'])
            if field == 'response': changed['provider_response']['http_status'] = 202
            if field == 'tariff': changed['tariff']['maximum_credits'] = 0
            path.write_text(json.dumps(changed))
            self.assertTrue(budget.audit_ledger(self.path, json.loads(self.path.read_text())), field)
        path.write_text(json.dumps(receipt))
        self.audit()

    def test_crash_after_raw_capture_or_settlement_recovers_without_dispatch(self):
        import run_attempt
        from provider_output import ResponseFile
        # The transport and authorization stay untouched during saved recovery.
        for stage in ('before-settlement', 'after-settlement'):
            self.transport.return_value = (200, '{"organic_results":[]}', {})
            context = (patch('budget_guard.settle', side_effect=OSError('fixture crash'))
                       if stage == 'before-settlement' else patch.object(ResponseFile, 'finish', return_value=False))
            with context:
                if stage == 'before-settlement':
                    # guarded_call reports settlement failure; finishing fails too,
                    # reproducing a crash with only the raw response durable.
                    with patch.object(ResponseFile, 'finish', return_value=False):
                        with self.assertRaises(OSError): self.lookup(stage)
                else:
                    with self.assertRaises(OSError): self.lookup(stage)
            count = self.transport.call_count
            rid, path, raw = self.receipt()
            self.assertEqual(raw['receipt_status'], 'response_received')
            result = run_attempt.recover_completed_attempts(self.path)
            self.assertEqual(result['recovered'], [rid])
            self.assertEqual(result['errors'], [])
            self.assertEqual(self.state()['calls'][rid]['actual_credits'], '5')
            saved = json.loads(path.read_text())
            self.assertEqual(saved['provider_response'], raw['provider_response'])
            self.assertEqual(saved['status'], 'no_results')
            self.assertEqual(saved['spend_receipt']['state'], self.state()['calls'][rid]['state'])
            self.assertEqual(self.transport.call_count, count)
            self.assertEqual(run_attempt.recover_completed_attempts(self.path)['recovered'], [])
            self.audit()
        self.assertEqual(budget.actual_cost_summary(self.state())['provider_usd'], .01)

    def test_transport_failure_capture_can_finish_after_write_failure(self):
        import run_attempt
        from provider_output import ResponseFile
        self.transport.side_effect = scrapingdog.ProviderResponseError('timeout')
        with patch.object(ResponseFile, 'finish', return_value=False):
            with self.assertRaises(OSError): self.lookup('lost-response')
        rid, _, _ = self.receipt()
        recovered = run_attempt.recover_completed_attempts(self.path)
        self.assertEqual(recovered['recovered'], [rid])
        self.assertEqual(recovered['errors'], [])
        self.assertEqual(self.state()['calls'][rid]['held_credits'], '5')
        self.assertIsNone(self.state()['calls'][rid]['actual_credits'])
        self.assertEqual(self.receipt()[2]['spend_receipt']['state'], self.state()['calls'][rid]['state'])
        self.transport.assert_called_once()
        self.audit()

    def test_deepline_raw_response_survives_final_receipt_write_failure(self):
        import run_attempt
        from provider_output import ResponseFile
        fixture = FixtureProvider()
        self.tools.execute = fixture
        with patch.object(ResponseFile, 'finish', return_value=False):
            with self.assertRaises(OSError): self.tools.lookup([check()])
        rid, path, raw = self.receipt()
        count = len(fixture.requests)
        outcome = run_attempt.recover_completed_attempts(self.path)
        self.assertEqual(outcome['recovered'], [rid])
        self.assertEqual(outcome['errors'], [])
        self.assertEqual(json.loads(path.read_text())['provider_response'], raw['provider_response'])
        self.assertEqual(self.state()['calls'][rid]['actual_credits'], '0.2')
        self.assertEqual(json.loads(path.read_text())['spend_receipt']['state'], 'settled')
        self.assertEqual(len(fixture.requests), count)
        self.audit()

    def test_saved_report_and_strict_cost_audit_keep_holds_separate(self):
        from run_costs import save_report
        import validate_run
        self.transport.side_effect = scrapingdog.ProviderResponseError('timeout')
        self.lookup('held')
        document = json.loads(self.path.read_text())
        errors = []
        validate_run._validate_cost_accounting(document, errors)
        self.assertEqual(errors, [])
        validate_run._validate_budget_accounting(document, errors)
        self.assertEqual(errors, [])
        before = self.receipt()[1].read_bytes()
        (self.path.parent / 'research-commentary.md').write_text('Offline billing journey.')
        saved = json.loads(save_report(self.path.parent, self.path).read_text())
        self.assertEqual(saved['provider_usd'], 0)
        self.assertEqual(saved['held_provider_usd'], .005)
        self.assertEqual(saved['budget_total_usd'], .005)
        self.assertEqual(saved['pending_provider_calls'], 0)
        report = (self.path.parent / 'report.md').read_text()
        self.assertIn('Provider budget held: $0.0050', report)
        self.assertIn('documented_tariff_hold', report)
        self.assertEqual(self.receipt()[1].read_bytes(), before)
        self.audit()

    def test_queued_partial_and_incomplete_responses_preserve_charge_ceiling(self):
        for query, response, expected in [
            ('queued', (202, '{"id":"queued"}', {}), None),
            ('error-envelope', (200, '{"success":false,"error":"upstream failed"}', {}), None),
            ('partial', (200, '{"partial":true,"organic_results":[]}', {}), '5'),
        ]:
            self.transport.return_value = response
            self.lookup(query)
            rid, _, _ = self.receipt()
            self.assertEqual(self.state()['calls'][rid]['actual_credits'], expected)
            self.audit()
        self.transport.side_effect = scrapingdog.ProviderResponseError('provider_error', response={
            'http_status': 200, 'incomplete': True, 'body': '{"organic_results":[]}'})
        self.lookup('incomplete')
        rid, _, _ = self.receipt()
        self.assertIsNone(self.state()['calls'][rid]['actual_credits'])
        self.assertEqual(self.state()['calls'][rid]['held_credits'], '5')
        self.audit()

    def test_known_tariffs_reserve_parallel_capacity_before_dispatch(self):
        # Use the public adapter with a tiny run to isolate the dispatch lock.
        path = self.path.parent / 'small.json'
        path.write_text(json.dumps({'request': {'target_count': 1}, 'accepted': [], 'routes': [],
            'budget': {'policy': 'actual_cost', 'paid_calls': 0, 'limits': {'deepline_credits': 0, 'scrapingdog_credits': 5}}}))
        budget.initialize(path, max_usd=.005, scrapingdog_usd_per_credit=.001)
        barrier = threading.Barrier(2)
        def run(rid):
            barrier.wait()
            return scrapingdog.run({'operation': 'google_search', 'query': rid,
                                    'spend': {'run_file': str(path), 'route_id': rid}})
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, ['first', 'second']))
        self.assertEqual(sorted(code for _, code in results), [0, 2])
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(budget.spending_stop(budget.load_ledger(path)), 'budget_exhausted')

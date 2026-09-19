"""Regressions from the September 19 live seven-provider billing audit."""
import copy
import json
import unittest
from unittest.mock import patch

import test_billing_reconciliation as reconciliation_tests
import test_actual_cost_budget as cost_tests
import billing_reconciliation as billing
import budget_guard as budget
import deepline


class LedgerContractTests(unittest.TestCase):
    setUp = reconciliation_tests.BillingReconciliationTests.setUp

    def debit(self, **changes):
        return dict(dict(id='debit-1', request_id='request-1', provider='fixture', operation='fixture_email_finder',
                    reason='charge_settle', billing_stage='posted', charge_state='posted',
                    charge_credits=.5, delta=-.5), **changes)

    def test_individual_debit_wins_over_grouped_usage_without_splitting(self):
        grouped = dict(self.row, credits=1, delta=-1, metadata={'chargeGroupIds': ['request-1', 'other']})
        rows = billing._ledger_rows([self.debit()])
        proof = billing.matching_charge(self.receipt, [grouped, *rows])
        self.assertEqual((proof['credits'], proof['id'], proof['source']), (.5, 'debit-1', 'credit_ledger'))
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [grouped, *rows]}})
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])
        self.assertIsNone(billing.matching_charge(self.receipt, [dict(proof, charge_credits=1)]))
        self.assertIsNone(billing.matching_charge(self.receipt, [proof, dict(proof, id='second-debit')]))

    def test_holds_refunds_mismatched_audits_and_rounded_usd_are_not_bills(self):
        for fields in ({'reason': 'charge_hold'}, {'charge_state': 'temporary_hold'}, {'status': 'pending'}, {'status': 'error'},
                       {'billing_stage': 'held'}, {'delta': .5}, {'charge_credits': None},
                       {'billing_audit': {'request_id': 'other'}},
                       {'metadata': {'chargeGroupId': 'other'}}, {'metadata': {'postedCredits': 1}}):
            rows = billing._ledger_rows([self.debit(**fields)])
            self.assertIsNone(billing.matching_charge(self.receipt, rows), fields)
        proof = billing.matching_charge(self.receipt, billing._ledger_rows([
            self.debit(charge_credits=.03, delta=-.03, deepline_rough_usd=0)]))
        self.assertEqual(proof['credits'], .03)
        self.assertNotIn('cost_usd', proof)

    def test_http_uses_same_auth_and_falls_back_to_final_free_usage(self):
        free = dict(self.row, status='no_result', charge_state='free', credits=0, delta=0, outcome='miss')
        def page(source, **options):
            self.assertEqual(options['key'], 'same-execution-key')
            self.assertGreater(options['timeout'], 0)
            return {'org_id': 'one', 'entries': [], 'has_more': False} if source == 'ledger' else {
                'org_id': 'one', 'recent': {'entries': [free]}}
        with patch('deepline_http.api_key', return_value='same-execution-key'), \
             patch('deepline_http.billing_page', side_effect=page) as read, \
             patch.object(deepline, '_invoke', side_effect=AssertionError('Do not change credential path')):
            result = billing.reconcile(self.path)
        self.assertEqual(result['unmatched'], [])
        self.assertEqual([call.args[0] for call in read.call_args_list], ['ledger', 'usage'])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_late_debit_is_checked_before_resuming_older_pages(self):
        with budget.transaction(self.path.parent / 'billing-status.json') as state:
            import hashlib
            state.update(attempt_signature=hashlib.sha256(json.dumps(['call-1']).encode()).hexdigest(),
                         attempts=1, ledger_cursor='older-history')
        with patch('deepline_http.api_key', return_value='same-key'), \
             patch('deepline_http.billing_page', return_value={'org_id': 'one',
                 'entries': [self.debit()], 'has_more': True, 'next_cursor': 'next'}) as read:
            result = billing.reconcile(self.path, refresh=True)
        self.assertEqual(result['unmatched'], [])
        self.assertEqual(read.call_count, 1)
        self.assertIsNone(read.call_args.kwargs['cursor'])

    def test_new_posting_on_second_page_is_never_skipped_for_old_cursor(self):
        with budget.transaction(self.path.parent / 'billing-status.json') as state:
            import hashlib
            state.update(attempt_signature=hashlib.sha256(json.dumps(['call-1']).encode()).hexdigest(),
                         attempts=1, ledger_cursor='old')
        seen = []
        def page(source, **options):
            seen.append((source, options['cursor']))
            if options['cursor'] is None:
                return {'entries': [], 'has_more': True, 'next_cursor': 'new-2'}
            self.assertEqual(options['cursor'], 'new-2')
            return {'entries': [self.debit()], 'has_more': False}
        with patch('deepline_http.api_key', return_value='same-key'), patch('deepline_http.billing_page', side_effect=page):
            result = billing.reconcile(self.path, refresh=True)
        self.assertEqual(result['unmatched'], [])
        self.assertEqual(seen, [('ledger', None), ('ledger', 'new-2')])

    def test_exact_delta_must_match_credit_amount_without_float_rounding(self):
        credits = '0.12345678901234567'
        debit = self.debit(charge_credits=credits, delta='-' + credits)
        self.assertIsNotNone(billing.matching_charge(self.receipt, billing._ledger_rows([debit])))
        debit['delta'] = '-0.12345678901234568'
        self.assertIsNone(billing.matching_charge(self.receipt, billing._ledger_rows([debit])))

    def test_contiguous_backlog_advances_across_bounded_reads_and_resume(self):
        seen = []
        def page(source, **options):
            cursor = options['cursor']
            seen.append((source, cursor))
            if source == 'usage':
                return {'recent': {'entries': []}}
            number = 1 if cursor is None else int(cursor)
            return {'entries': [self.debit()] if number == 6 else [], 'has_more': number < 6,
                    'next_cursor': str(number + 1) if number < 6 else None}
        with patch('deepline_http.api_key', return_value='same-key'), patch('deepline_http.billing_page', side_effect=page):
            first = billing.reconcile(self.path)
            self.assertEqual(first['ledger_cursor'], '5')
            self.assertEqual(first['unmatched'], ['call-1'])
            second = billing.reconcile(self.path, resume=True)
        self.assertEqual(second['unmatched'], [])
        self.assertEqual([cursor for source, cursor in seen if source == 'ledger'], [None, '2', '3', '4', None, '5', '6'])

    def test_ledger_transport_outage_uses_healthy_usage(self):
        import deepline_http
        free = dict(self.row, status='no_result', charge_state='free', credits=0, delta=0, outcome='miss')
        def page(source, **options):
            if source == 'ledger':
                raise deepline_http.BillingUnavailable('Billing feed unavailable')
            return {'recent': {'entries': [free]}}
        with patch('deepline_http.api_key', return_value='same-key'), patch('deepline_http.billing_page', side_effect=page) as read:
            result = billing.reconcile(self.path)
        self.assertEqual(result['unmatched'], [])
        self.assertEqual([call.args[0] for call in read.call_args_list], ['ledger', 'usage'])
        self.assertIn('ledger', result['feed_errors'])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_malformed_ledger_does_not_fall_back_to_usage(self):
        with patch('deepline_http.api_key', return_value='same-key'), \
             patch('deepline_http.billing_page', return_value={'entries': 'invalid'}) as read:
            result = billing.reconcile(self.path)
        self.assertEqual(result['unmatched'], ['call-1'])
        self.assertIn('recognized ledger rows', result['error'])
        self.assertTrue(all(call.args[0] == 'ledger' for call in read.call_args_list))

    def test_usage_uses_validated_offsets_instead_of_repeating_opaque_cursors(self):
        seen = []
        def page(source, **options):
            if source == 'ledger':
                return {'entries': []}
            seen.append(options['cursor'])
            offset = int(options['cursor'] or 0)
            return {'recent': {'entries': [self.row] if offset == 100 else [],
                    'offset': offset, 'next_offset': offset + 100, 'has_more': True,
                    'next_cursor': 'same-opaque-cursor'}}
        with patch('deepline_http.api_key', return_value='same-key'), patch('deepline_http.billing_page', side_effect=page):
            result = billing.reconcile(self.path)
        self.assertEqual(result['unmatched'], [])
        self.assertEqual(seen, [None, '100'])

    def test_changed_cursor_cannot_hide_repeated_records(self):
        count = 0
        def page(source, **options):
            nonlocal count
            count += 1
            return {'entries': [self.debit(request_id='another')], 'has_more': True, 'next_cursor': str(count)}
        with patch('deepline_http.api_key', return_value='same-key'), patch('deepline_http.billing_page', side_effect=page):
            result = billing.reconcile(self.path)
        self.assertEqual(result['unmatched'], ['call-1'])
        self.assertIn('repeated records', result['error'])
        self.assertEqual(count, 4)  # Two bounded attempts, two pages each.

    def test_same_record_id_with_conflicting_fields_remains_ambiguous(self):
        def page(source, **options):
            if source == 'usage':
                return {'recent': {'entries': []}}
            first = options['cursor'] is None
            return {'entries': [self.debit(status='pending' if first else 'completed')],
                    'has_more': first, 'next_cursor': 'next' if first else None}
        with patch('deepline_http.api_key', return_value='same-key'), patch('deepline_http.billing_page', side_effect=page):
            result = billing.reconcile(self.path)
        self.assertEqual(result['unmatched'], ['call-1'])
        self.assertNotIn('error', result)
        self.assertTrue(all(page['outcome'] == 'ok' for page in result['read_attempts'][0]['pages']))
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])



class PriceContractTests(unittest.TestCase):
    setUp = cost_tests.ActualCostTests.setUp
    call = cost_tests.ActualCostTests.call

    def test_cutoff_preserves_decimal_precision_before_reporting(self):
        exact = '0.12345678901234567'
        self.call('one', {'cost_usd': exact})
        with budget.transaction(budget.ledger_path(self.path)) as state:
            state['usd_limit'] = exact
        state = budget.load_ledger(self.path)
        self.assertEqual(budget.spending_stop(state), 'budget_exhausted')
        with self.assertRaisesRegex(budget.BudgetError, 'budget_exhausted'):
            budget.reserve({'run_file': str(self.path), 'route_id': 'two'}, 'deepline')
        json.dumps(budget.actual_cost_summary(state))

    def normalize(self, tool, data, bill=None, **envelope):
        raw = {'status': 'completed', 'job_id': 'request-1',
               'toolResponse': {'rawV2': data, 'view': 'rawV2'}, **envelope}
        if bill is not None:
            raw['billing'] = bill
        before = copy.deepcopy(raw)
        result, _ = deepline.normalize_response({'operation': 'execute', 'tool': tool, 'payload': {}, 'limit': 10},
                                              {'body': raw, 'exit_code': 0})
        self.assertEqual(raw, before)
        return result

    def test_final_queued_price_survives_partial_results_and_nonfinal_does_not_settle(self):
        final = {'credits_charged': .28, 'cost_usd': .028,
                 'pricing_status': 'final', 'settlement_status': 'queued'}
        result = self.normalize('fixture_tool', {'results': []}, final)
        self.assertEqual(result['billing'], final)
        self.assertTrue(result['billing_final'])
        self.assertEqual(budget.settlement_billing(dict(result, status='partial')), final)
        for pricing in ('estimated', 'pending', 'unknown', None):
            bill = dict(final, pricing_status=pricing)
            if pricing is None:
                del bill['pricing_status']
            result = self.normalize('fixture_tool', {'results': []}, bill)
            self.assertFalse(result['billing_final'])
            self.assertEqual(budget.settlement_billing(result), {})

    def test_ark_success_and_miss_preserve_company_list_and_billing(self):
        company = {'id': 'company-1', 'summary': {'name': 'Example'}}
        for rows, status in (([company], 'ok'), ([], 'no_results')):
            result = self.normalize('ai_ark_company_search', {'content': rows, 'numberOfElements': len(rows)})
            self.assertEqual(result['status'], status)
            self.assertEqual(len(result['results']), len(rows))
            self.assertNotIn('billing', result)  # Empty data never invents a bill.
        result = self.normalize('ai_ark_company_search', {'content': [], 'numberOfElements': 1})
        self.assertEqual(result['status'], 'schema_error')

    def test_serper_organic_results_are_evidence_without_pricing_by_row_count(self):
        data = {'data': {'organic': [{'title': 'Example', 'link': 'https://example.com', 'snippet': 'Source text'}]},
                'meta': {'status': 200}}
        bill = {'credits_charged': .02, 'cost_usd': .002, 'pricing_status': 'final', 'settlement_status': 'queued'}
        result = self.normalize('serper_google_search', data, bill)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['results'][0]['evidence_url'], 'https://example.com')
        self.assertEqual(result['billing'], bill)

    def test_limadata_organic_rows_preserve_flat_call_price(self):
        rows = [{'title': 'Example', 'url': 'https://example.com/' + str(i),
                 'snippet': 'Source text', 'position': i + 1} for i in range(8)]
        bill = {'credits_charged': .03, 'cost_usd': .003, 'pricing_status': 'final', 'settlement_status': 'queued'}
        result = self.normalize('limadata_search_web', {'organic': rows}, bill)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['results']), 8)
        self.assertEqual(result['results'][0]['evidence_url'], rows[0]['url'])
        self.assertEqual(result['results'][0]['evidence_text'], 'Source text')
        self.assertEqual(result['billing'], bill)  # Never multiply the call price by rows.
        self.assertEqual(budget.settlement_billing(result), bill)

    def test_limadata_empty_or_malformed_rows_do_not_invent_free_billing(self):
        empty = self.normalize('limadata_search_web', {'organic': []})
        self.assertEqual(empty['status'], 'no_results')
        self.assertNotIn('billing', empty)
        for data in ({'organic': ['bad']}, {'organic': [{'url': 'https://example.com'}]},
                     {'organic': [], 'error': 'upstream failure'}):
            result = self.normalize('limadata_search_web', data)
            self.assertNotEqual(result['status'], 'ok')
            self.assertNotIn('billing', result)

    def test_limadata_invalid_sources_keep_the_final_bill_without_becoming_evidence(self):
        bill = {'credits_charged': .03, 'cost_usd': .003, 'pricing_status': 'final'}
        rows = [{'url': url, 'title': 'Example'} for url in
                ('', '  ', 'javascript:alert(1)', 'https:///missing-host', 'https://[invalid')]
        rows += [{'url': 'https://example.com', 'title': title} for title in ('', '  ', None)]
        for row in rows:
            with self.subTest(row=row):
                result = self.normalize('limadata_search_web', {'organic': [row]}, bill)
                self.assertEqual(result['status'], 'schema_error')
                self.assertEqual(result['results'], [])
                self.assertEqual(budget.settlement_billing(result), bill)

    def test_parallel_dispatch_leases_allow_live_work_but_not_orphan_replay(self):
        import concurrent.futures
        import threading
        gate = threading.Barrier(3)
        def call(index):
            def execute():
                self.assertIsNone(budget.spending_stop(budget.load_ledger(self.path)))
                gate.wait(timeout=10)
                return {'status': 'ok', 'billing': {'credits_charged': .1}}, 0
            return budget.guarded_call({'spend': {'run_file': str(self.path), 'route_id': str(index)}},
                                       'deepline', execute)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(call, range(3)))
        self.assertEqual([code for _, code in results], [0, 0, 0])
        state = budget.load_ledger(self.path)
        self.assertEqual(budget.actual_cost_summary(state)['provider_usd'], .03)
        self.assertEqual(budget.actual_cost_summary(state)['pending_provider_calls'], 0)

    def test_exact_wire_bill_survives_receipt_ledger_and_numeric_report(self):
        from provider_output import load_json
        import run_attempt
        exact = '0.12345678901234567'
        raw = load_json('{"status":"completed","job_id":"exact", "billing": {"credits_charged":' + exact + ',"cost_usd":' + exact + ',"pricing_status":"final"}}')
        normalized, _ = deepline.normalize_response({'operation': 'execute', 'tool': 'fixture', 'payload': {}, 'limit': 1}, {'body': raw, 'exit_code': 0})
        self.assertEqual(normalized['billing']['credits_charged'], exact)
        body, _ = budget.guarded_call({'spend': {'run_file': str(self.path), 'route_id': 'exact'}}, 'deepline', lambda: (normalized, 0))
        folder = self.path.parent / 'receipts'
        folder.mkdir()
        (folder / 'exact.json').write_text(json.dumps(body))
        state = budget.load_ledger(self.path)
        self.assertEqual(state['calls']['exact']['actual_credits'], exact)
        self.assertEqual(state['calls']['exact']['actual_usd'], exact)
        doc = budget.read_object(self.path)
        number = budget.report_amount(exact)
        doc['routes'] = [{'route_id':'exact', 'provider':'deepline', 'paid_calls':1,
            'accepted_leads_before_call':0, 'cost_basis':'actual', 'cost_credits':number,
            'cost_upper_bound_credits':number, 'cost_usd':number}]
        self.assertEqual(budget.audit_ledger(self.path, doc), [])
        state['usd_limit'] = exact
        self.assertEqual(budget.spending_stop(state), 'budget_exhausted')

    def test_snapshot_settled_before_lease_check_does_not_report_crash(self):
        snapshots = []
        def execute():
            snapshots.append(budget.load_ledger(self.path))
            return {'status': 'ok', 'billing': {'cost_usd': 2.5}}, 0
        budget.guarded_call({'spend': {'run_file': str(self.path), 'route_id': 'racing'}}, 'deepline', execute)
        self.assertEqual(budget.actual_cost_summary(snapshots[0])['providers']['deepline']['pending_calls'], [])
        # A fresh reservation uses the current ledger and cannot exploit the old snapshot.
        with self.assertRaisesRegex(budget.BudgetError, 'budget_exhausted'):
            budget.reserve({'run_file': str(self.path), 'route_id': 'new'}, 'deepline')

    def test_dispatch_probe_accepts_permission_style_lock_contention(self):
        import os
        def execute():
            state = budget.load_ledger(self.path)
            # Windows msvcrt reports normal contention as PermissionError.
            lock = 'fcntl.flock' if os.name == 'posix' else 'msvcrt.locking'
            with patch(lock, side_effect=PermissionError(13, 'lock is held')):
                self.assertTrue(budget._dispatch_active(state, 'active', state['calls']['active']))
            return {'status': 'ok', 'billing': {'credits_charged': .1}}, 0
        _, code = budget.guarded_call({'spend': {'run_file': str(self.path), 'route_id': 'active'}},
                                      'deepline', execute)
        self.assertEqual(code, 0)

    def test_crashed_dispatch_preserves_unknown_bill_and_prevents_more_spending(self):
        import os
        import subprocess
        import sys
        from pathlib import Path
        script = """import os,sys
import budget_guard as b
b.guarded_call({'spend':{'run_file':sys.argv[1],'route_id':'crashed'}}, 'deepline', lambda: os._exit(7))
"""
        result = subprocess.run([sys.executable, '-c', script, str(self.path)], timeout=20,
            env={**os.environ, 'PYTHONPATH': str(Path(budget.__file__).parent)})
        self.assertEqual(result.returncode, 7)
        state = budget.load_ledger(self.path)
        self.assertEqual(budget.spending_stop(state), 'billing_pending')
        self.assertEqual(state['calls']['crashed']['state'], 'in_flight')  # Keep original evidence.
        with self.assertRaisesRegex(budget.BudgetError, 'billing_pending'):
            budget.reserve({'run_file': str(self.path), 'route_id': 'new'}, 'deepline')
        with self.assertRaisesRegex(budget.BudgetError, 'already'):
            budget.reserve({'run_file': str(self.path), 'route_id': 'crashed'}, 'deepline')

    def test_transaction_os_lock_recovers_after_crash_without_expiring_legacy_locks(self):
        import os
        import subprocess
        import sys
        from pathlib import Path
        path = budget.ledger_path(self.path)
        before = path.read_bytes()
        script = """import os,sys
from pathlib import Path
import budget_guard as b
with b.transaction(Path(sys.argv[1])) as state:
 state['blocked']='unsaved'
 os._exit(7)
"""
        result = subprocess.run([sys.executable, '-c', script, str(path)], timeout=20,
            env={**os.environ, 'PYTHONPATH': str(Path(budget.__file__).parent)})
        self.assertEqual(result.returncode, 7)
        self.assertEqual(path.read_bytes(), before)
        with budget.transaction(path) as state:
            self.assertIsNone(state['blocked'])
        legacy = path.with_name(path.name + '.lock')
        legacy.write_text('legacy owner unknown')
        with self.assertRaises(FileExistsError):
            with budget.transaction(path):
                self.fail('A legacy lock must still block')


class LegacyUsdTests(unittest.TestCase):
    def test_usd_only_overrun_reconciles_without_inventing_credits_or_resetting_caps(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'results.json'
            doc = {'request': {'target_count': 10, 'contact_fields': []}, 'accepted': [], 'routes': [],
                   'budget': {'paid_calls': 0, 'limits': {'deepline_credits': 50, 'scrapingdog_credits': 0}}}
            path.write_text(json.dumps(doc))
            budget.initialize(path, max_usd=5)
            body, code = budget.guarded_call({'spend': {'run_file': str(path), 'route_id': 'one',
                'max_cost_credits': 10}}, 'deepline', lambda: ({'provider': 'deepline', 'status': 'ok',
                'billing_final': True, 'billing': {'cost_usd': 2, 'pricing_status': 'final'}}, 0))
            self.assertEqual(code, 2)
            body.update(run_fingerprint=budget.run_fingerprint(path),
                        attempt={'action': {'id': 'one', 'cost_upper_bound_credits': 10}})
            folder = path.parent / 'receipts'
            folder.mkdir()
            receipt = folder / 'one.json'
            receipt.write_text(json.dumps(body))
            doc['routes'] = [{'route_id': 'one', 'provider': 'deepline', 'paid_calls': 1,
                'accepted_leads_before_call': 0, 'cost_basis': 'estimated', 'cost_credits': None,
                'cost_upper_bound_credits': 10, 'cost_usd': 2}]
            path.write_text(json.dumps(doc))
            original = receipt.read_bytes()
            result = budget.reconcile_overruns(path, [receipt], pricing_note='Verified exact USD receipt')
            self.assertIsNone(result['blocked'])
            state = budget.load_ledger(path)
            self.assertEqual(state['usd_limit'], '5')
            self.assertEqual(state['calls']['one']['actual_usd'], '2')
            self.assertIsNone(state['calls']['one']['actual_credits'])
            self.assertEqual(receipt.read_bytes(), original)
            self.assertEqual(budget.audit_ledger(path, doc), [])


class CatalogRedactionTests(unittest.TestCase):
    def test_schema_keeps_credential_field_contracts_and_redacts_actual_values(self):
        from jsonschema.validators import Draft202012Validator
        schema = {'type': 'object', 'properties': {
            'cookies': {'type': 'object', 'additionalProperties': {'type': 'string'}},
            'access_token': {'type': 'string', 'default': 'fixture-secret', 'examples': ['fixture-secret']},
            'nested': {'type': 'object', 'properties': {'password': {'type': 'string'}}}}}
        raw = {'inputSchema': {'jsonSchema': schema}, 'payload': {'access_token': 'fixture-secret',
               'cookies': {'session': 'fixture-secret'}, 'nested': {'password': 'fixture-secret'}}}
        result = deepline.redact(raw)
        Draft202012Validator.check_schema(result['inputSchema']['jsonSchema'])
        self.assertEqual(result['inputSchema']['jsonSchema']['properties']['cookies'], schema['properties']['cookies'])
        self.assertEqual(result['inputSchema']['jsonSchema']['properties']['access_token'], {'type': 'string'})
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertEqual(result['payload']['cookies'], '[REDACTED]')
        self.assertEqual(deepline.redact(result), result)

    def test_firecrawl_pending_and_completed_free_getter_keep_data_and_billing_separate(self):
        request = {'operation': 'execute', 'tool': 'firecrawl_batch_scrape', 'payload': {}, 'limit': 10}
        raw = {'status': 'completed', 'job_id': 'submission', 'toolResponse': {'rawV2': {'data': {
            'success': True, 'status': 'scraping', 'completed': 0, 'total': 1, 'data': []}}, 'view': 'data'}}
        body, _ = deepline.normalize_response(request, {'body': raw, 'exit_code': 0})
        self.assertEqual((body['status'], body['results']), ('partial', []))
        self.assertNotIn('billing', body)
        page = {'markdown': 'Captured source', 'metadata': {'sourceURL': 'https://example.com', 'statusCode': 200}}
        raw.update(status='no_result', job_id='free-read', toolResponse={'rawV2': {
            'success': True, 'status': 'completed', 'completed': 1, 'total': 1, 'data': [page]}, 'view': 'data'})
        body, _ = deepline.normalize_response(dict(request, tool='firecrawl_get_batch_scrape_status'),
                                             {'body': raw, 'exit_code': 0})
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['results'][0]['evidence_text'], 'Captured source')
        self.assertNotIn('billing', body)

    def test_free_status_data_is_not_confused_with_billing_no_result(self):
        receipt = {'tool': 'fixture_status', 'job_id': 'read-1', 'status': 'ok', 'results': [{'markdown': 'Saved page'}]}
        contract = {'toolId': 'fixture_status', 'provider': 'fixture',
                    'pricing': {'unit': 'call', 'creditsPerUnit': 0, 'usdPerUnit': 0}}
        row = {'id': 'usage-1', 'request_id': 'read-1', 'provider': 'fixture', 'operation': 'fixture_status',
               'status': 'no_result', 'charge_state': 'free', 'outcome': 'miss', 'credits': 0, 'delta': 0}
        proof = billing.matching_charge(receipt, [row], contract)
        self.assertIsNotNone(proof)
        self.assertIsNone(billing.billing_issue(receipt, proof, contract))
        self.assertIsNone(billing.matching_charge(receipt, [row]))
        self.assertIsNone(billing.matching_charge(receipt, [row], dict(contract, pricing={'unit': 'result', 'creditsPerUnit': 1})))

    def test_catalog_any_shorthand_preserves_constraints_and_literal_values(self):
        import research_input
        schema = {'type': 'object', 'required': ['query'], 'properties': {
            'query': {'type': 'string', 'minLength': 1},
            'outputSchema': {'type': 'any'},
            'literal': {'enum': [{'type': 'any'}]}}}
        original = copy.deepcopy(schema)
        research_input._check_native_schema(schema, {'query': 'Example', 'outputSchema': {'type': 'object'}, 'literal': {'type': 'any'}})
        self.assertEqual(schema, original)
        with self.assertRaisesRegex(ValueError, 'query'):
            research_input._check_native_schema(schema, {'query': ''})
        with self.assertRaisesRegex(ValueError, 'malformed'):
            research_input._check_native_schema({'type': 'typo'}, {})

    def test_exact_money_parsing_does_not_change_arbitrary_company_fields(self):
        from provider_output import load_json
        text = '{"credits":0.12345678901234567,"result":{"delta":0.12345678901234567},"billing":{"credits_charged":0.12345678901234567}}'
        body = load_json(text)
        self.assertIsInstance(body['credits'], float)
        self.assertIsInstance(body['result']['delta'], float)
        self.assertEqual(body['billing']['credits_charged'], '0.12345678901234567')
        self.assertEqual(load_json(text, billing_feed=True)['credits'], '0.12345678901234567')

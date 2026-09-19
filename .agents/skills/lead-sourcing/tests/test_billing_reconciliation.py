"""Read-only billing settlement never guesses costs or redispatches research."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import billing_reconciliation as billing
import budget_guard as budget
import deepline


class BillingReconciliationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'results.json'
        doc = {'request': {'target_count': 10, 'contact_fields': []}, 'accepted': [], 'routes': [],
               'budget': {'paid_calls': 0, 'limits': {'deepline_credits': 50, 'scrapingdog_credits': 0}}}
        self.path.write_text(json.dumps(doc))
        budget.initialize(self.path)
        budget.reserve({'run_file': str(self.path), 'route_id': 'call-1', 'max_cost_credits': 1}, 'deepline')
        self.receipt = {'run_fingerprint': budget.run_fingerprint(self.path), 'request_fingerprint': 'fingerprint',
                        'tool': 'fixture_email_finder', 'provider': 'deepline', 'job_id': 'request-1', 'status': 'no_results',
                        'attempt': {'action': {'id': 'call-1', 'cost_upper_bound_credits': 1}},
                        'spend_receipt': {'route_id': 'call-1', 'ledger': str(budget.ledger_path(self.path)), 'state': 'reserved'}}
        (self.path.parent / 'receipts').mkdir()
        self.receipt_path = self.path.parent / 'receipts/call-1.json'
        self.receipt_path.write_text(json.dumps(self.receipt))
        doc = budget.read_object(self.path)
        doc['routes'] = [{'route_id': 'call-1', 'provider': 'deepline', 'tool': 'fixture_email_finder', 'paid_calls': 1,
                          'request_fingerprint': 'fingerprint', 'accepted_leads_before_call': 0,
                          'cost_credits': None, 'cost_upper_bound_credits': 1, 'cost_basis': 'estimated'}]
        self.path.write_text(json.dumps(doc))
        self.row = {'id': 'ledger-1', 'request_id': 'request-1', 'operation': 'fixture_email_finder',
                    'provider': 'fixture', 'status': 'completed', 'charge_state': 'posted', 'credits': .5, 'delta': -.5}

    def test_unique_posted_charge_settles_once_preserving_original_receipt_and_caps(self):
        before = self.receipt_path.read_bytes()
        ledger = budget.load_ledger(self.path)
        result = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(result['matched'], ['call-1'])
        after = budget.load_ledger(self.path)
        self.assertEqual(after['calls']['call-1']['actual_credits'], '0.5')
        self.assertEqual(after['calls']['call-1']['maximum_credits'], '1')
        self.assertEqual({k: v for k, v in ledger.items() if k != 'calls'}, {k: v for k, v in after.items() if k != 'calls'})
        self.assertEqual(before, self.receipt_path.read_bytes())
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])
        billing.reconcile(self.path, fetch=lambda: self.fail('Do not repeat an unchanged billing read'))

    def test_ambiguous_unmatched_pending_and_missing_id_remain_reserved(self):
        for rows in ([self.row, self.row], [dict(self.row, request_id='other')], [dict(self.row, operation='other')],
                     [dict(self.row, provider='unrelated')],
                     [dict(self.row, metadata={'chargeGroupIds': ['one', 'two']})],
                     [dict(self.row, charge_state='held')], [dict(self.row, delta=1)], [dict(self.row, credits=True)]):
            with self.subTest(rows=rows):
                self.assertIsNone(billing.matching_charge(self.receipt, rows))
        self.assertIsNone(billing.matching_charge({'tool': 'fixture_email_finder'}, [self.row]))
        result = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [dict(self.row, request_id='other')]}})
        self.assertEqual(result['unmatched'], ['call-1'])
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])
        billing.reconcile(self.path, fetch=lambda: self.fail('Unchanged unresolved calls do not trigger repeated reads'))
        # A later invocation gets one fresh bounded read for late posting.
        result = billing.reconcile(self.path, refresh=True, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(result['matched'], ['call-1'])

    def test_overrun_is_recorded_and_blocks_instead_of_increasing_caps(self):
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [dict(self.row, credits=2, delta=-2)]}})
        ledger = budget.load_ledger(self.path)
        self.assertEqual(ledger['blocked'], budget.PRICE_OVERRUN)
        self.assertEqual(ledger['calls']['call-1']['actual_credits'], '2')
        self.assertEqual(ledger['calls']['call-1']['maximum_credits'], '1')
        self.assertTrue(budget.audit_ledger(self.path, budget.read_object(self.path)))
        result = budget.reconcile_overruns(self.path, [self.receipt_path], pricing_note='Fixture pricing policy corrected; original cap unchanged')
        self.assertIsNone(result['blocked'])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_failed_attempt_settles_only_from_matching_zero_charge_billing(self):
        # Observed Harvest timeout: raw error has an ID; billing reports the
        # corresponding failed operation attempt with explicit zero credits/delta.
        self.receipt.update(status='provider_error', results=[])
        self.receipt_path.write_text(json.dumps(self.receipt))
        before = self.receipt_path.read_bytes()
        failed = dict(self.row, status='error', charge_state='failed',
                      reason='operation_attempt', credits=0, delta=0, outcome='miss')
        result = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [failed]}})
        self.assertEqual(result['unmatched'], [])
        call = budget.load_ledger(self.path)['calls']['call-1']
        self.assertEqual(call['actual_credits'], '0')
        self.assertEqual(call['billing_evidence']['reason'], 'operation_attempt')
        self.assertEqual(before, self.receipt_path.read_bytes())
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_failed_billing_cannot_hide_unknown_charges_or_successful_results(self):
        receipt = dict(self.receipt, status='provider_error', results=[])
        failed = dict(self.row, status='error', charge_state='failed',
                      reason='operation_attempt', credits=0, delta=0, outcome='miss')
        for change in ({'status': 'pending'}, {'charge_state': 'temporary_hold'},
                       {'reason': None}, {'credits': None}, {'delta': None},
                       {'credits': .5, 'delta': -.5}, {'request_id': 'other'}):
            with self.subTest(change=change):
                self.assertIsNone(billing.matching_charge(receipt, [dict(failed, **change)]))
        for change in ({'status': 'ok'}, {'status': 'no_results'},
                       {'results': [{'company': 'A returned result'}]}):
            with self.subTest(receipt=change):
                self.assertIsNone(billing.matching_charge(dict(receipt, **change), [failed]))
        self.assertIsNone(billing.matching_charge(receipt, [failed, failed]))

    def test_explicit_no_result_billing_settles_zero_without_replaying(self):
        self.receipt.update(results=[])
        self.receipt_path.write_text(json.dumps(self.receipt))
        before = self.receipt_path.read_bytes()
        miss = dict(self.row, status='no_result', charge_state='free', credits=0,
                    delta=0, outcome='miss', provider_units=0, reason='operation_attempt')
        result = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [miss]}})
        self.assertEqual(result['unmatched'], [])
        self.assertEqual(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'], '0')
        self.assertEqual(before, self.receipt_path.read_bytes())
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_no_result_billing_requires_an_empty_response_and_final_free_charge(self):
        receipt = dict(self.receipt, results=[])
        miss = dict(self.row, status='no_result', charge_state='free', credits=0,
                    delta=0, outcome='miss', provider_units=0, reason='operation_attempt')
        for change in ({'request_id': 'other'}, {'provider': 'other'}, {'operation': 'other'},
                       {'charge_state': 'held'}, {'charge_state': 'posted'}, {'credits': None},
                       {'delta': None}, {'credits': .5, 'delta': -.5}, {'outcome': 'hit'},
                       {'metadata': {'chargeGroupIds': ['one', 'two']}}):
            with self.subTest(change=change):
                self.assertIsNone(billing.matching_charge(receipt, [dict(miss, **change)]))
        for change in ({'status': 'ok'}, {'status': 'provider_error'},
                       {'results': [{'name': 'A returned contact'}]}):
            with self.subTest(receipt=change):
                self.assertIsNone(billing.matching_charge(dict(receipt, **change), [miss]))
        self.assertIsNone(billing.matching_charge(receipt, [miss, miss]))

    def test_one_billing_entry_cannot_settle_two_different_routes(self):
        budget.reserve({'run_file': str(self.path), 'route_id': 'call-2', 'max_cost_credits': 1}, 'deepline')
        second = copy.deepcopy(self.receipt)
        second['request_fingerprint'] = 'another-fingerprint'
        (self.path.parent / 'receipts/call-2.json').write_text(json.dumps(second))
        doc = budget.read_object(self.path)
        doc['routes'].append(dict(doc['routes'][0], route_id='call-2', request_fingerprint='another-fingerprint'))
        self.path.write_text(json.dumps(doc))
        result = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(result['matched'], [])
        self.assertEqual(len(result['unmatched']), 2)

    def test_outage_retains_reservation_without_blocking_research(self):
        with patch.object(deepline, '_invoke', side_effect=deepline.CallTimeout('billing timeout', '', '')):
            result = billing.reconcile(self.path)
        self.assertIn('error', result)
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])

    def test_changed_billing_proof_fails_audit(self):
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        with budget.transaction(budget.ledger_path(self.path)) as ledger:
            ledger['calls']['call-1']['billing_evidence']['request_id'] = 'unrelated'
        self.assertTrue(any('posted billing evidence' in e for e in budget.audit_ledger(self.path, budget.read_object(self.path))))

    def prospector(self, count):
        """Minimal, anonymized shape from the 2026-09-16 live billing probe."""
        self.receipt.update(tool='prospector', status='ok', results=[{'persons': [
            {'id': f'contact-{i}', 'email_verified': True} for i in range(count)]}])
        self.receipt_path.write_text(json.dumps(self.receipt))
        catalog = {'toolId': 'prospector', 'provider': 'deepline_native', 'operation': 'prospector',
                   'operationId': 'deepline_native_prospector',
                   'operationAliases': ['prospector', 'deepline_native_prospector']}
        descriptor = {'provider': 'deepline', 'operation': 'describe', 'status': 'ok', 'tool': 'prospector',
                      'run_fingerprint': budget.run_fingerprint(self.path), 'request_fingerprint': 'catalog',
                      'results': [catalog]}
        (self.path.parent / 'receipts/catalog.json').write_text(json.dumps(descriptor))
        doc = budget.read_object(self.path)
        doc['routes'][0]['tool'] = 'prospector'
        doc['routes'].insert(0, {'route_id': 'catalog', 'provider': 'deepline', 'operation': 'describe',
                               'tool': 'prospector', 'provider_status': 'ok', 'request_fingerprint': 'catalog',
                               'paid_calls': 0, 'cost_credits': 0, 'cost_upper_bound_credits': 0, 'cost_basis': 'actual'})
        self.path.write_text(json.dumps(doc))
        self.row.update(provider='deepline_native', operation='prospector', charge_state='free',
                        credits=0, delta=0, outcome='miss', provider_units=0, metadata=None, pricing_basis='result')
        return catalog

    def test_catalog_alias_matches_both_spellings_without_provider_prefix_guess(self):
        catalog = self.prospector(0)
        for operation in ('prospector', 'deepline_native_prospector'):
            with self.subTest(operation=operation):
                row = dict(self.row, operation=operation)
                self.assertIsNotNone(billing.matching_charge(self.receipt, [row], catalog))
                self.assertIsNone(billing.matching_charge(self.receipt, [dict(row, provider='wrong')], catalog))
                self.assertIsNone(billing.matching_charge(self.receipt, [dict(row, request_id='other')], catalog))
        self.assertIsNone(billing.matching_charge(self.receipt, [self.row]))
        self.assertIsNone(billing.matching_charge(self.receipt, [self.row], dict(catalog, toolId='other',
                         operation='other', operationId='other', operationAliases=[])))

    def test_explicit_free_empty_result_settles_zero_with_null_metadata(self):
        self.prospector(0)
        result = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(result['unmatched'], [])
        ledger = budget.load_ledger(self.path)
        self.assertEqual(ledger['calls']['call-1']['actual_credits'], '0')
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])
        costs = budget.accounting_summary(ledger)['providers']['deepline']
        self.assertEqual(costs, dict(billed_usd=0, unresolved_reserved_usd=0, maximum_usd=0, unresolved_calls=0))
        budget.check_allowance(ledger, 'deepline', '49.5', 0)

    def test_zero_billing_with_returned_contacts_is_saved_without_releasing_reservation(self):
        for count in (1, 2):
            self.prospector(count)
            billing.reconcile(self.path, refresh=True, fetch=lambda: {'recent': {'entries': [self.row]}})
            ledger = budget.load_ledger(self.path)
            call = ledger['calls']['call-1']
            self.assertIsNone(call['actual_credits'])
            self.assertEqual(call['billing_evidence']['credits'], 0)
            self.assertIn('Results returned', call['billing_issue'])
            self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])
            summary = budget.accounting_summary(ledger)
            self.assertEqual(summary['providers']['deepline'], dict(billed_usd=0, unresolved_reserved_usd=.1,
                             maximum_usd=.1, unresolved_calls=1))
            self.assertEqual(len(summary['billing_issues']), 1)
            with self.assertRaises(budget.BudgetError):
                budget.check_allowance(ledger, 'deepline', '49.5', 0)

    def test_null_email_with_echoed_domain_settles_only_with_matching_free_billing(self):
        # Anonymized Hunter response from the Arizona run: metadata is not a hit.
        self.receipt.update(status='ok', results=[{'domain': 'example.org', 'email': None,
                            'first_name': None, 'last_name': None, 'sources': [],
                            'verification': {'date': None, 'status': None}}])
        self.receipt_path.write_text(json.dumps(self.receipt))
        before = self.receipt_path.read_bytes()
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': []}})
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])
        free = dict(self.row, credits=0, delta=0, charge_state='free', outcome='miss',
                    pricing_basis='result', provider_units=0)
        billing.reconcile(self.path, refresh=True, fetch=lambda: {'recent': {'entries': [free]}})
        call = budget.load_ledger(self.path)['calls']['call-1']
        self.assertEqual(call['actual_credits'], '0')
        self.assertIsNone(call['billing_issue'])
        self.assertEqual(before, self.receipt_path.read_bytes())
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_found_or_unrecognized_email_rows_keep_contradiction_reserve(self):
        proof = dict(self.row, credits=0, delta=0, outcome='miss', pricing_basis='result', provider_units=0)
        for row in ({'domain': 'example.org'}, {'email': 'person@example.org'},
                    {'email': None, 'contact_email': 'person@example.org'},
                    {'email': None, 'emails': ['person@example.org']},
                    {'email': None, 'work_email': 'person@example.org'},
                    {'email': None, 'workEmail': 'person@example.org'}):
            with self.subTest(row=row):
                self.assertIsNotNone(billing.billing_issue(dict(self.receipt, results=[row]), proof))
        self.assertIsNotNone(billing.billing_issue(
            dict(self.receipt, tool='fixture_company_search', results=[{'email': None, 'company': 'Example'}]), proof))

    def test_later_posted_correction_settles_once_and_preserves_billing_history(self):
        self.prospector(1)
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        posted = dict(self.row, id='ledger-corrected', credits=.5, delta=-.5, charge_state='posted', outcome='hit', provider_units=1)
        billing.reconcile(self.path, refresh=True, fetch=lambda: {'recent': {'entries': [posted]}})
        call = budget.load_ledger(self.path)['calls']['call-1']
        self.assertEqual(call['actual_credits'], '0.5')
        self.assertIsNone(call['billing_issue'])
        self.assertEqual(call['billing_history'][0]['credits'], 0)
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])
        billing.reconcile(self.path, refresh=True, fetch=lambda: self.fail('Already settled'))

    def test_missing_or_invalid_free_charge_never_becomes_zero(self):
        catalog = self.prospector(0)
        for change in ({'credits': None}, {'credits': .1, 'delta': -.1}, {'charge_state': 'pending'},
                       {'metadata': 'invalid'}, {'metadata': {'chargeGroupIds': ['other']}},
                       {'metadata': {'chargeGroupIds': ['request-1', 'request-2']}}):
            with self.subTest(change=change):
                self.assertIsNone(billing.matching_charge(self.receipt, [dict(self.row, **change)], catalog))
        self.assertIsNotNone(billing.matching_charge(self.receipt,
            [dict(self.row, metadata={'chargeGroupIds': ['request-1']})], catalog))

    def test_free_success_is_not_assumed_to_be_a_billing_error(self):
        self.prospector(1)
        row = dict(self.row, outcome='free_operation', provider_units=None, pricing_basis='attempt')
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [row]}})
        self.assertEqual(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'], '0')

    def test_catalog_free_call_settles_only_after_matching_billing_despite_miss_label(self):
        self.prospector(1)
        catalog_path = self.path.parent / 'receipts/catalog.json'
        descriptor = json.loads(catalog_path.read_text())
        descriptor['results'][0]['pricing'] = {'unit': 'call', 'creditsPerUnit': 0}
        catalog_path.write_text(json.dumps(descriptor))
        self.row.update(pricing_basis='attempt', pricing_model='fixed', provider_units=None)
        before = self.receipt_path.read_bytes()
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': []}})
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])
        billing.reconcile(self.path, refresh=True, fetch=lambda: {'recent': {'entries': [self.row]}})
        call = budget.load_ledger(self.path)['calls']['call-1']
        self.assertEqual(call['actual_credits'], '0')
        self.assertIsNone(call['billing_issue'])
        self.assertEqual(call['maximum_credits'], '1')
        self.assertEqual(before, self.receipt_path.read_bytes())
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])
        # The same catalog basis must be rechecked by the saved-evidence audit.
        descriptor['results'][0]['pricing']['creditsPerUnit'] = 1
        catalog_path.write_text(json.dumps(descriptor))
        self.assertTrue(any('posted billing evidence' in e for e in budget.audit_ledger(self.path, budget.read_object(self.path))))

    def test_unverified_or_paid_catalog_price_keeps_result_contradiction(self):
        self.prospector(1)
        for pricing in ({}, {'unit': 'call', 'creditsPerUnit': None},
                        {'unit': 'call', 'creditsPerUnit': False}, {'unit': 'call', 'creditsPerUnit': '0'},
                        {'unit': 'call', 'creditsPerUnit': .1}, {'unit': 'result', 'creditsPerUnit': 0}):
            with self.subTest(pricing=pricing):
                self.assertIsNotNone(billing.billing_issue(self.receipt, self.row, {'pricing': pricing}))

    def test_legacy_free_call_reservation_remains_auditable_until_reconciled(self):
        self.prospector(1)
        path = self.path.parent / 'receipts/catalog.json'
        descriptor = json.loads(path.read_text())
        descriptor['results'][0]['pricing'] = {'unit': 'call', 'creditsPerUnit': 0}
        path.write_text(json.dumps(descriptor))
        current = billing.billing_issue
        with patch.object(billing, 'billing_issue', side_effect=lambda receipt, proof, contract=None: current(receipt, proof)):
            billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        before = budget.load_ledger(self.path)
        self.assertIsNone(before['calls']['call-1']['actual_credits'])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])
        self.assertEqual(before, budget.load_ledger(self.path))
        billing.reconcile(self.path, refresh=True, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'], '0')

    def test_timeout_retries_only_the_billing_read_then_settles(self):
        with patch.object(deepline, '_invoke', side_effect=[deepline.CallTimeout('timeout', '', ''),
                (0, json.dumps({'recent': {'entries': [self.row]}}), '')]) as invoke:
            result = billing.reconcile(self.path)
        self.assertEqual(result['attempts'], 2)
        self.assertNotIn('error', result)
        self.assertEqual(result['matched'], ['call-1'])
        for call in invoke.call_args_list:
            self.assertEqual(call.args[0][1:3], ['billing', 'usage'])
            self.assertGreater(call.args[1], 0)
            self.assertLessEqual(call.args[1], 30)

    def test_billing_organization_binding_survives_a_new_call_set(self):
        billing.reconcile(self.path, fetch=lambda: {'org_id': 'original', 'recent': {'entries': []}})
        budget.reserve({'run_file': str(self.path), 'route_id': 'call-2', 'max_cost_credits': 1}, 'deepline')
        result = billing.reconcile(self.path, fetch=lambda: {
            'org_id': 'other', 'recent': {'entries': [self.row]}})
        self.assertIn('organization changed', result['error'])
        self.assertEqual(result['billing_org_id'], 'original')
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])

    def test_pagination_resumes_past_four_pages_without_replaying_calls(self):
        def page(command, timeout):
            cursor = command[command.index('--cursor') + 1] if '--cursor' in command else '0'
            index = int(cursor)
            return 0, json.dumps({'org_id': 'fixture-org', 'recent': {
                'entries': [self.row] if index == 4 else [],
                'next_cursor': str(index + 1) if index < 4 else None}}), ''
        with patch.object(deepline, '_invoke', side_effect=page) as invoke:
            first = billing.reconcile(self.path)
            self.assertEqual(first['next_cursor'], '4')
            self.assertEqual(invoke.call_count, 4)
            self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])
            second = billing.reconcile(self.path, refresh=True)
            self.assertEqual(second['matched'], ['call-1'])
            self.assertEqual(invoke.call_count, 5)
            self.assertIn('--cursor', invoke.call_args.args[0])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_matching_first_page_does_not_wait_for_an_unneeded_slow_page(self):
        first = (0, json.dumps({'recent': {'entries': [self.row], 'next_cursor': 'older'}}), '')
        with patch.object(deepline, '_invoke', side_effect=[first,
                deepline.CallTimeout('older page timed out', '', ''), first,
                deepline.CallTimeout('older page timed out', '', '')]) as invoke:
            result = billing.reconcile(self.path)
        self.assertEqual(result['matched'], ['call-1'])
        self.assertEqual(invoke.call_count, 1)
        self.assertNotIn('error', result)
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_malformed_page_cannot_settle_an_otherwise_matching_charge(self):
        page = (0, json.dumps({'recent': {'entries': [self.row, None]}}), '')
        with patch.object(deepline, '_invoke', return_value=page) as invoke:
            result = billing.reconcile(self.path)
        self.assertIn('error', result)
        self.assertEqual(invoke.call_count, 2)
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])

    def test_explicit_billing_resume_extends_reads_but_preserves_cap_and_calls(self):
        for _ in range(3):
            billing.reconcile(self.path, refresh=True, fetch=lambda: {'recent': {'entries': []}})
        before = budget.load_ledger(self.path)
        receipt = self.receipt_path.read_bytes()
        billing.reconcile(self.path, refresh=True, fetch=lambda: self.fail('Read limit must persist'))
        with budget.transaction(self.path.parent / 'billing-status.json') as status:
            status['next_cursor'] = 'older-page'
        with patch.object(deepline, '_invoke', return_value=(0, json.dumps({'recent': {'entries': [self.row]}}), '')) as invoke:
            result = billing.reconcile(self.path, resume=True)
        self.assertNotIn('--cursor', invoke.call_args.args[0])
        self.assertEqual(result['matched'], ['call-1'])
        self.assertEqual(result['attempts'], 4)
        self.assertEqual(result['attempt_limit'], 6)
        after = budget.load_ledger(self.path)
        self.assertEqual(set(before['calls']), set(after['calls']))
        self.assertEqual(before['usd_limit'], after['usd_limit'])
        self.assertEqual(receipt, self.receipt_path.read_bytes())

    def test_usd_only_actual_charge_is_not_selected_for_settlement_again(self):
        with budget.transaction(budget.ledger_path(self.path)) as ledger:
            ledger['version'] = 2
            ledger['calls']['call-1'].update(actual_usd='0.08', state='settled')
        billing.reconcile(self.path, fetch=lambda: self.fail('USD already settled'))
        self.assertEqual(budget.actual_cost_summary(budget.load_ledger(self.path))['provider_usd'], .08)

    def test_retry_budget_persists_and_refresh_does_not_reset_it(self):
        with patch.object(deepline, '_invoke', side_effect=deepline.CallTimeout('timeout', '', '')) as invoke:
            first = billing.reconcile(self.path)
            self.assertEqual(first['attempts'], 2)
            billing.reconcile(self.path)  # Cooldown applies even across a new caller.
            self.assertEqual(invoke.call_count, 2)
            final = billing.reconcile(self.path, refresh=True)
            self.assertEqual(final['attempts'], 3)
            billing.reconcile(self.path, refresh=True)
            self.assertEqual(invoke.call_count, 3)
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])

    def test_pending_billing_retries_after_cooldown_without_manual_refresh(self):
        with patch.object(billing.time, 'time', return_value=100):
            billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': []}})
        with patch.object(billing.time, 'time', return_value=159):
            billing.reconcile(self.path, fetch=lambda: self.fail('Cooldown'))
        with patch.object(billing.time, 'time', return_value=160):
            result = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(result['matched'], ['call-1'])

    def actual_cost_pending(self):
        document = budget.read_object(self.path)
        document['budget']['policy'] = 'actual_cost'
        self.path.write_text(json.dumps(document))
        with budget.transaction(budget.ledger_path(self.path)) as ledger:
            ledger['version'] = 2
            ledger['calls']['call-1'].update(state='pending_billing', total_before_usd='0')

    def test_wait_recovers_delayed_bill_without_replaying_or_resetting_limits(self):
        self.actual_cost_pending()
        before = self.receipt_path.read_bytes()
        ledger = budget.load_ledger(self.path)
        clock = [1000.0]
        def sleep(seconds):
            clock[0] += seconds
        responses = [(0, json.dumps({'recent': {'entries': rows}}), '') for rows in ([], [self.row])]
        with patch.object(billing.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(billing.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(billing.time, 'sleep', side_effect=sleep), \
             patch.object(deepline, '_invoke', side_effect=responses) as invoke:
            status = billing.wait_for_billing(self.path, deadline=1300)
        self.assertEqual(status['matched'], ['call-1'])
        self.assertEqual(clock[0], 1060)
        self.assertEqual(invoke.call_count, 2)
        self.assertTrue(all(call.args[0][1:3] == ['billing', 'usage'] for call in invoke.call_args_list))
        self.assertEqual(self.receipt_path.read_bytes(), before)
        after = budget.load_ledger(self.path)
        self.assertEqual(after['usd_limit'], ledger['usd_limit'])
        self.assertIsNone(budget.spending_stop(after))
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])

    def test_wait_respects_original_deadline_and_retains_unknown_cost(self):
        self.actual_cost_pending()
        clock = [1000.0]
        with patch.object(billing.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(billing.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(billing.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)) as sleep, \
             patch.object(deepline, '_invoke', return_value=(0, json.dumps({'recent': {'entries': []}}), '')) as invoke:
            billing.wait_for_billing(self.path, deadline=1030)
            self.assertEqual(invoke.call_count, 1)
            self.assertLessEqual(invoke.call_args.args[1], 30)
            sleep.assert_not_called()
            billing.wait_for_billing(self.path, deadline=999)
            self.assertEqual(invoke.call_count, 1)
        self.assertEqual(budget.spending_stop(budget.load_ledger(self.path)), 'billing_pending')

    def test_missing_id_is_actionable_and_does_not_poll_or_guess(self):
        self.actual_cost_pending()
        self.receipt.pop('job_id')
        self.receipt_path.write_text(json.dumps(self.receipt))
        with patch.object(deepline, '_invoke', side_effect=AssertionError('Cannot correlate a bill')), \
             patch.object(billing.time, 'sleep', side_effect=AssertionError('No useful wait')):
            status = billing.wait_for_billing(self.path)
        self.assertEqual(status['missing_request_ids'], ['call-1'])
        self.assertIn('Never replay', status['action_required'])
        self.assertEqual(json.loads((self.path.parent / 'billing-status.json').read_text()), status)
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])

    def test_slow_billing_read_cannot_retry_past_its_window(self):
        clock = [1000.0]
        def invoke(command, seconds):
            clock[0] += seconds
            raise deepline.CallTimeout('timeout', '', '')
        with patch.object(billing.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(deepline, '_invoke', side_effect=invoke) as read:
            status = billing.reconcile(self.path, timeout_seconds=7)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(status['attempts'], 1)
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])

    def test_legacy_failed_cache_does_not_permanently_suppress_retry(self):
        import hashlib
        signature = hashlib.sha256(json.dumps(['call-1']).encode()).hexdigest()
        (self.path.parent / 'billing-status.json').write_text(json.dumps(
            {'attempt_signature': signature, 'error': 'provider command timed out'}))
        result = billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(result['matched'], ['call-1'])

    def test_catalog_and_discrepancy_tampering_fail_existing_audit(self):
        self.prospector(1)
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        with budget.transaction(budget.ledger_path(self.path)) as ledger:
            ledger['calls']['call-1']['billing_issue'] = None
        self.assertTrue(budget.audit_ledger(self.path, budget.read_object(self.path)))

    def test_saved_report_shows_billed_and_reserved_without_changing_research(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'scripts'))
        from run_costs import save_report
        self.prospector(1)
        before = self.receipt_path.read_bytes()
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        (self.path.parent / 'research-commentary.md').write_text('Fixture research remains unchanged.')
        report_path = save_report(self.path.parent, self.path)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['provider_accounting']['providers']['deepline']['billed_usd'], 0)
        self.assertEqual(report['provider_accounting']['providers']['deepline']['unresolved_reserved_usd'], .1)
        self.assertEqual(len(report['provider_accounting']['billing_issues']), 1)
        text = (self.path.parent / 'report.md').read_text()
        self.assertIn('Provider charges (including documented endpoint tariffs): $0.0000', text)
        self.assertIn('Provider calls awaiting billing: 1', text)
        self.assertIn('Fixture research remains unchanged.', text)
        self.assertEqual(before, self.receipt_path.read_bytes())

    def test_finish_retries_billing_at_final_approval_after_initial_read_timeouts(self):
        from research_tools import ResearchTools
        tools = ResearchTools(self.path)
        with patch.object(ResearchTools, '_overview', return_value={'stop': 'continue'}), patch.object(
                deepline, '_invoke', side_effect=[deepline.CallTimeout('timeout', '', ''),
                    deepline.CallTimeout('timeout', '', ''), (0, json.dumps({'recent': {'entries': [self.row]}}), '')]) as invoke:
            first = tools.finish()
            self.assertEqual(first['status'], 'needs_research')
            self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])
            final = tools.finish(review_ref='final-approval-attempt')
            self.assertEqual(final['status'], 'needs_research')
            self.assertEqual(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'], '0.5')
            self.assertEqual(invoke.call_count, 3)

    def test_invalid_billing_payload_stays_reserved_and_read_retries_are_bounded(self):
        calls = []
        def fetch():
            calls.append(1)
            return []
        result = billing.reconcile(self.path, fetch=fetch)
        self.assertIn('error', result)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])

    def test_billed_plus_unresolved_reservations_are_not_double_counted(self):
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        budget.reserve({'run_file': str(self.path), 'route_id': 'call-2', 'max_cost_credits': 1}, 'deepline')
        summary = budget.accounting_summary(budget.load_ledger(self.path))
        self.assertEqual(summary['providers']['deepline'], dict(billed_usd=.05, unresolved_reserved_usd=.1,
                         maximum_usd=.15, unresolved_calls=1))
        self.assertEqual(summary['billing_issues'], [])

    def test_alias_proof_keeps_its_run_bound_catalog_reference(self):
        self.prospector(0)
        billing.reconcile(self.path, fetch=lambda: {'recent': {'entries': [self.row]}})
        catalog_path = self.path.parent / 'receipts/catalog.json'
        catalog = json.loads(catalog_path.read_text())
        catalog['run_fingerprint'] = 'another-run'
        catalog_path.write_text(json.dumps(catalog))
        self.assertTrue(budget.audit_ledger(self.path, budget.read_object(self.path)))

    def test_paid_bill_cannot_use_a_catalog_changed_after_dispatch(self):
        self.prospector(0)
        self.actual_cost_pending()
        path = self.path.parent / 'receipts/catalog.json'
        original = path.read_bytes()
        import hashlib
        with budget.transaction(budget.ledger_path(self.path)) as ledger:
            ledger['calls']['call-1'].update(catalog_route_id='catalog',
                catalog_sha256=hashlib.sha256(original).hexdigest())
        descriptor = json.loads(original)
        descriptor['results'][0]['operationAliases'].append('changed_operation')
        path.write_text(json.dumps(descriptor))
        status = billing.reconcile(self.path, fetch=lambda: {'recent': {
            'entries': [dict(self.row, operation='changed_operation')]}})
        self.assertIn('catalog changed since dispatch', status['error'])
        self.assertIsNone(budget.load_ledger(self.path)['calls']['call-1']['actual_credits'])
        path.write_bytes(original)
        result = billing.reconcile(self.path, refresh=True, fetch=lambda: {'recent': {'entries': [self.row]}})
        self.assertEqual(result['matched'], ['call-1'])
        self.assertEqual(budget.audit_ledger(self.path, budget.read_object(self.path)), [])
        path.write_text(json.dumps(descriptor))
        self.assertTrue(any('catalog changed since dispatch' in error
                            for error in budget.audit_ledger(self.path, budget.read_object(self.path))))

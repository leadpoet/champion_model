"""Finish existing verification jobs without reopening research or hiding costs."""
import copy
import json
import os
import subprocess
import unittest
from unittest.mock import patch

import budget_guard
import deepline
import email_receipts
import run_attempt
import test_email_receipts as email_tests
import test_finalization as final_tests
from email_fixtures import write_email_receipts


class FinalizationRecoveryTests(unittest.TestCase):
    def pending_run(self, price=0, getter_tool='bounceban_get_single_status'):
        helper = email_tests.EmailReceiptTests()
        self.addCleanup(helper.doCleanups)
        fixture, submission = helper.prepared_run()
        pending = {'status': 'verifying', 'id': 'saved-job'}
        with patch.object(deepline, '_invoke', return_value=(0, json.dumps(pending), '')):
            run_attempt.run_attempt(fixture.path, submission)
        description = fixture.spec('describe-getter')
        description['request'] = {'operation': 'describe', 'tool': getter_tool}
        contract = {'toolId': getter_tool, 'pricing': {'creditsPerUnit': price, 'unit': 'call'},
                    'inputSchema': {'fields': [{'name': 'id', 'type': 'string', 'required': True}]}}
        with patch.object(deepline, '_invoke', return_value=(0, json.dumps(contract), '')):
            run_attempt.run_attempt(fixture.path, description)
        getter = copy.deepcopy(submission)
        getter['action'].update(id='saved-status', status_read=True, cost_upper_bound_credits=0)
        getter['request'].update(tool=getter_tool, payload={'id': 'saved-job'})
        document = json.loads(fixture.path.read_text())
        document['request']['max_duration_seconds'] = 1
        fixture.path.write_text(json.dumps(document))
        return fixture, getter, pending

    def test_saved_free_getter_survives_deadline_and_finalization_without_resubmission(self):
        for finalization in ('0', '1'):
            with self.subTest(finalization=finalization):
                fixture, getter, pending = self.pending_run()
                original = json.loads(fixture.path.read_text())
                ledger = budget_guard.load_ledger(fixture.path)
                receipt = fixture.path.parent / 'receipts/bounceban-first.json'
                receipt_before = receipt.read_bytes()
                response = {'status': 'success', 'result': 'deliverable', 'email': 'buyer@target.example',
                            'billing': {'credits_charged': 0, 'cost_usd': 0, 'pricing_status': 'final', 'settlement_status': 'queued'}}
                with patch.dict(os.environ, {'TYCHE_FINALIZATION_ONLY': finalization}), \
                     patch.object(deepline, '_invoke', return_value=(0, json.dumps(response), '')) as provider:
                    result = run_attempt.run_attempt(fixture.path, getter)
                self.assertEqual(result['provider_status'], 'ok')
                self.assertEqual(provider.call_count, 1)
                self.assertEqual(provider.call_args.args[0][3], getter['request']['tool'])
                after = json.loads(fixture.path.read_text())
                self.assertEqual(after['request'], original['request'])
                self.assertEqual(after['stop_check']['started_at'], original['stop_check']['started_at'])
                self.assertEqual(receipt.read_bytes(), receipt_before)
                current = budget_guard.load_ledger(fixture.path)
                for rid, call in ledger['calls'].items():
                    self.assertEqual(current['calls'][rid], call)
                self.assertEqual(current['calls']['saved-status']['actual_credits'], '0')
                self.assertTrue(email_receipts.verification_finished(fixture.path, after, 'bounceban-first', pending))
                self.assertEqual(run_attempt.evaluate_stop(after, execution_budget=current)['decision'], 'time_limit_reached')

    def test_recovery_rejects_wrong_job_scope_email_provider_price_and_submission(self):
        for change in ('job', 'scope', 'email', 'provider', 'price', 'submission', 'cross_run', 'unknown_price'):
            with self.subTest(change=change):
                fixture, getter, _ = self.pending_run(price=.1 if change == 'price' else None if change == 'unknown_price' else 0)
                if change == 'job': getter['request']['payload']['id'] = 'other-job'
                if change == 'scope': getter['action']['scope'] = 'other.example'
                if change == 'email': getter['request']['payload']['email'] = 'other@target.example'
                if change == 'provider': getter['request']['tool'] = 'zerobounce_get_status'
                if change == 'submission': getter['request']['tool'] = 'bounceban_verify_single'
                if change == 'cross_run':
                    path = fixture.path.parent / 'receipts/bounceban-first.json'
                    saved = json.loads(path.read_text()); saved['run_fingerprint'] = 'another-run'
                    path.write_text(json.dumps(saved))
                before = fixture.path.read_bytes()
                ledger = budget_guard.ledger_path(fixture.path).read_bytes()
                with patch.dict(os.environ, {'TYCHE_FINALIZATION_ONLY': '1'}), \
                     patch.object(deepline, '_invoke') as provider, patch.object(budget_guard, 'reserve') as reserve:
                    with self.assertRaisesRegex(ValueError, 'Research is closed'):
                        run_attempt.run_attempt(fixture.path, getter)
                    provider.assert_not_called(); reserve.assert_not_called()
                self.assertEqual(fixture.path.read_bytes(), before)
                self.assertEqual(budget_guard.ledger_path(fixture.path).read_bytes(), ledger)

    def test_recovery_accepts_another_described_getter_without_pinning_a_tool_id(self):
        fixture, getter, _ = self.pending_run(getter_tool='bounceban_get_verification')
        with patch.dict(os.environ, {'TYCHE_FINALIZATION_ONLY': '1'}), patch.object(deepline, '_invoke',
                return_value=(0, json.dumps({'status': 'verifying', 'id': 'saved-job',
                                            'billing': {'credits_charged': 0, 'cost_usd': 0, 'pricing_status': 'final', 'settlement_status': 'queued'}}), '')):
            run_attempt.run_attempt(fixture.path, getter)
        self.assertEqual(json.loads(fixture.path.read_text())['routes'][-1]['provider_status'], 'partial')

    def test_repeated_pending_reads_form_a_complete_chain_automatically(self):
        fixture, getter, pending = self.pending_run()
        responses = [dict(pending), {'status': 'success', 'result': 'deliverable', 'email': 'buyer@target.example'}]
        with patch.dict(os.environ, {'TYCHE_FINALIZATION_ONLY': '1'}):
            for index, response in enumerate(responses):
                response['billing'] = {'credits_charged': 0, 'cost_usd': 0}
                getter['action']['id'] = 'status-' + str(index)
                with patch.object(deepline, '_invoke', return_value=(0, json.dumps(response), '')):
                    run_attempt.run_attempt(fixture.path, getter)
        document = json.loads(fixture.path.read_text())
        for rid in ('bounceban-first', 'status-0'):
            self.assertTrue(email_receipts.verification_finished(fixture.path, document, rid, pending))
        self.assertEqual(email_receipts.pending_verification_errors(document, fixture.path), [])

    def delivery_run(self, expired=False):
        helper = final_tests.FinalizationTests(); helper.setUp()
        self.addCleanup(helper.doCleanups)
        document = final_tests.completed_document()
        document['accepted'] = document['accepted'][:1]
        document['request'].update(target_count=1, contact_fields=['email'])
        for row in document['accepted']:
            row['primary_contact']['current_title'] = 'Chief Operating Officer'
        if expired:
            document['request'].update(target_count=2, max_duration_seconds=1)
        helper.save(document)
        budget_guard.initialize(helper.path, max_usd=1, scrapingdog_usd_per_credit=.1, verification_reserve_credits=.2)
        ledger_path, _ = budget_guard.reserve({'run_file': str(helper.path), 'route_id': 'selected-email',
                                              'max_cost_credits': .1}, 'deepline')
        budget_guard.settle(ledger_path, 'selected-email', {'credits_charged': .1, 'cost_usd': .01})
        budget_guard.reserve({'run_file': str(helper.path), 'route_id': 'pending-job',
                              'max_cost_credits': .2}, 'deepline')
        selected = {'email': 'selected@example.org', 'status': 'valid', 'source': {
            'route_id': 'selected-email', 'provider': 'deepline', 'operation': 'execute',
            'tool': 'zerobounce_validate', 'validator': 'zerobounce'}}
        document['accepted'][0]['primary_contact'].update(email=selected['email'], email_validation=selected)
        route = dict(document['routes'][0], route_id='selected-email', phase='email_validation',
                     provider='deepline', operation='execute', tool='zerobounce_validate',
                     provider_status='ok', paid_calls=1, cost_credits=.1, cost_upper_bound_credits=.1,
                     cost_basis='actual', accepted_leads_before_call=len(document['accepted']))
        pending_route = dict(route, route_id='pending-job', tool='bounceban_verify_single',
                             provider_status='partial', request_fingerprint='pending-request',
                             cost_credits=None, cost_upper_bound_credits=.2, cost_basis='estimated')
        document['routes'].extend([route, pending_route])
        document['stop_audit']['route_frontier'].extend([
            {'route_id': 'selected-email', 'state': 'exhausted', 'reason': 'Valid selected address reviewed',
             'exhaustion_basis': 'no_new_unique_candidates'},
            {'route_id': 'pending-job', 'state': 'continuable', 'reason': 'Unused address still verifying'}])
        run_attempt.refresh(document); helper.save(document)
        write_email_receipts(helper.path, document); helper.save(document)
        pending = {'status': 'verifying', 'id': 'unused-job'}
        saved = {**pending_route, 'receipt_status': 'complete', 'status': 'partial',
                 'run_fingerprint': budget_guard.run_fingerprint(helper.path), 'pending_verification': pending,
                 'attempt': {'request': {'operation': 'execute', 'tool': 'bounceban_verify_single',
                                         'payload': {'email': 'unused@example.org'}}},
                 'provider_response': {'exit_code': 0, 'body': pending, 'stderr': ''}}
        (helper.path.parent / 'receipts/pending-job.json').write_text(json.dumps(saved))
        return helper.path, document

    def test_unused_pending_address_allows_strict_delivery_with_unchanged_audit_and_costs(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                path, document = self.delivery_run(expired)
                ledger = budget_guard.ledger_path(path).read_bytes()
                receipts = {p.name: p.read_bytes() for p in (path.parent / 'receipts').iterdir()}
                checked = run_attempt.finalize_run(path)
                self.assertTrue(checked['delivery_allowed'])
                after = json.loads(path.read_text())
                self.assertEqual(after['stop_reason'], 'time_limit_reached' if expired else 'target_met')
                for field in ('accepted', 'routes', 'request', 'stop_check'):
                    self.assertEqual(after[field], document[field])
                self.assertEqual(after['stop_audit']['route_frontier'], document['stop_audit']['route_frontier'])
                self.assertEqual(budget_guard.ledger_path(path).read_bytes(), ledger)
                self.assertEqual({p.name: p.read_bytes() for p in (path.parent / 'receipts').iterdir()}, receipts)

    def test_pending_selected_or_backup_email_and_falsely_completed_job_still_block(self):
        for problem in ('selected', 'backup', 'closed', 'cross_run'):
            with self.subTest(problem=problem):
                path, document = self.delivery_run()
                if problem == 'selected': document['accepted'][0]['primary_contact']['email'] = 'unused@example.org'
                if problem == 'backup': document['accepted'][0]['backup_contacts'] = [{'email': 'unused@example.org'}]
                if problem == 'closed':
                    document['stop_audit']['route_frontier'][-1].update(state='exhausted', exhaustion_basis='no_new_unique_candidates')
                if problem == 'cross_run':
                    p = path.parent / 'receipts/pending-job.json'; saved = json.loads(p.read_text())
                    saved['run_fingerprint'] = 'another-run'; p.write_text(json.dumps(saved))
                path.write_text(json.dumps(document))
                with self.assertRaisesRegex(ValueError, 'Pending verification'):
                    run_attempt.finalize_run(path)

    def test_expired_run_exports_verified_leads_while_retaining_unused_pending_job(self):
        from test_export_xlsx import EXPORTER_PATH, read_first_sheet_rows
        node = os.environ.get('TYCHE_WORKSPACE_NODE')
        if not node or not os.environ.get('TYCHE_WORKSPACE_NODE_MODULES'):
            self.skipTest('Codex workbook runtime is not configured')
        path, document = self.delivery_run(expired=True)
        ledger = budget_guard.ledger_path(path).read_bytes()
        receipt = (path.parent / 'receipts/pending-job.json').read_bytes()
        result = subprocess.run([node, str(EXPORTER_PATH), str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        checked = json.loads((path.parent / 'validation.json').read_text())
        self.assertTrue(checked['delivery_allowed'])
        self.assertEqual(json.loads(path.read_text())['stop_reason'], 'time_limit_reached')
        rows = read_first_sheet_rows(path.parent / 'leads.xlsx')
        self.assertEqual(len(rows) - 1, len(document['accepted']))
        self.assertIn('selected@example.org', str(rows))
        self.assertNotIn('unused@example.org', str(rows))
        self.assertEqual(budget_guard.ledger_path(path).read_bytes(), ledger)
        self.assertEqual((path.parent / 'receipts/pending-job.json').read_bytes(), receipt)


if __name__ == '__main__':
    unittest.main()

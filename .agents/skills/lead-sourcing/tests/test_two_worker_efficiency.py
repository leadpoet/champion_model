"""Pacing and recovery use real saved state with a fake provider, never live calls."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_research_tools import FixtureProvider, check
from test_research_interface import setup_request
from research_tools import ResearchTools
import budget_guard as budget
import run_coordination as coordination


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'results.json'
        self.provider = FixtureProvider()
        self.request = copy.deepcopy(setup_request()['request'])
        self.request['contact_fields'] = []
        self.request['max_duration_seconds'] = 7200
        self.tools = ResearchTools(self.path, execute=self.provider, environment={'TYCHE_BUDGET_POLICY': 'reserved'})
        self.tools.start(self.request, max_usd=1)

    def workers(self):
        coordination.configure(self.path, 2)
        for number in (1, 2):
            worker = f'worker-{number}'
            coordination.register(self.path, worker, worker)
        return [ResearchTools(self.path, execute=self.provider, environment={
            'TYCHE_WORKER_ID': f'worker-{i}', 'TYCHE_WORKER_GENERATION': f'worker-{i}'}) for i in (1, 2)]

    def test_reserved_startup_with_required_email_creates_verification_reserve(self):
        path = self.root / 'email-run' / 'results.json'
        request = copy.deepcopy(self.request)
        request.update(target_count=10, contact_fields=['email'])
        tools = ResearchTools(path, execute=self.provider, environment={'TYCHE_BUDGET_POLICY': 'reserved'})
        tools.start(request, max_usd=5)
        ledger = budget.load_ledger(path)
        self.assertEqual(ledger['version'], 1)
        self.assertEqual(float(ledger['verification_reserve_credits']), 2)
        self.assertEqual(json.loads(path.read_text())['request']['contact_fields'], ['email'])
        self.assertFalse(any(row['operation'] == 'execute' for row in self.provider.requests))

    def test_budget_pacing_finishes_current_company_then_yields_without_resetting_ownership(self):
        one, two = self.workers()
        one.claim('one.test')
        two.claim('two.test')
        coordination.refresh_pacing(self.path)
        self.assertNotIn('serial_worker', coordination.snapshot(self.path))
        budget.reserve({'run_file': str(self.path), 'route_id': 'pending', 'max_cost_credits': 8}, 'deepline')
        coordination.refresh_pacing(self.path)
        self.assertEqual(coordination.snapshot(self.path)['serial_worker'], 'worker-1')
        coordination.require_claim(self.path, 'worker-2', 'worker-2', 'two.test', focus=True)
        coordination.reviewed(self.path, 'worker-2', 'worker-2', 'two.test', 'hold_account')
        self.assertEqual(two.call('tyche_claim', {'target': 'next.test'})['status'], 'worker_yield')
        self.assertEqual(two.call('tyche_finish', {})['status'], 'worker_yield')
        with self.assertRaises(coordination.WorkerYield):
            coordination.require_discovery(self.path, 'worker-2', 'worker-2')
        before = budget.load_ledger(self.path)
        coordination.configure(self.path, 2)
        self.assertEqual(coordination.snapshot(self.path)['claims']['two.test']['worker'], 'worker-2')
        self.assertEqual(budget.load_ledger(self.path), before)
        self.assertEqual(len(before['calls']), 1)

    def test_settlement_restores_parallel_work_without_releasing_claims(self):
        one, two = self.workers()
        one.claim('one.test')
        two.claim('two.test')
        budget.reserve({'run_file': str(self.path), 'route_id': 'pending', 'max_cost_credits': 8}, 'deepline')
        coordination.refresh_pacing(self.path)
        coordination.reviewed(self.path, 'worker-2', 'worker-2', 'two.test', 'hold_account')
        self.assertEqual(two.call('tyche_claim', {'target': 'next.test'})['status'], 'worker_yield')
        claims = coordination.snapshot(self.path)['claims']
        budget.settle(budget.ledger_path(self.path), 'pending', {'credits_charged': 6})
        before = budget.load_ledger(self.path)
        coordination.refresh_pacing(self.path)
        state = coordination.snapshot(self.path)
        self.assertNotIn('serial_worker', state)
        self.assertEqual(state['claims'], claims)
        self.assertEqual(budget.load_ledger(self.path), before)
        self.assertTrue(two.claim('next.test')['claimed'])
        self.assertEqual(len(state['pacing_history']), 1)

    def test_pacing_does_not_flap_at_the_drain_threshold(self):
        self.workers()
        budget.reserve({'run_file': str(self.path), 'route_id': 'pending', 'max_cost_credits': 8}, 'deepline')
        coordination.refresh_pacing(self.path)
        budget.settle(budget.ledger_path(self.path), 'pending', {'credits_charged': 7.5})
        coordination.refresh_pacing(self.path)
        self.assertEqual(coordination.snapshot(self.path)['serial_worker'], 'worker-1')

    def test_pacing_selects_a_running_worker_and_failed_owner_can_be_resumed(self):
        one, two = self.workers()
        one.claim('abandoned.test')
        coordination.update(self.path, lambda state: state['workers']['worker-1'].update(status='stopped', disabled=True))
        budget.reserve({'run_file': str(self.path), 'route_id': 'pending', 'max_cost_credits': 8}, 'deepline')
        coordination.refresh_pacing(self.path)
        self.assertEqual(coordination.snapshot(self.path)['serial_worker'], 'worker-2')
        before = budget.load_ledger(self.path)
        with self.assertRaisesRegex(ValueError, 'Finish current company abandoned.test'):
            coordination.require_discovery(self.path, 'worker-2', 'worker-2')
        state = coordination.snapshot(self.path)
        self.assertEqual(state['workers']['worker-2']['current_company'], 'abandoned.test')
        self.assertIsNone(state['workers']['worker-1']['current_company'])
        self.assertEqual(state['claims']['abandoned.test']['previous_owners'], ['worker-1'])
        self.assertEqual(budget.load_ledger(self.path), before)

    def test_live_or_uncertain_dispatch_ownership_cannot_be_transferred(self):
        one, two = self.workers()
        one.claim('uncertain.test')
        coordination.resume_abandoned(self.path, 'worker-2', 'worker-2')
        self.assertEqual(coordination.snapshot(self.path)['claims']['uncertain.test']['worker'], 'worker-1')
        coordination.update(self.path, lambda state: state['workers']['worker-1'].update(status='stopped', disabled=True))
        document = json.loads(self.path.read_text())
        document['stop_audit']['route_frontier'].append({'route_id': 'not-recorded', 'scope': 'uncertain.test'})
        self.path.write_text(json.dumps(document))
        coordination.resume_abandoned(self.path, 'worker-2', 'worker-2')
        self.assertEqual(coordination.snapshot(self.path)['claims']['uncertain.test']['worker'], 'worker-1')
        self.assertTrue(two.claim('other.test')['claimed'])

    def test_explicit_hard_provider_budget_survives_resume_and_rejects_policy_switch(self):
        before = budget.load_ledger(self.path)
        self.assertEqual(before['version'], 1)
        self.tools.start(self.request, max_usd=1)
        other = ResearchTools(self.path, execute=self.provider, environment={'TYCHE_BUDGET_POLICY': 'actual_cost'})
        with self.assertRaisesRegex(ValueError, 'policy'):
            other.start(self.request, max_usd=1)
        self.assertEqual(budget.load_ledger(self.path), before)

    def test_live_model_generations_are_active_but_stopped_and_replaced_usage_stays_missing(self):
        self.workers()
        folder = self.root / 'model-usage'
        folder.mkdir()
        for number in (1, 2):
            name = f'worker-{number}'
            (folder / (name + '.json')).write_text(json.dumps({'request_file': str(self.root / 'request.txt'),
                'worker_id': name, 'finished_at': None, 'responses': []}))
        summary = budget.model_cost_summary(self.root)
        self.assertEqual(summary['missing_model_usage'], [])
        self.assertEqual(summary['active_model_receipts'], ['worker-1', 'worker-2'])
        coordination.update(self.path, lambda state: state['workers']['worker-2'].update(status='stopped'))
        self.assertEqual(budget.model_cost_summary(self.root)['missing_model_usage'], ['worker-2.json'])
        coordination.register(self.path, 'worker-1', 'replacement')
        self.assertEqual(budget.model_cost_summary(self.root)['missing_model_usage'], ['worker-1.json', 'worker-2.json'])

    def test_free_existing_description_recovers_after_deadline_but_new_research_stays_closed(self):
        # Preserve a failed provider receipt, then emulate restored service and expiry.
        self.provider.raw = {'error': 'quota_exceeded', 'status': 'quota_exceeded'}
        failed = self.tools.lookup([check()])
        self.assertEqual(failed['status'], 'operationally_blocked')
        self.provider.raw = {'status': 'ok', 'element': {}}
        document = json.loads(self.path.read_text())
        started = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        document['stop_check']['started_at'] = started
        self.path.write_text(json.dumps(document))
        before = budget.load_ledger(self.path)
        first = len(self.provider.requests)
        with patch.dict('os.environ', {'TYCHE_FINALIZATION_ONLY': '1'}):
            self.assertTrue(self.tools.recover_access())
            self.assertFalse(self.tools.recover_access())
            with self.assertRaisesRegex(ValueError, 'Research is closed'):
                self.tools.lookup([check('another.test')])
            with self.assertRaisesRegex(ValueError, 'Research is closed'):
                self.tools.inspect(query='new companies')
            with self.assertRaisesRegex(ValueError, 'Research is closed'):
                self.tools.inspect(tool='new_lookup', refresh=True)
        self.assertEqual(budget.load_ledger(self.path), before)
        self.assertEqual(json.loads(self.path.read_text())['stop_check']['started_at'], started)
        self.assertEqual([v['operation'] for v in self.provider.requests[first:]], ['describe'])
        self.assertEqual(self.tools.inspect()['stop'], 'time_limit_reached')


if __name__ == '__main__':
    unittest.main()

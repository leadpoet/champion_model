import os
import contextlib
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from codex_tyche import (smoke, tool_configuration, workspace_environment, close_worker,
                        supervise_worker, original_start, research_deadline, write_worker_status, authorize_resume)
from datetime import datetime, timedelta, timezone


class WorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.contexts = contextlib.ExitStack()
        self.addCleanup(self.contexts.close)

    def test_new_run_has_no_implicit_deadline_and_saved_limits_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / 'request.txt'
            request.write_text('Five leads')
            started = '2020-01-01T00:00:00Z'
            self.assertIsNone(research_deadline(request, started))
            run = request.with_name('results.json')
            for duration in (None, 7200, 60):
                run.write_text(json.dumps({'request': {'max_duration_seconds': duration},
                                          'stop_check': {'started_at': started}}))
                expected = None if duration is None else datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp() + duration
                self.assertEqual(research_deadline(request, started), expected)

    def test_verified_partial_export_is_not_target_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / 'request.txt'; request.write_text('Ten leads')
            run = root / 'results.json'
            book = root / 'leads.xlsx'; book.write_bytes(b'fixture workbook')
            receipt = SimpleNamespace(data={'exit_code': 0, 'started_at': '2026-09-16T00:00:00Z'})
            from run_attempt import review_fingerprint
            for count, contact_target in ((5, None), (10, None), (10, 5)):
                complete = count == 10 and contact_target is None
                document = {'request': {'target_count': 10}, 'accepted': [{}] * count,
                            'stop_reason': 'target_met' if complete else 'time_limit_reached'}
                if contact_target:
                    document['request'].update(min_contacts_per_company=1, target_contacts_per_company=contact_target, contact_fields=[])
                    document['accepted'] = [{'primary_contact': {'full_name': 'Ada Example', 'current_title': 'Owner'}} for _ in range(count)]
                document['final_review'] = {'review_ref': review_fingerprint(document),
                                            'reviewed_at': '2026-09-16T01:00:00Z'}
                run.write_text(json.dumps(document))
                (root / 'validation.json').write_text(json.dumps({'delivery_allowed': True,
                    'completed_at': '2026-09-16T01:01:00Z',
                    'results_sha256': hashlib.sha256(run.read_bytes()).hexdigest(),
                    'workbook_sha256': hashlib.sha256(book.read_bytes()).hexdigest()}))
                before = run.read_bytes()
                with patch('run_attempt.delivery_preflight', return_value=(document, {'delivery_allowed': True})), \
                     patch('research_tools.ResearchTools.finish', side_effect=AssertionError('No re-export')):
                    status = json.loads(close_worker(request, receipt).read_text())
                self.assertEqual(status['status'], 'complete' if complete else 'partial')
                self.assertTrue(status['artifact_verified'])
                self.assertEqual(status['target_met'], complete)
                if contact_target:
                    self.assertEqual(status['contact_coverage']['target_shortfall'], 40)
                self.assertEqual(status['shortfall'], 10 - count)
                self.assertEqual(status['stop_reason'], document['stop_reason'])
                self.assertEqual(run.read_bytes(), before)

    def test_worker_limit_saves_resumable_status_without_relaunch_or_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / 'request.txt'
            request.write_text('fixture')
            run = root / 'results.json'
            run.write_text(json.dumps({'request': {'target_count': 10}, 'accepted': [{}] * 4}))
            before = run.read_bytes()
            receipt = SimpleNamespace(data={'exit_code': 1, 'failure_kind': 'model_usage_limit'})
            with patch('codex_tyche.subprocess.Popen', side_effect=AssertionError('Do not relaunch a model')):
                status = json.loads(close_worker(request, receipt).read_text())
            self.assertFalse(status['delivery_allowed'])
            self.assertEqual(status['reason'], 'model_usage_limit')
            self.assertEqual(status['accepted_count'], 4)
            self.assertEqual(run.read_bytes(), before)

    def test_worker_recovers_only_an_explicit_current_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / 'request.txt'; request.write_text('fixture')
            run = root / 'results.json'
            document = {'request': {'target_count': 1}, 'accepted': [{}], 'unresolved': [], 'rejected': []}
            reviewed = hashlib.sha256(json.dumps(dict(document, source_reviews=[]), sort_keys=True).encode()).hexdigest()
            document['final_review'] = {'review_ref': reviewed, 'reviewed_at': '2026-09-16T01:00:00Z'}
            run.write_text(json.dumps(document))
            receipt = SimpleNamespace(data={'exit_code': 1, 'started_at': '2026-09-16T00:00:00Z'})
            # Import the tool class via close_worker before patching; an
            # unreviewed initial pass must not invoke any exporter.
            original = run.read_text()
            run.write_text(json.dumps({k: v for k, v in document.items() if k != 'final_review'}))
            self.assertEqual(json.loads(close_worker(request, receipt).read_text())['status'], 'review_required')
            # This test isolates the review/hash recovery. Strict evidence and
            # ledger delivery is covered by the shared gate's offline journeys.
            self.contexts.enter_context(patch('run_attempt.delivery_preflight', return_value=(document, {'delivery_allowed': True})))
            run.write_text(original)
            def exported(*args, **kwargs):
                book = root / 'leads.xlsx'; book.write_bytes(b'fixture workbook')
                (root / 'validation.json').write_text(json.dumps({'delivery_allowed': True,
                    'completed_at': '2026-09-16T01:01:00Z',
                    'results_sha256': hashlib.sha256(run.read_bytes()).hexdigest(),
                    'workbook_sha256': hashlib.sha256(book.read_bytes()).hexdigest()}))
                return {'export': {'path': str(book)}}
            with patch('research_tools.ResearchTools.finish', side_effect=exported) as finish:
                status = json.loads(close_worker(request, receipt, {'TYCHE_WORKSPACE_NODE': '/fixture/node'}).read_text())
            finish.assert_called_once()
            self.assertTrue(status['delivery_allowed'])
            with patch('research_tools.ResearchTools.finish', side_effect=AssertionError('Already delivered')):
                self.assertTrue(json.loads(close_worker(request, receipt).read_text())['delivery_allowed'])
            later = SimpleNamespace(data={'exit_code': -15, 'failure_kind': 'deadline_reached',
                                          'started_at': '2026-09-16T02:00:00Z'})
            with patch('research_tools.ResearchTools.finish', side_effect=AssertionError('Old review cannot authorize repair recovery')):
                stale = json.loads(close_worker(request, later).read_text())
            self.assertFalse(stale['delivery_allowed'])
            self.assertEqual(stale['reason'], 'deadline_reached')
            # Explicit re-export in a later worker is still a valid delivery;
            # the original research need not change to prove export recovery.
            validation = root / 'validation.json'
            saved = json.loads(validation.read_text())
            saved['completed_at'] = '2026-09-16T02:01:00Z'
            validation.write_text(json.dumps(saved))
            with patch('research_tools.ResearchTools.finish', side_effect=AssertionError('Fresh export already exists')):
                self.assertTrue(json.loads(close_worker(request, later).read_text())['delivery_allowed'])
            document['accepted'] = [{'changed': True}]
            run.write_text(json.dumps(document))
            with patch('research_tools.ResearchTools.finish', side_effect=AssertionError('Stale review')):
                self.assertFalse(json.loads(close_worker(request, receipt).read_text())['delivery_allowed'])


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.contexts = contextlib.ExitStack()
        self.addCleanup(self.contexts.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.request = self.root / 'request.txt'
        self.request.write_text('Fixture request: fifteen leads.')
        self.path = self.root / 'results.json'
        self.started = datetime.now(timezone.utc).isoformat()
        self.document = {'request': {'target_count': 15, 'max_duration_seconds': 7200},
                         'stop_check': {'started_at': self.started}, 'accepted': [{}] * 6, 'routes': []}
        self.path.write_text(json.dumps(self.document))
        self.env = {'TYCHE_RUN_STARTED_AT': self.started}
        self.contexts.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.contexts.enter_context(contextlib.redirect_stderr(io.StringIO()))
        # Load local modules without constructing providers or calling them.
        research_deadline(self.request, self.started)
        self.progress = self.contexts.enter_context(patch('research_tools.ResearchTools._overview',
            return_value={'stop': 'continue', 'operational_block': None}))
        self.status = {'status': 'incomplete', 'delivery_allowed': False}
        self.contexts.enter_context(patch('codex_tyche.close_worker', side_effect=lambda *args:
            write_worker_status(self.request, self.status)))
        self.contexts.enter_context(patch('codex_tyche.save_report', return_value=self.root / 'run-costs.json'))

    def run_supervisor(self, worker):
        with patch('codex_tyche.execute_with_usage', side_effect=worker) as execute:
            result = supervise_worker(['codex', 'exec', '--json', 'Original request'], self.request, self.env, self.root)
        return result, execute

    def test_startup_watchdog_stops_once_without_a_run_or_budget(self):
        import time
        self.path.unlink()
        def worker(command, cwd, env, receipt, **options):
            self.assertGreater(options['deadline'](), time.time())
            self.assertLessEqual(options['deadline']() - time.time(), 600)
            receipt.data.update(status='failed', exit_code=-15, failure_kind='deadline_reached')
            receipt.save()
        result, execute = self.run_supervisor(worker)
        self.assertEqual(result, 1)
        self.assertEqual(execute.call_count, 1)
        status = json.loads((self.root / 'worker-status.json').read_text())
        self.assertEqual(status['reason'], 'startup_timeout')
        self.assertFalse(self.path.exists())
        receipts = list((self.root / 'model-usage').glob('*.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['failure_kind'], 'startup_timeout')

    def test_startup_watchdog_disappears_after_initializing_without_deadline(self):
        self.path.unlink()
        def worker(command, cwd, env, receipt, **options):
            self.assertIsNotNone(options['deadline']())
            self.document['request']['max_duration_seconds'] = None
            self.path.write_text(json.dumps(self.document))
            self.assertIsNone(options['deadline']())
            receipt.data.update(status='failed', exit_code=-15, failure_kind='cancelled')
        result, execute = self.run_supervisor(worker)
        self.assertEqual(result, 1)
        self.assertEqual(execute.call_count, 1)

    def test_combined_cutoff_saves_partial_output_without_starting_a_finalizer(self):
        import budget_guard
        self.document['budget'] = {'policy': 'actual_cost', 'paid_calls': 0,
            'limits': {'deepline_credits': 25, 'scrapingdog_credits': 0}}
        self.path.write_text(json.dumps(self.document))
        budget_guard.initialize(self.path, max_usd=2.5)
        def worker(command, cwd, env, receipt, **options):
            self.assertEqual(env['TYCHE_ACTIVE_MODEL_RECEIPT'], receipt.path.stem)
            self.assertIsNone(options['cost_stop']())
            receipt.observe({'type': 'thread.started', 'thread_id': 'fixture-thread'})
            usage = dict(input_tokens=0, cached_input_tokens=0, cache_write_input_tokens=0,
                output_tokens=2200000, reasoning_output_tokens=0, total_tokens=2200000)
            receipt.observe_response({'thread_id': 'fixture-thread', 'turn_id': 'fixture-turn',
                'response_id': 'fixture-response', 'usage': usage}, self.started, 'gpt-5.6-luna')
            receipt.observe({'type': 'turn.completed', 'usage': usage})
            self.assertEqual(options['cost_stop'](), 'budget_exhausted')
            receipt.finish(1)
            return 1
        with patch('confirmed_leads.update', return_value=None) as update:
            code, execute = self.run_supervisor(worker)
        self.assertEqual((code, execute.call_count), (1, 1))
        update.assert_called_once_with(self.path.resolve())
        saved = json.loads((self.root / 'worker-status.json').read_text())
        self.assertEqual(saved['reason'], 'budget_exhausted')
        self.assertEqual(saved['partial_output'], str(self.root.resolve() / 'leads.json'))
        self.assertEqual(len(list((self.root / 'model-usage').glob('*.json'))), 1)

    def test_cost_watcher_waits_for_settled_response_to_be_recorded(self):
        import budget_guard
        from codex_tyche import cost_stop
        self.document['budget'] = {'policy': 'actual_cost', 'paid_calls': 0,
            'limits': {'deepline_credits': 25, 'scrapingdog_credits': 0}}
        self.path.write_text(json.dumps(self.document))
        budget_guard.initialize(self.path, max_usd=2.5)
        budget_guard.reserve({'run_file': str(self.path), 'route_id': 'fixture'}, 'deepline')
        budget_guard.settle(budget_guard.ledger_path(self.path), 'fixture', {'cost_usd': 3})
        self.assertIsNone(cost_stop(self.request))
        self.document['routes'] = [{'route_id': 'fixture'}]
        self.path.write_text(json.dumps(self.document))
        self.assertEqual(cost_stop(self.request), 'budget_exhausted')

    def test_delayed_bill_pauses_provider_work_without_interrupting_model_usage(self):
        import budget_guard
        from codex_tyche import cost_stop
        self.document['budget'] = {'policy': 'actual_cost', 'paid_calls': 0,
            'limits': {'deepline_credits': 25, 'scrapingdog_credits': 0}}
        self.path.write_text(json.dumps(self.document))
        budget_guard.initialize(self.path, max_usd=2.5)
        budget_guard.reserve({'run_file': str(self.path), 'route_id': 'fixture'}, 'deepline')
        budget_guard.settle(budget_guard.ledger_path(self.path), 'fixture', {})
        self.document['routes'] = [{'route_id': 'fixture'}]
        self.path.write_text(json.dumps(self.document))
        self.assertIsNone(cost_stop(self.request, 'current-worker'))
        self.assertEqual(cost_stop(self.request), 'billing_pending')
        with self.assertRaisesRegex(budget_guard.BudgetError, 'billing_pending'):
            budget_guard.check_allowance(budget_guard.load_ledger(self.path), 'deepline', None, 6)
        # Delayed billing never disables the combined model-cost watchdog.
        folder = self.root / 'model-usage'
        folder.mkdir()
        (folder / 'current-worker.json').write_text(json.dumps({
            'request_file': str(self.request.resolve()), 'finished_at': None,
            'responses': [{'response_id': 'fixture-response', 'model': 'fixture', 'usage': {}, 'estimated_base_usd': 3}]}))
        self.assertEqual(cost_stop(self.request, 'current-worker'), 'budget_exhausted')

    def test_supervisor_waits_for_billing_before_another_model_turn(self):
        import budget_guard
        self.document['budget'] = {'policy': 'actual_cost', 'paid_calls': 0,
            'limits': {'deepline_credits': 25, 'scrapingdog_credits': 0}}
        self.path.write_text(json.dumps(self.document))
        budget_guard.initialize(self.path, max_usd=2.5)
        budget_guard.reserve({'run_file': str(self.path), 'route_id': 'fixture'}, 'deepline')
        budget_guard.settle(budget_guard.ledger_path(self.path), 'fixture', {})
        self.document['routes'] = [{'route_id': 'fixture'}]
        self.path.write_text(json.dumps(self.document))
        deadline = research_deadline(self.request, self.started)
        def wait(run_file, **options):
            self.assertEqual(options['deadline'], deadline)
            self.assertLessEqual(options['max_wait_seconds'], 120)
            self.assertFalse((self.root / 'model-usage').exists())
            self.assertEqual(json.loads((self.root / 'worker-status.json').read_text())['status'], 'waiting')
            budget_guard.settle(budget_guard.ledger_path(run_file), 'fixture', {'credits_charged': .5})
        def worker(command, cwd, env, receipt, **options):
            self.assertEqual(budget_guard.load_ledger(self.path)['calls']['fixture']['state'], 'settled')
            self.assertEqual(options['deadline'](), deadline)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
            self.status.update(delivery_allowed=True)
        with patch('run_attempt.recover_completed_attempts', return_value={'errors': []}), \
             patch('billing_reconciliation.reconcile'), \
             patch('billing_reconciliation.wait_for_billing', side_effect=wait) as recovery:
            result, execute = self.run_supervisor(worker)
        self.assertEqual((result, execute.call_count, recovery.call_count), (0, 1, 1))

    def test_six_of_fifteen_early_exit_resumes_same_clock_and_usage_directory(self):
        calls = []
        def worker(command, cwd, env, receipt, **options):
            calls.append((command, env, options['deadline']()))
            receipt.finish(0)
            receipt.data['status'] = 'complete'
            if len(calls) == 2:
                self.status.update(status='complete', delivery_allowed=True)
            return 0
        before = self.path.read_bytes()
        code, execute = self.run_supervisor(worker)
        self.assertEqual(code, 0)
        self.assertEqual(execute.call_count, 2)
        self.assertIn(str(self.path), calls[1][0][-1])
        self.assertIn('The saved request is authoritative', calls[1][0][-1])
        self.assertIn(str(self.path), calls[1][0][-1])
        self.assertEqual(calls[0][1]['TYCHE_RUN_STARTED_AT'], calls[1][1]['TYCHE_RUN_STARTED_AT'])
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(list((self.root / 'model-usage').glob('*.json'))), 2)

    def test_clean_unchanged_worker_exits_do_not_reintroduce_subjective_exhaustion(self):
        attempts = []
        def worker(command, cwd, env, receipt, **options):
            attempts.append(command)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
            if len(attempts) == 5:
                self.status.update(status='complete', delivery_allowed=True)
            return 0
        code, execute = self.run_supervisor(worker)
        self.assertEqual((code, execute.call_count), (0, 5))

    def test_research_handoff_starts_fresh_review_and_demotion_resumes_same_run(self):
        phases = []
        before = self.path.read_bytes()
        def worker(command, cwd, env, receipt, **options):
            phases.append(env['TYCHE_FINALIZATION_ONLY'])
            self.assertEqual(env['TYCHE_RUN_STARTED_AT'], self.started)
            if len(phases) == 1:
                self.progress.return_value = {'stop': 'target_met', 'operational_block': None}
            elif len(phases) == 2:
                self.assertNotIn('web_search="disabled"', command)
                self.assertIn('tyche_finish before individual field inspections', command[-1])
                self.assertIn('follow its review instructions', command[-1])
                self.assertNotIn('before tyche_finish', command[-1])
                self.assertNotIn('Use tyche_inspect first', command[-1])
                self.progress.return_value = {'stop': 'continue', 'operational_block': None}
            elif len(phases) == 3:
                self.assertNotIn('web_search="disabled"', command)
                self.assertIn('Use tyche_inspect first', command[-1])
                self.assertEqual(options['deadline'](), research_deadline(self.request, self.started))
                self.progress.return_value = {'stop': 'target_met', 'operational_block': None}
            else:
                self.status.update(delivery_allowed=True)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
        self.assertEqual(self.run_supervisor(worker)[0], 0)
        self.assertEqual(phases, ['0', '1', '0', '1'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(list((self.root / 'model-usage').glob('*.json'))), 4)

    def test_two_consecutive_actual_worker_failures_are_blocked(self):
        def worker(command, cwd, env, receipt, **options):
            receipt.finish(1)
            return 1
        code, execute = self.run_supervisor(worker)
        self.assertEqual((code, execute.call_count), (1, 2))
        status = json.loads((self.root / 'worker-status.json').read_text())
        self.assertEqual(status['reason'], 'repeated_worker_failure')
        self.assertFalse(status['delivery_allowed'])

    def test_successful_exit_resets_consecutive_failure_counter(self):
        codes = iter([1, 0, 1, 0])
        def worker(command, cwd, env, receipt, **options):
            code = next(codes)
            receipt.finish(code)
            if len(list((self.root / 'model-usage').glob('*.json'))) == 4:
                self.status.update(delivery_allowed=True)
                receipt.data['status'] = 'complete'
        code, execute = self.run_supervisor(worker)
        self.assertEqual((code, execute.call_count), (0, 4))

    def test_model_usage_blocker_and_user_cancel_never_restart(self):
        for failure in ('model_usage_limit', 'cancelled'):
            def worker(command, cwd, env, receipt, **options):
                receipt.data['failure_kind'] = failure
                receipt.finish(1)
                if failure == 'cancelled':
                    raise KeyboardInterrupt
            if failure == 'cancelled':
                with self.assertRaises(KeyboardInterrupt):
                    self.run_supervisor(worker)
            else:
                code, execute = self.run_supervisor(worker)
                self.assertEqual((code, execute.call_count), (1, 1))

    def test_evidenced_operational_block_prevents_any_worker_dispatch(self):
        self.progress.return_value = {'stop': 'continue', 'operational_block': 'mandatory provider access denied'}
        partial = {'exported': True, 'partial': True, 'delivery_allowed': False, 'rows': 6}
        with patch('research_tools.ResearchTools.export_partial', return_value=partial) as export:
            code, execute = self.run_supervisor(lambda *args, **kwargs: self.fail('No worker launch'))
        export.assert_called_once_with()
        self.assertEqual(code, 1)
        execute.assert_not_called()
        status = json.loads((self.root / 'worker-status.json').read_text())
        self.assertFalse(status['delivery_allowed'])
        self.assertEqual(status['partial_export'], partial)

    def test_explicit_resume_preserves_request_clock_and_ledger_and_is_idempotent(self):
        import budget_guard
        from validate_run import run_deadline
        self.document['stop_check']['started_at'] = '2020-01-01T00:00:00Z'
        self.document['budget'] = {'limits': {'deepline_credits': 50, 'scrapingdog_credits': 0}}
        self.path.unlink()
        budget_guard.create_run(self.path, self.document, max_usd=5, verification_reserve_credits=0)
        ledger = budget_guard.ledger_path(self.path)
        before = ledger.read_bytes()
        until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        authorize_resume(self.request, until, 'User: credits restored; continue.')
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['request'], self.document['request'])
        self.assertEqual(saved['stop_check']['started_at'], '2020-01-01T00:00:00Z')
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(run_deadline(saved).isoformat(), until)
        self.assertEqual(research_deadline(self.request, self.started), datetime.fromisoformat(until).timestamp())
        authorize_resume(self.request, until, 'User: credits restored; continue.')
        self.assertEqual(json.loads(self.path.read_text()), saved)
        for invalid, reason in [('2030-01-01T00:00:00', 'no timezone'), (until, ''),
                                ('2020-01-01T00:00:00Z', 'expired')]:
            with self.subTest(invalid=invalid, reason=reason), self.assertRaises(ValueError):
                authorize_resume(self.request, invalid, reason)
        corrupt = json.loads(self.path.read_text())
        corrupt['stop_check']['research_extensions'][0]['previous_deadline'] = until
        with self.assertRaisesRegex(ValueError, 'saved deadline'):
            run_deadline(corrupt)
        self.assertEqual(ledger.read_bytes(), before)

    def test_explicit_access_recovery_runs_once_and_preserves_paid_failure(self):
        self.progress.return_value = {'stop': 'continue', 'operational_block':
            'harvestapi_get_profile: quota_exceeded; restore provider access.'}
        before = self.path.read_bytes()
        def worker(command, cwd, env, receipt, **options):
            self.assertIn('refresh=true', command[-1])
            self.assertIn('do not repeat that request', command[-1])
            self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '0')
            receipt.finish(0)
        with patch('codex_tyche.execute_with_usage', side_effect=worker) as execute, \
                patch('research_tools.ResearchTools.export_partial', return_value={'exported': False}):
            code = supervise_worker(['codex', 'exec', 'Original'], self.request, self.env, self.root, resume=True)
        self.assertEqual(code, 1)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_blocked_startup_does_not_launch_repeated_model_sessions(self):
        self.path.unlink()
        def worker(command, cwd, env, receipt, **options):
            self.assertFalse((self.root / 'operational-status.json').exists(), 'Repeated blocked startup')
            (self.root / 'operational-status.json').write_text(json.dumps({
                'status': 'operationally_blocked', 'delivery_allowed': False,
                'reason': 'Required free catalog description timed out'}))
            receipt.finish(0)
            return 0
        code, execute = self.run_supervisor(worker)
        self.assertEqual((code, execute.call_count), (1, 1))
        status = json.loads((self.root / 'worker-status.json').read_text())
        self.assertEqual(status['reason'], 'Required free catalog description timed out')
        self.assertFalse(status['delivery_allowed'])
        self.assertFalse(self.path.exists())

    def test_uninitialized_startup_stops_without_losing_usage(self):
        self.path.unlink()
        self.request.write_text('Find five leads with a $0.01 combined budget.')
        original_request = self.request.read_bytes()
        for exit_code in (0, 1):
            for startup_status in (None, {'status': 'starting'}):
                with self.subTest(exit_code=exit_code, startup_status=startup_status):
                    marker = self.root / 'operational-status.json'
                    if startup_status is None:
                        marker.unlink(missing_ok=True)
                    else:
                        marker.write_text(json.dumps(startup_status))
                    saved_receipts = []
                    def worker(command, cwd, env, receipt, **options):
                        self.assertFalse(saved_receipts, 'Uninitialized startup must not restart')
                        usage = dict(input_tokens=0, cached_input_tokens=0, cache_write_input_tokens=0,
                            output_tokens=10000, reasoning_output_tokens=0, total_tokens=10000)
                        receipt.observe({'type': 'thread.started', 'thread_id': receipt.path.stem})
                        receipt.observe_response({'thread_id': receipt.path.stem, 'turn_id': 'turn',
                            'response_id': receipt.path.stem, 'usage': usage}, self.started, 'gpt-5.6-luna')
                        receipt.observe({'type': 'turn.completed', 'usage': usage})
                        receipt.finish(exit_code)
                        saved_receipts.append((receipt.path, receipt.path.read_bytes()))
                        return exit_code
                    code, execute = self.run_supervisor(worker)
                    self.assertEqual((code, execute.call_count), (1, 1))
                    status = json.loads((self.root / 'worker-status.json').read_text())
                    self.assertEqual(status['reason'], 'run_not_initialized')
                    self.assertFalse(status['delivery_allowed'])
                    self.assertFalse(self.path.exists())
                    self.assertEqual(self.request.read_bytes(), original_request)
                    receipt_path, original_receipt = saved_receipts[0]
                    self.assertEqual(receipt_path.read_bytes(), original_receipt)
                    receipt = json.loads(original_receipt)
                    self.assertTrue(receipt['usage_reconciled'])
                    self.assertEqual(receipt['estimated_base_usd'], .012)

    def test_incomplete_dispatch_accounting_blocks_before_model_work(self):
        recovery = {'recovered': [], 'pending': [{'ref': 'pending-call', 'receipt_status': 'pending'}],
                    'errors': ['paid route IDs must match the execution ledger; record every reserved call']}
        before = self.path.read_bytes()
        with patch('run_attempt.recover_completed_attempts', return_value=recovery) as recover:
            code, execute = self.run_supervisor(lambda *args, **kwargs: self.fail('No worker launch'))
        self.assertEqual(code, 1)
        execute.assert_not_called()
        recover.assert_called_once_with(self.path.resolve())
        status = json.loads((self.root / 'worker-status.json').read_text())
        self.assertEqual(status['reason'], 'saved_dispatch_accounting_incomplete')
        self.assertEqual(status['recovery'], recovery)
        self.assertFalse(status['delivery_allowed'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_expired_run_only_enters_bounded_source_review_without_provider_discovery(self):
        self.document['stop_check']['started_at'] = '2020-01-01T00:00:00Z'
        self.path.write_text(json.dumps(self.document))
        def worker(command, cwd, env, receipt, **options):
            self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '1')
            self.assertNotIn('web_search="disabled"', command)
            self.assertIn('tyche_finish before individual field inspections', command[-1])
            self.assertIn('follow its review instructions', command[-1])
            self.assertIn('Original request', command[-1])
            self.assertLess(options['deadline']() - datetime.now(timezone.utc).timestamp(), 601)
            self.status.update(delivery_allowed=True)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
        self.assertEqual(self.run_supervisor(worker)[0], 0)

    def test_target_met_resume_preserves_specific_review_feedback(self):
        self.progress.return_value = {'stop': 'target_met', 'operational_block': None}
        feedback = 'Review the saved office move: it does not establish upcoming building work.'
        command = ['codex', 'exec', '--json', feedback]
        def worker(actual, cwd, env, receipt, **options):
            self.assertIn(feedback, actual[-1])
            self.assertIn(str(self.path), actual[-1])
            self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '1')
            self.assertNotIn('web_search="disabled"', actual)
            self.assertEqual(options['deadline'](), research_deadline(self.request, self.started) + 600)
            self.status.update(delivery_allowed=True)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
        with patch('codex_tyche.execute_with_usage', side_effect=worker):
            self.assertEqual(supervise_worker(command, self.request, self.env, self.root), 0)
        self.assertEqual(command[-1], feedback)

    def test_corrected_feedback_is_not_replayed_into_every_finalizer(self):
        self.progress.return_value = {'stop': 'target_met', 'operational_block': None}
        feedback = 'Historical concern: hiring page lacks actual vacancies.'
        prompts = []
        def worker(command, cwd, env, receipt, **options):
            prompts.append(command[-1])
            self.status.update(delivery_allowed=len(prompts) == 2)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
        with patch('codex_tyche.execute_with_usage', side_effect=worker):
            self.assertEqual(supervise_worker(['codex', 'exec', feedback], self.request, self.env, self.root), 0)
        self.assertIn(feedback, prompts[0])
        self.assertNotIn(feedback, prompts[1])
        self.assertIn('current evidence', prompts[1])

    def test_finalization_has_one_grace_after_original_deadline_across_restarts(self):
        limit = research_deadline(self.request, self.started)
        self.progress.return_value = {'stop': 'target_met', 'operational_block': None}
        deadlines = []
        with patch('codex_tyche.time.time', return_value=limit - 600) as now:
            def worker(command, cwd, env, receipt, **options):
                deadlines.append(options['deadline']())
                self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '1')
                if len(deadlines) == 1:
                    now.return_value = limit + 20
                else:
                    self.status.update(delivery_allowed=True)
                receipt.finish(0)
                receipt.data['status'] = 'complete'
            self.assertEqual(self.run_supervisor(worker)[0], 0)
        self.assertEqual(deadlines, [limit + 600, limit + 600])

    def test_review_demotion_resumes_research_only_while_original_limits_allow(self):
        self.progress.return_value = {'stop': 'target_met', 'operational_block': None}
        calls = []
        def worker(command, cwd, env, receipt, **options):
            calls.append(env)
            if len(calls) == 1:
                self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '1')
                self.progress.return_value = {'stop': 'continue', 'operational_block': None}
            else:
                self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '0')
                self.assertNotIn('web_search="disabled"', command)
                self.assertEqual(options['deadline'](), research_deadline(self.request, self.started))
                self.status.update(delivery_allowed=True)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
        self.assertEqual(self.run_supervisor(worker)[0], 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['TYCHE_RUN_STARTED_AT'], calls[1]['TYCHE_RUN_STARTED_AT'])

    def test_review_demotion_cannot_reopen_research_after_deadline(self):
        self.document['stop_check']['started_at'] = '2020-01-01T00:00:00Z'
        self.path.write_text(json.dumps(self.document))
        self.progress.return_value = {'stop': 'target_met', 'operational_block': None}
        calls = []
        def worker(command, cwd, env, receipt, **options):
            calls.append(options['deadline']())
            self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '1')
            self.assertNotIn('web_search="disabled"', command)
            self.progress.return_value = {'stop': 'continue', 'operational_block': None}
            if len(calls) == 2:
                self.status.update(delivery_allowed=True)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
        self.assertEqual(self.run_supervisor(worker)[0], 0)
        self.assertEqual(calls[0], calls[1])

    def test_review_demotion_cannot_reopen_research_when_budget_is_exhausted(self):
        self.progress.return_value = {'stop': 'target_met', 'operational_block': None}
        calls = []
        def worker(command, cwd, env, receipt, **options):
            calls.append(options['deadline']())
            self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '1')
            self.assertNotIn('web_search="disabled"', command)
            self.progress.return_value = {'stop': 'budget_exhausted', 'operational_block': None}
            if len(calls) == 2:
                self.status.update(delivery_allowed=True)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
        self.assertEqual(self.run_supervisor(worker)[0], 0)
        self.assertEqual(calls[0], calls[1])

    def test_broken_review_state_stays_finalization_only_with_same_grace(self):
        self.progress.return_value = {'stop': 'target_met', 'operational_block': None}
        calls = []
        def worker(command, cwd, env, receipt, **options):
            calls.append(options['deadline']())
            self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '1')
            self.assertNotIn('web_search="disabled"', command)
            self.progress.return_value = {'stop': 'repair_state', 'operational_block': None}
            if len(calls) == 2:
                self.status.update(delivery_allowed=True)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
        self.assertEqual(self.run_supervisor(worker)[0], 0)
        self.assertEqual(calls[0], calls[1])

    def test_restart_before_setup_uses_first_usage_receipt_start(self):
        from run_costs import UsageReceipt
        self.path.unlink()
        receipt = UsageReceipt(self.request, 'gpt-5.6-luna', 'xhigh', 'fast')
        receipt.data['run_started_at'] = '2026-09-01T00:00:00Z'
        receipt.save()
        self.assertEqual(original_start(self.request, self.started), '2026-09-01T00:00:00Z')


class WorkspaceConfigurationTests(unittest.TestCase):

    def test_installed_bundle_paths_are_supplied_without_changing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle=Path(directory)/'.cache/codex-runtimes/codex-primary-runtime/dependencies'
            for name in ('node/bin/node','node/node_modules','python/bin/python3'):
                path=bundle/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()
            original={'PATH':'/existing'}
            with patch('codex_tyche.Path.home',return_value=Path(directory)):
                env=workspace_environment(original)
            self.assertEqual(original,{'PATH':'/existing'})
            self.assertEqual(env['TYCHE_WORKSPACE_NODE_MODULES'],str(bundle/'node/node_modules'))
            self.assertEqual(env['PATH'],str(bundle/'node/bin')+os.pathsep+'/existing')

    def test_explicit_host_configuration_is_preserved(self):
        supplied={'PATH':'/bin','TYCHE_WORKSPACE_NODE':'/custom/bin/node',
                  'TYCHE_WORKSPACE_NODE_MODULES':'/custom/node_modules','TYCHE_WORKSPACE_PYTHON':'/custom/bin/python3'}
        env=workspace_environment(supplied)
        for k in supplied:
            if k!='PATH':self.assertEqual(env[k],supplied[k])

    def test_native_config_binds_paths_and_forwards_names_not_secrets(self):
        with tempfile.TemporaryDirectory(prefix='tyche space ') as directory:
            path = Path(directory) / 'results.json'
            with patch.dict(os.environ, {'DEEPLINE_API_KEY': 'not-a-real-secret'}):
                config = tool_configuration(path, readonly=True)
            self.assertIn(str(path), config)
            self.assertIn('--read-only', config)
            self.assertIn('DEEPLINE_API_KEY', config)
            self.assertIn('CODEX_HOME', config)
            self.assertIn('TYCHE_RUN_STARTED_AT', config)
            self.assertIn('TYCHE_REQUEST_FILE', config)
            self.assertNotIn('not-a-real-secret', config)
            self.assertNotIn('sandbox_mode', config)
            self.assertNotIn('permission-profile', config)
            self.assertIn('required = true', config)

    def test_smoke_requires_successful_native_call_even_when_model_exits_zero(self):
        event = {'type':'item.completed', 'item':{'type':'mcp_tool_call', 'server':'tyche', 'tool':'tyche_inspect', 'status':'completed',
            'result': {'content': [{'type': 'text', 'text': '{"status":"not_started"}'}]}}}
        for status in ('completed', 'failed'):
            event['item']['status'] = status
            output = SimpleNamespace(returncode=0, stdout=(json.dumps(event)+'\n') * 2, stderr='')
            with patch('codex_tyche.subprocess.run', return_value=output), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                if status == 'completed': self.assertEqual(smoke(['fixture'], {}), 0)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'smoke test failed'): smoke(['fixture'], {})

    def test_smoke_rejects_completed_calls_with_unhealthy_or_missing_payloads(self):
        for payload in ({'status': 'operationally_blocked'}, {'status': 'recovery_required'}, None, [], 'invalid'):
            event = {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'server': 'tyche',
                'tool': 'tyche_inspect', 'status': 'completed',
                'result': {'content': [{'type': 'text', 'text': json.dumps(payload)}]}}}
            output = SimpleNamespace(returncode=0, stdout=(json.dumps(event)+'\n') * 2, stderr='')
            with self.subTest(payload=payload), patch('codex_tyche.subprocess.run', return_value=output), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'smoke test failed'):
                    smoke(['fixture'], {})


if __name__=='__main__':unittest.main()

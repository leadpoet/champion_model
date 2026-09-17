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
                        supervise_worker, original_start, research_deadline, write_worker_status)
from datetime import datetime, timezone


class WorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.contexts = contextlib.ExitStack()
        self.addCleanup(self.contexts.close)
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
        self.assertIn('Original request', calls[1][0][-1])
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
                self.assertIn('web_search="disabled"', command)
                self.progress.return_value = {'stop': 'continue', 'operational_block': None}
            elif len(phases) == 3:
                self.assertNotIn('web_search="disabled"', command)
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
        code, execute = self.run_supervisor(lambda *args, **kwargs: self.fail('No worker launch'))
        self.assertEqual(code, 1)
        execute.assert_not_called()

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

    def test_expired_run_only_enters_bounded_finalization_with_search_disabled(self):
        self.document['stop_check']['started_at'] = '2020-01-01T00:00:00Z'
        self.path.write_text(json.dumps(self.document))
        def worker(command, cwd, env, receipt, **options):
            self.assertEqual(env['TYCHE_FINALIZATION_ONLY'], '1')
            self.assertIn('web_search="disabled"', command)
            self.assertIn('No new searches', command[-1])
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
            self.assertIn('web_search="disabled"', actual)
            self.assertEqual(options['deadline'](), research_deadline(self.request, self.started) + 600)
            self.status.update(delivery_allowed=True)
            receipt.finish(0)
            receipt.data['status'] = 'complete'
        with patch('codex_tyche.execute_with_usage', side_effect=worker):
            self.assertEqual(supervise_worker(command, self.request, self.env, self.root), 0)
        self.assertEqual(command[-1], feedback)

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
            self.assertIn('web_search="disabled"', command)
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
            self.assertIn('web_search="disabled"', command)
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
            self.assertIn('web_search="disabled"', command)
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

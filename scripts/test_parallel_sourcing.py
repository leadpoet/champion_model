"""Exercise the real supervisor with controlled researchers, no provider spending."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import codex_tyche
from parallel_sourcing import run_research
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".agents/skills/lead-sourcing/tests"))
from test_research_tools import FixtureProvider
from test_research_interface import setup_request
from research_tools import ResearchTools
import run_coordination as coordination


class PoolTests(unittest.TestCase):
    def test_explicit_host_handoff_drains_active_pool_without_relaunch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / 'request.txt'
            request.write_text('Fixture ICP')
            run = root / 'results.json'
            env = {'TYCHE_RUN_STARTED_AT': datetime.now(timezone.utc).isoformat()}
            ResearchTools(run, execute=FixtureProvider()).start(setup_request()['request'])
            started = threading.Barrier(2)
            handoff = threading.Event()
            drained = []
            adapter = codex_tyche.LocalHost()
            adapter.research_finalization_ready = handoff.is_set

            def execute(command, cwd, worker_env, receipt, **options):
                started.wait(5)
                handoff.set()
                drained.append(worker_env['TYCHE_WORKER_ID'])
                receipt.finish(0)
                return 0

            with patch('run_costs.execute_with_usage', side_effect=execute), \
                    patch.object(ResearchTools, '_overview', return_value={'stop': 'continue'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                run_research(['codex', 'exec', 'Fixture ICP'], request, env, root,
                             count=2, host=adapter)

            self.assertEqual(sorted(drained), ['worker-1', 'worker-2'])
            self.assertEqual(coordination.snapshot(run)['phase'], 'finalization')

    def test_billing_settlement_after_drain_expiry_keeps_supervisor_handoff(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / 'request.txt'
            request.write_text('Fixture ICP')
            run = root / 'results.json'
            env = {'TYCHE_RUN_STARTED_AT': datetime.now(timezone.utc).isoformat()}
            ResearchTools(run, execute=FixtureProvider()).start(setup_request()['request'])
            pending = [True]
            offset = [0]
            cycles = []
            finish = threading.Event()
            real_time = time.time
            from concurrent.futures import wait as real_wait
            adapter = codex_tyche.LocalHost()
            adapter.reconcile_research = lambda path: None

            def execute(command, cwd, worker_env, receipt, **options):
                self.assertTrue(finish.wait(5))
                self.assertEqual(options['cost_stop'](), 'pool_stopped')
                receipt.finish(1)
                return 1

            def wait(active, **kwargs):
                cycles.append(True)
                if len(cycles) == 1:
                    offset[0] = 60
                elif len(cycles) == 2:
                    pending[0] = False
                else:
                    finish.set()
                    return real_wait(active, **kwargs)
                return set(), set(active)

            with patch('run_costs.execute_with_usage', side_effect=execute), \
                    patch('parallel_sourcing.wait', side_effect=wait), \
                    patch('parallel_sourcing.time.time', side_effect=lambda: real_time() + offset[0]), \
                    patch.object(ResearchTools, '_overview', side_effect=lambda: {
                        'stop': 'input_or_configuration_stop' if pending[0] else 'continue',
                        'stop_reason': 'billing_pending' if pending[0] else None}), \
                    patch.object(codex_tyche, 'cost_stop', side_effect=lambda *args:
                                 'billing_pending' if pending[0] else None), \
                    contextlib.redirect_stdout(io.StringIO()):
                run_research(['codex', 'exec', 'Fixture ICP'], request, env, root, count=1, host=adapter)
            self.assertEqual(coordination.snapshot(run)['phase'], 'finalization')
            self.assertEqual(len(list((root / 'model-usage').glob('*.json'))), 1)

    def test_pending_billing_reconciles_without_killing_turns_or_replaying_calls(self):
        for settlement in ('pending', 'immediate', 'during_drain', 'read_error'):
            with self.subTest(settlement=settlement), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                request = root / 'request.txt'
                request.write_text('Fixture ICP')
                run = root / 'results.json'
                env = {'TYCHE_RUN_STARTED_AT': datetime.now(timezone.utc).isoformat()}
                ResearchTools(run, execute=FixtureProvider()).start(setup_request()['request'])
                billing = threading.Event()
                reconciled = threading.Event()
                finished = threading.Event()
                started = threading.Barrier(2, action=billing.set)
                completed = threading.Barrier(2, action=finished.set)
                receipts = []
                reads = []
                usage = dict(input_tokens=100, cached_input_tokens=0, cache_write_input_tokens=0,
                             output_tokens=10, reasoning_output_tokens=0, total_tokens=110)
                adapter = codex_tyche.LocalHost()
                def reconcile(path):
                    self.assertEqual(path, run.resolve())
                    reads.append(path)
                    if settlement == 'immediate' or settlement == 'during_drain' and len(reads) > 1:
                        billing.clear()
                    reconciled.set()
                    if settlement == 'read_error':
                        raise OSError('Read-only billing unavailable')
                adapter.reconcile_research = reconcile

                def execute(command, cwd, worker_env, receipt, **options):
                    receipts.append(receipt)
                    started.wait(5)
                    self.assertTrue(reconciled.wait(5))
                    self.assertIsNone(options['cost_stop']())
                    self.assertIsNone(options['deadline']())
                    receipt.observe({'type': 'thread.started', 'thread_id': receipt.path.stem})
                    receipt.observe_response({'thread_id': receipt.path.stem, 'turn_id': 'turn',
                        'response_id': 'response', 'usage': usage}, '2026-09-19T00:00:00Z', codex_tyche.MODEL)
                    receipt.observe({'type': 'turn.completed', 'usage': usage})
                    receipt.finish(0)
                    completed.wait(5)
                    return 0

                def progress(tools):
                    return {'stop': ('input_or_configuration_stop' if billing.is_set() else
                                     'target_met' if finished.is_set() else 'continue'),
                            'stop_reason': 'billing_pending' if billing.is_set() else None}

                with patch('run_costs.execute_with_usage', side_effect=execute), \
                        patch.object(ResearchTools, '_overview', progress), \
                        patch.object(codex_tyche, 'cost_stop', side_effect=lambda request, active_model_receipt=None:
                                     'billing_pending' if billing.is_set() and active_model_receipt is None else None), \
                        contextlib.redirect_stdout(io.StringIO()):
                    run_research(['codex', 'exec', 'Fixture ICP'], request, env, root, count=2, host=adapter)
                self.assertEqual(len(receipts), 2)
                self.assertTrue(all(receipt.data['status'] == 'complete' for receipt in receipts))
                self.assertEqual(coordination.snapshot(run)['phase'], 'finalization')

    def test_billing_settled_between_decision_and_cost_read_does_not_kill_workers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / 'request.txt'
            request.write_text('Fixture ICP')
            run = root / 'results.json'
            env = {'TYCHE_RUN_STARTED_AT': datetime.now(timezone.utc).isoformat()}
            ResearchTools(run, execute=FixtureProvider()).start(setup_request()['request'])
            reconciled = threading.Event()
            receipts = []
            adapter = codex_tyche.LocalHost()
            adapter.reconcile_research = lambda path: reconciled.set()

            def execute(command, cwd, worker_env, receipt, **options):
                receipts.append(receipt)
                self.assertTrue(reconciled.wait(5))
                self.assertIsNone(options['cost_stop']())
                self.assertIsNone(options['deadline']())
                usage = dict(input_tokens=100, cached_input_tokens=0, cache_write_input_tokens=0,
                             output_tokens=10, reasoning_output_tokens=0, total_tokens=110)
                receipt.observe({'type': 'thread.started', 'thread_id': receipt.path.stem})
                receipt.observe_response({'thread_id': receipt.path.stem, 'turn_id': 'turn',
                    'response_id': 'response', 'usage': usage}, '2026-09-19T00:00:00Z', codex_tyche.MODEL)
                receipt.observe({'type': 'turn.completed', 'usage': usage})
                receipt.finish(0)
                return 0

            def progress(tools):
                return {'stop': 'target_met' if reconciled.is_set() else 'input_or_configuration_stop',
                        'stop_reason': None if reconciled.is_set() else 'billing_pending'}

            # The stop snapshot still saw pending billing, but the separate
            # cost check now sees settlement (or a not-yet-recorded route).
            with patch('run_costs.execute_with_usage', side_effect=execute), \
                    patch.object(ResearchTools, '_overview', progress), \
                    patch.object(codex_tyche, 'cost_stop', return_value=None), \
                    contextlib.redirect_stdout(io.StringIO()):
                run_research(['codex', 'exec', 'Fixture ICP'], request, env, root, count=1, host=adapter)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].data['status'], 'complete')
            self.assertEqual(coordination.snapshot(run)['phase'], 'finalization')

    def test_billing_stop_on_worker_exit_does_not_restart_the_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / 'request.txt'
            request.write_text('Fixture ICP')
            run = root / 'results.json'
            env = {'TYCHE_RUN_STARTED_AT': datetime.now(timezone.utc).isoformat()}
            ResearchTools(run, execute=FixtureProvider()).start(setup_request()['request'])
            observed = threading.Event()
            ended = threading.Event()
            calls = []

            def execute(command, cwd, worker_env, receipt, **options):
                calls.append(worker_env['TYCHE_WORKER_ID'])
                self.assertTrue(observed.wait(5))
                receipt.data['failure_kind'] = 'billing_pending'
                receipt.finish(1)
                ended.set()
                return 1

            def progress(tools):
                if ended.is_set():
                    return {'stop': 'input_or_configuration_stop', 'operational_block': 'billing_pending'}
                observed.set()
                return {'stop': 'continue'}

            with patch('run_costs.execute_with_usage', side_effect=execute), \
                    patch.object(ResearchTools, '_overview', progress), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'billing_pending'):
                    run_research(['codex', 'exec', 'Fixture ICP'], request, env, root, count=1)
            self.assertEqual(calls, ['worker-1'])
            self.assertEqual(coordination.snapshot(run)['phase'], 'blocked')

    def test_expired_crashed_pool_enters_review_only_after_process_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / 'results.json'
            coordination.configure(run, 2)
            coordination.register(run, 'worker-1', 'generation')
            coordination.claim(run, 'worker-1', 'generation', 'owned.test')
            receipts = run.parent / 'model-usage'
            receipts.mkdir()
            (receipts / 'generation.json').write_text(json.dumps({'process_group_id': 987654, 'finished_at': None}))
            original = coordination.snapshot(run)
            with patch.object(codex_tyche.os, 'killpg', return_value=None):
                with self.assertRaises(BlockingIOError):
                    codex_tyche.recover_stopped_workers(run)
            self.assertEqual(coordination.snapshot(run), original)
            with patch.object(codex_tyche.os, 'killpg', side_effect=ProcessLookupError):
                codex_tyche.recover_stopped_workers(run)
            current = coordination.snapshot(run)
            self.assertEqual(current['phase'], 'finalization')
            self.assertEqual(current['workers']['worker-1']['status'], 'stopped')
            self.assertEqual(current['claims'], original['claims'])
            api = ResearchTools(run, environment={'TYCHE_FINALIZATION_ONLY': '1'})
            with patch.object(api, '_finish', return_value={'review': 'ready'}) as finish:
                self.assertEqual(api.finish(), {'review': 'ready'})
                finish.assert_called_once()

    def test_process_death_releases_write_lock_without_changing_saved_data(self):
        import record_route
        import budget_guard
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / 'results.json'
            original = '{"accepted": [], "preserved": true}'
            for writer in ('record_route', 'budget_guard'):
                run.write_text(original)
                program = ("import os,sys; from pathlib import Path; import " + writer +
                           "; p=Path(sys.argv[1]); " +
                           ("record_route.mutate(p, lambda doc: os._exit(9))" if writer == 'record_route' else
                            "\nwith budget_guard.transaction(p) as doc:\n os._exit(9)"))
                child = subprocess.run([sys.executable, '-c', program, str(run)],
                    env=dict(os.environ, PYTHONPATH=str(codex_tyche.SKILL_ROOT / 'lead-sourcing/scripts')),
                    capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 9, child.stderr)
                self.assertEqual(run.read_text(), original)
                if writer == 'record_route':
                    self.assertTrue(os.path.samestat(run.with_name(run.name + '.lock').stat(),
                                                    run.with_name(run.name + '.write.lock').stat()))
                record_route.mutate(run, lambda doc: dict(doc, recovered=True))
                self.assertTrue(json.loads(run.read_text())['recovered'])
                self.assertFalse(run.with_name(run.name + '.lock').exists())

    def test_duplicate_supervisor_does_not_start_or_overwrite_live_status(self):
        with tempfile.TemporaryDirectory() as folder:
            request = Path(folder) / 'request.txt'
            request.write_text('fixture')
            run = request.with_name('results.json')
            status = request.with_name('worker-status.json')
            status.write_text('live owner')
            entered = threading.Event()
            release = threading.Event()
            def own():
                with coordination.locked(run, 'supervisor'):
                    entered.set()
                    release.wait(10)
            thread = threading.Thread(target=own)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                with patch.object(codex_tyche, '_supervise_worker') as invoke:
                    self.assertEqual(codex_tyche.supervise_worker([], request, {}, Path(folder)), 1)
                    invoke.assert_not_called()
                self.assertEqual(status.read_text(), 'live owner')
            finally:
                release.set()
                thread.join(5)

    def test_failed_initialization_stops_cleanly_without_starting_other_workers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / "request.txt"
            request.write_text("Fixture ICP")
            env = {"TYCHE_RUN_STARTED_AT": datetime.now(timezone.utc).isoformat()}
            calls = []
            def failed(command, cwd, worker_env, receipt, **options):
                calls.append(worker_env["TYCHE_WORKER_ID"])
                receipt.finish(1)
                return 1
            with patch("run_costs.execute_with_usage", side_effect=failed), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "run_not_initialized"):
                    run_research(["codex", "exec", "Fixture ICP"], request, env, root)
            self.assertEqual(calls, ["worker-1"])
            self.assertFalse((root / "results.json").exists())
            state = coordination.snapshot(root / "results.json")
            self.assertEqual(state["phase"], "blocked")
            self.assertEqual(state["workers"]["worker-1"]["status"], "stopped")

    def test_three_model_invocations_overlap_and_resume_one_shared_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / "request.txt"
            request.write_text("Fixture ICP")
            run = root / "results.json"
            env = {"TYCHE_RUN_STARTED_AT": datetime.now(timezone.utc).isoformat()}
            active = peak = finished = 0
            lock = threading.Lock()
            all_started = threading.Event()
            calls = []
            def execute(command, cwd, worker_env, receipt, **options):
                nonlocal active, peak, finished
                worker = worker_env["TYCHE_WORKER_ID"]
                tools = ResearchTools(run, execute=FixtureProvider(), environment=worker_env)
                if worker == "worker-1":
                    tools.start(setup_request()["request"])
                self.assertTrue(run.exists())
                self.assertTrue(tools.claim(worker + ".test")["claimed"])
                with lock:
                    calls.append((worker, worker_env, options["deadline"]()))
                    active += 1
                    peak = max(peak, active)
                    if active == 3:
                        all_started.set()
                self.assertTrue(all_started.wait(10))
                time.sleep(.1)
                with lock:
                    active -= 1
                    finished += 1
                receipt.finish(0)
                return 0
            original_overview = ResearchTools._overview
            def progress(tools):
                result = original_overview(tools)
                with lock:
                    if finished:
                        result["stop"] = "target_met"
                return result
            with patch("run_costs.execute_with_usage", side_effect=execute), patch.object(ResearchTools, "_overview", progress), contextlib.redirect_stdout(io.StringIO()):
                run_research(["codex", "exec", "Fixture ICP"], request, env, root, count=3)
            self.assertEqual(peak, 3)
            self.assertEqual(len(calls), 3)
            self.assertEqual(len({call[2] for call in calls}), 1)
            state = coordination.snapshot(run)
            self.assertEqual(state["phase"], "finalization")
            self.assertTrue(all(worker["status"] == "stopped" for worker in state["workers"].values()))
            self.assertEqual(len(state["claims"]), 3)
            receipts = [json.loads(path.read_text()) for path in (root / "model-usage").glob("*.json")]
            self.assertEqual({receipt["worker_id"] for receipt in receipts}, {"worker-1", "worker-2", "worker-3"})
            self.assertTrue(all(receipt["finished_at"] for receipt in receipts))
            self.assertFalse((root / "leads.xlsx").exists())

    def test_local_worker_failure_does_not_cancel_healthy_peer(self):
        self.local_failure("transport")

    def test_incomplete_usage_with_zero_exit_does_not_restart_forever(self):
        self.local_failure("incomplete")

    def local_failure(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / "request.txt"
            request.write_text("Fixture ICP")
            run = root / "results.json"
            env = {"TYCHE_RUN_STARTED_AT": datetime.now(timezone.utc).isoformat(),
                   "TYCHE_BUDGET_POLICY": "reserved"}
            started = threading.Event()
            complete = threading.Event()
            calls = []
            def execute(command, cwd, worker_env, receipt, **options):
                worker = worker_env["TYCHE_WORKER_ID"]
                calls.append(worker)
                tools = ResearchTools(run, execute=FixtureProvider(), environment=worker_env)
                if worker == "worker-1":
                    tools.start(setup_request()["request"])
                    self.assertTrue(started.wait(10))
                    if mode == 'transport':
                        raise OSError('Fixture worker transport failed after its owned process exited')
                    receipt.finish(0)
                    self.assertNotEqual(receipt.data['status'], 'complete')
                    return 2
                started.set()
                limit = time.monotonic() + 10
                while not coordination.snapshot(run)['workers']['worker-1'].get('disabled'):
                    self.assertLess(time.monotonic(), limit)
                    self.assertIsNone(options['deadline']())
                    time.sleep(.02)
                complete.set()
                receipt.finish(0)
                return 0
            original = ResearchTools._overview
            def progress(tools):
                result = original(tools)
                if complete.is_set():
                    result['stop'] = 'target_met'
                return result
            with patch('run_costs.execute_with_usage', side_effect=execute), patch.object(ResearchTools, '_overview', progress), contextlib.redirect_stdout(io.StringIO()):
                run_research(['codex', 'exec', 'Fixture ICP'], request, env, root, count=2)
            self.assertEqual(calls.count('worker-1'), 2)
            self.assertEqual(calls.count('worker-2'), 1)
            self.assertEqual(coordination.snapshot(run)['phase'], 'finalization')

    def test_resume_never_launches_a_disabled_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / 'request.txt'
            request.write_text('Fixture ICP')
            run = root / 'results.json'
            env = {'TYCHE_RUN_STARTED_AT': datetime.now(timezone.utc).isoformat(), 'TYCHE_BUDGET_POLICY': 'reserved'}
            ResearchTools(run, execute=FixtureProvider(), environment=env).start(setup_request()['request'])
            coordination.configure(run, 2)
            coordination.register(run, 'worker-1', 'old')
            coordination.update(run, lambda state: (state.update(ready=True), state['workers']['worker-1'].update(status='stopped', disabled=True)))
            finished = threading.Event()
            calls = []
            def execute(command, cwd, worker_env, receipt, **options):
                calls.append(worker_env['TYCHE_WORKER_ID'])
                finished.set()
                receipt.finish(0)
            original = ResearchTools._overview
            def progress(tools):
                result = original(tools)
                if finished.is_set():
                    result['stop'] = 'target_met'
                return result
            with patch('run_costs.execute_with_usage', side_effect=execute), patch.object(ResearchTools, '_overview', progress), contextlib.redirect_stdout(io.StringIO()):
                run_research(['codex', 'exec', 'Fixture ICP'], request, env, root, count=2)
            self.assertEqual(calls, ['worker-2'])

    def test_yielded_peer_resumes_when_serial_owner_retires(self):
        self.serial_failover(peer_registered=True)

    def test_unlaunched_peer_resumes_when_serial_owner_retires(self):
        self.serial_failover(peer_registered=False)

    def serial_failover(self, peer_registered):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = root / 'request.txt'
            request.write_text('Fixture ICP')
            run = root / 'results.json'
            env = {'TYCHE_RUN_STARTED_AT': datetime.now(timezone.utc).isoformat(), 'TYCHE_BUDGET_POLICY': 'reserved'}
            ResearchTools(run, execute=FixtureProvider(), environment=env).start(setup_request()['request'])
            coordination.configure(run, 2)
            for worker in (('worker-1', 'worker-2') if peer_registered else ('worker-1',)):
                coordination.register(run, worker, 'previous-' + worker)
            def drained(state):
                state.update(ready=True, serial_worker='worker-1')
                for row in state['workers'].values():
                    row['status'] = 'stopped'
            coordination.update(run, drained)
            finished = threading.Event()
            calls = []
            def execute(command, cwd, worker_env, receipt, **options):
                worker = worker_env['TYCHE_WORKER_ID']
                calls.append(worker)
                if worker == 'worker-1':
                    raise OSError('Fixture local transport failure')
                self.assertEqual(coordination.snapshot(run)['serial_worker'], worker)
                finished.set()
                receipt.finish(0)
            original = ResearchTools._overview
            def progress(tools):
                result = original(tools)
                if finished.is_set():
                    result['stop'] = 'target_met'
                return result
            with patch('run_costs.execute_with_usage', side_effect=execute), patch.object(ResearchTools, '_overview', progress), patch.object(coordination, 'refresh_pacing'), contextlib.redirect_stdout(io.StringIO()):
                run_research(['codex', 'exec', 'Fixture ICP'], request, env, root, count=2)
            self.assertEqual(calls, ['worker-1', 'worker-1', 'worker-2'])
            self.assertTrue(coordination.snapshot(run)['workers']['worker-1']['disabled'])


if __name__ == "__main__":
    unittest.main()

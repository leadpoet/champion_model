"""Exercise the real supervisor with controlled researchers, no provider spending."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import contextlib
import io
import json
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
                    self.assertGreater(options['deadline'](), time.time())
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
            with patch('run_costs.execute_with_usage', side_effect=execute), patch.object(ResearchTools, '_overview', progress), contextlib.redirect_stdout(io.StringIO()):
                run_research(['codex', 'exec', 'Fixture ICP'], request, env, root, count=2)
            self.assertEqual(calls, ['worker-1', 'worker-1', 'worker-2'])
            self.assertTrue(coordination.snapshot(run)['workers']['worker-1']['disabled'])


if __name__ == "__main__":
    unittest.main()

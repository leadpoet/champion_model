"""Shared worker-pool behavior at the Arena transport boundary, without spending."""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tyche_arena import host
from tyche_arena.broker import Broker
from tyche_arena.input import request_for
from scripts.parallel_sourcing import run_research
from research_tools import ResearchTools
import run_coordination as coordination
import budget_guard
from test_arena_public_web import ICP


class Environment(dict):
    def wait_idle(self, timeout):
        return True


def test_arena_uses_shared_two_worker_pool_with_isolated_profiles_and_owned_companies(tmp_path, monkeypatch):
    run = tmp_path / "results.json"
    request = tmp_path / "request.txt"
    request.write_text(json.dumps(ICP))
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 60)
    ResearchTools(run, execute=broker.execute).start(request_for(ICP, 2, 60))
    env = Environment(TYCHE_RUN_STARTED_AT=json.loads(run.read_text())["stop_check"]["started_at"],
                      TYCHE_PARALLEL_WORKERS="2")
    homes, gates, workers, receipts = [], [], [], []
    completed = []
    barrier = threading.Barrier(2)

    @contextmanager
    def session(**options):
        with tempfile.TemporaryDirectory(dir=tmp_path) as home:
            (Path(home) / "config.toml").write_text('model_provider = "arena"\n')
            homes.append(home)
            gates.append(options["request_gate"])
            yield Environment(CODEX_HOME=home)

    def execute(runtime, directory, environment, prompt, timeout, tail, *, receipt, deadline, cost_stop):
        worker = environment["TYCHE_WORKER_ID"]
        assert environment["TYCHE_WORKER_GENERATION"] == receipt.path.stem
        assert environment["TYCHE_PARALLEL_WORKERS"] == "2"
        assert callable(cost_stop)
        config = (Path(environment["CODEX_HOME"]) / "config.toml").read_text()
        assert '"TYCHE_WORKER_ID"' in config and '"TYCHE_WORKER_GENERATION"' in config
        tools = ResearchTools(run, environment=environment)
        claim = tools.claim("shared.example")
        if not claim["claimed"]:
            assert tools.claim("other.example")["claimed"]
        workers.append(worker)
        receipts.append(receipt.path)
        barrier.wait(5)
        completed.append(worker)
        return 0

    original = ResearchTools._overview
    def progress(tools):
        result = original(tools)
        if len(completed) == 2:
            result["stop"] = "target_met"
        return result

    runtime = SimpleNamespace(session=session, CODEX_BINARY="fixture")
    guard = SimpleNamespace(set_phase=lambda phase: None, research_denial=None,
                            _research_deadline=time.monotonic() + 60)
    adapter = host.ArenaHost(runtime, tmp_path, env, time.monotonic() + 120, guard)
    monkeypatch.setattr(host, "_codex_once", execute)
    monkeypatch.setattr(ResearchTools, "_overview", progress)
    run_research(["fixture", "exec", "Research the saved ICP"], request, env, tmp_path,
                 count=host.runner.DEFAULT_WORKERS, host=adapter)
    assert set(workers) == {"worker-1", "worker-2"}
    assert len(set(homes)) == 2 and len({id(gate) for gate in gates}) == 1
    state = coordination.snapshot(run)
    assert state["worker_count"] == 2 and state["phase"] == "finalization"
    assert state["conflicts"] == 1 and len(state["claims"]) == 2
    assert all(row["status"] == "stopped" for row in state["workers"].values())
    assert len(set(receipts)) == 2
    assert all(json.loads(path.read_text())["status"] == "complete" for path in receipts)
    assert not (tmp_path / "model-usage").exists()
    assert budget_guard.load_ledger(run)["usd_limit"] == "1.6"


def test_model_request_gate_shares_provider_process_lock_and_recovers_after_exit(tmp_path):
    run = tmp_path / "results.json"
    marker = tmp_path / "locked"
    program = """from pathlib import Path
import sys, time
import run_coordination as coordination
with coordination.locked(Path(sys.argv[1]), 'arena-billing'):
    Path(sys.argv[2]).touch()
    time.sleep(30)
"""
    process = subprocess.Popen([sys.executable, "-c", program, str(run), str(marker)],
        env=dict(os.environ, PYTHONPATH=str(ROOT / ".agents/skills/lead-sourcing/scripts")))
    try:
        until = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < until:
            time.sleep(.01)
        assert marker.exists()
        gate = host.RequestGate(run)
        assert gate.acquire(timeout=.05) is False
        process.terminate()
        process.wait(timeout=5)
        assert gate.acquire(timeout=.5) is True
        gate.release()
        assert not run.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_arena_process_deadline_changes_cancel_worker_and_leave_execution_receipt(tmp_path):
    program = tmp_path / "codex"
    program.write_text("#!/bin/sh\nexec sleep 30\n")
    program.chmod(0o755)
    request = tmp_path / "request.txt"
    request.write_text("fixture")
    receipt = host.ExecutionReceipt(request)
    until = time.time() + .2
    with pytest.raises(subprocess.TimeoutExpired):
        host._codex_once(SimpleNamespace(CODEX_BINARY=str(program)), tmp_path, dict(os.environ),
            "fixture", 30, bytearray(), receipt=receipt, deadline=lambda: until)
    assert type(receipt.data["process_group_id"]) is int
    with pytest.raises(ProcessLookupError):
        os.killpg(receipt.data["process_group_id"], 0)
    assert (receipt.path.parent / (receipt.path.stem + ".codex.log")).is_file()


def test_arena_shared_budget_stop_kills_worker_and_preserves_reason(tmp_path):
    program = tmp_path / "codex"
    program.write_text("#!/bin/sh\nexec sleep 30\n")
    program.chmod(0o755)
    request = tmp_path / "request.txt"
    request.write_text("fixture")
    receipt = host.ExecutionReceipt(request)
    started = time.monotonic()
    code = host._codex_once(SimpleNamespace(CODEX_BINARY=str(program)), tmp_path, dict(os.environ),
        "fixture", 30, bytearray(), receipt=receipt,
        cost_stop=lambda: "budget_reached" if time.monotonic() - started > .1 else None)
    receipt.finish(code)
    assert code == 1 and time.monotonic() - started < 3
    assert json.loads(receipt.path.read_text())["failure_kind"] == "budget_reached"
    with pytest.raises(ProcessLookupError):
        os.killpg(receipt.data["process_group_id"], 0)


def test_mcp_startup_does_not_wait_on_peer_and_lookup_restores_shared_state(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from tyche_arena import mcp
    run = tmp_path / 'results.json'
    ResearchTools(run, execute=Broker(tmp_path / 'worker.sock', time.monotonic() + 60).execute).start(
        request_for(ICP, 2, 60))
    monkeypatch.setenv('LAB_ARENA_WORKER_SOCKET', str(tmp_path / 'worker.sock'))
    monkeypatch.setenv('LAB_ARENA_OUTPUT_PATH', str(tmp_path / 'companies.json'))
    monkeypatch.setitem(sys.modules, 'lab_arena_checkpoint', SimpleNamespace(write=lambda *_args: None))
    snapshots = []
    def resume(path):
        snapshots.append(path)
        return {'deepline': 7, 'scrapingdog': 2}, {'deepline': True, 'scrapingdog': False}
    monkeypatch.setattr(mcp, 'broker_resume_state', resume)
    gate = host.RequestGate(run)
    assert gate.acquire(timeout=.1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            tools = pool.submit(mcp.LabTools, run, time.monotonic() + 60).result(timeout=2)
            assert snapshots == []
        finally:
            gate.release()
    def dispatch(name, arguments):
        assert tools.broker.provider_calls('deepline') == 7
        assert tools.broker.provider_is_blocked('deepline') is True
        return {'restored_before_dispatch': True}
    monkeypatch.setattr(tools, '_call', dispatch)
    assert tools.call('tyche_lookup', {}) == {'restored_before_dispatch': True}
    assert snapshots == [run]

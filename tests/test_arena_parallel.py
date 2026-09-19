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
from scripts.parallel_sourcing import _billing_pending, run_research
from research_tools import ResearchTools
import run_coordination as coordination
import budget_guard
from test_arena_public_web import ICP


class Environment(dict):
    def wait_idle(self, timeout):
        return True


def _unrecorded_call(run_file, billing, route_id="pending-route"):
    ledger, route_id = budget_guard.reserve({
        "run_file": str(run_file), "route_id": route_id, "max_cost_credits": 0,
    }, "deepline")
    if billing is not None:
        budget_guard.settle(ledger, route_id, billing)
    return route_id


def test_pool_recognizes_real_settlement_to_route_billing_gap(tmp_path):
    run = tmp_path / "results.json"
    ResearchTools(run, execute=Broker(tmp_path / "worker.sock", time.monotonic() + 60).execute).start(
        request_for(ICP, 1, 60))
    route_id = _unrecorded_call(run, {})

    progress = ResearchTools(run)._overview()

    assert progress["stop"] == "continue"
    assert progress["stop_reason"] is None
    assert _billing_pending(progress) is False
    assert budget_guard.spending_stop(budget_guard.load_ledger(run)) == "billing_pending"
    assert host.runner.cost_stop(tmp_path / "request.txt") is None
    assert route_id not in {route["route_id"] for route in json.loads(run.read_text())["routes"]}


@pytest.mark.parametrize(("billing", "admission_stop"), [
    (None, None),
    ({"cost_usd": 0.01}, None),
    ({}, None),
    ({"cost_usd": 1}, "budget_exhausted"),
])
def test_arena_admission_uses_real_spend_stop_while_owner_drains(
        tmp_path, monkeypatch, billing, admission_stop):
    run = tmp_path / "results.json"
    request = tmp_path / "request.txt"
    request.write_text("fixture")
    ResearchTools(run, execute=Broker(tmp_path / "worker.sock", time.monotonic() + 60).execute).start(
        request_for(ICP, 1, 60))
    _unrecorded_call(run, billing)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    admission = []

    @contextmanager
    def session(**options):
        admission.append(options["request_guard"]())
        yield Environment(CODEX_HOME=str(home))

    def execute(_runtime, _directory, _environment, _prompt, _timeout, _tail,
                *, receipt, deadline, cost_stop):
        assert deadline is None
        assert cost_stop() is None
        return 0

    class Guard:
        research_denial = None
        _research_deadline = time.monotonic() + 30

        @staticmethod
        def set_phase(_phase):
            return None

        @staticmethod
        def __call__():
            return True

    runtime = SimpleNamespace(session=session, CODEX_BINARY="fixture")
    adapter = host.ArenaHost(runtime, tmp_path, Environment(), time.monotonic() + 60, Guard())
    receipt = host.ExecutionReceipt(request)
    monkeypatch.setattr(host, "_codex_once", execute)

    assert host.runner.cost_stop(request, receipt.path.stem, admission=True) == admission_stop
    assert adapter.execute_research(
        ["fixture", "exec", "research"], request, {}, receipt, profile=tmp_path,
        deadline=lambda: time.time(), output=None, cost_stop=lambda: None,
    ) == 0
    assert admission == [admission_stop is None]
    assert json.loads(receipt.path.read_text())["status"] == "complete"


def test_actual_cost_pacing_ignores_holds_but_reacts_to_confirmed_spend(tmp_path):
    run = tmp_path / "results.json"
    ResearchTools(run, execute=Broker(tmp_path / "worker.sock", time.monotonic() + 60).execute).start(
        request_for(ICP, 1, 60), max_usd=.1,
        provider_credit_limits={"deepline": 25, "scrapingdog": 100},
        scrapingdog_usd_per_credit=.001,
    )
    coordination.configure(run, 2)
    coordination.register(run, "worker-1", "generation-1")
    coordination.register(run, "worker-2", "generation-2")
    ledger_path, route_id = budget_guard.reserve(
        {"run_file": str(run), "route_id": "held-scrapingdog"},
        "scrapingdog", tariff={"maximum_credits": 100},
    )
    totals = budget_guard.actual_cost_summary(budget_guard.load_ledger(run))
    assert totals["total_usd"] == 0
    assert totals["budget_total_usd"] == .1

    coordination.refresh_pacing(run)
    assert coordination.snapshot(run).get("serial_worker") is None

    budget_guard.settle(ledger_path, route_id, {"credits_charged": 81})
    coordination.refresh_pacing(run)
    assert coordination.snapshot(run)["serial_worker"] == "worker-1"


def test_arena_uses_shared_two_worker_pool_with_isolated_profiles_and_owned_companies(tmp_path, monkeypatch):
    monkeypatch.setattr(host.runner, "DEFAULT_WORKERS", 2)
    run = tmp_path / "results.json"
    request = tmp_path / "request.txt"
    request.write_text(json.dumps(ICP))
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 60)
    ResearchTools(run, execute=broker.execute).start(request_for(ICP, 2, 60))
    env = Environment(TYCHE_RUN_STARTED_AT=json.loads(run.read_text())["stop_check"]["started_at"],
                      TYCHE_PARALLEL_WORKERS="2")
    homes, selections, workers, receipts = [], [], [], []
    completed = []
    barrier = threading.Barrier(2)

    @contextmanager
    def session(**options):
        with tempfile.TemporaryDirectory(dir=tmp_path) as home:
            (Path(home) / "config.toml").write_text('model_provider = "arena"\n')
            homes.append(home)
            selections.append(options)
            yield Environment(CODEX_HOME=home)

    def execute(runtime, directory, environment, prompt, timeout, tail, *, receipt, deadline, cost_stop):
        worker = environment["TYCHE_WORKER_ID"]
        assert environment["TYCHE_WORKER_GENERATION"] == receipt.path.stem
        assert environment["TYCHE_PARALLEL_WORKERS"] == "2"
        assert deadline is None and callable(cost_stop)
        assert cost_stop() is None
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
    response_deadline = time.monotonic() + 120
    adapter = host.ArenaHost(runtime, tmp_path, env, response_deadline, guard)
    monkeypatch.setattr(host, "_codex_once", execute)
    monkeypatch.setattr(ResearchTools, "_overview", progress)
    run_research(["fixture", "exec", "Research the saved ICP"], request, env, tmp_path,
                 count=host.runner.DEFAULT_WORKERS, host=adapter)
    assert set(workers) == {"worker-1", "worker-2"}
    assert len(set(homes)) == 2
    assert len({id(selection["request_gate"]) for selection in selections}) == 1
    assert {selection["response_deadline"] for selection in selections} == {response_deadline}
    state = coordination.snapshot(run)
    assert state["worker_count"] == 2 and state["phase"] == "finalization"
    assert state["conflicts"] == 1 and len(state["claims"]) == 2
    assert all(row["status"] == "stopped" for row in state["workers"].values())
    assert len(set(receipts)) == 2
    assert all(json.loads(path.read_text())["status"] == "complete" for path in receipts)
    assert not (tmp_path / "model-usage").exists()
    assert budget_guard.load_ledger(run)["usd_limit"] == "1.6"


def test_headroom_joins_shared_pool_then_uses_same_run_for_final_review(tmp_path, monkeypatch):
    run = tmp_path / "results.json"
    request = tmp_path / "request.txt"
    request.write_text(json.dumps(ICP))
    ResearchTools(run, execute=Broker(tmp_path / "worker.sock", time.monotonic() + 60).execute).start(
        request_for(ICP, 2, 60))
    saved = json.loads(run.read_text())
    ledger_path = budget_guard.ledger_path(run)
    ledger_before = ledger_path.read_bytes()
    env = Environment(TYCHE_RUN_STARTED_AT=saved["stop_check"]["started_at"],
                      TYCHE_PARALLEL_WORKERS="2")
    response_deadline = time.monotonic() + 120
    quota_reads = iter((159, 159, 160, 160))
    quota_observed = []

    def quota_usage():
        used = next(quota_reads)
        quota_observed.append(used)
        return {"providers": {"openrouter": {
            "limit": 200, "used": used, "remaining": 200 - used, "inflight": 0,
        }}}

    class QuotaUnavailable(RuntimeError):
        pass

    monkeypatch.setattr(host, "QUOTA_SNAPSHOT_FRESHNESS_SECONDS", 0)
    guard = host.ArenaQuotaGuard(
        quota_usage, QuotaUnavailable, time.monotonic() + 60, response_deadline,
    )
    guard.preflight()
    final_environment = Environment()
    adapter = host.ArenaHost(
        SimpleNamespace(session=None, CODEX_BINARY="fixture"),
        tmp_path, final_environment, response_deadline, guard,
    )
    research_barrier = threading.Barrier(2)
    denied = threading.Event()
    drained = []
    research_dispatches = []
    finalization_calls = []
    delivered = [False]

    @contextmanager
    def session(**options):
        with tempfile.TemporaryDirectory(dir=tmp_path) as home:
            (Path(home) / "config.toml").write_text('model_provider = "arena"\n')

            class WorkerEnvironment(Environment):
                def wait_idle(self, timeout):
                    assert 0 <= timeout <= 125
                    drained.append(self["TYCHE_WORKER_ID"])
                    return True

            environment = WorkerEnvironment(
                CODEX_HOME=home, request_guard=options["request_guard"],
            )
            yield environment

    adapter.runtime.session = session

    def execute(_runtime, _directory, environment, _prompt, _timeout, _tail,
                *, receipt=None, deadline=None, cost_stop=None):
        if receipt is None:
            assert environment["TYCHE_FINALIZATION_ONLY"] == "1"
            assert guard() is True
            finalization_calls.append((
                run.read_bytes(), ledger_path.read_bytes(),
                sorted(path.name for path in (tmp_path / "worker-executions").glob("*.json")),
            ))
            delivered[0] = True
            return 0
        research_barrier.wait(5)
        if environment["request_guard"]():
            research_dispatches.append(environment["TYCHE_WORKER_ID"])
            assert denied.wait(5)
            return 0
        denied.set()
        return 1

    monkeypatch.setattr(host, "_codex_once", execute)
    monkeypatch.setattr(host, "full_delivery", lambda _directory: delivered[0])
    monkeypatch.setattr(ResearchTools, "_overview", lambda _tools: {"stop": "continue"})

    assert host.runner.supervise_worker(
        ["fixture", "exec", "Research the saved ICP"], request, env, tmp_path,
        host=adapter,
    ) == 0
    assert len(research_dispatches) == 1
    assert len(finalization_calls) == 1
    assert finalization_calls[0][0] == run.read_bytes()
    assert finalization_calls[0][1] == ledger_before == ledger_path.read_bytes()
    assert len(finalization_calls[0][2]) == 2
    assert quota_observed == [159, 159, 160, 160]
    assert sorted(drained) == ["worker-1", "worker-2"]
    assert guard.research_denial == "finalization_headroom"
    receipts = [json.loads(path.read_text()) for path in
                (tmp_path / "worker-executions").glob("*.json")]
    assert len(receipts) == 2
    assert all(row["status"] == "complete" for row in receipts)
    assert coordination.snapshot(run)["phase"] == "finalization"


def test_parallel_workers_join_before_exact_receipt_recovery_without_replay(tmp_path, monkeypatch):
    native_tests = ROOT / ".agents/skills/lead-sourcing/tests"
    sys.path.insert(0, str(native_tests))
    try:
        from test_stop_policy import action
    finally:
        sys.path.remove(str(native_tests))
    import run_attempt

    run = tmp_path / "results.json"
    request = tmp_path / "request.txt"
    request.write_text(json.dumps(ICP))
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 60)
    ResearchTools(run, execute=broker.execute).start(request_for(ICP, 2, 60))
    env = Environment(TYCHE_RUN_STARTED_AT=json.loads(run.read_text())["stop_check"]["started_at"],
                      TYCHE_PARALLEL_WORKERS="2")
    response_deadline = time.monotonic() + 120
    guard = SimpleNamespace(set_phase=lambda phase: None, research_denial=None,
                            _research_deadline=time.monotonic() + 60)
    active_sessions = 0
    drained_sessions = 0
    session_lock = threading.Lock()
    captured = []
    dispatches = []
    completed = []
    barrier = threading.Barrier(2)
    recovery_states = []

    @contextmanager
    def session(**options):
        nonlocal active_sessions, drained_sessions
        assert options["response_deadline"] == response_deadline
        with tempfile.TemporaryDirectory(dir=tmp_path) as home:
            (Path(home) / "config.toml").write_text('model_provider = "arena"\n')
            class WorkerEnvironment(Environment):
                def wait_idle(self, timeout):
                    nonlocal drained_sessions
                    assert 0 <= timeout <= 120 + host.PROCESS_RECEIPT_MARGIN_SECONDS
                    with session_lock:
                        drained_sessions += 1
                    return True
            with session_lock:
                active_sessions += 1
            try:
                yield WorkerEnvironment(CODEX_HOME=home)
            finally:
                with session_lock:
                    active_sessions -= 1

    def execute(_runtime, _directory, environment, _prompt, _timeout, _tail,
                *, receipt, deadline, cost_stop):
        assert deadline is None and cost_stop() is None
        worker = environment["TYCHE_WORKER_ID"]
        number = worker.rsplit("-", 1)[1]
        spec = {
            "action": dict(
                action("parallel-" + number, provider="deepline", paid_calls=1,
                       cost_upper_bound_credits=.2),
                phase="account_discovery", approach="parallel-worker-" + number,
            ),
            "request": {
                "operation": "execute", "tool": "fixture-search",
                "payload": {"query": "parallel-company-" + number},
            },
        }

        def interrupted(provider_request, capture):
            def dispatch():
                dispatches.append(provider_request["payload"]["query"])
                barrier.wait(5)
                response = {"exit_code": 0, "body": {"status": "completed", "result": {"data": []}},
                            "stderr": ""}
                capture(response)
                captured.append((spec, response))
                raise OSError("fixture interruption after capture")
            return budget_guard.guarded_call(provider_request, "deepline", dispatch)

        with pytest.raises(OSError, match="after capture"):
            run_attempt.run_attempt(run, spec, execute=interrupted)
        completed.append(worker)
        return 0

    real_recover = run_attempt.recover_completed_attempts

    def recover(path):
        with session_lock:
            recovery_states.append((active_sessions, len(completed)))
        return real_recover(path)

    runtime = SimpleNamespace(session=session, CODEX_BINARY="fixture")
    adapter = host.ArenaHost(runtime, tmp_path, env, response_deadline, guard)
    original = ResearchTools._overview

    def progress(tools):
        value = original(tools)
        if len(completed) == 2:
            value["stop"] = "target_met"
        return value

    monkeypatch.setattr(host, "_codex_once", execute)
    monkeypatch.setattr(run_attempt, "recover_completed_attempts", recover)
    monkeypatch.setattr(ResearchTools, "_overview", progress)

    run_research(["fixture", "exec", "Research the saved ICP"], request, env, tmp_path,
                 count=2, host=adapter)

    assert sorted(dispatches) == ["parallel-company-1", "parallel-company-2"]
    assert recovery_states[0] == (0, 0)
    assert recovery_states[-1] == (0, 2)
    assert drained_sessions == 2
    for spec, response in captured:
        saved = json.loads((tmp_path / "receipts" / (spec["action"]["id"] + ".json")).read_text())
        assert saved["receipt_status"] == "complete"
        assert saved["provider_response"] == response
        replay = []
        retry = {**spec, "action": {**spec["action"], "id": spec["action"]["id"] + "-retry"}}
        with pytest.raises(ValueError, match="already attempted or pending"):
            run_attempt.run_attempt(run, retry, execute=lambda *_args: replay.append(True))
        assert replay == []


@pytest.mark.parametrize("denial,expected", [
    ("research_deadline", "deadline_reached"),
    ("quota_unavailable", "host_limit"),
    ("quota_regressed", "host_limit"),
    ("finalization_headroom", None),
])
def test_parallel_worker_drain_defers_shared_stop_and_preserves_host_reason(
        tmp_path, monkeypatch, denial, expected):
    request = tmp_path / "request.txt"
    request.write_text("fixture")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    response_deadline = time.monotonic() + 30
    selections = []
    ledger = {
        "version": 2, "calls": {"admitted": {"state": "in_flight"}},
    }
    monkeypatch.setattr(host.budget_guard, "load_ledger", lambda _path: ledger)
    monkeypatch.setattr(host.runner, "saved_run", lambda _path: {"routes": []})

    @contextmanager
    def session(**options):
        selections.append(options)
        assert options["request_guard"]() is False
        yield Environment(CODEX_HOME=str(home))

    def execute(_runtime, _directory, _environment, _prompt, timeout, _tail,
                *, receipt, deadline, cost_stop):
        assert deadline is None
        assert cost_stop() is None
        ledger["calls"].clear()
        assert cost_stop() == "provider_stop"
        assert 29 < timeout <= 30 + host.PROCESS_RECEIPT_MARGIN_SECONDS
        return 1

    class Guard:
        research_denial = denial
        _research_deadline = time.monotonic() + 1

        @staticmethod
        def set_phase(phase):
            return None

        @staticmethod
        def __call__():
            return True

    guard = Guard()
    runtime = SimpleNamespace(session=session, CODEX_BINARY="fixture")
    adapter = host.ArenaHost(runtime, tmp_path, Environment(), response_deadline, guard)
    receipt = host.ExecutionReceipt(request)
    monkeypatch.setattr(host, "_codex_once", execute)

    code = adapter.execute_research(
        ["fixture", "exec", "research"], request, {}, receipt, profile=tmp_path,
        deadline=lambda: time.time(), output=None, cost_stop=lambda: "provider_stop",
    )

    assert selections[0]["response_deadline"] == response_deadline
    saved = json.loads(receipt.path.read_text())
    if expected is None:
        assert code == 0
        assert saved["status"] == "complete"
        assert saved["research_stop"] == "finalization_headroom"
        assert "failure_kind" not in saved
    else:
        assert code == 1
        assert saved["failure_kind"] == expected


def test_parallel_worker_does_not_start_session_after_absolute_deadline(tmp_path):
    request = tmp_path / "request.txt"
    request.write_text("fixture")
    sessions = []

    @contextmanager
    def session(**options):
        sessions.append(options)
        yield Environment()

    guard = SimpleNamespace(set_phase=lambda phase: None, research_denial=None,
                            _research_deadline=time.monotonic() - 2)
    runtime = SimpleNamespace(session=session, CODEX_BINARY="fixture")
    adapter = host.ArenaHost(runtime, tmp_path, Environment(), time.monotonic() - 1, guard)
    receipt = host.ExecutionReceipt(request)

    assert adapter.execute_research(
        ["fixture", "exec", "research"], request, {}, receipt, profile=tmp_path,
        deadline=lambda: time.time(), output=None, cost_stop=lambda: None,
    ) == 1
    assert sessions == []
    assert json.loads(receipt.path.read_text())["failure_kind"] == "deadline_reached"


def test_parallel_worker_fails_closed_when_its_response_bridge_is_not_idle(
        tmp_path, monkeypatch):
    request = tmp_path / "request.txt"
    request.write_text("fixture")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')

    class BusyEnvironment(Environment):
        @staticmethod
        def wait_idle(timeout):
            assert timeout > 0
            return False

    @contextmanager
    def session(**_options):
        yield BusyEnvironment(CODEX_HOME=str(home))

    guard = SimpleNamespace(set_phase=lambda phase: None, research_denial=None,
                            _research_deadline=time.monotonic() + 10)
    runtime = SimpleNamespace(session=session, CODEX_BINARY="fixture")
    adapter = host.ArenaHost(runtime, tmp_path, Environment(), time.monotonic() + 20, guard)
    receipt = host.ExecutionReceipt(request)
    monkeypatch.setattr(host, "_codex_once", lambda *_args, **_kwargs: 0)

    assert adapter.execute_research(
        ["fixture", "exec", "research"], request, {}, receipt, profile=tmp_path,
        deadline=lambda: time.time(), output=None, cost_stop=lambda: None,
    ) == 1
    saved = json.loads(receipt.path.read_text())
    assert saved["status"] == "failed" and saved["failure_kind"] == "host_limit"


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

"""Offline supervisor recovery: saved research, failure bounds and deadlines."""

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tyche_arena import runtime


def test_launch_drains_admitted_response_before_recovery_audit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    run = tmp_path / "run"
    run.mkdir()
    (run / "results.json").write_text('{"routes": []}')
    events = []
    response_pending = [True]
    selections = []

    class Environment(dict):
        def wait_idle(self, timeout):
            assert timeout > 0
            events.append("wait_idle")
            response_pending[0] = False
            return True

    @contextmanager
    def session(**selection):
        selections.append(selection)
        yield Environment(CODEX_HOME=str(home))

    def recover(_path):
        events.append("recover")
        assert response_pending == [False]
        return {"recovered": ["saved-response"], "pending": [], "errors": []}

    def execute(*_args, **_kwargs):
        events.append("codex")
        return 0

    monkeypatch.setattr(runtime.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(runtime, "recover_completed_attempts", recover)
    monkeypatch.setattr(runtime, "progress", lambda _path: {"stop": "continue", "operational_block": None})
    monkeypatch.setattr(runtime, "_codex_once", execute)
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: "codex" in events)
    host = SimpleNamespace(session=session, CODEX_BINARY="fixture-codex")
    guard = SimpleNamespace(set_phase=lambda _phase: None, research_denial=None)

    runtime.launch(host, run, 120.0, 130.0, 30.0, guard)

    assert events == ["wait_idle", "recover", "codex"]
    assert selections[0]["response_deadline"] == 130.0


@pytest.mark.parametrize("saved_progress", [False, True])
@pytest.mark.parametrize("stop_after_progress", ["deliver", "provider_stop", "deadline"])
def test_nonzero_exits_preserve_progress_and_existing_stop_bounds(
        tmp_path, monkeypatch, saved_progress, stop_after_progress):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    run = tmp_path / "run"
    run.mkdir()
    (run / "results.json").write_text('{"routes": []}')
    clock, invocations, delivered = [100.0], [], [False]

    class Environment(dict):
        def wait_idle(self, timeout):
            assert timeout > 0
            return True

    @contextmanager
    def session(**selection):
        yield Environment(CODEX_HOME=str(home))

    def execute(_host, directory, environment, prompt, timeout, _tail):
        invocations.append((clock[0], timeout))
        if len(invocations) <= 2:
            clock[0] += 5
            if saved_progress:
                # A successful research tool saved another provider result
                # before the later model request ended this process in error.
                (directory / "results.json").write_text(json.dumps({
                    "routes": [{"route_id": str(i), "status": "ok"}
                               for i in range(len(invocations))]}))
            if stop_after_progress == "deadline" and len(invocations) == 2:
                clock[0] = 130.0
            return 1
        delivered[0] = True
        return 0

    def progress(_path):
        stopped = stop_after_progress == "provider_stop" and len(invocations) == 2
        return {"stop": "provider_stop" if stopped else "continue",
                "operational_block": "provider_stop" if stopped else None}

    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime, "progress", progress)
    monkeypatch.setattr(runtime, "_codex_once", execute)
    monkeypatch.setattr(runtime, "recover_completed_attempts", lambda _path: {"errors": []})
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: delivered[0])
    host = SimpleNamespace(session=session, CODEX_BINARY="fixture-codex")
    guard = SimpleNamespace(set_phase=lambda phase: None, research_denial=None)

    def launch():
        runtime.launch(host, run, 120.0, 130.0, 30.0, guard)

    if not saved_progress:
        with pytest.raises(RuntimeError, match="failed twice"):
            launch()
        assert len(invocations) == 2 and not delivered[0]
    elif stop_after_progress == "provider_stop":
        with pytest.raises(RuntimeError, match="operationally blocked"):
            launch()
        assert len(invocations) == 2 and not delivered[0]
    elif stop_after_progress == "deadline":
        with pytest.raises(subprocess.TimeoutExpired):
            launch()
        assert len(invocations) == 2 and not delivered[0]
    else:
        launch()
        assert len(invocations) == 3 and delivered[0]
        assert invocations[-1][0] + invocations[-1][1] <= 130.0

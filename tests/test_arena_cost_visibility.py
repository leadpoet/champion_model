"""The model sees host totals without estimating, duplicating, or caching bills."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tyche_arena import host  # Initialize the shared research module path.
from tyche_arena.broker import Broker, sourcing_cost_snapshot
from tyche_arena.mcp import model_result


class Unavailable(RuntimeError):
    pass


def _cost():
    return {
        "successful_microusd": 1_020_000,
        "success_unresolved_microusd": 3_000_000,
        "settled_microusd": 1_025_000,
        "reserved_or_uncertain_microusd": 3_000_000,
        "inflight_calls": 1,
        "success_unresolved_calls": 1,
        "admission_cap_microusd": 4_000_000,
        "per_qualified_pair_cap_microusd": 800_000,
    }


def _install(monkeypatch, read):
    def validate(document):
        if not isinstance(document, dict) or document.get("schema_version") != "fixture.v2":
            raise Unavailable("invalid snapshot")
        return document

    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        quota_usage=read, QuotaUnavailable=Unavailable,
        validate_quota_cost_snapshot=validate,
    ))


def test_host_cost_is_preserved_without_local_provider_double_count(monkeypatch, tmp_path):
    calls = []
    cost = _cost()

    def read(**options):
        calls.append(options)
        return {"schema_version": "fixture.v2", "sourcing_cost": copy.deepcopy(cost)}

    _install(monkeypatch, read)
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 60,
                    initial_calls={"deepline": 70, "scrapingdog": 12})
    budget = broker.local_dispatch_budget()
    exposed = budget["authoritative_sourcing_cost"]
    assert {key: exposed[key] for key in cost} == cost
    assert exposed["scope"] == "all_execute_attempts_for_this_icp"
    assert "successful_microusd is measured successful settled spend" in exposed["note"]
    assert "success_unresolved_microusd is a provisional upper bound" in exposed["note"]
    assert "it is not measured spend" in exposed["note"]
    assert "Do not use unresolved holds to infer cost eligibility" in exposed["note"]
    assert "conservative scoring cost" not in exposed["note"]
    assert exposed["success_unresolved_microusd"] > exposed["per_qualified_pair_cap_microusd"]
    assert budget["providers"]["deepline"] == {"used": 70}
    assert calls == [{"include_sourcing_cost": True}]
    result = model_result({"text": "x" * 40000}, budget)
    assert result["truncated"] is True
    assert json.loads(json.dumps(result))["arena_budget"] == budget


@pytest.mark.parametrize("failure", ["unavailable", "invalid"])
def test_failed_cost_read_never_reuses_previous_spend(monkeypatch, failure):
    count = 0

    def read(**options):
        nonlocal count
        count += 1
        if count == 1:
            return {"schema_version": "fixture.v2", "sourcing_cost": _cost()}
        if failure == "unavailable":
            raise Unavailable("stale lease or unavailable transport")
        return {"schema_version": "malformed"}

    _install(monkeypatch, read)
    assert sourcing_cost_snapshot()["successful_microusd"] == 1_020_000
    assert sourcing_cost_snapshot() == {"status": "unavailable"}
    assert count == 2


def test_counter_only_host_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        quota_usage=lambda: pytest.fail("Do not request v2 from a counter-only host"),
        QuotaUnavailable=Unavailable,
    ))
    assert sourcing_cost_snapshot() == {"status": "unavailable"}


def test_observed_host_spend_changes_without_a_model_owned_cost_cache(monkeypatch):
    values = iter((500_000, 750_000))

    def read(**options):
        cost = _cost()
        cost["successful_microusd"] = next(values)
        return {"schema_version": "fixture.v2", "sourcing_cost": cost}

    _install(monkeypatch, read)
    assert sourcing_cost_snapshot()["successful_microusd"] == 500_000
    assert sourcing_cost_snapshot()["successful_microusd"] == 750_000


def test_model_uses_actual_host_cost_schema(monkeypatch):
    source = Path(os.environ["LAB_ARENA_REFERENCE_SOURCE"])
    spec = importlib.util.spec_from_file_location(
        "cost_visibility_host_checkpoint", source / "lab_arena/lab_arena_checkpoint.py")
    checkpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoint)
    document = {
        "schema_version": checkpoint.QUOTA_COST_SNAPSHOT_SCHEMA_VERSION,
        "providers": {name: {"limit": 200, "used": 10, "remaining": 190, "inflight": 0}
                      for name in checkpoint.QUOTA_PROVIDERS},
        "sourcing_cost": _cost(),
    }
    checkpoint.quota_usage = lambda **options: copy.deepcopy(document)
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", checkpoint)
    observed = sourcing_cost_snapshot()
    assert {key: observed[key] for key in _cost()} == _cost()
    # A malformed host response must not be displayed as a valid zero-cost balance.
    document["sourcing_cost"]["successful_microusd"] = True
    assert sourcing_cost_snapshot() == {"status": "unavailable"}

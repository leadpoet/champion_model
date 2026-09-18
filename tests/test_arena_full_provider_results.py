"""Provider rows beyond a preview remain usable through Arena delivery."""

import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_arena_codex import (
    ICP, arena_operations, budget_guard, lab, no_host_quota_cache_delay,
    scenario,
)


@pytest.mark.parametrize("mode", ["deliver", "partial_timeout", "partial_error"])
@pytest.mark.parametrize("legacy_preview", [False, True])
def test_later_provider_rows_reach_reviewed_arena_checkpoint(
        lab, monkeypatch, arena_operations, mode, legacy_preview):
    reference = Path(os.environ["LAB_ARENA_REFERENCE_SOURCE"])
    spec = importlib.util.spec_from_file_location(
        "full_rows_checkpoint_reference", reference / "lab_arena/lab_arena_checkpoint.py")
    checkpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoint)
    current = sys.modules["lab_arena_checkpoint"]
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=lambda rows: checkpoint.write(rows, output_path=lab.output),
        quota_usage=current.quota_usage, QuotaUnavailable=current.QuotaUnavailable))

    provider = lab.provider

    def many_pages(parameters):
        body = provider(parameters)
        if parameters["tool"] == "generic_http_request":
            body = copy.deepcopy(body)
            body["results"] = [{
                "markdown": "Archive navigation only; no qualification evidence.",
                "metadata": {"statusCode": 200,
                             "sourceUrl": f"https://example.com/archive/{index}"},
            } for index in range(10)] + body["results"]
        return body

    monkeypatch.setattr(lab, "provider", many_pages)
    captured = {}

    def paged_scenario():
        program = scenario("tyche_finish" if mode == "deliver" else "tyche_checkpoint")
        command = next(program)
        while True:
            result = yield command
            if (command[0] == "tyche_lookup"
                    and command[1]["checks"][0]["tool"] == "generic_http_request"):
                view = result["lookups"][0]
                assert (len(view["results"]), view["result_count"], view["next_offset"]) == (10, 12, 10)
                tools = lab.research[0].research
                receipt_path = tools.path.parent / "receipts" / (view["route"] + ".json")
                saved = json.loads(receipt_path.read_text())
                assert len(saved["results"]) == 12
                if legacy_preview:
                    saved["results"] = saved["results"][:10]
                    receipt_path.write_text(json.dumps(saved))
                before = receipt_path.read_bytes()
                calls = len(lab.frames)
                ledger = budget_guard.load_ledger(tools.path)
                first = yield "tyche_inspect", {"ref": view["route"]}
                assert (len(first["results"]), first["result_count"], first["next_offset"]) == (10, 12, 10)
                page = yield "tyche_inspect", {"ref": view["route"], "offset": 10}
                assert [row["ref"] for row in page["results"]] == [
                    view["route"] + ":10", view["route"] + ":11"]
                assert page["next_offset"] is None
                assert (receipt_path.read_bytes(), len(lab.frames),
                        budget_guard.load_ledger(tools.path)) == (before, calls, ledger)
                captured.update(path=receipt_path, before=before)
                result = {"lookups": [page]}
            try:
                command = program.send(result)
            except StopIteration:
                return

    lab.program = paged_scenario
    lab.mode = mode
    from harness import run_icp
    rows = run_icp(ICP)
    host_output = importlib.import_module("lab_arena.output")
    document = host_output.output_document_from_bytes(
        lab.output.read_bytes(), expected_schema_version="leadpoet.lab_arena.output.v5")
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert document == host_output.output_document_from_bytes(
        json.dumps({"companies": rows}).encode(),
        expected_schema_version="leadpoet.lab_arena.output.v5")
    assert len(rows) == 1
    assert rows[0]["contact"]["email"] == "ada@example.com"
    assert rows[0]["intent_signals"][0]["date"] == "2026-08-12"
    assert captured["path"].read_bytes() == captured["before"]
    assert sum(frame["tool"] == "generic_http_request" for frame in lab.frames) == 1

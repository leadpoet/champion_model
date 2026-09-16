"""TYCHE-only offline contracts; no Leadpoet imports, Codex or live providers."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import base64
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import tomllib
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tyche_arena import runtime
from tyche_arena.broker import Broker, BrokerError, BrokerRefusal, DEEPLINE_DISPATCH_LIMIT
from tyche_arena.input import request_for
from tyche_arena.mcp import LAB_TOOLS, LabTools, model_result
from tyche_arena.output import companies, signal_date
from research_tools import ResearchTools
import budget_guard

ICP = {"intent_details_policy": "intent_details_v1", "contact_policy": "contacts_v1",
       "industry": "Manufacturing", "required_attribute": "Manufactures products for retailers",
       "intent_signals": ["Recently integrated an acquired warehouse", "Recently announced a strategic partnership"],
       "intent_max_age_days": 365,
       "bonus_intents": [{"intent_signal": "Recently announced a strategic partnership", "intent_category": "PARTNERSHIP", "intent_max_age_days": 90}],
       "target_roles": ["Director of Supply Chain"], "target_seniority": "Director+",
       "contact_geography": {"countries": ["US"], "regions": ["OH"], "cities": ["Columbus"]},
       "excluded_companies": ["excluded.example.com"]}
COMPANY_URL = "https://www.linkedin.com/company/example-products"
PERSON_URL = "https://www.linkedin.com/in/ada-example"
PARAGRAPH = ("Example Products connected its acquired warehouse to a shared WMS on August 12, 2026. "
             "The project covers inventory visibility and fulfillment across the combined operation. "
             "This recent integration may increase its need to coordinate stock and orders between warehouses.")


def lookup(tool, inputs, phase="account_verification", **extra):
    return {"checks": [{"target": "example.com", "purpose": "Verify fixture evidence", "phase": phase,
                        "tool": tool, "inputs": inputs, **extra}]}


def scenario(finish_tool="tyche_finish"):
    company = yield "tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL})
    company_ref = company["lookups"][0]["results"][0]["ref"]
    pages = yield "tyche_lookup", lookup("generic_http_request", {"url": "https://example.com/news", "method": "GET"})
    refs = [row["ref"] for row in pages["lookups"][0]["results"]]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "qualify_account", "reason": "Company and signal verified",
        "company": {"ref": company_ref, "industry": "Manufacturing", "sub_industry": "Textiles",
            "description": "Example Products manufactures packaged goods, tools, and accessories. It supplies retailers with consumer products.",
            "classification_note": "Canonical taxonomy classification"},
        "account_fit": {"ref": refs[0], "fit_claim": "Manufacturing account"},
        "qualification_checks": [
            {"requirement_ref": "attribute:0", "status": "pass", "claim": "Manufactures consumer products for retailers", "evidence": [{"ref": refs[0]}]},
            {"requirement_ref": "signal:0", "status": "pass", "claim": "Connected an acquired warehouse to a shared WMS", "evidence": [{"ref": refs[1], "event_date": "2026-08-12"}]}],
        "intent_details": PARAGRAPH}],
        "sources": [{"ref": pages["lookups"][0]["route"], "state": "exhausted", "reason": "Both fixture sources reviewed"}]}
    profile = yield "tyche_lookup", lookup("harvestapi_get_profile", {"url": PERSON_URL, "main": "true"}, "contact_verification")
    profile_ref = profile["lookups"][0]["results"][0]["ref"]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "hold_contact", "reason": "Verify selected email",
        "primary_contact": {"ref": profile_ref, "requested_role": "Director of Supply Chain", "role_match": "exact"}}]}
    enriched = yield "tyche_lookup", lookup("harvestapi_get_profile", {"findEmail": "true"}, "contact_discovery", contact_ref=profile_ref)
    profile_ref = enriched["lookups"][0]["results"][0]["ref"]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "hold_contact", "reason": "Select enriched profile",
        "primary_contact": {"ref": profile_ref, "requested_role": "Director of Supply Chain", "role_match": "exact"}}]}
    email = yield "tyche_lookup", lookup("zerobounce_validate", {"email": "ada@example.com"}, "email_validation", contact_ref=profile_ref)
    email_ref = email["lookups"][0]["results"][0]["ref"]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "accept", "reason": "Verified company and current buyer",
        "primary_contact": {"email_ref": email_ref}}]}
    if finish_tool is None:
        return
    packet = yield finish_tool, {}
    assert packet["status"] == "review_required", packet
    company = packet["companies"][0]
    assert len(company["signal_checks"]) == 1
    assert company["signal_checks"][0]["evidence"][0]["event_date"] == "2026-08-12"
    assert not any(check.get("signal") for check in company["qualification_checks"])
    final = yield finish_tool, {"review_ref": packet["review_ref"]}
    assert final["checkpoint_saved"], final
    assert final["delivery_allowed"] == (finish_tool == "tyche_finish"), final


def raw_response_scenario():
    company = yield "tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL})
    company_ref = company["lookups"][0]["results"][0]["ref"]
    page = yield "tyche_lookup", lookup(
        "firecrawl_scrape", {"url": "https://example.com/about", "zeroDataRetention": True},
        max_cost_credits=.02)
    page_ref = page["lookups"][0]["results"][0]["ref"]
    answer = yield "tyche_lookup", lookup("exa_answer", {"query": "Example Products warehouse integration", "text": True},
                                            approach="Citation-backed signal verification")
    answer_rows = answer["lookups"][0]["results"]
    assert answer_rows[0]["facts"]["provider_answer"] == "Generated summary; review its citations."
    assert answer_rows[0]["facts"]["evidence_text"] != answer_rows[0]["facts"]["provider_answer"]
    signal_ref = answer_rows[0]["ref"]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "qualify_account", "reason": "Company and signal verified",
        "company": {"ref": company_ref, "industry": "Manufacturing", "sub_industry": "Textiles",
            "description": "Example Products manufactures packaged goods, tools, and accessories. It supplies retailers with consumer products.",
            "classification_note": "Canonical taxonomy classification"},
        "account_fit": {"ref": page_ref, "fit_claim": "Manufacturing account"},
        "qualification_checks": [
            {"requirement_ref": "attribute:0", "status": "pass", "claim": "Manufactures consumer products for retailers", "evidence": [{"ref": page_ref}]},
            {"requirement_ref": "signal:0", "status": "pass", "claim": "Connected an acquired warehouse to a shared WMS", "evidence": [{"ref": signal_ref, "event_date": "2026-08-12"}]}],
        "intent_details": PARAGRAPH}],
        "sources": [{"ref": page["lookups"][0]["route"], "state": "exhausted", "reason": "Account page reviewed"},
                    {"ref": answer["lookups"][0]["route"], "state": "exhausted", "reason": "Answer citations reviewed"}]}
    profile = yield "tyche_lookup", lookup("harvestapi_get_profile", {"url": PERSON_URL, "main": "true"}, "contact_verification")
    profile_ref = profile["lookups"][0]["results"][0]["ref"]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "hold_contact", "reason": "Verify selected email",
        "primary_contact": {"ref": profile_ref, "requested_role": "Director of Supply Chain", "role_match": "exact"}}]}
    enriched = yield "tyche_lookup", lookup("harvestapi_get_profile", {"findEmail": "true"}, "contact_discovery", contact_ref=profile_ref)
    profile_ref = enriched["lookups"][0]["results"][0]["ref"]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "hold_contact", "reason": "Select enriched profile",
        "primary_contact": {"ref": profile_ref, "requested_role": "Director of Supply Chain", "role_match": "exact"}}]}
    email = yield "tyche_lookup", lookup("zerobounce_validate", {"email": "ada@example.com"}, "email_validation", contact_ref=profile_ref)
    email_ref = email["lookups"][0]["results"][0]["ref"]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "accept", "reason": "Verified company and current buyer",
        "primary_contact": {"email_ref": email_ref}}]}
    evidence_review = yield "tyche_inspect", {"target": "example.com", "field": "evidence_review"}
    assert evidence_review["sources"][page_ref]["text"].startswith("Example Products manufactures")
    assert evidence_review["sources"][signal_ref]["text"].startswith("On August 12")
    packet = yield "tyche_finish", {}
    final = yield "tyche_finish", {"review_ref": packet["review_ref"]}
    assert final["checkpoint_saved"] and final["delivery_allowed"]



class ProviderFixture:
    def __init__(self):
        self.frames = []
        self.provider_responses = []
        self.raw_envelopes = False

    def provider(self, parameters):
        tool = parameters["tool"]
        data = {
            "harvestapi_get_company": {"status": "ok", "element": {"name": "Example Products",
                "website": "https://example.com", "linkedinUrl": COMPANY_URL,
                "employeeCountRange": {"start": 201, "end": 500},
                "locations": [{"headquarter": True, "country": "United States", "geographicArea": "Ohio"}]}},
            "harvestapi_get_profile": {"status": "ok", "element": {"id": "profile-123", "linkedinUrl": PERSON_URL,
                "firstName": "Ada", "lastName": "Example", "emails": [{"email": "ada@example.com", "status": "valid"}],
                "currentPosition": [{"companyName": "Example Products", "title": "Director of Supply Chain", "companyLinkedinUrl": COMPANY_URL}],
                "location": {"parsed": {"countryFull": "United States", "state": "Ohio", "city": "Columbus"}}}},
            "zerobounce_validate": {"status": "ok", "data": {"address": "ada@example.com", "status": "valid", "sub_status": ""}},
            "exa_answer": {"answer": "Generated summary; review its citations.", "citations": [
                {"id": "citation-1", "url": "https://example.com/news/wms-project", "title": "Warehouse project",
                 "text": "On August 12, 2026, Example Products connected its acquired warehouse to one WMS.",
                 "publishedDate": "2026-08-20"}], "requestId": "exa-request-1"},
            "generic_http_request": {"results": [
                {"url": "https://example.com/about", "text": "Example Products manufactures packaged goods, tools and accessories for retailers.", "date": "2026-08-10"},
                {"url": "https://example.com/news/wms-project", "text": "On August 12, 2026, the company connected its acquired warehouse to one WMS. The project covers inventory visibility and fulfillment.", "date": "2026-08-20"}]},
            "firecrawl_scrape": {"markdown": "Example Products manufactures packaged goods, tools and accessories for retailers.",
                "metadata": {"statusCode": 200, "sourceURL": "https://example.com/about",
                             "url": "https://example.com/about"}}}
        rate = {"harvestapi_get_company": .03, "harvestapi_get_profile": .14, "zerobounce_validate": .28,
                "exa_answer": .07, "generic_http_request": 0, "firecrawl_scrape": .02}[tool]
        if tool == "harvestapi_get_profile" and parameters["payload"].get("main") == "true":
            rate = .03
            data[tool]["element"].pop("emails")
        billing = {"credits_charged": rate, "cost_usd": round(rate * .1, 8)}
        body = {**data[tool], "billing": billing, "request_id": "fixture-request-" + str(len(self.frames))}
        if self.raw_envelopes and tool in {"exa_answer", "firecrawl_scrape", "harvestapi_get_company"}:
            if tool == "harvestapi_get_company":
                raw_data = {"status": 200, "element": data[tool]["element"], "error": None}
            else:
                raw_data = data[tool]
            body = {"status": "completed", "result": {"data": raw_data}, "billing": billing,
                    "job_id": "fixture-job-" + str(len(self.frames))}
        self.provider_responses.append((copy.deepcopy(parameters), copy.deepcopy(body)))
        return body


@pytest.fixture
def lab(tmp_path, monkeypatch):
    fixture = ProviderFixture()
    fixture.processes = []
    fixture.sessions = []
    fixture.research = []
    fixture.mode = "deliver"
    fixture.program = scenario
    fixture.after_program = lambda tools: None
    fixture.worker_starts = 0
    fixture.output = tmp_path / "companies.json"
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "1")
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setenv("LAB_ARENA_OUTPUT_PATH", str(fixture.output))
    monkeypatch.setenv("LAB_ARENA_EVALUATION_DATE", "2026-09-15")
    monkeypatch.delenv("TYCHE_REQUEST_FILE", raising=False)
    original_mkdtemp = runtime.tempfile.mkdtemp
    monkeypatch.setattr(runtime.tempfile, "mkdtemp", lambda **kwargs: original_mkdtemp(prefix="run-", dir=tmp_path))

    def request(self, operation, parameters, *, admitted=False):
        assert operation == "deepline.execute"
        assert admitted is True
        fixture.frames.append(copy.deepcopy(parameters))
        return 200, {}, fixture.provider(parameters)

    monkeypatch.setattr(Broker, "request", request)

    def checkpoint(rows):
        fixture.output.write_text(json.dumps({"companies": rows}))

    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=checkpoint))

    @contextmanager
    def session(**selection):
        fixture.sessions.append(selection)
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text('model_provider = "arena"\n[model_providers.arena]\nwire_api = "responses"\n')
        environment = {"CODEX_HOME": str(codex_home), "HOME": str(codex_home), "PYTHONPATH": "/agent:/agent/source:/agent/deps"}
        try:
            yield environment
        finally:
            fixture.session_closed = True

    fixture.host = SimpleNamespace(session=session, CODEX_BINARY="/usr/local/bin/codex")
    monkeypatch.setattr(runtime, "require_lab", lambda: fixture.host)
    monkeypatch.setattr(runtime.os, "killpg", lambda *args: None)

    class Process:
        pid = 999999
        returncode = 0

        def __init__(self, command, **kwargs):
            fixture.processes.append(self)
            fixture.worker_starts += 1
            self.command, self.kwargs = command, kwargs
            self.stdout = io.BytesIO(b"fixture diagnostics\n")
            self.waited = False
            self.run_dir = Path(command[command.index("-C") + 1])
            config = tomllib.loads((Path(kwargs["env"]["CODEX_HOME"]) / "config.toml").read_text())
            fixture.config = config
            assert config["model_providers"]["arena"]["wire_api"] == "responses"
            assert kwargs["start_new_session"]
            self.prompt = kwargs["stdin"].read()
            assert self.prompt.startswith((b"Research the authoritative", b"Continue the SAME", b"Finalize the SAME"))

        def wait(self, timeout=None):
            if self.waited:
                return self.returncode
            self.waited = True
            if fixture.mode == "early_clean" and fixture.worker_starts == 1:
                return 0
            if fixture.mode in {"clean_noop", "prose"}:
                if fixture.mode == "prose":
                    (self.run_dir / "final.txt").write_text("I delivered all the leads.")
                return 0
            if fixture.mode == "timeout":
                raise subprocess.TimeoutExpired(self.command, timeout)
            if fixture.worker_starts > 1 and fixture.mode != "early_clean":
                self.returncode = 1
                return self.returncode
            arguments = fixture.config["mcp_servers"]["tyche"]["args"]
            tools = LabTools(
                Path(arguments[arguments.index("--run-file") + 1]),
                float(arguments[arguments.index("--deadline") + 1]),
                float(arguments[arguments.index("--response-deadline") + 1]),
            )
            fixture.research.append(tools)
            program = fixture.program()
            command = next(program)
            while True:
                result = tools.call(*command)
                try:
                    command = program.send(result)
                except StopIteration:
                    break
            fixture.after_program(tools)
            if fixture.mode == "partial_timeout":
                raise subprocess.TimeoutExpired(self.command, timeout)
            if fixture.mode == "partial_error":
                self.returncode = 1
            if fixture.mode == "tamper":
                fixture.output.write_text('{"companies": []}')
            return 0

    monkeypatch.setattr(runtime.subprocess, "Popen", Process)
    return fixture


def test_trigger_returns_reviewed_checkpoint_with_codex_configuration(lab):
    from harness import run_icp

    rows = run_icp(ICP)
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert len(rows) == 1
    assert rows[0]["contact"]["email_source"] == {
        "provider": "harvestapi", "tool": "harvestapi_get_profile", "record_id": "profile-123"}
    assert rows[0]["intent_signals"][0]["matched_icp_signal"] == 0
    assert rows[0]["intent_signals"][0]["date"] == "2026-08-12"
    assert lab.sessions == [{"model": "openai/gpt-5.6-luna", "reasoning_effort": "xhigh"}]
    assert "service_tier" not in lab.config
    assert lab.config["mcp_servers"]["tyche"]["required"]
    assert "PYTHONPATH" in lab.config["mcp_servers"]["tyche"]["env_vars"]
    assert "model_catalog_json" not in lab.config  # retain native Codex model behavior
    assert "features.image_generation=false" in lab.processes[0].command
    assert "agents.enabled=false" in lab.processes[0].command
    assert "features.multi_agent_v2=false" in lab.processes[0].command
    assert "model_auto_compact_token_limit" not in lab.config
    assert "model_auto_compact_token_limit_scope" not in lab.config
    assert "tool_output_token_limit" not in lab.config
    assert lab.session_closed and len(lab.processes) == 1
    assert lab.processes[0].command[0] == "/usr/local/bin/codex"
    assert not (lab.research[0].research.path.parent / "leads.xlsx").exists()
    ledger = budget_guard.load_ledger(lab.research[0].research.path)
    assert ledger["calls"] and all(call["actual_credits"] is not None for call in ledger["calls"].values())
    with pytest.raises(ValueError, match="delivered"):
        lab.research[0].call("tyche_review", {})
    with pytest.raises(ValueError, match="initialized"):
        lab.research[0].call("tyche_start", {})


def test_full_delivery_rejects_current_request_drift_after_validation(lab):
    runtime.run(ICP)
    run_file = lab.research[0].research.path
    run_dir = run_file.parent
    assert runtime.full_delivery(run_dir)

    document = json.loads(run_file.read_text())
    request = json.loads(document["request"]["original_text"])
    request["target_roles"] = ["Chief Financial Officer"]
    document["request"]["original_text"] = json.dumps(request, sort_keys=True)
    run_file.write_text(json.dumps(document, indent=2) + "\n")

    assert runtime.full_delivery(run_dir) is False


def test_premature_clean_exit_continues_same_run_inside_one_runtime_session(lab):
    lab.mode = "early_clean"

    rows = runtime.run(ICP)

    assert len(rows) == 1
    assert len(lab.sessions) == 1
    assert len(lab.processes) == 2
    assert lab.processes[0].run_dir == lab.processes[1].run_dir
    assert lab.processes[0].kwargs["env"]["CODEX_HOME"] == lab.processes[1].kwargs["env"]["CODEX_HOME"]
    assert lab.processes[0].prompt.startswith(b"Research the authoritative")
    assert lab.processes[1].prompt.startswith(b"Continue the SAME saved Arena run")


def test_deadline_enters_bounded_finalization_only_in_same_session(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "arena"\n')
    sessions = []

    @contextmanager
    def session(**selection):
        sessions.append(selection)
        yield {"CODEX_HOME": str(codex_home), "PYTHONPATH": "/agent:/agent/source:/agent/deps"}

    host = SimpleNamespace(session=session, CODEX_BINARY="/usr/local/bin/codex")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "results.json").write_text("{}")
    calls = []

    def execute_once(_host, directory, environment, prompt, timeout, _tail):
        calls.append((dict(environment), prompt, timeout))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("codex", timeout)
        return 0

    monkeypatch.setattr(runtime, "progress", lambda _path: {"stop": "continue", "operational_block": None})
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: len(calls) >= 2)
    now = time.monotonic()
    runtime.launch(host, run_dir, now + 1, now + runtime.RUN_SECONDS, runtime.RUN_SECONDS)

    assert len(sessions) == 1 and len(calls) == 2
    assert "TYCHE_FINALIZATION_ONLY" not in calls[0][0]
    assert calls[1][0]["TYCHE_FINALIZATION_ONLY"] == "1"
    assert calls[1][1].startswith("Finalize the SAME saved Arena run")
    assert 0 < calls[1][2] <= runtime.FINALIZATION_SECONDS
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert "TYCHE_FINALIZATION_ONLY" in config["mcp_servers"]["tyche"]["env_vars"]


def test_repeated_clean_noop_exits_are_bounded(lab):
    lab.mode = "clean_noop"

    with pytest.raises(RuntimeError, match="without saved progress"):
        runtime.run(ICP)

    assert len(lab.sessions) == 1
    assert len(lab.processes) == runtime.MAX_UNCHANGED_EXITS


def test_mcp_relaunch_restores_deepline_dispatch_count_from_durable_ledger(lab):
    runtime.run(ICP)
    run_file = lab.research[0].research.path
    ledger = budget_guard.load_ledger(run_file)
    expected = sum(call["provider"] == "deepline" for call in ledger["calls"].values())

    resumed = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)

    assert expected > 0
    assert resumed.broker.local_dispatch_budget()["used"] == expected
    assert resumed.broker.local_dispatch_budget()["remaining"] == DEEPLINE_DISPATCH_LIMIT - expected


def test_mcp_relaunch_restores_transport_uncertainty_without_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    run_file = tmp_path / "run" / "results.json"
    seed = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(request=request_for(ICP, 1, 30), max_usd=.5)
    session = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)

    def transport_lost(*_args, **_kwargs):
        raise BrokerError("fixture transport lost after dispatch")

    monkeypatch.setattr(Broker, "request", transport_lost)
    first = session.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    assert first["lookups"][0]["status"] == "timeout"
    before = budget_guard.load_ledger(run_file)
    assert len(before["calls"]) == 1 and next(iter(before["calls"].values()))["actual_credits"] is None

    resumed = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)
    assert resumed.broker.provider_blocked is True

    def must_not_dispatch(*_args, **_kwargs):
        raise AssertionError("uncertain provider call was replayed")

    monkeypatch.setattr(Broker, "request", must_not_dispatch)
    second = resumed.call("tyche_lookup", lookup(
        "harvestapi_get_company", {"url": "https://www.linkedin.com/company/another-example"}))
    assert second["lookups"][0]["status"] == "config_error"
    assert budget_guard.load_ledger(run_file) == before


def test_mcp_relaunch_does_not_treat_known_http_422_as_transport_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    run_file = tmp_path / "run" / "results.json"
    seed = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(request=request_for(ICP, 1, 30), max_usd=.5)
    session = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)
    dispatched = []

    def known_failure(_self, operation, parameters, *, admitted=False):
        assert operation == "deepline.execute" and admitted is True
        dispatched.append(parameters)
        return 422, {}, {"status": "error", "error": {"code": "invalid_input", "message": "fixture"}}

    monkeypatch.setattr(Broker, "request", known_failure)
    session.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    first_call = next(iter(budget_guard.load_ledger(run_file)["calls"].values()))
    assert first_call["actual_credits"] is None
    resumed = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)

    assert resumed.broker.provider_blocked is False
    resumed.call("tyche_lookup", lookup(
        "harvestapi_get_company", {"url": "https://www.linkedin.com/company/another-example"}))
    assert len(dispatched) == 2
    assert len(budget_guard.load_ledger(run_file)["calls"]) == 2


def test_mcp_relaunch_blocks_paid_research_when_durable_receipt_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    run_file = tmp_path / "run" / "results.json"
    seed = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(request=request_for(ICP, 1, 30), max_usd=.5)
    session = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)

    monkeypatch.setattr(Broker, "request", lambda *_args, **_kwargs: (
        422, {}, {"status": "error", "error": {"code": "invalid_input", "message": "fixture"}}))
    session.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    route_id = next(iter(budget_guard.load_ledger(run_file)["calls"]))
    receipt = run_file.parent / "receipts" / (route_id + ".json")
    receipt.unlink()

    resumed = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)
    assert resumed.broker.provider_blocked is True


def test_finalization_only_native_mcp_refuses_lookup_without_dispatch_or_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    run_file = tmp_path / "run" / "results.json"
    seed = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(request=request_for(ICP, 1, 30), max_usd=.5)
    session = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)
    before = budget_guard.load_ledger(run_file)
    monkeypatch.setenv("TYCHE_FINALIZATION_ONLY", "1")

    with pytest.raises(ValueError, match="Research is closed"):
        session.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))

    assert session.broker.calls == 0
    assert budget_guard.load_ledger(run_file) == before


def test_raw_deepline_results_survive_lookup_review_receipts_and_output_mapping(lab):
    lab.raw_envelopes = True
    lab.program = raw_response_scenario

    rows = runtime.run(ICP)

    assert len(rows) == 1
    assert rows[0]["company_name"] == "Example Products"
    assert rows[0]["intent_signals"][0]["url"] == "https://example.com/news/wms-project"
    assert rows[0]["required_attribute"]["evidence_url"] == "https://example.com/about"
    run_file = lab.research[0].research.path
    receipts = [json.loads(path.read_text()) for path in (run_file.parent / "receipts").glob("*.json")]
    by_tool = {receipt["tool"]: receipt for receipt in receipts if receipt.get("tool")}

    exa = by_tool["exa_answer"]
    assert exa["status"] == "ok" and exa["billing"] == {"credits_charged": .07, "cost_usd": .007}
    assert exa["results"][0]["evidence_text"].startswith("On August 12")
    assert exa["results"][0]["provider_answer"] == "Generated summary; review its citations."
    assert exa["results"][0].get("company") is None and exa["results"][0].get("domain") is None
    assert exa["provider_response"]["body"]["status"] == "completed"
    assert exa["provider_response"]["body"]["result"]["data"]["answer"].startswith("Generated summary")

    page = by_tool["firecrawl_scrape"]
    assert page["status"] == "ok" and page["results"][0]["evidence_url"] == "https://example.com/about"
    assert page["results"][0]["content_format"] == "markdown"
    assert page["provider_response"]["body"]["result"]["data"]["metadata"]["statusCode"] == 200
    assert page["provider_response"]["body"]["result"]["data"]["markdown"].startswith("Example Products manufactures")

    company = by_tool["harvestapi_get_company"]
    assert company["status"] == "ok" and company["results"][0]["company"] == "Example Products"
    assert company["provider_response"]["body"]["result"]["data"]["status"] == 200
    ledger = budget_guard.load_ledger(run_file)
    actual = sorted(float(call["actual_credits"]) for call in ledger["calls"].values())
    assert actual == [.02, .03, .03, .07, .14, .28]


def test_arena_signal_date_preserves_reviewed_precision_without_using_publication_date():
    assert signal_date({"event_date": "2026-08-12", "date": "2026-08-20", "date_basis": "published"}) == "2026-08-12"
    assert signal_date({"event_date": "2026-08", "date": "2026-08-20", "date_basis": "published"}) is None
    assert signal_date({"event_date": "2026", "date": "2026-08-20", "date_basis": "published"}) is None
    assert signal_date({"date": "2026-08-20", "date_basis": "observed_current"}) == "2026-08-20"


@pytest.mark.parametrize("mode,error", [("prose", RuntimeError), ("tamper", ValueError), ("timeout", subprocess.TimeoutExpired)])
def test_failed_or_fabricated_completion_never_returns_leads(lab, mode, error):
    lab.mode = mode
    with pytest.raises(error):
        runtime.run(ICP)
    assert 1 <= len(lab.processes) <= runtime.MAX_UNCHANGED_EXITS and lab.session_closed
    assert (lab.processes[0].run_dir / "failure.json").exists()


def test_stale_evidence_and_wrong_profile_email_block_delivery(lab, monkeypatch):
    runtime.run(ICP)
    run_file = lab.research[0].research.path
    import linkedin_receipts
    original = linkedin_receipts._saved_profile

    def altered(*args, **kwargs):
        profile = original(*args, **kwargs)
        if args[3] == "in":
            profile["emails"] = [{"email": "someone-else@example.com"}]
        return profile

    with monkeypatch.context() as patch:
        patch.setattr(linkedin_receipts, "_saved_profile", altered)
        with pytest.raises(ValueError, match="absent from the selected"):
            companies(run_file, ICP)
    document = json.loads(run_file.read_text())
    document["accepted"][0]["intent_details"] += " Changed after approval."
    run_file.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="current final evidence review"):
        companies(run_file, ICP)


def test_lab_only_guard_blocks_local_execution_before_starting(monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", Path("/tmp/local-tyche"))
    with pytest.raises(RuntimeError, match="only in the Leadpoet lab"):
        runtime.run(ICP)


def test_generated_primary_bonus_order_and_age_limits(tmp_path):
    import research_input

    extra = {"intent_signal": "Opened a distribution center", "max_age_days": 30}
    icp = {
        **ICP,
        "prompt": "Find matching accounts without adding this prose as a requirement.",
        "bonus_intents": ICP["bonus_intents"] + [extra],
    }
    request = request_for(icp, 2, 2640)
    assert [s["importance"] for s in request["buying_signals"]] == ["required", "preferred", "preferred"]
    assert [s["max_age_days"] for s in request["buying_signals"]] == [365, 90, 30]
    assert [s["kind"] for s in request["buying_signals"]] == ["arena_signal_0", "arena_signal_1", "arena_signal_2"]
    assert json.loads(request["original_text"]) == icp
    assert "custom_criteria" not in request["icp"]
    assert icp["prompt"] not in request["icp"].get("required_attributes", [])
    assert research_input.normalize_request(request, tmp_path / "results.json")
    assert request["signal_match_mode"] == "all"


def test_advertised_mcp_contract_fits_pr198_structural_bounds():
    from tyche_tools import serve

    incoming = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
    outgoing = io.StringIO()
    serve(SimpleNamespace(), incoming, outgoing, tools=LAB_TOOLS)
    tools = json.loads(outgoing.getvalue())["result"]["tools"]
    assert {t["name"] for t in tools} == {"tyche_lookup", "tyche_review", "tyche_inspect", "tyche_finish", "tyche_checkpoint"}
    request = {"model": runtime.MODEL, "input": "Research", "tools": [
        {"type": "function", "name": t["name"], "parameters": t["inputSchema"]} for t in tools]}

    def bounds(value, depth=0):
        assert depth <= 12
        if isinstance(value, dict):
            assert len(value) <= 64
            for child in value.values():
                bounds(child, depth + 1)
        elif isinstance(value, list):
            assert len(value) <= 128
            for child in value:
                bounds(child, depth + 1)
    bounds(request)
    assert len(json.dumps(request).encode()) < 1_000_000
    assert len(runtime.instructions()) < 32000
    assert "web" not in LAB_TOOLS["tyche_review"][1]["properties"]
    preview = model_result({"status": "review_required", "review_ref": "abc", "text": "x" * 40000})
    assert preview["truncated"] and preview["review_ref"] == "abc"
    assert "incomplete" in preview["next"]


def test_model_result_truncation_preserves_local_budget_metadata():
    budget = {"scope": "local_adapter_dispatch_count", "used": 2, "limit": 30, "remaining": 28,
              "authoritative_billing": False}
    preview = model_result({"status": "review_required", "text": "x" * 40000}, budget)
    assert preview["truncated"] is True
    assert preview["arena_budget"] == budget


def test_local_dispatch_budget_is_lock_protected_and_session_local(tmp_path):
    first = Broker(tmp_path / "first.sock", time.monotonic() + 30)
    second = Broker(tmp_path / "second.sock", time.monotonic() + 30)
    first.calls = 7
    assert first.local_dispatch_budget()["used"] == 7
    assert first.local_dispatch_budget()["remaining"] == DEEPLINE_DISPATCH_LIMIT - 7
    assert second.local_dispatch_budget()["used"] == 0
    assert second.local_dispatch_budget()["remaining"] == DEEPLINE_DISPATCH_LIMIT
    assert first.local_dispatch_budget()["authoritative_billing"] is False


@pytest.mark.parametrize("name,arguments", [("tyche_inspect", {}), ("tyche_checkpoint", {})])
def test_every_lab_tool_return_includes_local_dispatch_budget(name, arguments):
    tools = LabTools.__new__(LabTools)
    tools.lock = threading.Lock()
    tools.delivered = False
    budget = {"scope": "local_adapter_dispatch_count", "used": 3, "limit": 30, "remaining": 27,
              "authoritative_billing": False}
    tools.broker = SimpleNamespace(local_dispatch_budget=lambda: budget)
    tools.research = SimpleNamespace(call=lambda tool, payload: {"status": "ok"})
    tools.checkpoint = lambda **payload: {"status": "checkpoint_saved"}
    assert tools.call(name, arguments)["arena_budget"] == budget


def test_runtime_explains_fixed_arena_limits_without_guessing_openrouter_remaining():
    guidance = runtime.instructions()
    assert "60 OpenRouter and 30 Deepline dispatches per attempt" in guidance
    assert "failures and transparent free 429 retries consume OpenRouter slots" in guidance
    assert "Exact OpenRouter remaining capacity is unavailable" in guidance
    assert "local Deepline adapter dispatch count" in guidance
    assert "not authoritative billing" in guidance


def test_provider_deadlines_quotas_and_no_model_fallback(tmp_path):
    broker = Broker(tmp_path / "missing.sock", time.monotonic() - 1)
    args = {"tool": "harvestapi_get_company", "payload": {}}
    with pytest.raises(BrokerRefusal, match="deadline"):
        broker.request("deepline.execute", args)
    broker.deadline = time.monotonic() + 30
    broker.calls = DEEPLINE_DISPATCH_LIMIT
    with pytest.raises(BrokerRefusal, match="quota"):
        broker.request("deepline.execute", args)
    for operation in ("openrouter.chat", "openrouter.responses"):
        with pytest.raises(ValueError, match="Unsupported"):
            broker.request(operation, {})
    broker.calls = 0
    tools = ResearchTools(tmp_path / "research/results.json", execute=broker.execute)
    tools.start(request=request_for(ICP, 1, 20), max_usd=.5)
    failed = tools.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    assert failed["lookups"][0]["status"] == "timeout"
    assert all(call["actual_credits"] is None for call in budget_guard.load_ledger(tools.path)["calls"].values())
    with pytest.raises(BrokerRefusal, match="blocked_after_uncertain"):
        broker.request("deepline.execute", args)


@pytest.mark.parametrize("reason", ["deadline", "quota", "stopped"])
def test_local_predispatch_refusal_has_no_native_reservation(tmp_path, reason):
    broker = Broker(tmp_path / "missing.sock", time.monotonic() + 30)
    if reason == "deadline":
        broker.deadline = time.monotonic() - 1
    elif reason == "quota":
        broker.calls = DEEPLINE_DISPATCH_LIMIT
    else:
        broker.stopped.set()
    tools = ResearchTools(tmp_path / "research/results.json", execute=broker.execute)
    tools.start(request=request_for(ICP, 1, 20), max_usd=.5)
    result = tools.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    document = json.loads(tools.path.read_text())
    route = document["routes"][-1]
    assert route["paid_calls"] == 0
    assert budget_guard.load_ledger(tools.path)["calls"] == {}
    assert result["lookups"][0]["status"] == (
        "quota_exceeded" if reason == "quota" else "config_error"
    )


def test_native_budget_refusal_releases_the_local_dispatch_slot(tmp_path):
    broker = Broker(tmp_path / "must-not-connect.sock", time.monotonic() + 30)
    captured = []
    result, code = broker.execute({
        "operation": "execute",
        "tool": "harvestapi_get_company",
        "payload": {"url": COMPANY_URL},
        # No spend binding: the native budget guard must reject before dispatch.
    }, captured.append)
    assert code == 2
    assert result["status"] == "quota_exceeded"
    assert result["request_sent"] is False
    assert broker.calls == 0
    assert captured == []


def test_admitted_call_uses_response_deadline_after_research_closes(monkeypatch):
    from tyche_arena import broker

    clock = [9.0]
    provider_body = json.dumps({"status": "ok", "results": []}).encode()
    reply = json.dumps({"status": 200, "headers": {},
                        "body_b64": base64.b64encode(provider_body).decode()}).encode()

    class Connection:
        def __init__(self):
            self.reply = len(reply).to_bytes(4, "big") + reply

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, value):
            assert value > 0

        def connect(self, _path):
            clock[0] = 15.0

        def sendall(self, frame):
            size = int.from_bytes(frame[:4], "big")
            sent = json.loads(frame[4:4 + size])
            assert sent["timeout_ms"] == 60_000

        def recv(self, size):
            part, self.reply = self.reply[:size], self.reply[size:]
            return part

    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker.socket, "socket", lambda *_args: Connection())
    instance = Broker("/tmp/fixture.sock", 10.0, response_deadline=100.0,
                      catalog={"harvestapi_get_company": {}})
    assert instance.request("deepline.execute", {
        "tool": "harvestapi_get_company", "payload": {},
    }) == (200, {}, {"status": "ok", "results": []})
    assert instance.calls == 1 and clock[0] > instance.deadline


def test_local_dispatch_limit_is_atomic_under_parallel_admission():
    instance = Broker("/tmp/fixture.sock", time.monotonic() + 30)

    def admit(_index):
        try:
            instance._admit()
            return "admitted"
        except BrokerRefusal:
            return "refused"

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(admit, range(DEEPLINE_DISPATCH_LIMIT + 2)))
    assert outcomes.count("admitted") == DEEPLINE_DISPATCH_LIMIT
    assert outcomes.count("refused") == 2
    assert instance.calls == DEEPLINE_DISPATCH_LIMIT


@pytest.mark.parametrize("reason", ["deadline", "quota"])
def test_no_send_refusal_preserves_native_finish_semantics(lab, monkeypatch, reason):
    """A no-send receipt fixes accounting; native stop policy still decides delivery."""

    from datetime import datetime, timedelta
    from harness import run_icp
    import run_attempt
    import validate_run

    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario(None)

    def refuse_then_finish(tools):
        before = budget_guard.load_ledger(tools.research.path)
        if reason == "deadline":
            tools.broker.deadline = time.monotonic() - 1
        else:
            tools.broker.calls = DEEPLINE_DISPATCH_LIMIT
        refused = tools.call("tyche_lookup", lookup(
            "harvestapi_get_company", {"url": "https://www.linkedin.com/company/late-example"}))
        assert refused["lookups"][0]["status"] == (
            "config_error" if reason == "deadline" else "quota_exceeded")
        after = budget_guard.load_ledger(tools.research.path)
        assert after["calls"] == before["calls"]
        route = json.loads(tools.research.path.read_text())["routes"][-1]
        assert route["paid_calls"] == 0

        started = datetime.fromisoformat(
            json.loads(tools.research.path.read_text())["stop_check"]["started_at"].replace("Z", "+00:00"))
        finished = started + timedelta(seconds=runtime.RESEARCH_SECONDS + 1)
        real_datetime = validate_run.datetime

        class FinishedClock(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return finished if tz is None else finished.astimezone(tz)

        assert run_attempt.evaluate_stop is validate_run.evaluate_stop
        monkeypatch.setattr(validate_run, "datetime", FinishedClock)
        packet = tools.call("tyche_finish", {})
        if reason == "quota":
            assert packet["status"] == "operationally_blocked"
            assert packet["delivery_allowed"] is False
            assert not lab.output.exists()
            return
        assert packet["status"] == "review_required", json.dumps(packet, sort_keys=True)
        delivered = tools.call("tyche_finish", {"review_ref": packet["review_ref"]})
        assert delivered["checkpoint_saved"] and delivered["delivery_allowed"]

    lab.after_program = refuse_then_finish
    if reason == "quota":
        with pytest.raises(RuntimeError, match="operationally blocked"):
            run_icp(ICP)
    else:
        assert len(run_icp(ICP)) == 1


def test_trickled_response_uses_one_absolute_wait_limit(monkeypatch):
    from tyche_arena import broker

    clock = iter([0.0, 0.4, 0.8, 1.2])
    monkeypatch.setattr(broker.time, "monotonic", lambda: next(clock))
    connection = SimpleNamespace(settimeout=lambda value: None, recv=lambda size: b"x")
    with pytest.raises(TimeoutError, match="wait limit"):
        Broker._receive(connection, 4, deadline=1.0)


def test_connect_time_does_not_extend_the_provider_send_deadline(monkeypatch):
    from tyche_arena import broker

    now = [0.0]
    monkeypatch.setattr(broker.time, "monotonic", lambda: now[0])

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            self.timeout = value

        def connect(self, path):
            now[0] = 120.0

        def sendall(self, frame):
            assert self.timeout == 5.0
            raise TimeoutError("fixture send exceeded remaining time")

    monkeypatch.setattr(broker.socket, "socket", lambda *args: Connection())
    with pytest.raises(BrokerError, match="do not retry"):
        Broker("/tmp/fixture.sock", 1000).request("deepline.execute", {"tool": "exa_search", "payload": {}})


def test_runtime_requires_the_pinned_lab_helper(monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", Path("/agent/source"))
    monkeypatch.setattr(Path, "is_socket", lambda self: True)
    monkeypatch.setattr(runtime.os, "access", lambda *args: True)
    for name in ("LAB_ARENA_WORKER_SOCKET", "LAB_ARENA_WEB_EGRESS_SOCKET"):
        monkeypatch.setenv(name, "/run/lab_arena/" + name + ".sock")
    monkeypatch.setenv("LAB_ARENA_OUTPUT_PATH", "/output/companies.json")
    helper = SimpleNamespace(__file__="/agent/lab_arena_codex.py", CODEX_VERSION=runtime.CODEX_VERSION,
                             CODEX_BINARY="/usr/local/bin/codex", session=lambda **kwargs: None)
    checkpoint = SimpleNamespace(__file__="/agent/lab_arena_checkpoint.py")
    monkeypatch.setattr(runtime.importlib, "import_module", lambda name: helper if name == "lab_arena_codex" else checkpoint)
    assert runtime.require_lab() is helper
    helper.CODEX_VERSION = "different-version"
    with pytest.raises(RuntimeError, match="unavailable"):
        runtime.require_lab()
    helper.CODEX_VERSION = runtime.CODEX_VERSION
    helper.__file__ = "/tmp/copied-lab-helper.py"
    with pytest.raises(RuntimeError, match="unavailable"):
        runtime.require_lab()


def test_mcp_exits_when_its_parent_dies_despite_separate_process_group(tmp_path):
    # Real Python processes, no Codex/Leadpoet/network. Reproduce Codex 0.154.0's
    # process_group(0) MCP launcher, then abruptly kill the owning process.
    marker = tmp_path / "child.json"
    child_code = "\n".join([
        "import json, os, sys, threading, time",
        "from pathlib import Path",
        "from tyche_arena.mcp import watch_parent",
        "parent = os.getppid()",
        "threading.Thread(target=watch_parent, args=(parent, threading.Event()), daemon=True).start()",
        "Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp(), 'parent': parent}))",
        "time.sleep(90)",
    ])
    parent_code = "\n".join([
        "import subprocess, sys, time",
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], start_new_session=True)",
        "time.sleep(90)",
    ])
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    parent = subprocess.Popen([sys.executable, "-c", parent_code, child_code, str(marker)],
                              env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child = None
    try:
        # Startup is not the behavior under test; allow a loaded CI/desktop
        # host to initialize Python within the production MCP startup budget.
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists(), "fixture child did not initialize"
        child = json.loads(marker.read_text())
        assert child["parent"] == parent.pid and child["pgid"] == child["pid"]
        parent.kill()
        # The orphan holds both inherited pipes until watch_parent exits it.
        stdout, stderr = parent.communicate(timeout=5)
        assert not stderr, stderr.decode()
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        if child:
            try:
                os.kill(child["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        parent.stdout.close()
        parent.stderr.close()


def test_checkpoint_failure_never_reports_delivery(lab, monkeypatch):
    def failed_write(rows):
        raise OSError("fixture output mount is unavailable")

    monkeypatch.setattr(sys.modules["lab_arena_checkpoint"], "write", failed_write)
    with pytest.raises(OSError, match="output mount"):
        runtime.run(ICP)
    assert not lab.output.exists()
    assert lab.research[0].delivered is False


def test_final_review_has_time_for_two_brokered_model_responses():
    # PR #198's complete model socket wait can be 185s per turn. Final packet
    # review and approval must both fit after research stops on a shortfall.
    assert runtime.RUN_SECONDS - runtime.RESEARCH_SECONDS >= 2 * 185 + 30


@pytest.mark.parametrize("mode", ["partial_timeout", "partial_error", "deliver"])
def test_partial_checkpoint_survives_unfinished_research(lab, monkeypatch, mode):
    from harness import run_icp

    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario("tyche_checkpoint")
    lab.mode = mode

    def continue_research(tools):
        assert not tools.delivered
        before = json.loads(lab.output.read_text())
        # A real tool review creates a new, unfinished candidate after delivery.
        tools.call("tyche_review", {"companies": [{"target": "unfinished.example.com",
            "decision": "hold_account", "reason": "Still checking the required signal"}]})
        saved = json.loads(tools.research.path.read_text())
        assert saved["request"]["target_count"] == 5
        assert saved["unresolved"]
        assert tools.call("tyche_finish", {})["delivery_allowed"] is False
        assert json.loads(lab.output.read_text()) == before
        # An interrupted subsequent reservation must not discard the checkpoint.
        with budget_guard.transaction(tools.research.path.with_name("results.json.budget.json")) as ledger:
            ledger["blocked"] = "Later call billing is uncertain; do not retry"

    lab.after_program = continue_research
    rows = run_icp(ICP)
    assert len(rows) == 1
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert json.loads(lab.research[0].research.path.read_text())["request"]["target_count"] == 5
    # A partial checkpoint is not full delivery, so the supervisor continues;
    # this fixture's later workers fail and the outer run records that failure.
    assert (lab.processes[0].run_dir / "failure.json").exists()
    assert 1 <= len(lab.processes) <= 3 and lab.session_closed


def test_accepted_but_unreviewed_leads_are_not_checkpointed(lab, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario(None)
    lab.mode = "partial_timeout"
    with pytest.raises(RuntimeError, match="failed twice"):
        runtime.run(ICP)
    assert not lab.output.exists()
    assert len(json.loads(lab.research[0].research.path.read_text())["accepted"]) == 1


def test_checkpoint_needs_current_review_and_can_be_updated(lab, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario(None)

    def approve_incrementally(tools):
        packet = tools.call("tyche_checkpoint", {})
        assert packet["status"] == "review_required"
        assert packet["companies"] and packet["sources"]
        assert not lab.output.exists()
        def unexpected_rebuild(*args):
            raise AssertionError("Do not rebuild an unchanged review packet")
        with monkeypatch.context() as unchanged:
            unchanged.setattr(tools.research, "_company_review", unexpected_rebuild)
            repeated = tools.call("tyche_checkpoint", {})
        assert repeated["unchanged"] and repeated["review_ref"] == packet["review_ref"]
        assert not repeated["delivery_allowed"] and not repeated.get("checkpoint_saved")
        assert "companies" not in repeated and not lab.output.exists()
        changed = PARAGRAPH + " Better coordination may support fulfillment reliability."
        tools.call("tyche_review", {"companies": [{"target": "example.com", "decision": "accept",
            "reason": "Clarified the conditional relevance", "intent_details": changed}]})
        fresh = tools.call("tyche_checkpoint", {"review_ref": packet["review_ref"]})
        assert fresh["status"] == "review_required" and fresh["review_ref"] != packet["review_ref"]
        assert not lab.output.exists()
        saved = tools.call("tyche_checkpoint", {"review_ref": fresh["review_ref"]})
        assert saved["checkpoint_saved"] and not saved["delivery_allowed"]
        assert json.loads(lab.output.read_text())["companies"][0]["intent_details"] == changed
        tools.call("tyche_review", {"companies": [{"target": "example.com", "decision": "accept",
            "reason": "Use the concise reviewed description", "intent_details": PARAGRAPH}]})
        newer = tools.call("tyche_checkpoint", {})
        assert newer["status"] == "review_required"
        # Unapproved revisions do not overwrite a published checkpoint.
        assert json.loads(lab.output.read_text())["companies"][0]["intent_details"] == changed
        assert tools.call("tyche_checkpoint", {"review_ref": newer["review_ref"]})["checkpoint_saved"]

    lab.after_program = approve_incrementally
    assert runtime.run(ICP)[0]["intent_details"] == PARAGRAPH


@pytest.mark.parametrize("corruption", ["contact", "evidence", "provenance"])
def test_partial_checkpoint_preserves_qualification_and_contact_gates(lab, monkeypatch, corruption):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario(None)

    def corrupt(tools):
        path = tools.research.path
        saved = json.loads(path.read_text())
        row = saved["accepted"][0]
        if corruption == "contact":
            row["primary_contact"].pop("email")
        elif corruption == "evidence":
            row["qualification_checks"][0]["status"] = "unknown"
        else:
            row["primary_contact"]["email"] = "someone-else@example.com"
        path.write_text(json.dumps(saved))
        result = tools.call("tyche_checkpoint", {})
        assert result["status"] == "needs_repair", result
        assert not result["checkpoint_saved"]
        assert not lab.output.exists()

    lab.after_program = corrupt
    with pytest.raises(RuntimeError, match="failed twice"):
        runtime.run(ICP)


def test_failed_partial_checkpoint_keeps_previous_host_output(lab, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario("tyche_checkpoint")

    def failed_update(tools):
        previous = lab.output.read_bytes()
        local_previous = tools.research.path.with_name("companies.json").read_bytes()
        snapshot = tools.research.path.with_name("checkpoint-results.json").read_bytes()
        tools.call("tyche_review", {"companies": [{"target": "example.com", "decision": "accept",
            "reason": "Clarified the relevance", "intent_details": PARAGRAPH + " Better coordination may help."}]})
        packet = tools.call("tyche_checkpoint", {})
        def fail(rows):
            raise OSError("fixture output mount is unavailable")
        tools.write_checkpoint = fail
        with pytest.raises(OSError, match="output mount"):
            tools.call("tyche_checkpoint", {"review_ref": packet["review_ref"]})
        assert lab.output.read_bytes() == previous
        assert tools.research.path.with_name("companies.json").read_bytes() == local_previous
        assert tools.research.path.with_name("checkpoint-results.json").read_bytes() == snapshot
        assert not tools.delivered

    lab.after_program = failed_update
    assert len(runtime.run(ICP)) == 1

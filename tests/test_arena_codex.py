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


class IdleEnvironment(dict):
    def wait_idle(self, timeout_seconds):
        assert timeout_seconds > 0
        return True


class QuotaUnavailable(RuntimeError):
    pass


def quota_snapshot(*, used=0, inflight=0, openrouter_limit=200):
    return {
        "schema_version": "leadpoet.lab_arena.quota_snapshot.v1",
        "providers": {
            name: {"limit": limit, "used": used if name == "openrouter" else 0,
                   "remaining": limit - used if name == "openrouter" else limit,
                   "inflight": inflight if name == "openrouter" else 0}
            for name, limit in (("scrapingdog", 30), ("deepline", 30), ("openrouter", openrouter_limit))
        },
    }


def quota_guard(deadline, response_deadline, reader=lambda: quota_snapshot(), *, clock=time.monotonic):
    guard = runtime.ArenaQuotaGuard(reader, QuotaUnavailable, deadline, response_deadline, clock=clock)
    guard.preflight()
    return guard


@pytest.fixture(autouse=True)
def no_host_quota_cache_delay(monkeypatch):
    """Most model tests exercise behavior after an already-fresh snapshot."""
    monkeypatch.setattr(runtime, "QUOTA_SNAPSHOT_FRESHNESS_SECONDS", 0)


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
    fixture.openrouter_used = 0
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

    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=checkpoint, quota_usage=lambda: quota_snapshot(used=fixture.openrouter_used),
        QuotaUnavailable=QuotaUnavailable,
    ))

    @contextmanager
    def session(**selection):
        fixture.request_guard = selection.pop("request_guard")
        fixture.sessions.append(selection)
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text('model_provider = "arena"\n[model_providers.arena]\nwire_api = "responses"\n')
        environment = IdleEnvironment(CODEX_HOME=str(codex_home), HOME=str(codex_home), PYTHONPATH="/agent:/agent/source:/agent/deps")
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
            if not fixture.request_guard():
                self.returncode = 1
                return self.returncode
            fixture.openrouter_used += 1
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


def test_arena_handoff_uses_fresh_labtools_without_research_reset(lab):
    lab.program = lambda: scenario(None)

    def finish_in_fresh_context(tools):
        tools.research.environment["TYCHE_FINALIZATION_ONLY"] = "0"
        original_review = tools.research.review_delivery
        def fail_review(*_args, **_kwargs):
            raise RuntimeError("checkpoint review fixture failure")
        tools.research.review_delivery = fail_review
        with pytest.raises(RuntimeError, match="checkpoint review fixture failure"):
            tools.checkpoint()
        assert tools.research.environment["TYCHE_FINALIZATION_ONLY"] == "0"
        tools.research.review_delivery = original_review

        ledger_before = budget_guard.ledger_path(tools.research.path).read_bytes()
        calls_before = len(lab.frames)
        handoff = tools.call("tyche_finish", {})
        assert handoff["status"] == "review_handoff"
        assert handoff["delivery_allowed"] is False
        assert not lab.output.exists()
        assert len(lab.frames) == calls_before
        assert budget_guard.ledger_path(tools.research.path).read_bytes() == ledger_before

        fresh = LabTools(tools.research.path, tools.broker.deadline, tools.broker.response_deadline)
        fresh.research.environment["TYCHE_FINALIZATION_ONLY"] = "1"
        assert fresh.broker.local_dispatch_budget()["used"] == tools.broker.local_dispatch_budget()["used"]
        packet = fresh.call("tyche_finish", {})
        assert packet["status"] == "review_required"
        delivered = fresh.call("tyche_finish", {"review_ref": packet["review_ref"]})
        assert delivered["checkpoint_saved"] and delivered["delivery_allowed"]
        assert len(lab.frames) == calls_before
        assert budget_guard.ledger_path(tools.research.path).read_bytes() == ledger_before

    lab.after_program = finish_in_fresh_context
    assert len(runtime.run(ICP)) == 1
    assert lab.output.exists()


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


def test_launch_recovers_completed_attempt_before_continuation_without_new_provider_call(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    fixture = ProviderFixture()
    provider_calls = []

    def request_call(_self, operation, parameters, *, admitted=False):
        assert operation == "deepline.execute" and admitted is True
        provider_calls.append(copy.deepcopy(parameters))
        return 200, {}, fixture.provider(parameters)

    monkeypatch.setattr(Broker, "request", request_call)
    run_dir = tmp_path / "run"
    run_file = run_dir / "results.json"
    seed = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(request=request_for(ICP, 1, 30), max_usd=.5)
    tools = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)
    tools.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    route_id = next(iter(budget_guard.load_ledger(run_file)["calls"]))
    receipt = run_file.parent / "receipts" / (route_id + ".json")
    ledger_before, receipt_before = budget_guard.ledger_path(run_file).read_bytes(), receipt.read_bytes()
    document = json.loads(run_file.read_text())
    started_at = document["stop_check"]["started_at"]
    document["routes"] = [route for route in document["routes"] if route["route_id"] != route_id]
    existing_route_ids = {route["route_id"] for route in document["routes"]}
    run_file.write_text(json.dumps(document, indent=2) + "\n")
    provider_count = len(provider_calls)

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    model_calls = []

    @contextmanager
    def host_session(**_selection):
        yield IdleEnvironment(CODEX_HOME=str(home))

    def execute_once(_host, _directory, environment, prompt, timeout, _tail):
        recovered = json.loads(run_file.read_text())
        assert {route["route_id"] for route in recovered["routes"]} == existing_route_ids | {route_id}
        model_calls.append((dict(environment), prompt, timeout))
        return 0

    monkeypatch.setattr(runtime.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: bool(model_calls))
    host = SimpleNamespace(session=host_session, CODEX_BINARY="codex")
    runtime.launch(host, run_dir, 130.0, 160.0, 60.0,
                   quota_guard(130.0, 160.0, clock=lambda: 100.0))

    recovered = json.loads(run_file.read_text())
    assert recovered["stop_check"]["started_at"] == started_at
    assert len(model_calls) == 1 and model_calls[0][2] == 60.0
    assert len(provider_calls) == provider_count
    assert budget_guard.ledger_path(run_file).read_bytes() == ledger_before
    assert receipt.read_bytes() == receipt_before


def test_launch_recovers_independent_complete_attempt_but_blocks_pending_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    fixture = ProviderFixture()
    provider_calls = []

    def request_call(_self, operation, parameters, *, admitted=False):
        assert operation == "deepline.execute" and admitted is True
        provider_calls.append(copy.deepcopy(parameters))
        return 200, {}, fixture.provider(parameters)

    monkeypatch.setattr(Broker, "request", request_call)
    run_dir = tmp_path / "run"
    run_file = run_dir / "results.json"
    seed = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(request=request_for(ICP, 1, 30), max_usd=.5)
    tools = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)
    tools.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    tools.call("tyche_lookup", lookup("harvestapi_get_company", {
        "url": "https://www.linkedin.com/company/another-example"}))
    route_ids = list(budget_guard.load_ledger(run_file)["calls"])
    assert len(route_ids) == 2
    document = json.loads(run_file.read_text())
    document["routes"] = [route for route in document["routes"] if route["route_id"] not in route_ids]
    existing_route_ids = {route["route_id"] for route in document["routes"]}
    run_file.write_text(json.dumps(document, indent=2) + "\n")
    pending_receipt = run_file.parent / "receipts" / (route_ids[1] + ".json")
    pending = json.loads(pending_receipt.read_text())
    pending["receipt_status"] = "response_received"
    pending_receipt.write_text(json.dumps(pending, indent=2) + "\n")
    ledger_before = budget_guard.ledger_path(run_file).read_bytes()
    receipts_before = {rid: (run_file.parent / "receipts" / (rid + ".json")).read_bytes()
                       for rid in route_ids}
    provider_count = len(provider_calls)

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')

    @contextmanager
    def host_session(**_selection):
        yield IdleEnvironment(CODEX_HOME=str(home))

    monkeypatch.setattr(runtime, "_codex_once",
                        lambda *_args, **_kwargs: pytest.fail("model continuation must stay blocked"))
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: False)
    now = time.monotonic()
    with pytest.raises(RuntimeError, match="saved dispatch accounting is incomplete"):
        runtime.launch(SimpleNamespace(session=host_session, CODEX_BINARY="codex"), run_dir,
                       now + 30, now + 60, 60, quota_guard(now + 30, now + 60))

    recovered_routes = {route["route_id"] for route in json.loads(run_file.read_text())["routes"]}
    assert recovered_routes == existing_route_ids | {route_ids[0]}
    assert route_ids[1] not in recovered_routes
    assert len(provider_calls) == provider_count
    assert budget_guard.ledger_path(run_file).read_bytes() == ledger_before
    assert {rid: (run_file.parent / "receipts" / (rid + ".json")).read_bytes()
            for rid in route_ids} == receipts_before


def test_deadline_enters_bounded_finalization_only_in_same_session(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "arena"\n')
    sessions = []

    @contextmanager
    def session(**selection):
        sessions.append(selection)
        yield IdleEnvironment(CODEX_HOME=str(codex_home), PYTHONPATH="/agent:/agent/source:/agent/deps")

    host = SimpleNamespace(session=session, CODEX_BINARY="/usr/local/bin/codex")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "results.json").write_text("{}")
    calls = []
    clock = [time.monotonic()]

    def execute_once(_host, directory, environment, prompt, timeout, _tail):
        calls.append((dict(environment), prompt, timeout))
        if len(calls) == 1:
            clock[0] += 2
            raise subprocess.TimeoutExpired("codex", timeout)
        return 0

    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime, "progress", lambda _path: {"stop": "continue", "operational_block": None})
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: len(calls) >= 2)
    now = clock[0]
    runtime.launch(host, run_dir, now + 1, now + runtime.RUN_SECONDS, runtime.RUN_SECONDS,
                   quota_guard(now + 1, now + runtime.RUN_SECONDS, clock=lambda: clock[0]))

    assert len(sessions) == 1 and len(calls) == 2
    assert calls[0][0]["TYCHE_FINALIZATION_ONLY"] == "0"
    assert calls[1][0]["TYCHE_FINALIZATION_ONLY"] == "1"
    assert calls[1][1].startswith("Finalize the SAME saved Arena run")
    assert "tyche_finish before individual field inspections" in calls[1][1]
    assert "Assess exact requirements from the source passages before editing prose" in calls[1][1]
    assert "Start with tyche_inspect" not in calls[1][1]
    assert 0 < calls[1][2] <= runtime.FINALIZATION_SECONDS
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert "TYCHE_FINALIZATION_ONLY" in config["mcp_servers"]["tyche"]["env_vars"]


def test_review_demotion_resumes_same_run_before_research_deadline(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "arena"\n')

    @contextmanager
    def session(**selection):
        yield IdleEnvironment(CODEX_HOME=str(codex_home), PYTHONPATH="/agent:/agent/source:/agent/deps")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "results.json").write_text("{}")
    calls = []
    states = iter((
        {"stop": "target_met", "operational_block": None},
        {"stop": "continue", "operational_block": None},
    ))

    def execute_once(_host, directory, environment, prompt, timeout, _tail):
        calls.append((dict(environment), prompt, timeout))
        return 0

    monkeypatch.setattr(runtime, "progress", lambda _path: next(states))
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: len(calls) >= 2)
    now = time.monotonic()
    host = SimpleNamespace(session=session, CODEX_BINARY="/usr/local/bin/codex")
    runtime.launch(host, run_dir, now + 120, now + 420, 420,
                   quota_guard(now + 120, now + 420))

    assert len(calls) == 2
    assert calls[0][0]["TYCHE_FINALIZATION_ONLY"] == "1"
    assert "save it and return" in calls[0][1]
    assert 0 < calls[0][2] <= 420
    assert calls[1][0]["TYCHE_FINALIZATION_ONLY"] == "0"
    assert calls[1][1].startswith("Continue the SAME saved Arena run")


def test_missing_idle_wait_fails_before_starting_codex(lab, monkeypatch):
    monkeypatch.delattr(IdleEnvironment, "wait_idle")
    with pytest.raises(RuntimeError, match="passive idle-wait support"):
        runtime.run(ICP)
    assert not lab.processes and not lab.frames and not lab.output.exists()


def test_quota_preflight_failure_keeps_local_state_but_makes_no_provider_call(lab, monkeypatch):
    def unavailable():
        raise QuotaUnavailable("quota unavailable")

    monkeypatch.setattr(sys.modules["lab_arena_checkpoint"], "quota_usage", unavailable)
    with pytest.raises(RuntimeError, match="quota snapshot unavailable"):
        runtime.run(ICP)
    assert not lab.processes and not lab.frames and not lab.output.exists()
    runs = list(lab.output.parent.glob("run-*/results.json"))
    assert len(runs) == 1
    assert json.loads(runs[0].read_text())["request"]["max_duration_seconds"] == runtime.RESEARCH_SECONDS


def test_slow_quota_preflight_preserves_native_and_host_deadlines(lab, monkeypatch):
    clock = [100.0]
    native_starts, launches, preflights = [], [], []
    original_start = ResearchTools.start
    original_launch = runtime.launch

    def start(tools, *args, **kwargs):
        native_starts.append(clock[0])
        return original_start(tools, *args, **kwargs)

    reads = [0]

    def quota_usage():
        if reads[0] == 0:
            preflights.append({"clock": clock[0], "provider_frames": len(lab.frames)})
            assert list(lab.output.parent.glob("run-*/results.json"))
            clock[0] += 35.0
        reads[0] += 1
        return quota_snapshot(used=lab.openrouter_used)

    def launch(host, run_dir, deadline, response_deadline, remaining, guard):
        document = json.loads((run_dir / "results.json").read_text())
        launches.append((clock[0], deadline, response_deadline, remaining,
                         document["request"]["max_duration_seconds"]))
        return original_launch(host, run_dir, deadline, response_deadline, remaining, guard)

    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ResearchTools, "start", start)
    monkeypatch.setattr(sys.modules["lab_arena_checkpoint"], "quota_usage", quota_usage)
    monkeypatch.setattr(runtime, "launch", launch)

    rows = runtime.run(ICP)

    assert len(rows) == 1
    assert native_starts == [100.0]
    assert preflights == [{"clock": 100.0, "provider_frames": 0}]
    assert launches == [(135.0, 2170.0, 2770.0, 2635.0, 2070)]
    assert reads[0] >= 2


@pytest.mark.parametrize("old_request_finishes", [True, False])
def test_interrupted_research_waits_without_extending_finalization(tmp_path, monkeypatch, old_request_finishes):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "results.json").write_text("{}")
    clock = [100.0]
    waits, calls = [], []

    class Environment(IdleEnvironment):
        def wait_idle(self, timeout_seconds):
            waits.append(timeout_seconds)
            if len(waits) == 1:
                return True
            clock[0] += 7 if old_request_finishes else timeout_seconds
            return old_request_finishes

    @contextmanager
    def session(**_selection):
        yield Environment(CODEX_HOME=str(home))

    def execute_once(_host, _directory, environment, prompt, timeout, _tail):
        calls.append((dict(environment), timeout))
        if len(calls) == 1:
            clock[0] = 110.0
            raise subprocess.TimeoutExpired("codex", timeout)
        assert len(waits) == 2 and old_request_finishes
        assert environment["TYCHE_FINALIZATION_ONLY"] == "1"
        return 0

    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime, "progress", lambda _path: {"stop": "continue"})
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _path: len(calls) == 2)
    host = SimpleNamespace(session=session, CODEX_BINARY="codex")
    if old_request_finishes:
        runtime.launch(host, directory, 110, 120, 20,
                       quota_guard(110, 120, clock=lambda: clock[0]))
        assert len(calls) == 2 and calls[1][1] == 3
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            runtime.launch(host, directory, 110, 120, 20,
                           quota_guard(110, 120, clock=lambda: clock[0]))
        assert len(calls) == 1
    assert waits == [20, 10]


def test_quota_guard_waits_for_fresh_authoritative_headroom(monkeypatch):
    clock = [100.0]
    snapshots = []
    used = iter((40, 40, 40, 41))

    def reader():
        snapshots.append(clock[0])
        return quota_snapshot(used=next(used), openrouter_limit=60)

    monkeypatch.setattr(runtime, "QUOTA_SNAPSHOT_FRESHNESS_SECONDS", 1.05)
    monkeypatch.setattr(runtime.threading.Event, "wait", lambda _self, delay: clock.__setitem__(0, clock[0] + delay))
    guard = quota_guard(200.0, 300.0, reader, clock=lambda: clock[0])

    assert guard() is True
    # A fresh unchanged snapshot is authoritative: the admitted request did
    # not consume a ledger identity, so it must not create a false local quota.
    assert guard() is True
    assert guard() is False
    assert guard.research_denial == "finalization_headroom"
    assert snapshots == pytest.approx([100.0, 101.05, 102.1, 103.15])


@pytest.mark.parametrize("openrouter_limit", [60, 200])
def test_quota_guard_uses_host_limit_and_reserves_finalization_headroom(monkeypatch, openrouter_limit):
    used = [0]

    def reader():
        return quota_snapshot(used=used[0], openrouter_limit=openrouter_limit)

    monkeypatch.setattr(runtime, "QUOTA_SNAPSHOT_FRESHNESS_SECONDS", 0)
    now = time.monotonic()
    guard = quota_guard(now + 200, now + 300, reader)
    admitted = 0
    while guard():
        admitted += 1
        used[0] += 1

    assert admitted == openrouter_limit - 19
    assert used[0] == openrouter_limit - 19
    assert guard.research_denial == "finalization_headroom"
    guard.set_phase("finalization")
    for _ in range(19):
        assert guard() is True
        used[0] += 1
    assert used[0] == openrouter_limit
    assert guard() is False
    assert guard() is False


def test_quota_guard_rechecks_deadline_after_snapshot_wait(monkeypatch):
    clock = [100.0]
    calls = []

    def reader():
        calls.append(clock[0])
        if len(calls) > 1:
            clock[0] = 111.0
        return quota_snapshot()

    monkeypatch.setattr(runtime, "QUOTA_SNAPSHOT_FRESHNESS_SECONDS", 0)
    guard = quota_guard(110.0, 120.0, reader, clock=lambda: clock[0])

    assert guard() is False
    assert guard.research_denial == "research_deadline"


def test_quota_guard_unavailable_is_fail_closed_in_both_phases(monkeypatch):
    clock = [100.0]
    calls = [quota_snapshot(), QuotaUnavailable("quota unavailable"),
             QuotaUnavailable("quota unavailable")]
    monkeypatch.setattr(runtime, "QUOTA_SNAPSHOT_FRESHNESS_SECONDS", 0)
    def reader():
        value = calls.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    guard = runtime.ArenaQuotaGuard(reader, QuotaUnavailable, 110.0, 120.0,
                                    clock=lambda: clock[0])
    guard.preflight()

    assert guard() is False
    assert guard.research_denial == "quota_unavailable"
    guard.set_phase("finalization")
    assert guard() is False


def test_headroom_boundary_waits_for_native_deadline_before_finalization(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "results.json").write_text("{}")
    clock = [100.0]
    calls, phases = [], []
    guard_holder = {}

    @contextmanager
    def session(**selection):
        guard_holder["guard"] = selection["request_guard"]
        yield IdleEnvironment(CODEX_HOME=str(home))

    def execute_once(_host, _directory, environment, prompt, timeout, _tail):
        calls.append((dict(environment), prompt, timeout, clock[0]))
        phases.append(guard_holder["guard"]())
        return 1 if len(calls) == 1 else 0

    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime, "progress", lambda _path: {"stop": "continue"})
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _path: len(calls) == 2)
    monkeypatch.setattr(runtime, "_passive_wait_until", lambda deadline: clock.__setitem__(0, deadline))
    reader = lambda: quota_snapshot(used=41, openrouter_limit=60)  # only 19 identities remain
    guard = quota_guard(110.0, 130.0, reader, clock=lambda: clock[0])

    runtime.launch(SimpleNamespace(session=session, CODEX_BINARY="codex"), directory,
                   110.0, 130.0, 30.0, guard)

    assert phases == [False, True]
    assert calls[0][3] == 100.0 and calls[1][3] == 110.0
    assert calls[0][0]["TYCHE_FINALIZATION_ONLY"] == "0"
    assert calls[1][0]["TYCHE_FINALIZATION_ONLY"] == "1"
    assert calls[1][1].startswith("Finalize the SAME saved Arena run")


def test_admitted_research_can_drain_past_soft_deadline(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "results.json").write_text("{}")
    clock = [100.0]
    calls, guard_holder = [], {}

    @contextmanager
    def session(**selection):
        guard_holder["guard"] = selection["request_guard"]
        yield IdleEnvironment(CODEX_HOME=str(home))

    def execute_once(_host, _directory, environment, prompt, timeout, _tail):
        calls.append((dict(environment), timeout, clock[0]))
        assert guard_holder["guard"]() is True
        if len(calls) == 1:
            clock[0] = 115.0
        return 0

    used = iter((0, 0, 1))
    reader = lambda: quota_snapshot(used=next(used))
    guard = quota_guard(110.0, 130.0, reader, clock=lambda: clock[0])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime, "progress", lambda _path: {"stop": "continue"})
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _path: len(calls) == 2)

    runtime.launch(SimpleNamespace(session=session, CODEX_BINARY="codex"), directory,
                   110.0, 130.0, 30.0, guard)

    assert calls[0][1] == 30.0  # response deadline, not the 10-second research edge
    assert calls[1][0]["TYCHE_FINALIZATION_ONLY"] == "1"
    assert calls[1][2] == 115.0


def test_idle_timeout_keeps_a_reviewed_partial_checkpoint(lab, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario("tyche_checkpoint")
    lab.mode = "partial_timeout"
    waits = []

    def wait_idle(_environment, timeout_seconds):
        waits.append(timeout_seconds)
        return len(waits) == 1

    monkeypatch.setattr(IdleEnvironment, "wait_idle", wait_idle)
    rows = runtime.run(ICP)
    assert len(rows) == 1
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert len(lab.processes) == 1 and len(waits) == 2
    failure = json.loads((lab.processes[0].run_dir / "failure.json").read_text())
    assert failure["error"] == "TimeoutExpired"


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


@pytest.mark.parametrize(
    "case,company_size,employee_count,expected",
    [
        ("outside", {"min_employees": 11, "max_employees": 50}, ["11-50"], "saved"),
        ("matching", {"min_employees": 51, "max_employees": 200}, ["51-200"], "blocked"),
        ("overlap", {"min_employees": 100, "max_employees": 500}, ["51-200"], "blocked"),
        ("noncontiguous", None, ["11-50", "201-500"], "blocked"),
    ],
)
def test_lab_tools_size_review_uses_saved_receipt_without_extra_dispatch(
    tmp_path, monkeypatch, case, company_size, employee_count, expected
):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / (case + ".sock")))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    fixture = ProviderFixture()
    provider = fixture.provider

    def sized_provider(parameters):
        body = provider(parameters)
        if expected == "blocked" and parameters["tool"] == "harvestapi_get_company":
            body["element"]["employeeCountRange"] = {"start": 51, "end": 200}
        return body

    run_file = tmp_path / case / "results.json"
    request = request_for({**ICP, "employee_count": employee_count}, 1, 30)
    if company_size is not None:
        request["icp"]["company_size"] = company_size
    seed = Broker(str(tmp_path / (case + ".sock")), time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(request=request, max_usd=.5)

    def request_call(_self, operation, parameters, *, admitted=False):
        assert operation == "deepline.execute" and admitted is True
        fixture.frames.append(copy.deepcopy(parameters))
        return 200, {}, sized_provider(parameters)

    monkeypatch.setattr(Broker, "request", request_call)
    session = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)
    lookup_result = session.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    ref = lookup_result["lookups"][0]["results"][0]["ref"]
    before = budget_guard.load_ledger(run_file), len(fixture.frames)
    review = {"companies": [{"target": "example.com", "decision": "reject",
        "reason": "Reviewed company size", "company": {"ref": ref}}]}
    if expected == "blocked":
        with pytest.raises(ValueError, match="requires an evidenced required failure"):
            session.call("tyche_review", review)
        assert (budget_guard.load_ledger(run_file), len(fixture.frames)) == before
        return

    result = session.call("tyche_review", review)
    assert result["saved_companies"] == ["example.com"]
    assert (budget_guard.load_ledger(run_file), len(fixture.frames)) == before
    row = json.loads(run_file.read_text())["rejected"][0]
    if company_size is None:
        assert row["qualification_checks"] == []
    else:
        check = row["qualification_checks"][0]
        assert (check["criterion"], check["status"], check["importance"]) == (
            "company_size", "fail", "required"
        )
        assert check["evidence"][0]["source"]["route_id"] == ref.split(":")[0]


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


def test_runtime_explains_fixed_arena_limits_and_passive_headroom():
    guidance = runtime.instructions()
    assert runtime.MAX_CODEX_INVOCATIONS == 200
    assert "200 OpenRouter and 30 Deepline dispatches per attempt" in guidance
    assert "failures and transparent free 429 retries consume OpenRouter slots" in guidance
    assert "passively tracks OpenRouter capacity and reserves finalization headroom" in guidance
    assert "does not authorize early or incomplete delivery" in guidance
    assert "local Deepline adapter dispatch count" in guidance
    assert "not authoritative billing" in guidance


def test_latest_native_finalization_budget_fits_the_hard_limit():
    assert runtime.FINALIZATION_SECONDS == 600
    assert runtime.RESEARCH_SECONDS == 2070
    assert runtime.RUN_SECONDS == 2670
    assert runtime.RUN_SECONDS + 30 == 45 * 60


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
    checkpoint = SimpleNamespace(__file__="/agent/lab_arena_checkpoint.py",
                                 quota_usage=lambda: quota_snapshot(),
                                 QuotaUnavailable=QuotaUnavailable)
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
        # The real Arena worker passes research phase "0" into MCP. A partial
        # checkpoint must still use its in-session approval flow.
        tools.research.environment["TYCHE_FINALIZATION_ONLY"] = "0"
        packet = tools.call("tyche_checkpoint", {})
        assert packet["status"] == "review_required"
        assert tools.research.environment["TYCHE_FINALIZATION_ONLY"] == "0"
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


@pytest.mark.parametrize("finish_tool", ["tyche_checkpoint", "tyche_finish"])
def test_finalization_projects_before_review_then_accepts_provider_backed_repair(lab, monkeypatch, finish_tool):
    lab.program = lambda: scenario(None)

    def repair_missing_hq(tools):
        document = json.loads(tools.research.path.read_text())
        document["accepted"][0]["company"].pop("hq_country")
        tools.research.path.write_text(json.dumps(document))

        blocked = tools.call(finish_tool, {})
        assert blocked["status"] == "needs_repair"
        assert any("company country must be nonempty text" in error for error in blocked["errors"])
        assert "final_review" not in json.loads(tools.research.path.read_text())
        assert not lab.output.exists()

        source = document["accepted"][0]["company"]["employee_range_evidence"]["source"]
        company_ref = source["route_id"] + ":0"
        tools.call("tyche_review", {"companies": [{"target": "example.com", "decision": "accept",
            "reason": "Restore the provider-backed headquarters field", "company": {"ref": company_ref}}]})
        packet = tools.call(finish_tool, {})
        assert packet["status"] == "review_required" and not lab.output.exists()
        saved = tools.call(finish_tool, {"review_ref": packet["review_ref"]})
        assert saved["status"] == "checkpoint_saved" and saved["companies"][0]["country"] == "United States"
        assert saved["delivery_allowed"] == (finish_tool == "tyche_finish")
        assert json.loads(lab.output.read_text())["companies"] == saved["companies"]

    lab.after_program = repair_missing_hq
    assert runtime.run(ICP)[0]["country"] == "United States"


@pytest.mark.parametrize("finish_tool", ["tyche_checkpoint", "tyche_finish"])
def test_finalization_rejects_contact_geography_before_review_approval(lab, monkeypatch, finish_tool):
    lab.program = lambda: scenario(None)
    icp = copy.deepcopy(ICP)
    icp["contact_geography"] = {"countries": ["CA"]}

    def reject_out_of_scope_contact(tools):
        blocked = tools.call(finish_tool, {})
        assert blocked["status"] == "needs_repair"
        assert any("contact_geography mismatch: country" in error for error in blocked["errors"])
        assert "final_review" not in json.loads(tools.research.path.read_text())
        assert not lab.output.exists()

    lab.after_program = reject_out_of_scope_contact
    with pytest.raises(RuntimeError, match="failed twice"):
        runtime.run(icp)


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

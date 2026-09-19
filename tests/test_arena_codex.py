"""TYCHE-only offline contracts; optionally import the supplied authoritative Arena validator."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import base64
import copy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from types import SimpleNamespace

import pytest

REAL_POPEN = subprocess.Popen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tyche_arena import host as runtime
from tyche_arena import output as arena_output
from tyche_arena.broker import (Broker, BrokerError, BrokerRefusal, DEEPLINE_WAIT_SECONDS,
                                SCRAPINGDOG_RUNTIME_HANDLE)
from tyche_arena.input import request_for
from tyche_arena.mcp import (LAB_TOOLS, LabTools, broker_resume_state,
                             evidence_review_page, model_result)
from tyche_arena.mcp import EVIDENCE_REVIEW_PAGE_CHARACTERS, MODEL_RESULT_MAX_CHARACTERS
from tyche_arena.output import companies, company_stage_evidence, signal_date
from research_tools import ResearchTools, TOOLS
import budget_guard
import confirmed_leads
import deepline
import email_receipts
import run_attempt
import scrapingdog


@pytest.fixture
def arena_operations():
    """Load the exact host operation table supplied for this integration review."""

    configured = os.environ.get("LAB_ARENA_REFERENCE_SOURCE")
    if not configured:
        pytest.skip("set LAB_ARENA_REFERENCE_SOURCE to run authoritative Arena integration checks")
    source = Path(configured)
    if not (source / "lab_arena/operations.py").is_file():
        pytest.skip("authoritative Lab Arena operation source is unavailable")
    original_path = list(sys.path)
    original_modules = {name for name in sys.modules if name == "lab_arena" or name.startswith("lab_arena.")}
    sys.path.insert(0, str(source))
    try:
        yield importlib.import_module("lab_arena.operations")
    finally:
        sys.path[:] = original_path
        for name in list(sys.modules):
            if (name == "lab_arena" or name.startswith("lab_arena.")) and name not in original_modules:
                sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def arena_output_binding(tmp_path, monkeypatch):
    """Every LabTools fixture has the same fail-closed host output binding as production."""
    monkeypatch.setenv("LAB_ARENA_OUTPUT_PATH", str(tmp_path / "arena-output.json"))
    # These recorded serial journeys isolate continuation/checkpoint behavior.
    # test_arena_parallel exercises the production two-worker entrypoint.
    monkeypatch.setattr(runtime.runner, "DEFAULT_WORKERS", 1)


def test_labtools_requires_host_output_binding_before_research(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.delenv("LAB_ARENA_OUTPUT_PATH")
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    run_file = tmp_path / "run" / "results.json"
    seed = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(
        request=request_for(ICP, 1, 30), max_usd=.5,
    )
    with pytest.raises(KeyError, match="LAB_ARENA_OUTPUT_PATH"):
        LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)


@pytest.fixture
def arena_worker_runtime(arena_operations):
    """Load the authoritative broker result and worker boundary together."""

    host_broker = importlib.import_module("lab_arena.broker")
    host_runner = importlib.import_module("lab_arena.runner")
    return SimpleNamespace(
        BrokerResult=host_broker.BrokerResult,
        error_result=host_broker._error_result,
        RunState=host_runner.RunState,
        WorkerSocketServer=host_runner.WorkerSocketServer,
    )


class FramedArenaWorker:
    """Small Unix worker that applies the production operation validator."""

    def __init__(self, path, operations, responses):
        self.path = str(path)
        self.operations = operations
        self.responses = list(responses)
        self.frames = []
        self.errors = []
        self.stopped = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen()
        self.listener.settimeout(.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    @staticmethod
    def _receive(connection, size):
        data = bytearray()
        while len(data) < size:
            part = connection.recv(size - len(data))
            if not part:
                raise RuntimeError("fixture request was truncated")
            data.extend(part)
        return bytes(data)

    def _serve(self):
        try:
            for response in self.responses:
                while not self.stopped.is_set():
                    try:
                        connection, _ = self.listener.accept()
                        break
                    except TimeoutError:
                        continue
                else:
                    return
                with connection:
                    size = int.from_bytes(self._receive(connection, 4), "big")
                    frame = json.loads(self._receive(connection, size))
                    operation = frame["operation_id"]
                    parameters = self.operations.validate_operation_request(operation, frame["parameters"])
                    assert 1 <= frame["timeout_ms"] <= self.operations.OPERATIONS[operation].timeout_seconds * 1000
                    self.frames.append({**frame, "parameters": parameters})
                    if response == "disconnect":
                        continue
                    if isinstance(response, str):
                        document = {"error": response}
                    else:
                        status, headers, body = response
                        document = {"status": status, "headers": headers,
                                    "body_b64": base64.b64encode(body).decode()}
                    encoded = json.dumps(document, separators=(",", ":")).encode()
                    connection.sendall(len(encoded).to_bytes(4, "big") + encoded)
        except Exception as exc:
            self.errors.append(exc)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)
        Path(self.path).unlink(missing_ok=True)
        assert not self.thread.is_alive()
        if self.errors:
            raise self.errors[0]


def native_scrapingdog_research(tmp_path, monkeypatch, socket_path):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    broker = Broker(socket_path, time.monotonic() + 30, response_deadline=time.monotonic() + 60)
    research = ResearchTools(tmp_path / "research/results.json", execute=broker.execute)
    request = request_for(ICP, 1, 30)
    research.start(request=request, max_usd=1,
                   provider_credit_limits={"deepline": 10, "scrapingdog": 20_000},
                   scrapingdog_usd_per_credit=runtime.SCRAPINGDOG_USD_PER_CREDIT)
    return research, broker


def scrapingdog_lookup_request(inputs, target="discovery", max_cost_credits=5):
    return {"checks": [{"target": target, "purpose": "Test native brokered evidence",
                         "phase": "account_discovery", "provider": "scrapingdog",
                         "inputs": inputs}]}


@contextmanager
def native_finalization_recovery_fixture():
    """Reuse upstream's receipt-complete pending-verification fixture."""
    native_tests = ROOT / ".agents/skills/lead-sourcing/tests"
    inserted = str(native_tests) not in sys.path
    if inserted:
        sys.path.insert(0, str(native_tests))
    try:
        from test_finalization_recovery import FinalizationRecoveryTests
        case = FinalizationRecoveryTests(
            "test_saved_free_getter_survives_deadline_and_finalization_without_resubmission"
        )
        try:
            yield case.pending_run()
        finally:
            case.doCleanups()
    finally:
        if inserted:
            sys.path.remove(str(native_tests))


class IdleEnvironment(dict):
    def wait_idle(self, timeout_seconds):
        assert timeout_seconds > 0
        return True


class QuotaUnavailable(RuntimeError):
    pass


def quota_snapshot(*, used=0, inflight=0, openrouter_limit=200,
                   deepline_limit=30, deepline_used=0,
                   scrapingdog_limit=30, scrapingdog_used=0):
    values = {
        "scrapingdog": (scrapingdog_limit, scrapingdog_used, 0),
        "deepline": (deepline_limit, deepline_used, 0),
        "openrouter": (openrouter_limit, used, inflight),
    }
    return {
        "schema_version": "leadpoet.lab_arena.quota_snapshot.v1",
        "providers": {
            name: {"limit": limit, "used": provider_used,
                   "remaining": limit - provider_used, "inflight": provider_inflight}
            for name, (limit, provider_used, provider_inflight) in values.items()
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


def dereference_tool_schema(schema, field):
    reference = schema["properties"][field]["$ref"]
    assert reference.startswith("#/$defs/")
    return schema["$defs"][reference.removeprefix("#/$defs/")]


def review_findings(packet, tools=None):
    """Build explicit fixture findings from the exact source packet under review."""
    if "companies" not in packet:
        document = tools.research._document()
        rows = (confirmed_leads.pending(tools.research.path, document)
                if packet["review_ref"].startswith("confirmed:") else document["accepted"])
        views = [tools.research.inspect(
            target=row["company"]["domain"], field="evidence_review") for row in rows]
        packet = {"companies": [
            {**view["company"], "sources": view["sources"]} for view in views]}
    return [{
        "target": company["company"]["domain"],
        "source_refs": list(company["sources"]),
        "finding": (
            "Captured manufacturing and completed integration support the fixture fit; "
            "potential coordination benefits remain qualified analysis."
        ),
    } for company in packet["companies"]]


def scenario(finish_tool="tyche_finish"):
    company = yield "tyche_lookup", lookup(
        "harvestapi_get_company", {"url": COMPANY_URL}, "account_discovery")
    company_ref = company["lookups"][0]["results"][0]["ref"]
    pages = yield "tyche_lookup", lookup("generic_http_request", {"url": "https://example.com/news", "method": "GET"})
    refs = [row["ref"] for row in pages["lookups"][0]["results"]]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "qualify_account", "reason": "Company and signal verified",
        "company": {"ref": company_ref, "discovery_source": {"ref": company_ref},
            "industry": "Manufacturing", "sub_industry": "Textiles",
            "description": "Example Products manufactures packaged goods, tools, and accessories. It supplies retailers with consumer products.",
            "classification_note": "Canonical taxonomy classification"},
        "account_fit": {"ref": refs[0], "fit_claim": "Manufacturing account"},
        "qualification_checks": [
            {"requirement_ref": "icp:industries", "status": "pass", "claim": "Manufactures consumer products", "evidence": [{"ref": refs[0]}]},
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
    accepted = yield "tyche_review", {"companies": [{"target": "example.com", "decision": "accept", "reason": "Verified company and current buyer",
        "primary_contact": {"email_ref": email_ref, "email_source": {"ref": profile_ref}}}]}
    if finish_tool is None:
        return
    packet = accepted if finish_tool == "tyche_checkpoint" else (yield finish_tool, {})
    assert packet["status"] == "review_required", packet
    company = packet["companies"][0]
    assert len(company["signal_checks"]) == 1
    assert company["signal_checks"][0]["evidence"][0]["event_date"] == "2026-08-12"
    assert not any(check.get("signal") for check in company["qualification_checks"])
    final = yield finish_tool, {
        "review_ref": packet["review_ref"],
        "review_findings": review_findings(packet),
    }
    assert final["checkpoint_saved"], final
    assert final["delivery_allowed"] == (finish_tool == "tyche_finish"), final


def capture_accepted_review(captured):
    """Expose the adapter result returned by the native accepted transition."""
    program = scenario(None)
    command = next(program)
    while True:
        result = yield command
        if (command[0] == "tyche_review"
                and any(row.get("decision") == "accept"
                        for row in command[1].get("companies", []))):
            captured.append(result)
        try:
            command = program.send(result)
        except StopIteration:
            return


def reviewed_company(target, company_url, person_url, email, page_url, event_date,
                     paragraph, *, approve=True):
    company = yield "tyche_lookup", lookup(
        "harvestapi_get_company", {"url": company_url})
    company_ref = company["lookups"][0]["results"][0]["ref"]
    pages = yield "tyche_lookup", {
        "checks": [{"target": target, "purpose": "Verify fixture evidence",
                    "phase": "account_verification", "tool": "generic_http_request",
                    "inputs": {"url": page_url, "method": "GET"}}]}
    refs = [row["ref"] for row in pages["lookups"][0]["results"]]
    yield "tyche_review", {"companies": [{
        "target": target, "decision": "qualify_account",
        "reason": "Company and signal verified",
        "company": {"ref": company_ref, "industry": "Manufacturing",
                    "sub_industry": "Textiles",
                    "description": "The company manufactures packaged goods, tools, and accessories for retailers.",
                    "classification_note": "Canonical taxonomy classification"},
        "account_fit": {"ref": refs[0], "fit_claim": "Manufacturing account"},
        "qualification_checks": [
            {"requirement_ref": "icp:industries", "status": "pass",
             "claim": "Manufactures consumer products", "evidence": [{"ref": refs[0]}]},
            {"requirement_ref": "attribute:0", "status": "pass",
             "claim": "Manufactures consumer products for retailers", "evidence": [{"ref": refs[0]}]},
            {"requirement_ref": "signal:0", "status": "pass",
             "claim": "Connected an acquired warehouse to a shared WMS",
             "evidence": [{"ref": refs[1], "event_date": event_date}]},
        ],
        "intent_details": paragraph,
    }], "sources": [{"ref": pages["lookups"][0]["route"], "state": "exhausted",
                      "reason": "Both fixture sources reviewed"}]}
    profile = yield "tyche_lookup", {
        "checks": [{"target": target, "purpose": "Verify fixture contact",
                    "phase": "contact_verification", "tool": "harvestapi_get_profile",
                    "inputs": {"url": person_url, "main": "true"}}]}
    profile_ref = profile["lookups"][0]["results"][0]["ref"]
    yield "tyche_review", {"companies": [{
        "target": target, "decision": "hold_contact", "reason": "Verify selected email",
        "primary_contact": {"ref": profile_ref, "requested_role": "Director of Supply Chain",
                            "role_match": "exact"}}]}
    enriched = yield "tyche_lookup", {
        "checks": [{"target": target, "purpose": "Find fixture email",
                    "phase": "contact_discovery", "tool": "harvestapi_get_profile",
                    "inputs": {"findEmail": "true"}, "contact_ref": profile_ref}]}
    profile_ref = enriched["lookups"][0]["results"][0]["ref"]
    yield "tyche_review", {"companies": [{
        "target": target, "decision": "hold_contact", "reason": "Select enriched profile",
        "primary_contact": {"ref": profile_ref, "requested_role": "Director of Supply Chain",
                            "role_match": "exact"}}]}
    validation = yield "tyche_lookup", {
        "checks": [{"target": target, "purpose": "Verify fixture email",
                    "phase": "email_validation", "tool": "zerobounce_validate",
                    "inputs": {"email": email}, "contact_ref": profile_ref}]}
    email_ref = validation["lookups"][0]["results"][0]["ref"]
    packet = yield "tyche_review", {"companies": [{
        "target": target, "decision": "accept", "reason": "Verified company and current buyer",
        "primary_contact": {"email_ref": email_ref}}]}
    assert packet["status"] == "review_required"
    if approve:
        saved = yield "tyche_review", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet),
        }
        assert saved["checkpoint_saved"] and saved["delivery_allowed"] is False


def two_company_checkpoint_scenario():
    yield from reviewed_company(
        "example.com", COMPANY_URL, PERSON_URL, "ada@example.com",
        "https://example.com/news", "2026-08-12", PARAGRAPH)
    second_paragraph = (
        "Second Products connected its acquired warehouse to a shared WMS on August 13, 2026. "
        "The project covers inventory visibility and fulfillment across the combined operation. "
        "This recent integration may increase its need to coordinate stock and orders between warehouses."
    )
    yield from reviewed_company(
        "second.example", "https://www.linkedin.com/company/second-example",
        "https://www.linkedin.com/in/bob-second", "bob@second.example",
        "https://second.example/news", "2026-08-13", second_paragraph)


def incremental_checkpoint_scenario(*, approve_count=2):
    yield from reviewed_company(
        "example.com", COMPANY_URL, PERSON_URL, "ada@example.com",
        "https://example.com/news", "2026-08-12", PARAGRAPH)
    second_paragraph = (
        "Second Products connected its acquired warehouse to a shared WMS on August 13, 2026. "
        "The project covers inventory visibility and fulfillment across the combined operation. "
        "This recent integration may increase its need to coordinate stock and orders between warehouses."
    )
    yield from reviewed_company(
        "second.example", "https://www.linkedin.com/company/second-example",
        "https://www.linkedin.com/in/bob-second", "bob@second.example",
        "https://second.example/news", "2026-08-13", second_paragraph,
        approve=approve_count >= 2)


def raw_response_scenario(include_geography=False, page_capture=False):
    company = yield "tyche_lookup", lookup(
        "harvestapi_get_company", {"url": COMPANY_URL}, "account_discovery")
    company_ref = company["lookups"][0]["results"][0]["ref"]
    page = yield "tyche_lookup", lookup(
        "firecrawl_scrape", {"url": "https://example.com/about", "zeroDataRetention": True})
    page_ref = page["lookups"][0]["results"][0]["ref"]
    answer = yield "tyche_lookup", lookup("exa_answer", {"query": "Example Products warehouse integration", "text": True},
                                            approach="Citation-backed signal verification")
    answer_rows = answer["lookups"][0]["results"]
    if page_capture:
        assert answer_rows[0]["facts"]["evidence_text"].startswith("On August 12")
    else:
        assert answer_rows[0]["facts"]["provider_answer"] == "Generated summary; review its citations."
        assert answer_rows[0]["facts"]["evidence_text"] != answer_rows[0]["facts"]["provider_answer"]
    signal_ref = answer_rows[0]["ref"]
    yield "tyche_review", {"companies": [{"target": "example.com", "decision": "qualify_account", "reason": "Company and signal verified",
        "company": {"ref": company_ref, "discovery_source": {"ref": company_ref},
            "industry": "Manufacturing", "sub_industry": "Textiles",
            "description": "Example Products manufactures packaged goods, tools, and accessories. It supplies retailers with consumer products.",
            "classification_note": "Canonical taxonomy classification"},
        "account_fit": {"ref": page_ref, "fit_claim": "Manufacturing account"},
        "qualification_checks": [
            {"requirement_ref": "icp:industries", "status": "pass", "claim": "Manufactures consumer products", "evidence": [{"ref": page_ref}]},
            *([{"requirement_ref": "icp:geographies", "status": "pass", "claim": "Based in the United States", "evidence": [{"ref": page_ref}]}] if include_geography else []),
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
        "primary_contact": {"email_ref": email_ref, "email_source": {"ref": profile_ref}}}]}
    evidence_review = yield "tyche_inspect", {"target": "example.com", "field": "evidence_review"}
    assert evidence_review["sources"][page_ref]["text"].startswith("Example Products manufactures")
    assert evidence_review["sources"][signal_ref]["text"].startswith("On August 12")
    packet = yield "tyche_finish", {}
    final = yield "tyche_finish", {
        "review_ref": packet["review_ref"],
        "review_findings": review_findings(packet),
    }
    assert final["checkpoint_saved"] and final["delivery_allowed"]



class ProviderFixture:
    def __init__(self):
        self.frames = []
        self.provider_responses = []
        self.raw_envelopes = False
        self.page_capture = False

    def provider(self, parameters):
        tool = parameters["tool"]
        payload = parameters.get("payload", {})
        request_text = json.dumps(parameters)
        second = any(marker in request_text for marker in (
            "second-example", "second.example", "bob-second"))
        company_name = "Second Products" if second else "Example Products"
        company_url = ("https://www.linkedin.com/company/second-example"
                       if second else COMPANY_URL)
        person_url = ("https://www.linkedin.com/in/bob-second"
                      if second else PERSON_URL)
        website = "https://second.example" if second else "https://example.com"
        email = "bob@second.example" if second else "ada@example.com"
        data = {
            "harvestapi_get_company": {"status": "ok", "element": {"name": company_name,
                "website": website, "linkedinUrl": company_url,
                "employeeCountRange": {"start": 201, "end": 500},
                "locations": [{"headquarter": True, "country": "United States", "geographicArea": "Ohio"}]}},
            "harvestapi_get_profile": {"status": "ok", "element": {"id": "profile-456" if second else "profile-123", "linkedinUrl": person_url,
                "firstName": "Bob" if second else "Ada", "lastName": "Second" if second else "Example", "emails": [{"email": email, "status": "valid"}],
                "currentPosition": [{"companyName": company_name, "title": "Director of Supply Chain", "companyLinkedinUrl": company_url}],
                "location": {"parsed": {"countryFull": "United States", "state": "Ohio", "city": "Columbus"}}}},
            "zerobounce_validate": {"status": "ok", "data": {"address": email, "status": "valid", "sub_status": ""}},
            "exa_answer": {"answer": "Generated summary; review its citations.", "citations": [
                {"id": "citation-1", "url": "https://example.com/news/wms-project", "title": "Warehouse project",
                 "text": "On August 12, 2026, Example Products connected its acquired warehouse to one WMS.",
                 "publishedDate": "2026-08-20"}], "requestId": "exa-request-1"},
            "generic_http_request": {"results": [
                {"markdown": "Example Products manufactures packaged goods, tools and accessories for retailers.",
                 "metadata": {"statusCode": 200, "sourceUrl": "https://example.com/about", "publishedTime": "2026-08-10"}},
                {"markdown": "On August 12, 2026, the company connected its acquired warehouse to one WMS. The project covers inventory visibility and fulfillment.",
                 "metadata": {"statusCode": 200, "sourceUrl": "https://example.com/news/wms-project", "publishedTime": "2026-08-20"}}]},
            "firecrawl_scrape": {"markdown": "Example Products manufactures packaged goods, tools and accessories for retailers. It is based in the United States.",
                "metadata": {"statusCode": 200, "sourceURL": "https://example.com/about",
                             "url": "https://example.com/about"}}}
        if second:
            data["generic_http_request"] = {"results": [
                {"markdown": "Second Products manufactures packaged goods, tools and accessories for retailers.",
                 "metadata": {"statusCode": 200, "sourceUrl": "https://second.example/about", "publishedTime": "2026-08-11"}},
                {"markdown": "On August 13, 2026, Second Products connected an acquired warehouse to one WMS. The project covers inventory visibility and fulfillment.",
                 "metadata": {"statusCode": 200, "sourceUrl": "https://second.example/news/wms-project", "publishedTime": "2026-08-21"}},
            ]}
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
            elif self.page_capture and tool == "exa_answer":
                raw_data = {"results": [{
                    "success": True,
                    "markdown": row["text"],
                    "metadata": {
                        "sourceUrl": row["url"],
                        "publishedTime": row["publishedDate"],
                    },
                } for row in data[tool]["citations"]]}
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
    fixture.checkpoints = []
    fixture.mode = "deliver"
    fixture.program = scenario
    fixture.after_program = lambda tools: None
    fixture.worker_starts = 0
    fixture.openrouter_used = 0
    fixture.deepline_limit = 30
    fixture.deepline_used = 0
    fixture.scrapingdog_limit = 30
    fixture.scrapingdog_used = 0
    fixture.output = tmp_path / "companies.json"
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "1")
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setenv("LAB_ARENA_OUTPUT_PATH", str(fixture.output))
    monkeypatch.setenv("LAB_ARENA_EVALUATION_DATE", "2026-09-15")
    monkeypatch.delenv("SCRAPINGDOG_API_KEY", raising=False)
    monkeypatch.delenv("TYCHE_REQUEST_FILE", raising=False)
    original_mkdtemp = runtime.tempfile.mkdtemp
    monkeypatch.setattr(runtime.tempfile, "mkdtemp", lambda **kwargs: original_mkdtemp(prefix="run-", dir=tmp_path))

    def request(self, operation, parameters, *, admitted=False, timeout_seconds=None):
        assert operation == "deepline.execute"
        assert admitted is True
        assert timeout_seconds == 240.0
        fixture.frames.append(copy.deepcopy(parameters))
        fixture.deepline_used += 1
        return 200, {}, fixture.provider(parameters)

    monkeypatch.setattr(Broker, "request", request)

    def checkpoint(rows):
        fixture.output.write_text(json.dumps({"companies": rows}))
        fixture.checkpoints.append(copy.deepcopy(rows))

    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=checkpoint, quota_usage=lambda: quota_snapshot(
            used=fixture.openrouter_used,
            deepline_limit=fixture.deepline_limit,
            deepline_used=fixture.deepline_used,
            scrapingdog_limit=fixture.scrapingdog_limit,
            scrapingdog_used=fixture.scrapingdog_used,
        ),
        QuotaUnavailable=QuotaUnavailable,
    ))

    @contextmanager
    def session(**selection):
        fixture.request_guard = selection.pop("request_guard")
        assert isinstance(selection.pop("request_gate"), runtime.RequestGate)
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
            assert self.prompt.startswith((b"Research the authoritative", b"Continue the SAME", b"Current invocation feedback"))

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


def test_trigger_returns_reviewed_checkpoint_with_codex_configuration(lab, monkeypatch):
    from harness import run_icp

    shared_calls = []
    main_supervisor = runtime.runner.supervise_worker
    def supervise(*args, **kwargs):
        shared_calls.append(kwargs['host'])
        return main_supervisor(*args, **kwargs)
    monkeypatch.setattr(runtime.runner, 'supervise_worker', supervise)
    rows = run_icp(ICP)
    assert len(shared_calls) == 1 and isinstance(shared_calls[0], runtime.ArenaHost)
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert len(rows) == 1
    assert rows[0]["contact"]["email_source"] == {
        "provider": "harvestapi", "tool": "harvestapi_get_profile", "record_id": "profile-123"}
    assert rows[0]["intent_signals"][0]["matched_icp_signal"] == 0
    assert rows[0]["intent_signals"][0]["date"] == "2026-08-12"
    assert len(lab.sessions) == 1
    assert {key: lab.sessions[0][key] for key in ("model", "reasoning_effort", "web_search")} == {
        "model": "openai/gpt-5.6-luna", "reasoning_effort": "high", "web_search": "live"}
    assert lab.sessions[0]["response_deadline"] > time.monotonic()
    assert "service_tier" not in lab.config
    assert lab.config["mcp_servers"]["tyche"]["required"]
    tool_timeout = lab.config["mcp_servers"]["tyche"]["tool_timeout_sec"]
    assert runtime.RUN_SECONDS <= tool_timeout <= runtime.RUN_SECONDS + runtime.MCP_RESPONSE_MARGIN_SECONDS
    assert runtime.RUN_SECONDS + runtime.PROCESS_RECEIPT_MARGIN_SECONDS < 2700
    assert runtime.MCP_TOOL_TIMEOUT_SECONDS > DEEPLINE_WAIT_SECONDS
    assert "PYTHONPATH" in lab.config["mcp_servers"]["tyche"]["env_vars"]
    assert "SCRAPINGDOG_API_KEY" in lab.config["mcp_servers"]["tyche"]["env_vars"]
    assert "model_catalog_json" not in lab.config  # retain native Codex model behavior
    assert "features.image_generation=false" in lab.processes[0].command
    assert "agents.enabled=false" in lab.processes[0].command
    assert "features.multi_agent_v2=false" in lab.processes[0].command
    assert "model_auto_compact_token_limit" not in lab.config
    assert "model_auto_compact_token_limit_scope" not in lab.config
    assert "tool_output_token_limit" not in lab.config
    assert lab.session_closed and len(lab.processes) == 1
    assert lab.processes[0].command[0] == "/usr/local/bin/codex"
    assert lab.processes[0].kwargs["env"]["TYCHE_PARALLEL_WORKERS"] == "1"
    assert not (lab.research[0].research.path.parent / "leads.xlsx").exists()
    ledger = budget_guard.load_ledger(lab.research[0].research.path)
    assert ledger["calls"] and all(call["actual_credits"] is not None for call in ledger["calls"].values())
    assert ledger["credit_limits"]["scrapingdog"] == "0"
    assert ledger["usd_per_credit"]["scrapingdog"] is None
    with pytest.raises(ValueError, match="delivered"):
        lab.research[0].call("tyche_review", {})
    with pytest.raises(ValueError, match="initialized"):
        lab.research[0].call("tyche_start", {})


@pytest.mark.parametrize(("remaining", "expected"), [
    (600.0, 120),
    (17.75, 17),
    (0.75, 0),
    (-1.0, 0),
])
def test_arena_mcp_startup_timeout_stays_within_response_deadline(monkeypatch, remaining, expected):
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 100.0)

    config = tomllib.loads(runtime.tool_configuration(
        Path("/tmp/results.json"), 110.0, 100.0 + remaining,
    ))

    startup_timeout = config["mcp_servers"]["tyche"]["startup_timeout_sec"]
    assert startup_timeout == expected
    assert startup_timeout <= max(0, remaining)


@pytest.mark.parametrize("provider_limit", [30, 200])
def test_model_entrypoint_does_not_encode_host_provider_quota(lab, provider_limit):
    lab.deepline_limit = provider_limit

    rows = runtime.run(ICP)

    assert len(rows) == 1 and lab.checkpoints
    budget = lab.research[0].broker.local_dispatch_budget()["providers"]["deepline"]
    assert budget == {"used": lab.deepline_used}


def test_arena_handoff_uses_fresh_labtools_without_research_reset(lab):
    lab.program = lambda: scenario(None)

    def finish_in_fresh_context(tools):
        tools.research.environment["TYCHE_FINALIZATION_ONLY"] = "0"
        checkpoint = tools.checkpoint()
        assert checkpoint["status"] == "review_required"
        assert checkpoint["review_scope"] == "confirmed_leads"
        assert tools.research.environment["TYCHE_FINALIZATION_ONLY"] == "0"

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
        assert (fresh.broker.local_dispatch_budget()["providers"]
                == tools.broker.local_dispatch_budget()["providers"])
        packet = fresh.call("tyche_finish", {})
        assert packet["status"] == "review_required"
        delivered = fresh.call("tyche_finish", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet),
        })
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


def test_arena_lookup_schema_preserves_bounded_native_batch():
    native_limit = TOOLS["tyche_lookup"][1]["properties"]["checks"]["maxItems"]
    arena_limit = dereference_tool_schema(LAB_TOOLS["tyche_lookup"][1], "checks")["maxItems"]

    assert native_limit == arena_limit == 3
    assert runtime.MCP_TOOL_TIMEOUT_SECONDS == arena_limit * DEEPLINE_WAIT_SECONDS + 15


def test_native_batch_and_concurrent_mcp_request_have_strict_dispatch_bound(tmp_path, monkeypatch):
    from tyche_tools import serve

    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    run_file = tmp_path / "run" / "results.json"
    seed = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(
        request=request_for(ICP, 1, 30), max_usd=.5,
    )
    active = threading.Event()
    release = threading.Event()
    provider_calls = []

    def request_call(_self, operation, parameters, *, admitted=False, timeout_seconds=None):
        assert operation == "deepline.execute" and admitted is True
        assert timeout_seconds == 240.0
        provider_calls.append(copy.deepcopy(parameters))
        if len(provider_calls) == 1:
            active.set()
            assert release.wait(10)
        return 200, {}, {
            "status": "completed",
            "result": {"data": {"element": None, "status": 200}},
            "billing": {"credits_charged": 0.03},
        }

    monkeypatch.setattr(Broker, "request", request_call)
    tools = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)
    second_replied = threading.Event()

    class Incoming:
        def __iter__(self):
            yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "tyche_lookup",
                "arguments": {"checks": [
                    {**lookup("harvestapi_get_company", {
                        "url": f"https://www.linkedin.com/company/batch-{index}",
                    })["checks"][0], "target": f"batch-{index}.example"}
                    for index in range(3)
                ]},
            }}) + "\n"
            assert active.wait(5)
            yield json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "tyche_lookup",
                "arguments": lookup("harvestapi_get_company", {
                    "url": "https://www.linkedin.com/company/overlap-example",
                }),
            }}) + "\n"
            assert second_replied.wait(5)
            release.set()

    class Outgoing(io.StringIO):
        def write(self, value):
            written = super().write(value)
            if json.loads(value)["id"] == 2:
                second_replied.set()
            return written

    outgoing = Outgoing()
    serve(tools, Incoming(), outgoing, tools=LAB_TOOLS)
    responses = {row["id"]: row for row in map(json.loads, outgoing.getvalue().splitlines())}
    first = json.loads(responses[1]["result"]["content"][0]["text"])
    second = json.loads(responses[2]["result"]["content"][0]["text"])

    assert [row["status"] for row in first["lookups"]] == ["no_results"] * 3
    assert second["status"] == "arena_busy"
    assert second["request_sent"] is False and second["retryable"] is True
    assert len(provider_calls) == 3
    ledger = budget_guard.load_ledger(run_file)
    assert len(ledger["calls"]) == 3
    assert {call["actual_credits"] for call in ledger["calls"].values()} == {"0.03"}
    receipts = [json.loads((run_file.parent / "receipts" / f"{route_id}.json").read_text())
                for route_id in ledger["calls"]]
    assert all(receipt["receipt_status"] == "complete" for receipt in receipts)
    assert all(route.get("target") != "example.com"
               for route in json.loads(run_file.read_text())["routes"])


def test_premature_clean_exit_continues_same_run_inside_one_runtime_session(lab):
    lab.mode = "early_clean"

    rows = runtime.run(ICP)

    assert len(rows) == 1
    assert len(lab.sessions) == 1
    assert len(lab.processes) == 2
    assert lab.processes[0].run_dir == lab.processes[1].run_dir
    assert lab.processes[0].kwargs["env"]["CODEX_HOME"] == lab.processes[1].kwargs["env"]["CODEX_HOME"]
    assert lab.processes[0].prompt.startswith(b"Research the authoritative")
    assert lab.processes[1].prompt.startswith(b"Continue the SAME saved run")


def test_launch_recovers_completed_attempt_before_continuation_without_new_provider_call(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    fixture = ProviderFixture()
    provider_calls = []

    def request_call(_self, operation, parameters, *, admitted=False, timeout_seconds=None):
        assert operation == "deepline.execute" and admitted is True
        assert timeout_seconds == 240.0
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
    document["rejected"] = [{
        "stage": "account", "candidate": {"domain": "rejected.example"},
        "reason_code": "not_icp_fit", "reason_text": "Explicit saved review rejection",
    }]
    rejected_before = copy.deepcopy(document["rejected"])
    document["routes"] = [route for route in document["routes"] if route["route_id"] != route_id]
    existing_route_ids = {route["route_id"] for route in document["routes"]}
    run_file.write_text(json.dumps(document, indent=2) + "\n")
    provider_count = len(provider_calls)

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    model_calls = []
    events = []

    class RecoveryEnvironment(IdleEnvironment):
        def wait_idle(self, timeout_seconds):
            assert timeout_seconds == 60.0
            events.append("idle")
            return True

    @contextmanager
    def host_session(**_selection):
        yield RecoveryEnvironment(CODEX_HOME=str(home))

    native_recover = run_attempt.recover_completed_attempts

    def recover(path):
        assert events[-1] == "idle"
        events.append("recover")
        return native_recover(path)

    def execute_once(_host, _directory, environment, prompt, timeout, _tail):
        events.append("model")
        recovered = json.loads(run_file.read_text())
        assert {route["route_id"] for route in recovered["routes"]} == existing_route_ids | {route_id}
        model_calls.append((dict(environment), prompt, timeout))
        return 0

    monkeypatch.setattr(runtime.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(run_attempt, "recover_completed_attempts", recover)
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: bool(model_calls))
    host = SimpleNamespace(session=host_session, CODEX_BINARY="codex")
    runtime.launch(host, run_dir, 130.0, 160.0, 60.0,
                   quota_guard(130.0, 160.0, clock=lambda: 100.0))

    recovered = json.loads(run_file.read_text())
    assert recovered["stop_check"]["started_at"] == started_at
    assert recovered["rejected"] == rejected_before
    assert events == ["idle", "recover", "model"]
    assert len(model_calls) == 1 and model_calls[0][2] == 60.0 + runtime.PROCESS_RECEIPT_MARGIN_SECONDS
    assert len(provider_calls) == provider_count
    assert budget_guard.ledger_path(run_file).read_bytes() == ledger_before
    assert receipt.read_bytes() == receipt_before


def test_launch_recovers_independent_complete_attempt_but_blocks_pending_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    fixture = ProviderFixture()
    provider_calls = []

    def request_call(_self, operation, parameters, *, admitted=False, timeout_seconds=None):
        assert operation == "deepline.execute" and admitted is True
        assert timeout_seconds == 240.0
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
    pending.pop("provider_response", None)
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
    with pytest.raises(RuntimeError, match="saved_dispatch_accounting_incomplete"):
        runtime.launch(SimpleNamespace(session=host_session, CODEX_BINARY="codex"), run_dir,
                       now + 30, now + 60, 60, quota_guard(now + 30, now + 60))

    recovered_routes = {route["route_id"] for route in json.loads(run_file.read_text())["routes"]}
    assert recovered_routes == existing_route_ids | {route_ids[0]}
    assert route_ids[1] not in recovered_routes
    assert len(provider_calls) == provider_count
    assert budget_guard.ledger_path(run_file).read_bytes() == ledger_before
    assert {rid: (run_file.parent / "receipts" / (rid + ".json")).read_bytes()
            for rid in route_ids} == receipts_before


def seed_supervisor(directory, duration=120):
    from datetime import datetime, timezone
    (directory / "results.json").write_text(json.dumps({
        "request": {"original_text": json.dumps(ICP), "target_count": 1,
                    "max_duration_seconds": duration},
        "stop_check": {"started_at": datetime.now(timezone.utc).isoformat()},
        "accepted": [], "routes": [],
    }))


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
    seed_supervisor(run_dir, duration=1)
    calls = []
    clock = [time.monotonic()]

    def execute_once(_host, directory, environment, prompt, timeout, _tail):
        calls.append((dict(environment), prompt, timeout))
        if len(calls) == 1:
            clock[0] += 2
            raise subprocess.TimeoutExpired("codex", timeout)
        return 0

    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ResearchTools, "_overview", lambda _path: {"stop": "continue", "operational_block": None})
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: len(calls) >= 2)
    now = clock[0]
    runtime.launch(host, run_dir, now + 1, now + runtime.RUN_SECONDS, runtime.RUN_SECONDS,
                   quota_guard(now + 1, now + runtime.RUN_SECONDS, clock=lambda: clock[0]))

    assert len(sessions) == 1 and len(calls) == 2
    assert sessions[0]["response_deadline"] == now + runtime.RUN_SECONDS
    assert calls[0][0]["TYCHE_FINALIZATION_ONLY"] == "0"
    assert calls[1][0]["TYCHE_FINALIZATION_ONLY"] == "1"
    assert "Research has stopped" in calls[1][1]
    assert "tyche_finish before individual field inspections" in calls[1][1]
    assert "Start with tyche_inspect" not in calls[1][1]
    assert 0 < calls[1][2] <= runtime.FINALIZATION_SECONDS + 1
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert "TYCHE_FINALIZATION_ONLY" in config["mcp_servers"]["tyche"]["env_vars"]


def completion_review_tools(
    tmp_path, stop="time_limit_reached", *, ready=True, has_candidate=True,
):
    """Small finalization boundary fixture with no provider or filesystem writes."""
    row = {
        "candidate": {"domain": "ready.example"},
        "stage": "contact",
        "reason_text": "Saved buyer awaits a final model decision",
        "primary_contact": {"email": "buyer@ready.example"},
    }
    document = {
        "request": {"target_count": 2},
        "accepted": [],
        "unresolved": [row] if has_candidate else [],
    }
    candidate = {
        "target": "ready.example",
        "profile_verified": ready,
        "email_usable": ready,
        "missing": [] if ready else ["Current profile evidence is missing"],
        "saved_valid_emails": [],
        "recent_email_decisions": [],
        "blocked_actions": {},
        "next": "Review saved evidence.",
    }

    class Research:
        environment = {"TYCHE_FINALIZATION_ONLY": "1"}
        review_delivery = None
        path = tmp_path / "results.json"

        def __init__(self):
            self.calls = []

        def _document(self):
            return document

        def _overview(self):
            return {"stop": stop, "blocked_actions": {},
                    "completion_candidates": [candidate] if has_candidate else []}

        def _completion_candidates(self, scoped, _decision):
            if not scoped.get("unresolved") or not has_candidate:
                return []
            target = run_attempt._company_key(scoped["unresolved"][0])
            return [{**candidate, "target": target}]

        def call(self, name, arguments):
            self.calls.append((name, copy.deepcopy(arguments)))
            if name == "tyche_review":
                decision = arguments["companies"][0]
                row["reason_text"] = decision["reason"]
                if decision["decision"] in {"accept", "reject"}:
                    document["unresolved"].clear()
                if decision["decision"] == "accept":
                    accepted = copy.deepcopy(row)
                    accepted["company"] = accepted.pop("candidate")
                    document["accepted"].append(accepted)
                return {"status": "confirmed_leads_saved", "delivery_allowed": False}
            assert name == "tyche_finish"
            if stop not in run_attempt.DELIVERY_STOPS:
                return {"status": "needs_research", "delivery_allowed": False}
            review = self.review_delivery(document)
            if review is not None:
                return review
            return {"status": "delivered", "delivery_allowed": True,
                    "checkpoint_saved": bool(document["accepted"])}

    tools = LabTools.__new__(LabTools)
    tools.research = Research()
    tools._native_review_delivery = None
    tools._completion_assessments = set()
    tools.lock = threading.Lock()
    tools.delivered = False
    tools.broker = SimpleNamespace(local_dispatch_budget=lambda: {"providers": {}})
    tools._publish_confirmed = lambda: ({
        "checkpoint_saved": True, "confirmed_count": len(document["accepted"]),
    } if document["accepted"] else None)
    tools._review_delivery = lambda _document, *_args: tools._completion_review_gate()
    return tools, document


@pytest.mark.parametrize("stop", ["time_limit_reached", "budget_exhausted"])
def test_finish_requires_explicit_review_of_ready_contact_at_delivery_stop(
    tmp_path, stop,
):
    tools, document = completion_review_tools(tmp_path, stop)

    blocked = tools.call("tyche_finish", {})

    assert blocked["status"] == "completion_review_required"
    assert blocked["delivery_allowed"] is False
    assert blocked["completion_candidates"][0]["target"] == "ready.example"
    assert blocked["completion_candidates"][0]["saved_hold_reason"] == document["unresolved"][0]["reason_text"]
    assert blocked["completion_candidates"][0]["assessment_ref"].startswith("ready-contact:")
    assert set(blocked["completion_candidates"][0]) == {
        "target", "assessment_ref", "saved_hold_reason", "profile_verified", "email_usable", "missing",
    }
    assert tools.research.calls == [("tyche_finish", {})]


def test_finish_reviews_every_ready_contact_beyond_native_advice_preview(tmp_path):
    tools, document = completion_review_tools(tmp_path)
    original = document["unresolved"][0]
    for index in range(2, 6):
        row = copy.deepcopy(original)
        row["candidate"]["domain"] = f"ready-{index}.example"
        row["reason_text"] = f"Saved hold reason {index}"
        document["unresolved"].append(row)

    blocked = tools.call("tyche_finish", {})

    assert [item["target"] for item in blocked["completion_candidates"]] == [
        "ready.example", "ready-2.example", "ready-3.example", "ready-4.example", "ready-5.example",
    ]


def test_failed_hold_publication_does_not_release_ready_contact_gate(tmp_path):
    tools, _document = completion_review_tools(tmp_path)
    tools._publish_confirmed = lambda: (_ for _ in ()).throw(RuntimeError("checkpoint failed"))

    with pytest.raises(RuntimeError, match="checkpoint failed"):
        tools.call("tyche_review", {"companies": [{
            "target": "ready.example", "decision": "hold_contact",
            "reason": "Keep this candidate held after reviewing the saved evidence",
        }]})

    assert not tools._completion_assessments
    assert tools.call("tyche_finish", {})["status"] == "completion_review_required"


@pytest.mark.parametrize("ready,has_candidate", [(False, True), (True, False)])
def test_finish_keeps_missing_evidence_and_empty_shortfalls_unchanged(
    tmp_path, ready, has_candidate,
):
    tools, _document = completion_review_tools(
        tmp_path, ready=ready, has_candidate=has_candidate,
    )

    result = tools.call("tyche_finish", {})

    assert result["status"] == "delivered"
    assert [name for name, _arguments in tools.research.calls] == ["tyche_finish"]


def test_review_demotion_resumes_same_run_before_research_deadline(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model_provider = "arena"\n')

    @contextmanager
    def session(**selection):
        yield IdleEnvironment(CODEX_HOME=str(codex_home), PYTHONPATH="/agent:/agent/source:/agent/deps")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seed_supervisor(run_dir)
    calls = []
    states = iter((
        {"stop": "target_met", "operational_block": None},
        {"stop": "continue", "operational_block": None},
    ))

    def execute_once(_host, directory, environment, prompt, timeout, _tail):
        calls.append((dict(environment), prompt, timeout))
        return 0

    monkeypatch.setattr(ResearchTools, "_overview", lambda _path: next(states))
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _directory: len(calls) >= 2)
    now = time.monotonic()
    host = SimpleNamespace(session=session, CODEX_BINARY="/usr/local/bin/codex")
    runtime.launch(host, run_dir, now + 120, now + 420, 420,
                   quota_guard(now + 120, now + 420))

    assert len(calls) == 2
    assert calls[0][0]["TYCHE_FINALIZATION_ONLY"] == "1"
    assert "save them and return" in calls[0][1]
    assert 0 < calls[0][2] <= 420
    assert calls[1][0]["TYCHE_FINALIZATION_ONLY"] == "0"
    assert calls[1][1].startswith("Continue the SAME saved run")


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
    seed_supervisor(directory)
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
    monkeypatch.setattr(ResearchTools, "_overview", lambda _path: {"stop": "continue"})
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _path: len(calls) == 2)
    host = SimpleNamespace(session=session, CODEX_BINARY="codex")
    if old_request_finishes:
        runtime.launch(host, directory, 110, 120, 20,
                       quota_guard(110, 120, clock=lambda: clock[0]))
        assert len(calls) == 2 and calls[1][1] == pytest.approx(3, abs=.01)
    else:
        with pytest.raises(RuntimeError, match="host_limit"):
            runtime.launch(host, directory, 110, 120, 20,
                           quota_guard(110, 120, clock=lambda: clock[0]))
        assert len(calls) == 1
    assert waits == pytest.approx([20, 10], abs=0.01)


def test_quota_guard_waits_for_fresh_authoritative_headroom(monkeypatch):
    clock = [100.0]
    snapshots = []
    used = iter((19, 19, 19, 20))

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

    assert admitted == openrouter_limit - 40
    assert used[0] == openrouter_limit - 40
    assert guard.research_denial == "finalization_headroom"
    guard.set_phase("finalization")
    for _ in range(40):
        assert guard() is True
        used[0] += 1
    assert used[0] == openrouter_limit
    assert guard() is False
    assert guard() is False


def test_finalization_can_finish_after_reference_and_packet_review_with_host_retries(monkeypatch):
    used = [159]
    monkeypatch.setattr(runtime, "QUOTA_SNAPSHOT_FRESHNESS_SECONDS", 0)
    now = time.monotonic()
    guard = quota_guard(now + 200, now + 300, lambda: quota_snapshot(used=used[0]))
    assert guard() is True
    # One admitted research request may consume twelve host identities.
    used[0] += 12
    assert guard() is False
    assert guard.research_denial == "finalization_headroom"
    guard.set_phase("finalization")
    # The pilot needed nineteen reference/paging/review turns and still had
    # to approve and finish. These are model turns, not extra provider calls.
    for _ in range(19):
        assert guard() is True
        used[0] += 1
    for _ in range(2):
        assert guard() is True
        used[0] += 1
    assert used[0] == 192
    while used[0] < 200:
        assert guard() is True
        used[0] += 1
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
    calls = [quota_snapshot()] + [QuotaUnavailable("quota unavailable")] * 6
    monkeypatch.setattr(runtime, "QUOTA_SNAPSHOT_FRESHNESS_SECONDS", 0)
    monkeypatch.setattr(runtime.threading.Event, "wait", lambda _self, delay: clock.__setitem__(0, clock[0] + delay))
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
    assert not calls


@pytest.mark.parametrize("phase", ["research", "finalization"])
@pytest.mark.parametrize("failures", [1, 2])
def test_quota_read_recovers_transient_unavailability_before_admission(monkeypatch, phase, failures):
    clock = [100.0]
    values = [quota_snapshot(used=30)] + [QuotaUnavailable("unavailable")] * failures + [quota_snapshot(used=31)]
    reads = []

    def reader():
        reads.append(clock[0])
        value = values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(runtime.threading.Event, "wait", lambda _self, delay: clock.__setitem__(0, clock[0] + delay))
    guard = quota_guard(110.0, 120.0, reader, clock=lambda: clock[0])
    guard.set_phase(phase)
    assert guard() is True
    assert guard.research_denial is None
    assert guard._last_used == 31
    assert len(reads) == failures + 2 and not values
    assert clock[0] == pytest.approx(100 + failures * runtime.QUOTA_READ_RETRY_SECONDS)


@pytest.mark.parametrize("phase", ["research", "finalization"])
def test_quota_read_retry_stops_at_original_phase_deadline(monkeypatch, phase):
    clock = [100.0]
    reads = []

    def reader():
        reads.append(clock[0])
        if len(reads) == 1:
            return quota_snapshot(used=30)
        raise QuotaUnavailable("unavailable")

    monkeypatch.setattr(runtime.threading.Event, "wait", lambda _self, delay: clock.__setitem__(0, clock[0] + delay))
    guard = quota_guard(100.4, 100.8, reader, clock=lambda: clock[0])
    guard.set_phase(phase)
    assert guard() is False
    assert reads == [100, 100]
    assert clock[0] == (100.4 if phase == "research" else 100.8)
    assert guard() is False
    assert len(reads) == 2


@pytest.mark.parametrize("used,reason", [(29, "quota_regressed"), (181, "finalization_headroom")])
def test_recovered_quota_read_still_enforces_monotonic_usage_and_headroom(monkeypatch, used, reason):
    values = iter([quota_snapshot(used=30), QuotaUnavailable("unavailable"), quota_snapshot(used=used)])

    def reader():
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(runtime, "QUOTA_READ_RETRY_SECONDS", .001)
    now = time.monotonic()
    guard = quota_guard(now + 10, now + 20, reader)
    assert guard() is False
    assert guard.research_denial == reason


@pytest.mark.parametrize("failure_at", [1, 2])
def test_transient_quota_read_preserves_native_research_review_and_checkpoint(lab, monkeypatch, failure_at):
    reads = []

    def reader():
        reads.append((lab.worker_starts, len(lab.frames), lab.openrouter_used))
        if len(reads) == failure_at:
            raise QuotaUnavailable("unavailable")
        return quota_snapshot(used=lab.openrouter_used)

    monkeypatch.setattr(runtime, "QUOTA_READ_RETRY_SECONDS", .001)
    monkeypatch.setattr(sys.modules["lab_arena_checkpoint"], "quota_usage", reader)
    rows = runtime.run(ICP)
    assert len(rows) == 1
    assert rows[0]["intent_details"] == PARAGRAPH
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert len(lab.processes) == 1 and lab.openrouter_used == 1
    assert len(reads) == 3 and all(provider_frames == 0 for _, provider_frames, _ in reads)
    assert lab.request_guard.research_denial is None
    assert lab.session_closed


def test_headroom_boundary_enters_finalization_without_relaunching_research_worker(lab, monkeypatch):
    lab.openrouter_used = 200 - runtime.OPENROUTER_RESEARCH_HEADROOM
    lab.mode = "early_clean"
    rows = runtime.run(ICP)
    assert len(rows) == 1
    assert len(lab.processes) == 2
    assert lab.processes[0].kwargs["env"]["TYCHE_FINALIZATION_ONLY"] == "0"
    assert "TYCHE_HOST_RESEARCH_STOP" not in lab.processes[0].kwargs["env"]
    assert all(process.kwargs["env"]["TYCHE_FINALIZATION_ONLY"] == "1"
               for process in lab.processes[1:])
    assert all(process.kwargs["env"]["TYCHE_HOST_RESEARCH_STOP"] == "finalization_headroom"
               for process in lab.processes[1:])
    assert lab.openrouter_used == 200 - runtime.OPENROUTER_RESEARCH_HEADROOM + 1
    assert lab.output.exists()
    status = json.loads((lab.processes[0].run_dir / "worker-status.json").read_text())
    assert status["host_reason"] == "finalization_headroom"
    assert status["delivery_allowed"] is True


def test_admitted_research_can_drain_past_soft_deadline(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')
    directory = tmp_path / "run"
    directory.mkdir()
    seed_supervisor(directory)
    clock = [100.0]
    calls, guard_holder = [], {}

    @contextmanager
    def session(**selection):
        guard_holder["guard"] = selection["request_guard"]
        yield IdleEnvironment(CODEX_HOME=str(home))

    def execute_once(_host, _directory, environment, prompt, timeout, _tail):
        calls.append((dict(environment), timeout, clock[0]))
        if len(calls) == 1:
            assert timeout == 30.0 + runtime.PROCESS_RECEIPT_MARGIN_SECONDS
            assert guard_holder["guard"]() is True
            # The MCP child returns after research admission closes. Its
            # process remains alive to save the response, then the next model
            # admission reports the ordinary research cutoff.
            clock[0] = 115.0
            assert guard_holder["guard"]() is False
            return 1
        return 0

    used = iter((0, 0, 1))
    reader = lambda: quota_snapshot(used=next(used))
    guard = quota_guard(110.0, 130.0, reader, clock=lambda: clock[0])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ResearchTools, "_overview", lambda _path: {"stop": "continue"})
    monkeypatch.setattr(runtime, "_codex_once", execute_once)
    monkeypatch.setattr(runtime, "full_delivery", lambda _path: len(calls) == 2)

    runtime.launch(SimpleNamespace(session=session, CODEX_BINARY="codex"), directory,
                   110.0, 130.0, 30.0, guard)

    assert calls[0][1] == 30.0 + runtime.PROCESS_RECEIPT_MARGIN_SECONDS
    assert calls[1][0]["TYCHE_FINALIZATION_ONLY"] == "1"
    assert calls[1][2] == 115.0


def test_real_provider_child_captures_after_research_cutoff_then_recovers_once(
        tmp_path, monkeypatch):
    native_tests = ROOT / ".agents/skills/lead-sourcing/tests"
    sys.path.insert(0, str(native_tests))
    try:
        from test_stop_policy import action, stop_document
    finally:
        sys.path.remove(str(native_tests))

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_file = run_dir / "results.json"
    document = stop_document([], target_count=25)
    document["request"]["contact_fields"] = []
    document["stop_audit"] = {"route_frontier": []}
    run_file.write_text(json.dumps(document))
    budget_guard.initialize(run_file, max_usd=2, scrapingdog_usd_per_credit=.1)
    request_file = run_dir / "request.txt"
    request_file.write_text("fixture")
    spec = {
        "action": dict(
            action("one", provider="deepline", paid_calls=1,
                   cost_upper_bound_credits=.2),
            phase="account_discovery", approach="product-search",
        ),
        "request": {
            "operation": "execute", "tool": "fixture-search",
            "payload": {"query": "custom tablecloths"},
        },
    }
    marker = run_dir / "dispatches.txt"
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, time\n"
        "from pathlib import Path\n"
        "import budget_guard, run_attempt\n"
        "run_file = Path(os.environ['FIXTURE_RUN_FILE'])\n"
        "spec = json.loads(os.environ['FIXTURE_SPEC'])\n"
        "marker = Path(os.environ['FIXTURE_MARKER'])\n"
        "def interrupted(request, capture):\n"
        "    def dispatch():\n"
        "        with marker.open('a') as stream: stream.write('dispatch\\n')\n"
        "        time.sleep(0.15)\n"
        "        capture({'exit_code': 0, 'body': {'status': 'completed', 'result': {'data': []}}, 'stderr': ''})\n"
        "        raise OSError('fixture interruption after capture')\n"
        "    return budget_guard.guarded_call(request, 'deepline', dispatch)\n"
        "try:\n"
        "    run_attempt.run_attempt(run_file, spec, execute=interrupted)\n"
        "except OSError:\n"
        "    pass\n",
    )
    executable.chmod(0o755)
    started = time.monotonic()
    research_cutoff = started + .05
    response_deadline = started + 2

    class DeadlineGuard:
        phase = None

        def set_phase(self, phase):
            self.phase = phase

        @property
        def research_denial(self):
            return "research_deadline" if time.monotonic() >= research_cutoff else None

        def __call__(self):
            return time.monotonic() < research_cutoff

    guard = DeadlineGuard()

    class Environment(dict):
        @staticmethod
        def wait_idle(timeout_seconds):
            assert timeout_seconds >= 0
            return True

    scripts = ROOT / ".agents/skills/lead-sourcing/scripts"
    environment = Environment(
        os.environ,
        PYTHONPATH=os.pathsep.join((str(ROOT), str(scripts))),
        FIXTURE_RUN_FILE=str(run_file), FIXTURE_SPEC=json.dumps(spec),
        FIXTURE_MARKER=str(marker),
    )
    host = runtime.ArenaHost(
        SimpleNamespace(CODEX_BINARY=str(executable)), run_dir, environment,
        response_deadline, guard,
    )
    monkeypatch.setattr(runtime, "full_delivery", lambda _run_dir: False)

    _status, execution = host.run_once(
        [str(executable), "exec", "fixture"], request_file, environment, tmp_path,
        deadline=lambda: time.time() + .05, terminal=False, attempt=0,
    )

    assert time.monotonic() >= research_cutoff
    assert guard() is False
    assert execution["failure_kind"] == "deadline_reached"
    receipt = run_dir / "receipts/one.json"
    captured = json.loads(receipt.read_text())
    assert captured["receipt_status"] == "response_received"
    recovery = run_attempt.recover_completed_attempts(run_file)
    assert recovery == {"recovered": ["one"], "pending": [], "errors": []}
    saved = json.loads(receipt.read_text())
    assert saved["provider_response"] == captured["provider_response"]
    assert saved["receipt_status"] == "complete"
    assert marker.read_text().splitlines() == ["dispatch"]
    replay = []
    with pytest.raises(ValueError, match="already attempted or pending"):
        run_attempt.run_attempt(run_file, {**spec, "action": {**spec["action"], "id": "retry"}},
                                execute=lambda *_args: replay.append(True))
    assert replay == []

    final_calls = []
    monkeypatch.setattr(runtime, "_codex_once",
                        lambda *_args: final_calls.append(True) or 0)
    host.run_once(
        [str(executable), "exec", "finalize"], request_file, environment, tmp_path,
        deadline=lambda: time.time() + 1, terminal=True, attempt=1,
    )
    assert guard.phase == "finalization" and final_calls == [True]


def test_arena_cleanup_end_still_kills_the_real_process_group(tmp_path):
    completed = tmp_path / "completed"
    executable = tmp_path / "slow-codex"
    executable.write_text(
        "#!" + sys.executable + "\n"
        "import os, time\n"
        "from pathlib import Path\n"
        "time.sleep(2)\n"
        "Path(os.environ['FIXTURE_COMPLETED']).write_text('late')\n",
    )
    executable.chmod(0o755)
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        runtime._codex_once(
            SimpleNamespace(CODEX_BINARY=str(executable)), tmp_path,
            dict(os.environ, FIXTURE_COMPLETED=str(completed)),
            "fixture", .1, bytearray(),
        )

    assert time.monotonic() - started < 1
    assert not completed.exists()


def test_recovery_at_absolute_deadline_drains_idle_state_before_host_stop(
        tmp_path, monkeypatch):
    waits = []

    class Environment(dict):
        @staticmethod
        def wait_idle(timeout_seconds):
            waits.append(timeout_seconds)
            return True

    guard = SimpleNamespace(research_denial=None, set_phase=lambda phase: None)
    host = runtime.ArenaHost(
        SimpleNamespace(CODEX_BINARY="codex"), tmp_path, Environment(),
        time.monotonic() - 1, guard,
    )

    assert host.before_recovery(tmp_path / "request.txt", {}) is None
    assert waits == [0]
    assert host.prepare(tmp_path / "request.txt", {})["reason"] == "host_deadline"
    monkeypatch.setattr(runtime, "_codex_once",
                        lambda *_args: pytest.fail("post-deadline process must not start"))
    monkeypatch.setattr(runtime, "full_delivery", lambda _run_dir: False)
    request_file = tmp_path / "request.txt"
    request_file.write_text("fixture")
    _status, execution = host.run_once(
        ["codex", "exec", "fixture"], request_file, Environment(), tmp_path,
        deadline=lambda: time.time() + 1, terminal=False, attempt=0,
    )
    assert execution["failure_kind"] == "deadline_reached"


def test_idle_timeout_keeps_a_reviewed_partial_checkpoint(lab, monkeypatch, capsys):
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
    assert failure["error"] == "RuntimeError"
    diagnostics = [json.loads(line.removeprefix(runtime.EXECUTION_DIAGNOSTIC_PREFIX))
                   for line in capsys.readouterr().err.splitlines()
                   if line.startswith(runtime.EXECUTION_DIAGNOSTIC_PREFIX)]
    failures = [row for row in diagnostics if row["event"] == "supervisor_failure"]
    assert failures == [{"schema_version": 1, "event": "supervisor_failure",
                         "failure_class": "runtime_error", "reason": "unexpected"}]


def test_execution_diagnostics_are_closed_payload_free_and_nonthrowing(capsys, monkeypatch):
    secret = "PRIVATE_SECRET_VALUE"
    runtime.emit_supervisor_failure(RuntimeError("unrecognized " + secret))
    lines = [line for line in capsys.readouterr().err.splitlines()
             if line.startswith(runtime.EXECUTION_DIAGNOSTIC_PREFIX)]
    assert len(lines) == 1
    assert all(len((line + "\n").encode("ascii")) <= runtime.MAX_EXECUTION_DIAGNOSTIC_BYTES
               and secret not in line for line in lines)
    failure = json.loads(lines[0].removeprefix(runtime.EXECUTION_DIAGNOSTIC_PREFIX))
    assert failure == {"schema_version": 1, "event": "supervisor_failure",
                       "failure_class": "runtime_error", "reason": "unexpected"}
    for invalid in ({**failure, "schema_version": True},
                    {**failure, "failure_class": []},
                    {**failure, "reason": {}},
                    {**failure, "secret": secret}):
        assert runtime._diagnostic_line(invalid) is None

    class BrokenStderr:
        def write(self, _value):
            raise OSError(secret)
        def flush(self):
            raise OSError(secret)

    monkeypatch.setattr(runtime.sys, "stderr", BrokenStderr())
    runtime.emit_supervisor_failure(subprocess.TimeoutExpired("codex", 1))


def test_checkpoint_transition_is_closed_bounded_and_payload_free(tmp_path):
    run_file = tmp_path / "results.json"
    run_file.write_text(json.dumps({"accepted": [], "rejected": [], "unresolved": []}))
    before = [{"company_linkedin": "https://linkedin.com/company/private", "value": "PRIVATE"}]
    summary = arena_output.checkpoint_transition(run_file, before, [])
    line = runtime._diagnostic_line({
        "schema_version": 1, "event": "checkpoint_transition", **summary})
    assert summary["reason"] == "missing_accepted"
    assert line is not None and len(line.encode("ascii")) <= runtime.MAX_CHECKPOINT_DIAGNOSTIC_BYTES
    assert "PRIVATE" not in line and "linkedin" not in line
    assert runtime.MAX_EXECUTION_DIAGNOSTIC_BYTES == 256
    for invalid in (
        {"schema_version": 1, "event": "checkpoint_transition", **summary, "private": "PRIVATE"},
        {"schema_version": 1, "event": "checkpoint_transition", **summary, "final_count": True},
        {"schema_version": 1, "event": "checkpoint_transition", **summary, "reason": "unchanged"},
    ):
        assert runtime._diagnostic_line(invalid) is None


@pytest.mark.parametrize("state,reason,count", [
    ("rejected", "rejected", "rejected_count"),
    ("unresolved", "unresolved", "unresolved_count"),
])
def test_checkpoint_transition_classifies_final_reduction(tmp_path, state, reason, count):
    run_file = tmp_path / "results.json"
    identity = "https://linkedin.com/company/example"
    run_file.write_text(json.dumps({
        "accepted": [], state: [{"company": {"linkedin_url": identity}}],
        "rejected" if state == "unresolved" else "unresolved": [],
    }))
    summary = arena_output.checkpoint_transition(
        run_file, [{"company_linkedin": identity}], [])
    assert summary["reason"] == reason and summary[count] == 1


def test_checkpoint_transition_distinguishes_changed_and_mixed(tmp_path, monkeypatch):
    run_file = tmp_path / "results.json"
    one = "https://linkedin.com/company/one"
    two = "https://linkedin.com/company/two"
    run_file.write_text(json.dumps({
        "request": {"original_text": "{}"},
        "accepted": [{"company": {"linkedin_url": one}}],
        "rejected": [], "unresolved": [],
    }))
    monkeypatch.setattr(arena_output, "_project_companies", lambda *_args, **_kwargs: [
        {"company_linkedin": one, "value": "new"}
    ])
    changed = arena_output.checkpoint_transition(
        run_file, [{"company_linkedin": one, "value": "old"}],
        [],
    )
    assert changed["reason"] == "changed_accepted" and changed["changed_count"] == 1

    monkeypatch.setattr(arena_output, "_project_companies", lambda *_args, **_kwargs: [])
    run_file.write_text(json.dumps({
        "accepted": [], "rejected": [{"company": {"linkedin_url": one}}],
        "unresolved": [{"company": {"linkedin_url": two}}],
    }))
    mixed = arena_output.checkpoint_transition(
        run_file, [{"company_linkedin": one}, {"company_linkedin": two}], [])
    assert mixed["reason"] == "mixed"
    assert mixed["rejected_count"] == mixed["unresolved_count"] == 1


def test_changed_row_still_delivered_is_outside_revocation_audit(tmp_path):
    run_file = tmp_path / "results.json"
    run_file.write_text(json.dumps({
        "accepted": [], "rejected": [], "unresolved": [],
    }))
    identity = "https://linkedin.com/company/example"
    assert arena_output.checkpoint_transition(
        run_file,
        [{"company_linkedin": identity, "value": "old"}],
        [{"company_linkedin": identity, "value": "changed"}],
    ) is None


def test_removed_changed_accepted_row_is_not_reported_as_rejected(
        tmp_path, monkeypatch):
    run_file = tmp_path / "results.json"
    identity = "https://linkedin.com/company/example"
    run_file.write_text(json.dumps({
        "request": {"original_text": "{}"},
        "accepted": [{"company": {"linkedin_url": identity}}],
        "rejected": [{"company": {"linkedin_url": identity}}],
        "unresolved": [],
    }))
    monkeypatch.setattr(arena_output, "_project_companies", lambda *_args, **_kwargs: [
        {"company_linkedin": identity, "value": "changed"}
    ])
    summary = arena_output.checkpoint_transition(
        run_file, [{"company_linkedin": identity, "value": "old"}],
        [],
    )
    assert summary["reason"] == "changed_accepted"
    assert summary["changed_count"] == 1 and summary["rejected_count"] == 0


def test_log_selection_prefers_matching_non_unchanged_transition(tmp_path):
    rows = []
    digest = arena_output.canonical_output_sha256(rows)
    changed = "sha256:" + "a" * 64
    missing = {
        "schema_version": 1, "event": "checkpoint_transition", "reason": "missing_accepted",
        "checkpoint_count": 1, "final_count": 0, "rejected_count": 0,
        "unresolved_count": 0, "changed_count": 0, "missing_count": 1,
        "checkpoint_sha256": changed, "final_sha256": digest,
    }
    unchanged = {**missing, "reason": "unchanged", "checkpoint_count": 0,
                 "missing_count": 0, "checkpoint_sha256": digest}
    (tmp_path / "codex.log").write_text(
        runtime._diagnostic_line(missing) + runtime._diagnostic_line(unchanged)
        + runtime.EXECUTION_DIAGNOSTIC_PREFIX + '{"private":"PRIVATE"}\n')
    assert runtime._logged_checkpoint_transition(tmp_path, rows) == missing


def test_native_checkpoint_log_writer_is_bounded_and_preserved(tmp_path):
    rows = []
    digest = arena_output.canonical_output_sha256(rows)
    summary = {
        "reason": "unchanged", "checkpoint_count": 0, "final_count": 0,
        "rejected_count": 0, "unresolved_count": 0, "changed_count": 0,
        "missing_count": 0, "checkpoint_sha256": digest, "final_sha256": digest,
    }
    assert runtime.retain_checkpoint_transition(tmp_path, summary) is True
    payload = runtime._bounded_codex_log(tmp_path)
    assert payload is not None
    assert runtime._closed_checkpoint_lines(payload) == [payload]
    assert runtime._logged_checkpoint_transition(tmp_path, rows)["reason"] == "unchanged"

    retraction = {
        **summary, "reason": "missing_accepted", "checkpoint_count": 1,
        "missing_count": 1, "checkpoint_sha256": "sha256:" + "a" * 64,
    }
    (tmp_path / "codex.log").write_bytes(b"x" * runtime.MAX_LOG_BYTES)
    assert runtime.retain_checkpoint_transition(tmp_path, retraction) is True
    assert (tmp_path / "codex.log").stat().st_size == runtime.MAX_LOG_BYTES
    assert runtime._logged_checkpoint_transition(tmp_path, rows)["reason"] == "missing_accepted"
    with (tmp_path / "codex.log").open("ab") as stream:
        stream.write(b"x")
    assert runtime._bounded_codex_log(tmp_path) is None

    (tmp_path / "codex.log").unlink()
    target = tmp_path / "other.log"
    target.write_bytes(b"")
    (tmp_path / "codex.log").symlink_to(target)
    assert runtime.retain_checkpoint_transition(tmp_path, summary) is False
    assert target.read_bytes() == b""


@pytest.mark.parametrize("exc,expected", [
    (RuntimeError("TYCHE saved dispatch accounting is incomplete: PRIVATE"),
     ("runtime_error", "saved_dispatch_accounting")),
    (RuntimeError("TYCHE run is operationally blocked: PRIVATE"),
     ("runtime_error", "operational_block")),
    (RuntimeError("Lab Codex failed twice before delivery"),
     ("runtime_error", "two_failed_codex_exits")),
    (RuntimeError("Lab Codex exited repeatedly without saved progress"),
     ("runtime_error", "unchanged_exit_limit")),
    (RuntimeError("Arena Codex invocation limit reached before delivery"),
     ("runtime_error", "invocation_limit")),
    (ValueError("No reviewed TYCHE checkpoint was delivered"),
     ("validation_error", "checkpoint_unavailable")),
    (ValueError("Lab output differs from the reviewed TYCHE checkpoint"),
     ("validation_error", "output_validation")),
])
def test_supervisor_diagnostic_known_failure_mapping(exc, expected, capsys):
    runtime.emit_supervisor_failure(exc)
    line = capsys.readouterr().err.strip()
    document = json.loads(line.removeprefix(runtime.EXECUTION_DIAGNOSTIC_PREFIX))
    assert (document["failure_class"], document["reason"]) == expected


def test_repeated_clean_noop_exits_are_bounded(lab):
    lab.mode = "clean_noop"

    with pytest.raises(RuntimeError, match="repeated_worker_no_progress"):
        runtime.run(ICP)

    assert len(lab.sessions) == 1
    assert len(lab.processes) == runtime.runner.MAX_UNCHANGED_EXITS


def test_mcp_relaunch_restores_deepline_dispatch_count_from_durable_ledger(lab):
    runtime.run(ICP)
    run_file = lab.research[0].research.path
    ledger = budget_guard.load_ledger(run_file)
    expected = sum(call["provider"] == "deepline" for call in ledger["calls"].values())

    resumed = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)

    assert expected > 0
    budget = resumed.broker.local_dispatch_budget()["providers"]["deepline"]
    assert budget == {"used": expected}


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

    def request_call(_self, operation, parameters, *, admitted=False, timeout_seconds=None):
        assert operation == "deepline.execute" and admitted is True
        assert timeout_seconds == 240.0
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


def test_catalog_search_filters_zero_scores_and_keeps_empty_query_browsing(tmp_path):
    catalog = {
        "company": {"toolId": "company", "description": "Company profile lookup"},
        "news": {"toolId": "news", "description": "Recent article search"},
    }
    broker = Broker(tmp_path / "unused.sock", time.monotonic() + 30, catalog=catalog)

    missing, code = broker.execute({"operation": "search", "query": "patents", "limit": 10}, lambda _: None)
    browsing, _ = broker.execute({"operation": "search", "query": "", "limit": 1}, lambda _: None)
    matching, _ = broker.execute({"operation": "search", "query": "article", "limit": 10}, lambda _: None)

    assert code == 0 and missing["status"] == "no_results" and missing["results"] == []
    assert browsing["status"] == "ok" and browsing["results"] == [catalog["company"]]
    assert matching["status"] == "ok" and matching["results"] == [catalog["news"]]


def test_catalog_schemas_remain_valid_after_receipt_redaction():
    from jsonschema.validators import validator_for

    catalog = json.loads((ROOT / "tyche_arena/catalog.json").read_text())["tools"]
    for contract in catalog.values():
        schema = deepline.redact(contract).get("inputSchema", {}).get("jsonSchema")
        if schema is not None:
            validator_for(schema).check_schema(schema)


def test_bounceban_catalog_matches_host_and_rejects_webhook_before_dispatch(arena_operations):
    import research_input

    contract = json.loads((ROOT / "tyche_arena/catalog.json").read_text())["tools"]["bounceban_verify_single"]
    allowed = {"email", "mode", "disable_catchall_verify"}
    assert {field["name"] for field in contract["inputSchema"]["fields"]} == allowed
    assert set(contract["inputSchema"]["jsonSchema"]["properties"]) == allowed
    dispatched = []

    def prepare(payload):
        request = {"operation": "execute", "tool": "bounceban_verify_single", "payload": payload}
        research_input.check_tool_contract({"results": [contract]}, request)
        frame = {"tool": request["tool"], "payload": payload}
        dispatched.append(frame)
        return arena_operations.validate_operation_request("deepline.execute", frame)

    with pytest.raises(ValueError, match="Additional properties are not allowed.*url"):
        prepare({"email": "buyer@example.com", "url": "https://example.org/hook"})
    assert dispatched == []

    payload = {"email": "buyer@example.com", "mode": "deepverify", "disable_catchall_verify": "1"}
    assert prepare(payload) == {"tool": "bounceban_verify_single", "payload": payload}
    assert dispatched == [{"tool": "bounceban_verify_single", "payload": payload}]


def test_contextdev_catalog_is_free_and_matches_the_host_route(arena_operations):
    import research_input

    name = "contextdev_get_web_scrape_markdown"
    contract = json.loads((ROOT / "tyche_arena/catalog.json").read_text())["tools"][name]
    schema = contract["inputSchema"]["jsonSchema"]
    assert contract["toolId"] == name and contract["provider"] == "contextdev"
    assert contract["callable"] is True
    assert contract["pricing"] == {
        "displayText": "Free",
        "unit": "call",
        "creditsPerUnit": 0,
        "usdPerUnit": 0,
        "currency": "USD",
        "summary": None,
        "details": [],
    }
    assert schema["required"] == ["url"] and schema["additionalProperties"] is False
    payload = {"url": "https://example.com/about", "includeLinks": True}
    request = {"operation": "execute", "tool": name, "payload": payload}
    research_input.check_tool_contract({"results": [contract]}, request)
    assert arena_operations.validate_operation_request(
        "deepline.execute", {"tool": name, "payload": payload}
    ) == {"tool": name, "payload": payload}


def test_contextdev_native_bridge_dispatches_and_normalizes_free_page(tmp_path, monkeypatch):
    name = "contextdev_get_web_scrape_markdown"
    contract = json.loads((ROOT / "tyche_arena/catalog.json").read_text())["tools"][name]
    broker = Broker(
        tmp_path / "unused.sock",
        time.monotonic() + 30,
        catalog={name: contract},
    )
    frames = []

    def request_call(_operation, parameters, *, admitted=False, timeout_seconds=None):
        frames.append((copy.deepcopy(parameters), admitted, timeout_seconds))
        return 200, {}, {
            "job_id": "iad1::contextdev",
            "status": "completed",
            "result": {"data": {
                "success": True,
                "url": "https://example.com/about",
                "markdown": "# Example",
                "contentLength": 9,
                "metadata": {
                    "sourceUrl": "https://example.com/about",
                    "finalUrl": "https://example.com/about",
                },
            }},
        }

    monkeypatch.setattr(broker, "request", request_call)
    monkeypatch.setattr(
        budget_guard,
        "guarded_call",
        lambda _request, _provider, dispatch, *, tariff=None: dispatch(),
    )
    captured = []
    request = {
        "operation": "execute",
        "tool": name,
        "payload": {"url": "https://example.com/about"},
        "timeout_seconds": 30,
        "limit": 1,
        "spend": {"max_cost_credits": 0},
    }
    assert broker._requires_paid_dispatch(request, "deepline") is False
    result, code = broker.execute(request, captured.append)

    assert code == 0 and result["status"] == "ok", (code, result, captured, frames)
    assert result["results"][0]["evidence_text"] == "# Example"
    assert frames == [(
        {"tool": name, "payload": {"url": "https://example.com/about"}},
        True,
        30,
    )]
    assert captured[0]["arena"]["status"] == 200


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
    with pytest.raises(ValueError, match="billing_pending"):
        resumed.call("tyche_lookup", lookup(
            "harvestapi_get_company", {"url": "https://www.linkedin.com/company/another-example"}))
    assert budget_guard.load_ledger(run_file) == before


def test_mcp_relaunch_does_not_treat_known_http_422_as_transport_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(write=lambda rows: None))
    run_file = tmp_path / "run" / "results.json"
    seed = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    ResearchTools(run_file, execute=seed.execute).start(request=request_for(ICP, 1, 30), max_usd=.5)
    session = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)
    dispatched = []

    def known_failure(_self, operation, parameters, *, admitted=False, timeout_seconds=None):
        assert operation == "deepline.execute" and admitted is True
        assert timeout_seconds == 240.0
        dispatched.append(parameters)
        return 422, {}, {"status": "error", "error": {"code": "invalid_input", "message": "fixture"}}

    monkeypatch.setattr(Broker, "request", known_failure)
    session.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    first_call = next(iter(budget_guard.load_ledger(run_file)["calls"].values()))
    assert first_call["actual_credits"] is None
    resumed = LabTools(run_file, time.monotonic() + 30, time.monotonic() + 60)

    assert resumed.broker.provider_blocked is False
    with pytest.raises(ValueError, match="billing_pending"):
        resumed.call("tyche_lookup", lookup(
            "harvestapi_get_company", {"url": "https://www.linkedin.com/company/another-example"}))
    assert len(dispatched) == 1
    assert len(budget_guard.load_ledger(run_file)["calls"]) == 1


@pytest.mark.parametrize(("tool", "http_status", "provider_body", "settled_microusd", "expected_cost",
                          "expected_status", "expected_stop"), [
    ("contextdev_post_news_search", 200, {
        "status": "completed",
        "job_id": "contextdev-free-completed",
        "result": {"data": [{
            "title": "Public update",
            "url": "https://example.com/update",
            "description": "Example published an update.",
            "published_at": "2026-09-18T12:00:00.000Z",
        }]},
    }, 0, "0", "ok", "continue"),
    ("contextdev_post_news_search", 422, {
        "error": "INPUT_VALIDATION_ERROR",
        "code": "UPSTREAM_BAD_INPUT",
        "message": "Upstream input validation failed",
        "error_category": "upstream",
        "failure_origin": "provider",
        "operation": "contextdev_post_news_search",
        "provider": "contextdev",
        "upstream_error_code": "INPUT_VALIDATION_ERROR",
        "upstream_status": 400,
        "tool_error": {
            "schemaVersion": "1",
            "code": "UPSTREAM_BAD_INPUT",
            "category": "upstream",
            "origin": "provider",
            "provider": "contextdev",
            "operation": "contextdev_post_news_search",
            "toolId": "contextdev_post_news_search",
            "statusCode": 400,
            "retryable": False,
            "requestId": "fixture-request",
            "networkKind": None,
            "networkScope": None,
            "retryAfterMs": None,
        },
    }, 0, "0", "schema_error", "continue"),
    ("contextdev_post_news_search", 200, {
        "status": "completed",
        "job_id": "contextdev-settled-cost",
        "result": {"data": []},
    }, 60_000, "0.06", "no_results", "continue"),
    ("firecrawl_scrape", 502, {
        "error": {
            "message": "Upstream page fetch failed",
            "code": "UPSTREAM_HTTP_ERROR",
        },
    }, 0, "0", "provider_error", "continue"),
])
def test_authoritative_arena_settlement_reopens_model_admission_without_rewriting_provider_response(
        tmp_path, monkeypatch, arena_worker_runtime, tool, http_status, provider_body,
        settled_microusd, expected_cost, expected_status, expected_stop):
    host = arena_worker_runtime
    socket_path = Path(f"/tmp/arena-settlement-{os.getpid()}-{http_status}.sock")
    run_file = tmp_path / "results.json"
    request_file = tmp_path / "request.txt"
    request_file.write_text("fixture")
    broker = Broker(socket_path, time.monotonic() + 30,
                    response_deadline=time.monotonic() + 60)
    research = ResearchTools(run_file, execute=broker.execute)
    research.start(request=request_for(ICP, 1, 30), max_usd=.5)
    research.inspect(tool=tool)
    inputs = ({"url": "https://example.com/"} if tool == "firecrawl_scrape" else {
        "searchBy": {"type": "entity", "entity": {"type": "domain", "domain": "example.com"}},
        "limit": 3,
    })
    check = lookup(tool, inputs)
    encoded = json.dumps(provider_body, separators=(",", ":")).encode()
    call_identity = "sha256:" + hashlib.sha256(encoded).hexdigest()

    class Api:
        @staticmethod
        def provider(_run_id, _lease_token, frame):
            return host.BrokerResult(
                http_status,
                {deepline.ARENA_SETTLED_MICROUSD_HEADER.upper(): "999"},
                encoded,
                {
                    "call_identity": call_identity,
                    "operation_id": frame["operation_id"],
                    "provider": "deepline",
                    "funding_source": "host",
                    "action_sequence": frame["action_sequence"],
                    "outcome": "settled",
                    "actual_microusd": settled_microusd,
                    "idempotent": False,
                },
            ).to_document()

    worker = host.WorkerSocketServer(
        socket_path,
        Api(),
        host.RunState(
            lease={"run_id": "settlement-proof-run", "kind": "execute"},
            lease_token="settlement-proof-token",
        ),
    )
    worker.start()
    try:
        outcome = research.lookup(check["checks"])["lookups"][0]
    finally:
        worker.stop()

    assert outcome["status"] == expected_status
    ledger = budget_guard.load_ledger(run_file)
    call = ledger["calls"][outcome["route"]]
    assert call["state"] == "settled"
    assert call["actual_credits"] is None and call["actual_usd"] == expected_cost
    receipt = json.loads((run_file.parent / "receipts" / f"{outcome['route']}.json").read_text())
    assert "billing" not in receipt["provider_response"]["body"]
    assert receipt["provider_response"]["arena"]["headers"] == {
        deepline.ARENA_SETTLED_MICROUSD_HEADER: str(settled_microusd),
    }
    assert receipt["billing"] == {
        "cost_usd": expected_cost,
        "basis": "arena_authoritative_settlement",
    }
    replay, _ = deepline.normalize_response(
        receipt["attempt"]["request"], receipt["provider_response"])
    assert replay["billing"] == receipt["billing"]
    assert budget_guard.audit_ledger(run_file, json.loads(run_file.read_text())) == []
    decision = run_attempt.evaluate_stop(json.loads(run_file.read_text()), execution_budget=ledger)
    assert decision["decision"] == expected_stop

    admission = []
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "arena"\n')

    @contextmanager
    def session(**options):
        admission.append(options["request_guard"]())
        yield IdleEnvironment(CODEX_HOME=str(home))

    class Guard:
        research_denial = None
        _research_deadline = time.monotonic() + 30

        @staticmethod
        def set_phase(_phase):
            return None

        @staticmethod
        def __call__():
            return True

    adapter = runtime.ArenaHost(
        SimpleNamespace(session=session, CODEX_BINARY="fixture"),
        tmp_path, IdleEnvironment(), time.monotonic() + 60, Guard())
    execution = runtime.ExecutionReceipt(request_file)
    monkeypatch.setattr(runtime, "_codex_once", lambda *_args, **_kwargs: 0)
    assert adapter.execute_research(
        ["fixture", "exec", "research"], request_file, {}, execution,
        profile=tmp_path, deadline=lambda: time.time(), output=None,
        cost_stop=lambda: None) == 0
    assert admission == [True]


def test_arena_settlement_receipt_conflict_fails_closed():
    request = {
        "operation": "execute",
        "tool": "contextdev_post_news_search",
        "payload": {},
        "limit": 1,
    }
    response = {
        "body": {
            "status": "completed",
            "job_id": "conflicting-billing",
            "result": {"data": []},
            "billing": {"cost_usd": 0.01},
        },
        "exit_code": 0,
        "stderr": "",
        "arena": {
            "status": 200,
            "headers": {deepline.ARENA_SETTLED_MICROUSD_HEADER: "0"},
        },
    }
    with pytest.raises(ValueError, match="conflicts with provider billing"):
        deepline.normalize_response(request, response)


@pytest.mark.parametrize("headers", [
    {deepline.ARENA_SETTLED_MICROUSD_HEADER: "01"},
    {deepline.ARENA_SETTLED_MICROUSD_HEADER: "9223372036854775808"},
    {
        deepline.ARENA_SETTLED_MICROUSD_HEADER: "0",
        deepline.ARENA_SETTLED_MICROUSD_HEADER.upper(): "0",
    },
])
def test_arena_settlement_receipt_rejects_malformed_or_duplicate_proof(headers):
    response = {
        "body": {"status": "completed", "job_id": "bad-proof", "result": {"data": []}},
        "exit_code": 0,
        "stderr": "",
        "arena": {"status": 200, "headers": headers},
    }
    request = {
        "operation": "execute", "tool": "contextdev_post_news_search", "payload": {}, "limit": 1,
    }

    with pytest.raises(ValueError, match="settlement proof is invalid"):
        deepline.normalize_response(request, response)


def test_native_provider_header_cannot_claim_arena_settlement():
    response = {
        "body": {"status": "completed", "job_id": "native", "result": {"data": []}},
        "headers": {deepline.ARENA_SETTLED_MICROUSD_HEADER: "0"},
        "exit_code": 0,
        "stderr": "",
    }
    request = {
        "operation": "execute", "tool": "contextdev_post_news_search", "payload": {}, "limit": 1,
    }

    normalized, _ = deepline.normalize_response(request, response)

    assert "billing" not in normalized


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


@pytest.mark.parametrize("include_geography", [False, True])
@pytest.mark.parametrize("page_capture", [False, True])
def test_raw_deepline_results_survive_lookup_review_receipts_and_output_mapping(
        lab, include_geography, page_capture):
    lab.raw_envelopes = True
    lab.page_capture = page_capture
    lab.program = lambda: raw_response_scenario(include_geography, page_capture)
    icp = copy.deepcopy(ICP)
    if include_geography:
        icp["geography"] = "United States"

    if page_capture:
        rows = runtime.run(icp)
    else:
        with pytest.raises(RuntimeError, match="invalid_or_unavailable_runtime_state"):
            runtime.run(icp)
    run_file = lab.research[0].research.path
    document = json.loads(run_file.read_text())
    receipts = [json.loads(path.read_text()) for path in (run_file.parent / "receipts").glob("*.json")]
    by_tool = {receipt["tool"]: receipt for receipt in receipts if receipt.get("tool")}

    exa = by_tool["exa_answer"]
    assert exa["status"] == "ok" and exa["billing"] == {"credits_charged": .07, "cost_usd": .007}
    assert exa["results"][0]["evidence_text"].startswith("On August 12")
    if page_capture:
        assert "provider_answer" not in exa["results"][0]
    else:
        assert exa["results"][0]["provider_answer"] == "Generated summary; review its citations."
    assert exa["results"][0].get("company") is None and exa["results"][0].get("domain") is None
    assert exa["provider_response"]["body"]["status"] == "completed"
    if page_capture:
        captured = exa["provider_response"]["body"]["result"]["data"]["results"][0]
        assert captured["success"] is True
        assert "statusCode" not in captured["metadata"]
    else:
        assert exa["provider_response"]["body"]["result"]["data"]["answer"].startswith("Generated summary")
    replay, _ = deepline.normalize_response(exa["attempt"]["request"], exa["provider_response"])
    assert replay["evidence"] == exa["evidence"]
    assert replay["billing"] == exa["provider_response"]["body"]["billing"]
    assert len([frame for frame in lab.frames if frame["tool"] == "exa_answer"]) == 1

    page = by_tool["firecrawl_scrape"]
    assert page["status"] == "ok" and page["results"][0]["evidence_url"] == "https://example.com/about"
    assert page["results"][0]["content_format"] == "markdown"
    assert page["provider_response"]["body"]["result"]["data"]["metadata"]["statusCode"] == 200
    assert page["provider_response"]["body"]["result"]["data"]["markdown"].startswith("Example Products manufactures")

    company = by_tool["harvestapi_get_company"]
    assert company["status"] == "ok" and company["results"][0]["company"] == "Example Products"
    assert company["provider_response"]["body"]["result"]["data"]["status"] == 200
    if not page_capture:
        assert not document["accepted"]
        assert not lab.output.exists()
        ledger = budget_guard.load_ledger(run_file)
        actual = sorted(float(call["actual_credits"]) for call in ledger["calls"].values())
        assert actual == [.02, .03, .07]
        return

    assert len(rows) == 1
    assert rows[0]["company_name"] == "Example Products"
    assert rows[0]["intent_signals"][0]["url"] == "https://example.com/news/wms-project"
    assert rows[0]["required_attribute"]["evidence_url"] == "https://example.com/about"
    accepted = document["accepted"][0]
    filters = {check["criterion"] for check in accepted["qualification_checks"]}
    assert "industries: manufacturing" in filters
    assert ("geographies: united states" in filters) == include_geography
    assert accepted["company"]["discovery_source"]["source"]["tool"] == "harvestapi_get_company"
    assert accepted["primary_contact"]["email_source"]["source"]["tool"] == "harvestapi_get_profile"
    ledger = budget_guard.load_ledger(run_file)
    actual = sorted(float(call["actual_credits"]) for call in ledger["calls"].values())
    assert actual == [.02, .03, .03, .07, .14, .28]


def test_arena_signal_date_preserves_reviewed_precision_without_using_publication_date():
    assert signal_date({"event_date": "2026-08-12", "date": "2026-08-20", "date_basis": "published"}) == "2026-08-12"
    assert signal_date({"event_date": "2026-08", "date": "2026-08-20", "date_basis": "published"}) is None
    assert signal_date({"event_date": "2026", "date": "2026-08-20", "date_basis": "published"}) is None
    assert signal_date({"event_date": "2026-02-30"}) is None
    assert signal_date({"date": "2026-08-20", "date_basis": "observed_current"}) == "2026-08-20"


@pytest.mark.parametrize("key", ["url", "text", "date", "date_basis"])
def test_arena_evidence_aliases_match_native_presence_precedence(key):
    from tyche_arena.output import evidence_value

    assert evidence_value({key: "short"}, key) == "short"
    assert evidence_value({key: "short", "evidence_" + key: "native"}, key) == "native"
    assert evidence_value({key: "short", "evidence_" + key: None}, key) is None
    assert evidence_value({key: "short", "evidence_" + key: ""}, key) == ""


def test_arena_signal_date_uses_native_activity_and_observation_fields():
    assert signal_date({"event_date": "2026-08-12", "evidence_event_date": "2026-09-01"}) == "2026-08-12"
    assert signal_date({"evidence_event_date": "2026-09-01"}) is None
    assert signal_date({"date": "2026-08-01", "date_basis": "published",
                        "evidence_date": "2026-08-20", "evidence_date_basis": "observed_current"}) == "2026-08-20"
    assert signal_date({"date": "2026-08-01", "date_basis": "observed_current",
                        "evidence_date": "2026-08-20", "evidence_date_basis": "published"}) is None


def test_arena_stage_evidence_projects_passed_sources_with_financing_first():
    long_quote = "funding proof " + "x" * 2_100
    row = {"qualification_checks": [
        {"status": "pass", "criterion": "industry", "evidence": [
            {"url": "https://example.com/about", "text": "Company profile."},
            {"url": "https://example.com/about", "text": "Company profile."},
        ]},
        {"status": "fail", "evidence": [
            {"url": "https://example.com/ignored", "text": "Do not project."},
        ]},
        {"status": "pass", "criterion": "regional operations", "evidence": [
            {"evidence_url": "https://news.example.com/financing", "evidence_text": long_quote},
            {"url": "https://example.com/jobs", "text": "Company is hiring."},
            {"url": "https://example.com/fourth", "text": "Past the cap."},
        ]},
    ]}

    assert company_stage_evidence(row) == [
        {"url": "https://news.example.com/financing", "quote": long_quote[:2_000]},
        {"url": "https://example.com/about", "quote": "Company profile."},
        {"url": "https://example.com/jobs", "quote": "Company is hiring."},
    ]


def test_arena_stage_evidence_ranks_before_canonical_url_deduplication():
    row = {"qualification_checks": [
        {"status": "pass", "criterion": "industry", "evidence": [
            {"url": "https://AFFINIA.example:443/about#company",
             "text": "Affinia makes industrial products."},
        ]},
        {"status": "pass", "criterion": "regional operations", "evidence": [
            {"url": "https://affinia.example/about",
             "text": "Affinia completed a Series B funding round."},
        ]},
    ]}

    assert company_stage_evidence(row) == [{
        "url": "https://affinia.example/about",
        "quote": "Affinia completed a Series B funding round.",
    }]


def projection_field_scenario(*, stage=None, competing_quote=..., forged_native_quote=False):
    """Apply optional adapter fields through the existing approved lead flow."""
    program = scenario()
    command = next(program)
    while True:
        if command[0] == "tyche_review":
            for company in command[1].get("companies", []):
                if company["decision"] == "qualify_account":
                    company["company"]["company_stage"] = stage
                    if competing_quote is not ...:
                        proof = company["qualification_checks"][1]["evidence"][0]
                        captured = "Example Products manufactures packaged goods, tools and accessories for retailers."
                        proof["text"] = captured if forged_native_quote else competing_quote
                        proof["evidence_text"] = competing_quote if forged_native_quote else captured
        result = yield command
        try:
            command = program.send(result)
        except StopIteration:
            return


@pytest.mark.parametrize("stage", [None, "", "Series A"])
def test_arena_approved_optional_stage_is_text(lab, stage, arena_operations):
    lab.program = lambda: projection_field_scenario(stage=stage)
    rows = runtime.run(ICP)
    assert rows[0]["company_stage"] == (stage or "")
    output = importlib.import_module("lab_arena.output")
    validated = output.output_document_from_bytes(json.dumps({"companies": rows}).encode(),
        expected_schema_version="leadpoet.lab_arena.output.v5")
    assert len(validated["companies"]) == 1


@pytest.mark.parametrize("stage", [123, ["Series A"], {"stage": "Series A"}])
def test_arena_projection_preflight_rejects_nontext_stage(lab, stage):
    from tyche_arena.output import accepted_preflight, projection_preflight

    assert len(runtime.run(ICP)) == 1
    run_file = lab.research[0].research.path
    document = json.loads(run_file.read_text())
    document["accepted"][0]["company"]["company_stage"] = stage
    assert accepted_preflight(run_file, document) == []
    assert projection_preflight(run_file, document, ICP) == [
        "Arena output projection: Arena company_stage must be text when supplied"]


STAGE_CLAIM = "The company completed a Series B funding round."


def observed_stage_provider(lab):
    provider = lab.provider
    def with_stage(parameters):
        body = provider(parameters)
        if parameters["tool"] == "generic_http_request":
            body["results"][0]["markdown"] += " " + STAGE_CLAIM
        return body
    lab.provider = with_stage


def observed_stage_scenario(captured, *, label=None, two_companies=False):
    program = two_company_checkpoint_scenario() if two_companies else scenario(None)
    command = next(program)
    while True:
        for company in command[1].get("companies", []):
            if company["decision"] == "qualify_account":
                proof = copy.deepcopy(company["qualification_checks"][1]["evidence"])
                company["qualification_checks"].append({"requirement_ref": "attribute:1",
                    "status": "pass", "claim": STAGE_CLAIM, "evidence": proof})
                if label is not None and (not two_companies or company["target"] == "example.com"):
                    company["company"]["company_stage"] = label
        result = yield command
        if command[0] == "tyche_review" and any(c.get("decision") == "accept"
                                                for c in command[1].get("companies", [])):
            captured.append(result)
            if two_companies and len(captured) == 2:
                assert result["status"] == "needs_repair"
                return
        try:
            command = program.send(result)
        except StopIteration:
            return


@pytest.mark.parametrize("mode", ["deliver", "partial_timeout"])
@pytest.mark.parametrize("label", ["Series B", "Seed"])
def test_observed_stage_native_review_atomic_checkpoint_and_frozen_scorer(
        lab, monkeypatch, arena_operations, mode, label):
    reference = Path(os.environ["LAB_ARENA_REFERENCE_SOURCE"])
    spec = importlib.util.spec_from_file_location("observed_stage_checkpoint_reference",
        reference / "lab_arena/lab_arena_checkpoint.py")
    checkpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoint)
    current = sys.modules["lab_arena_checkpoint"]
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=lambda rows: checkpoint.write(rows, output_path=lab.output),
        quota_usage=current.quota_usage, QuotaUnavailable=current.QuotaUnavailable))
    observed_stage_provider(lab)
    captured = []
    lab.program = lambda: observed_stage_scenario(captured, label=label)
    lab.mode = mode
    def approve(tools):
        assert captured[0]["status"] == "review_required" and not lab.output.exists()
        ledger, calls = budget_guard.load_ledger(tools.research.path), len(lab.frames)
        saved = tools.call("tyche_review", {"review_ref": captured[0]["review_ref"],
            "review_findings": review_findings(captured[0])})
        assert saved["checkpoint_saved"]
        assert (budget_guard.load_ledger(tools.research.path), len(lab.frames)) == (ledger, calls)
    lab.after_program = approve
    rows = runtime.run({**ICP, "company_stage": "Series B"})
    assert rows[0]["company_stage"] == label
    script = """import json,sys
from types import SimpleNamespace
from qualification.scoring.lead_scorer import _submitted_stage_decision, _combine_submitted_and_observed
from lab_arena.output import output_document_from_bytes
rows=output_document_from_bytes(open(sys.argv[1],'rb').read(),expected_schema_version='leadpoet.lab_arena.output.v5')['companies']
decision=_submitted_stage_decision(SimpleNamespace(company_stage=rows[0]['company_stage']),SimpleNamespace(company_stage='Series B'))
print(json.dumps([decision,_combine_submitted_and_observed(decision,'match'),_combine_submitted_and_observed(decision,'unavailable')]))
"""
    with monkeypatch.context() as real_process:
        real_process.setattr(subprocess, "Popen", REAL_POPEN)
        completed = subprocess.run([sys.executable, "-B", "-c", script, str(lab.output)],
            env={**os.environ, "PYTHONPATH": str(reference)}, capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == (["match", "match", "unavailable"] if label == "Series B"
                                           else ["mismatch", "mismatch", "mismatch"])


@pytest.mark.parametrize("label", [None, "", "   "])
def test_required_stage_label_blocks_approval_without_using_claim_or_target(lab, label):
    observed_stage_provider(lab)
    captured = []
    lab.program = lambda: observed_stage_scenario(captured, label=label)
    def check(tools):
        assert captured[0]["status"] == "needs_repair"
        assert "company.company_stage with tyche_review" in " ".join(captured[0]["errors"])
        document = tools.research._document()
        assert document["accepted"][0]["qualification_checks"][-1]["claim"] == STAGE_CLAIM
        assert confirmed_leads.read(tools.research.path, document)["leads"] == []
        result = tools.call("tyche_review", {"review_ref": "confirmed:unapproved",
            "review_findings": []})
        assert result["status"] == "needs_repair"
        assert not lab.output.exists()
    lab.after_program = check
    with pytest.raises(RuntimeError, match="repeated_worker_failure"):
        runtime.run({**ICP, "company_stage": "Series B"})


def test_missing_new_stage_preserves_unchanged_prior_checkpoint_on_timeout(lab, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    observed_stage_provider(lab)
    captured = []
    lab.program = lambda: observed_stage_scenario(captured, label="Series B", two_companies=True)
    lab.mode = "partial_timeout"
    rows = runtime.run({**ICP, "company_stage": "Series B"})
    assert len(rows) == 1 and rows[0]["company_linkedin"] == COMPANY_URL
    assert rows[0]["company_stage"] == "Series B"
    assert len(json.loads(lab.output.read_text())["companies"]) == 1
    assert all(checkpoint == lab.checkpoints[0] for checkpoint in lab.checkpoints)


def test_missing_stage_is_repaired_through_native_review_without_redispatch(
        lab, monkeypatch, arena_operations):
    reference = Path(os.environ["LAB_ARENA_REFERENCE_SOURCE"])
    spec = importlib.util.spec_from_file_location("repaired_stage_checkpoint_reference",
        reference / "lab_arena/lab_arena_checkpoint.py")
    checkpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoint)
    current = sys.modules["lab_arena_checkpoint"]
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=lambda rows: checkpoint.write(rows, output_path=lab.output),
        quota_usage=current.quota_usage, QuotaUnavailable=current.QuotaUnavailable))
    observed_stage_provider(lab)
    captured = []
    lab.program = lambda: observed_stage_scenario(captured)
    lab.mode = "partial_timeout"
    def repair_and_approve(tools):
        assert captured[0]["status"] == "needs_repair" and not lab.output.exists()
        document = tools.research._document()
        proofs = copy.deepcopy(document["accepted"][0]["qualification_checks"])
        ledger, calls = budget_guard.load_ledger(tools.research.path), len(lab.frames)
        packet = tools.call("tyche_review", {"companies": [{"target": "example.com",
            "decision": "accept", "reason": "Set the observed stage from the saved reviewed funding passage",
            "company": {"company_stage": "Series B"}}]})
        assert packet["status"] == "review_required" and packet["companies"][0]["sources"]
        assert tools.research._document()["accepted"][0]["qualification_checks"] == proofs
        assert not lab.output.exists()
        approved = tools.call("tyche_review", {"review_ref": packet["review_ref"],
            "review_findings": review_findings(packet)})
        assert approved["checkpoint_saved"]
        assert (budget_guard.load_ledger(tools.research.path), len(lab.frames)) == (ledger, calls)
    lab.after_program = repair_and_approve
    rows = runtime.run({**ICP, "company_stage": "Series B"})
    assert rows[0]["company_stage"] == "Series B"
    assert json.loads(lab.output.read_text()) == {"companies": rows}


def test_clearing_same_confirmed_stage_revokes_its_checkpoint(lab):
    observed_stage_provider(lab)
    captured = []
    lab.program = lambda: observed_stage_scenario(captured, label="Series B")
    lab.mode = "partial_timeout"
    def approve_then_clear(tools):
        approved = tools.call("tyche_review", {"review_ref": captured[0]["review_ref"],
            "review_findings": review_findings(captured[0])})
        assert approved["checkpoint_saved"]
        assert json.loads(lab.output.read_text())["companies"][0]["company_stage"] == "Series B"
        ledger, calls = budget_guard.load_ledger(tools.research.path), len(lab.frames)
        changed = tools.call("tyche_review", {"companies": [{"target": "example.com",
            "decision": "accept", "reason": "Withdraw the previously observed stage label",
            "company": {"company_stage": ""}}]})
        assert changed["status"] == "needs_repair"
        assert confirmed_leads.read(tools.research.path, tools.research._document())["leads"] == []
        assert json.loads(lab.output.read_text()) == {"companies": []}
        assert (budget_guard.load_ledger(tools.research.path), len(lab.frames)) == (ledger, calls)
    lab.after_program = approve_then_clear
    assert runtime.run({**ICP, "company_stage": "Series B"}) == []
    assert json.loads(lab.output.read_text()) == {"companies": []}


@pytest.mark.parametrize("value,expected", [(None, ""), ("Any", ""), ("ALL", ""),
    ("Unknown", ""), ("N/A", ""), ("NA", ""), ("Not specified", ""), ("---", ""),
    ([], ""), ([" ", "Any", "Series B"], ""), (["", "Series B", "Seed"], "Series B")])
def test_stage_constraint_matches_frozen_input_and_unset_rules(value, expected, arena_operations):
    from tyche_arena.input import required_company_stage
    icp = {**ICP, "icp_id": "fixture-stage", "employee_count": ["201-500"], "company_stage": value}
    assert required_company_stage(icp) == expected
    attributes = request_for(icp, 1, 30)["icp"]["required_attributes"]
    assert [a for a in attributes if a.startswith("company_stage:")] == (
        ["company_stage: " + expected] if expected else [])
    script = """import json,sys
from qualification.scoring.competition import _normalized_icp
from qualification.scoring.lead_scorer import _normalize_company_stage
print(json.dumps(bool(_normalize_company_stage(_normalized_icp(json.loads(sys.stdin.read()))['company_stage']))))
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], input=json.dumps(icp),
        env={**os.environ, "PYTHONPATH": os.environ["LAB_ARENA_REFERENCE_SOURCE"]},
        capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == bool(expected)


def test_arena_stage_schema_is_explicit_without_native_mutation():
    from tyche_arena.mcp import LAB_TOOLS
    # The adapter converts repeated schemas to refs; inspect the fully copied native schema seam.
    native = TOOLS["tyche_review"][1]["properties"]["companies"]["items"]["properties"]["company"]["properties"]
    assert "company_stage" not in native
    assert "observed current stage label" in json.dumps(LAB_TOOLS["tyche_review"][1])


def test_arena_approved_attribute_uses_native_verified_quote(lab, arena_operations):
    lab.program = lambda: projection_field_scenario(
        competing_quote="A competing passage absent from the captured source.")
    rows = runtime.run(ICP)
    assert rows[0]["required_attribute"]["evidence_quote"] == (
        "Example Products manufactures packaged goods, tools and accessories for retailers.")
    output = importlib.import_module("lab_arena.output")
    assert len(output.output_document_from_bytes(json.dumps({"companies": rows}).encode(),
        expected_schema_version="leadpoet.lab_arena.output.v5")["companies"]) == 1
    assert [frame["tool"] for frame in lab.frames] == [
        "harvestapi_get_company", "generic_http_request", "harvestapi_get_profile",
        "harvestapi_get_profile", "zerobounce_validate"]


def test_arena_native_quote_override_must_match_captured_source(lab):
    lab.program = lambda: projection_field_scenario(
        competing_quote="A competing passage absent from the captured source.",
        forged_native_quote=True)
    with pytest.raises(RuntimeError, match="invalid_or_unavailable_runtime_state"):
        runtime.run(ICP)
    assert not lab.output.exists()


@pytest.mark.parametrize("mode,error", [("prose", RuntimeError), ("tamper", ValueError), ("timeout", RuntimeError)])
def test_failed_or_fabricated_completion_never_returns_leads(lab, mode, error):
    lab.mode = mode
    with pytest.raises(error):
        runtime.run(ICP)
    assert 1 <= len(lab.processes) <= runtime.runner.MAX_UNCHANGED_EXITS and lab.session_closed
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
    with pytest.raises(ValueError, match="current evidence review"):
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
    assert {t["name"] for t in tools} == {
        "tyche_lookup", "tyche_review", "tyche_inspect", "tyche_finish", "tyche_checkpoint",
        "tyche_open", "tyche_claim",
    }
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


def _oversized_native_review_case():
    """Build the provider-free 45-attribute accepted review used by recovery285."""
    native_tests = ROOT / ".agents/skills/lead-sourcing/tests"
    sys.path.insert(0, str(native_tests))
    from test_client_output import client_document
    from test_research_tools import ResearchToolTests, captured_page, check

    case = ResearchToolTests("runTest")
    case.setUp()
    template = client_document()
    row = template["accepted"][0]
    company = row["company"]
    person = row["primary_contact"]
    case.request = request_for(ICP, 1, runtime.RESEARCH_SECONDS)
    case.request["icp"]["required_attributes"] = [
        ICP["required_attribute"],
        *(f"Verified operating attribute {index:02d}" for index in range(1, 45)),
    ]
    original = case.path.parent.parent / "request.txt"
    original.write_text(json.dumps(ICP), encoding="utf-8")
    case.tools.environment["TYCHE_REQUEST_FILE"] = str(original)
    case.start()

    case.provider.raw = {
        "status": "ok",
        "element": {
            "name": company["canonical_name"],
            "website": company["website"],
            "linkedinUrl": company["linkedin_url"],
            "employeeCountRange": {"start": 201, "end": 500},
            "locations": [{"headquarter": True, "country": "United States", "geographicArea": "Ohio"}],
        },
    }
    selected = case.lookup(check(
        "example.com", inputs={"url": company["linkedin_url"]},
    ))["lookups"][0]["results"][0]["ref"]
    page_refs = [captured_page(
        case.tools, case.provider, target="example.com",
        url=evidence["evidence_url"], text=evidence["evidence_text"],
        date=evidence["evidence_date"],
    ) for evidence in (row["account_fit"], row["signal_evidence"])]
    checks = [{
        "criterion": "recent integration",
        "signal": "arena_signal_0",
        "status": "pass",
        "claim": "Recent integration verified",
        "evidence": [{"ref": page_refs[1], "event_date": "2026-08-12"}],
    }, {
        "requirement_ref": "icp:industries", "status": "pass",
        "claim": "Manufacturing account", "evidence": [{"ref": page_refs[0]}],
    }]
    checks.extend({
        "requirement_ref": f"attribute:{index}",
        "status": "pass",
        "claim": f"Operating attribute {index:02d} is supported by the company source passage.",
        "evidence": [{"ref": page_refs[0]}],
    } for index in range(45))
    case.tools.call("tyche_review", {
        "companies": [{
            "target": "example.com",
            "decision": "qualify_account",
            "reason": "All synthetic criteria reviewed",
            "company": {"ref": selected, **{key: company[key] for key in (
                "industry", "sub_industry", "description", "classification_note",
            )}},
            "account_fit": {"ref": page_refs[0], "fit_claim": row["account_fit"]["fit_claim"]},
            "qualification_checks": checks,
            "intent_details": row["intent_details"],
        }],
        "sources": [{"ref": ref.rsplit(":", 1)[0], "state": "exhausted",
                     "reason": "Fixture captured page reviewed"} for ref in page_refs],
    })

    case.provider.raw = {
        "status": "ok",
        "element": {
            "linkedinUrl": person["linkedin_url"],
            "firstName": "Ada",
            "lastName": "Example",
            "currentPosition": [{
                "companyName": company["canonical_name"],
                "title": person["current_title"],
                "companyLinkedinUrl": company["linkedin_url"],
            }],
            "location": {"linkedinText": "Columbus, Ohio, United States", "parsed": {
                "city": "Columbus", "state": "Ohio", "countryFull": "United States",
            }},
        },
    }
    profile = case.lookup(check(
        "example.com", phase="contact_verification", tool="harvestapi_get_profile",
        inputs={"url": person["linkedin_url"]},
    ))["lookups"][0]["results"][0]["ref"]
    case.tools.review(companies=[{
        "target": "example.com", "decision": "hold_contact", "reason": "Selected buyer",
        "primary_contact": {"ref": profile, "requested_role": person["requested_role"], "role_match": "exact"},
    }])
    case.provider.raw = {
        "status": "ok",
        "element": {
            "id": "profile-123",
            "linkedinUrl": person["linkedin_url"],
            "firstName": "Ada",
            "lastName": "Example",
            "emails": [{"email": person["email"], "status": "valid"}],
            "currentPosition": [{
                "companyName": company["canonical_name"],
                "title": person["current_title"],
                "companyLinkedinUrl": company["linkedin_url"],
            }],
            "location": {"parsed": {
                "city": "Columbus", "state": "Ohio", "countryFull": "United States",
            }},
        },
    }
    profile = case.lookup(check(
        "example.com", phase="contact_discovery", tool="harvestapi_get_profile",
        inputs={"findEmail": "true"}, contact_ref=profile,
    ))["lookups"][0]["results"][0]["ref"]
    case.tools.review(companies=[{
        "target": "example.com", "decision": "hold_contact", "reason": "Selected enriched buyer",
        "primary_contact": {"ref": profile, "requested_role": person["requested_role"], "role_match": "exact"},
    }])
    case.provider.raw = {"status": "ok", "data": {
        "address": person["email"], "status": "valid", "sub_status": "", "domain_is_catch_all": True,
    }}
    verifier = case.lookup(check(
        "example.com", phase="email_validation", tool="zerobounce_validate",
        inputs={"email": person["email"]},
    ))["lookups"][0]["results"][0]["ref"]
    case.tools.review(
        companies=[{
            "target": "example.com", "decision": "accept", "reason": "Complete synthetic record",
            "primary_contact": {"email_ref": verifier},
        }],
        sources=[{
            "ref": ref, "state": "exhausted", "reason": "Selected fixture evidence reviewed",
        } for ref in (selected, profile, verifier)],
    )
    return case


def test_oversized_native_evidence_review_pages_reconstruct_without_approval(
        tmp_path, monkeypatch):
    case = _oversized_native_review_case()
    output = tmp_path / "companies.json"
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "unused-worker.sock"))
    monkeypatch.setenv("LAB_ARENA_OUTPUT_PATH", str(output))
    monkeypatch.setenv("TYCHE_FINALIZATION_ONLY", "1")
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=lambda rows: output.write_text(json.dumps({"companies": rows})),
    ))
    try:
        tools = LabTools(case.path, time.monotonic() + 30, time.monotonic() + 60)
        native_review = tools.research.inspect(target="example.com", field="evidence_review")
        canonical = json.dumps(
            native_review, ensure_ascii=True, allow_nan=False,
            separators=(",", ":"), sort_keys=True,
        )
        assert len(canonical) > MODEL_RESULT_MAX_CHARACTERS
        before = case.path.read_bytes()

        pages = []
        offset = 0
        content_hash = None
        total_characters = None
        while offset is not None:
            page = tools.call("tyche_inspect", {
                "target": "example.com", "field": "evidence_review", "offset": offset,
            })
            assert page["status"] == "evidence_review_page"
            assert page["offset"] == offset
            assert len(page["content"]) <= EVIDENCE_REVIEW_PAGE_CHARACTERS
            assert len(json.dumps(page, ensure_ascii=True)) < MODEL_RESULT_MAX_CHARACTERS
            assert page.get("truncated") is not True
            assert "arena_budget" in page and "arena_budget" not in page["content"]
            content_hash = content_hash or page["content_sha256"]
            total_characters = total_characters or page["total_characters"]
            assert page["content_sha256"] == content_hash
            assert page["total_characters"] == total_characters
            pages.append(page["content"])
            offset = page["next_offset"]

        reconstructed = "".join(pages)
        assert reconstructed == canonical
        assert json.loads(reconstructed) == native_review
        assert content_hash == hashlib.sha256(canonical.encode("ascii")).hexdigest()
        assert total_characters == len(canonical)
        assert case.path.read_bytes() == before
        assert "final_review" not in json.loads(case.path.read_text())

        packet = tools.call("tyche_finish", {})
        assert packet["status"] == "review_required", packet
        assert packet["review_ref"]
        assert "final_review" not in json.loads(case.path.read_text())
        repeated = tools.call("tyche_finish", {})
        assert repeated["review_ref"] == packet["review_ref"]
        assert "final_review" not in json.loads(case.path.read_text())
        delivered = tools.call("tyche_finish", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet, tools),
        })
        assert delivered["delivery_allowed"] is True
    finally:
        case.doCleanups()


def test_evidence_review_paging_is_stateless_and_small_views_stay_unchanged():
    budget = {"scope": "local_adapter_dispatch_count", "providers": {}}

    def session(result):
        tools = LabTools.__new__(LabTools)
        tools.lock = threading.Lock()
        tools.delivered = False
        tools.broker = SimpleNamespace(local_dispatch_budget=lambda: budget)
        tools.research = SimpleNamespace(call=lambda *_args: copy.deepcopy(result))
        return tools

    small = {"company": {"domain": "small.test"}, "sources": {}}
    for offset in (0, EVIDENCE_REVIEW_PAGE_CHARACTERS):
        assert session(small).call("tyche_inspect", {
            "target": "small.test", "field": "evidence_review", "offset": offset,
        }) == {**small, "arena_budget": budget}

    first = session({"review": "a" * 30000}).call("tyche_inspect", {
        "target": "first.test", "field": "evidence_review", "offset": 0,
    })
    second = session({"review": "b" * 30000}).call("tyche_inspect", {
        "target": "second.test", "field": "evidence_review", "offset": 0,
    })
    assert first["content_sha256"] != second["content_sha256"]
    assert first["content"] != second["content"]
    assert first["offset"] == second["offset"] == 0

    escaped = model_result(evidence_review_page({"review": "\\" * 30000}, 0), budget)
    assert len(escaped["content"]) == EVIDENCE_REVIEW_PAGE_CHARACTERS
    assert len(json.dumps(escaped, ensure_ascii=True)) < MODEL_RESULT_MAX_CHARACTERS
    assert escaped.get("truncated") is not True


def test_large_valid_empty_review_remains_compact_and_unpaged(tmp_path):
    native_tests = ROOT / ".agents/skills/lead-sourcing/tests"
    sys.path.insert(0, str(native_tests))
    from test_output_contract import VALIDATOR, shortfall_result
    import run_attempt

    document = shortfall_result()
    document["request"]["original_text"] = "Synthetic valid-empty interface fixture."
    document["request"]["buying_signals"] = [{"kind": "intent", "importance": "required"}]
    document["rejected"] = [{
        "company": {"domain": f"rejected-{index:03d}.test"},
        "reason_code": "not_icp_fit",
        "qualification_checks": [{
            "criterion": "intent", "signal": "intent", "importance": "required",
            "status": "fail", "claim": "The saved evidence shows no matching intent.",
            "evidence": [{
                "url": f"https://rejected-{index:03d}.test/source",
                "text": "No matching intent.",
            }],
        }],
    } for index in range(400)]
    document["stop_audit"].update(run_attempt.calculate_review_counts(document))
    assert VALIDATOR.validate_run(document) == []
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    tools = ResearchTools(path, environment={"TYCHE_FINALIZATION_ONLY": "1"})

    packet = model_result(tools.review_delivery(document), {})
    rejected_page = model_result(tools.inspect(field="rejected", offset=390, limit=10), {})
    assert packet["status"] == "review_required"
    assert packet.get("truncated") is not True
    assert packet["companies"] == []
    assert "rejected" not in packet
    assert rejected_page["total"] == 400
    assert len(rejected_page["value"]) == 10
    assert rejected_page.get("status") != "evidence_review_page"


@pytest.mark.parametrize("arguments", [
    {"target": "example.com", "field": "evidence_review", "offset": -1},
    {"target": "example.com", "field": "evidence_review", "limit": 0},
    {"target": "example.com", "ref": "route:0", "field": "evidence_review"},
])
def test_evidence_review_paging_preserves_native_argument_validation(arguments):
    tools = LabTools.__new__(LabTools)
    tools.lock = threading.Lock()
    tools.delivered = False
    tools.broker = SimpleNamespace(local_dispatch_budget=lambda: {})
    tools.research = ResearchTools(Path("/does/not/matter"))
    with pytest.raises(ValueError):
        tools.call("tyche_inspect", arguments)


def test_local_dispatch_budget_is_lock_protected_and_session_local(tmp_path):
    first = Broker(tmp_path / "first.sock", time.monotonic() + 30)
    second = Broker(tmp_path / "second.sock", time.monotonic() + 30)
    first.calls = 7
    assert first.local_dispatch_budget()["providers"]["deepline"] == {"used": 7}
    assert second.local_dispatch_budget()["providers"]["deepline"] == {"used": 0}
    assert second.local_dispatch_budget()["providers"]["scrapingdog"] == {"used": 0}
    assert first.local_dispatch_budget()["authoritative_billing"] is False


@pytest.mark.parametrize("name,arguments", [("tyche_inspect", {}), ("tyche_checkpoint", {})])
def test_every_lab_tool_return_includes_local_dispatch_budget(
    tmp_path, name, arguments,
):
    tools = LabTools.__new__(LabTools)
    tools.lock = threading.Lock()
    tools.delivered = False
    budget = {"scope": "local_adapter_dispatch_count", "used": 3, "limit": 30, "remaining": 27,
              "authoritative_billing": False}
    tools.broker = SimpleNamespace(local_dispatch_budget=lambda: budget)
    tools.research = SimpleNamespace(
        path=tmp_path / "results.json",
        call=lambda tool, payload: {"status": "ok"},
    )
    tools.checkpoint = lambda **payload: {"status": "checkpoint_saved"}
    assert tools.call(name, arguments)["arena_budget"] == budget


def test_arena_uses_shared_instructions_and_only_adds_host_contract():
    guidance = runtime.instructions()
    assert runtime.runner.ISOLATION_INSTRUCTIONS in guidance
    assert "host owns credentials, provider billing, quotas and the hard deadline" in guidance
    assert "/output/companies.json" in guidance
    assert "Hosted web search is disabled" not in guidance
    assert "try a different catalogued source" not in guidance
    assert runtime.REASONING_EFFORT == runtime.runner.REASONING_EFFORT == "high"


def test_latest_native_finalization_budget_fits_the_hard_limit():
    assert runtime.FINALIZATION_SECONDS == 600
    assert runtime.RESEARCH_SECONDS == 2070
    assert runtime.RUN_SECONDS == 2670
    assert runtime.RUN_SECONDS + 30 == 45 * 60


def test_provider_deadlines_and_no_model_fallback(tmp_path):
    broker = Broker(tmp_path / "missing.sock", time.monotonic() - 1)
    args = {"tool": "harvestapi_get_company", "payload": {}}
    with pytest.raises(BrokerRefusal, match="deadline"):
        broker.request("deepline.execute", args)
    broker.deadline = time.monotonic() + 30
    for operation in ("openrouter.chat", "openrouter.responses", "deepline.search", "deepline.describe"):
        with pytest.raises(ValueError, match="Unsupported"):
            broker.request(operation, {})
    tools = ResearchTools(tmp_path / "research/results.json", execute=broker.execute)
    tools.start(request=request_for(ICP, 1, 20), max_usd=.5)
    failed = tools.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    assert failed["lookups"][0]["status"] == "timeout"
    assert all(call["actual_credits"] is None for call in budget_guard.load_ledger(tools.path)["calls"].values())
    with pytest.raises(BrokerRefusal, match="blocked_after_uncertain"):
        broker.request("deepline.execute", args)


def test_native_free_status_recovery_crosses_arena_research_deadline(
        tmp_path, arena_operations):
    response = json.dumps({
        "status": "success", "result": "deliverable",
        "email": "buyer@target.example",
        "billing": {"credits_charged": 0, "cost_usd": 0},
    }).encode()
    socket_path = Path("/tmp") / (
        "tyche-status-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16] + ".sock"
    )
    with native_finalization_recovery_fixture() as (fixture, getter, pending), \
            FramedArenaWorker(socket_path, arena_operations,
                              [(200, {"content-type": "application/json"}, response)]) as worker:
        session = LabTools.__new__(LabTools)
        session.broker = Broker(
            socket_path, time.monotonic() - 1,
            response_deadline=time.monotonic() + 30,
        )
        session.research = ResearchTools(
            fixture.path, execute=session._execute,
            environment={"TYCHE_FINALIZATION_ONLY": "1"},
        )

        result = run_attempt.run_attempt(
            fixture.path, getter, execute=session._execute
        )

        document = json.loads(fixture.path.read_text())
        assert result["provider_status"] == "ok"
        assert session.broker.calls == 1
        assert len(worker.frames) == 1
        assert worker.frames[0]["operation_id"] == "deepline.execute"
        assert worker.frames[0]["parameters"] == {
            "tool": "bounceban_get_single_status", "payload": {"id": "saved-job"}}
        assert email_receipts.verification_finished(
            fixture.path, document, "bounceban-first", pending
        )
        call = budget_guard.load_ledger(fixture.path)["calls"]["saved-status"]
        assert call["actual_credits"] == "0"


@pytest.mark.parametrize("invalid", ["malformed", "cross_run", "resubmission"])
def test_arena_finalization_recovery_rejects_untrusted_attempts(tmp_path, invalid):
    socket_path = tmp_path / "must-not-connect.sock"
    with native_finalization_recovery_fixture() as (fixture, getter, _pending):
        session = LabTools.__new__(LabTools)
        session.broker = Broker(
            socket_path, time.monotonic() - 1,
            response_deadline=time.monotonic() + 30,
        )
        session.research = ResearchTools(
            fixture.path, execute=session._execute,
            environment={"TYCHE_FINALIZATION_ONLY": "1"},
        )
        before = fixture.path.read_bytes()
        ledger_before = budget_guard.ledger_path(fixture.path).read_bytes()

        if invalid == "malformed":
            captured = []
            body, code = session._execute(getter["request"], captured.append)
            # Deepline's native normalizer historically returns zero for this
            # structured pre-dispatch refusal; the status and dispatch bit are
            # the authoritative outcome.
            assert code == 0
            assert body["request_sent"] is False
            assert body["status"] == "config_error"
            assert captured[0]["arena"]["error"] == "deadline_reached"
        else:
            if invalid == "cross_run":
                receipt = fixture.path.parent / "receipts/bounceban-first.json"
                damaged = json.loads(receipt.read_text())
                damaged["run_fingerprint"] = "another-run"
                receipt.write_text(json.dumps(damaged))
                before = fixture.path.read_bytes()
                ledger_before = budget_guard.ledger_path(fixture.path).read_bytes()
            else:
                getter["action"].pop("status_read")
                getter["action"]["cost_upper_bound_credits"] = .06
                getter["request"].update(
                    tool="bounceban_verify_single",
                    payload={"email": "buyer@target.example"},
                )
            with pytest.raises(ValueError, match=(
                    "Research is closed|action not eligible|already attempted|saved pending job")):
                run_attempt.run_attempt(
                    fixture.path, getter, execute=session._execute
                )

        assert session.broker.calls == 0
        assert fixture.path.read_bytes() == before
        assert budget_guard.ledger_path(fixture.path).read_bytes() == ledger_before


@pytest.mark.parametrize("reason", ["deadline", "stopped"])
def test_local_predispatch_refusal_has_no_native_reservation(tmp_path, reason):
    broker = Broker(tmp_path / "missing.sock", time.monotonic() + 30)
    if reason == "deadline":
        broker.deadline = time.monotonic() - 1
    else:
        broker.stopped.set()
    tools = ResearchTools(tmp_path / "research/results.json", execute=broker.execute)
    tools.start(request=request_for(ICP, 1, 20), max_usd=.5)
    result = tools.call("tyche_lookup", lookup("harvestapi_get_company", {"url": COMPANY_URL}))
    document = json.loads(tools.path.read_text())
    route = document["routes"][-1]
    assert route["paid_calls"] == 0
    assert budget_guard.load_ledger(tools.path)["calls"] == {}
    assert result["lookups"][0]["status"] == "config_error"
    receipt = json.loads((tools.path.parent / "receipts" / (route["route_id"] + ".json")).read_text())
    assert receipt["request_sent"] is False
    assert receipt["provider_response"]["arena"]["error"] in {
        "deadline": {"deadline_reached"},
        # stop() also expires the deadline, so either no-send guard can win.
        "stopped": {"stopped", "deadline_reached"},
    }[reason]


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


def test_native_paid_batch_serializes_before_model_reservation_and_host_worker(
        tmp_path, arena_worker_runtime):
    """The native three-worker batch must match Arena's one-paid-call ICP gate."""

    host = arena_worker_runtime

    class Api:
        def __init__(self):
            self.lock = threading.Lock()
            self.active = 0
            self.maximum_active = 0
            self.calls = 0

        def provider(self, _run_id, _lease_token, frame):
            with self.lock:
                self.calls += 1
                ordinal = self.calls
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                overlapping = self.active > 1
            try:
                if overlapping:
                    return host.error_result("provider_unavailable", {
                        "operation_id": frame["operation_id"],
                        "provider": "deepline",
                        "funding_source": "host",
                        "status": 502,
                        "provider_status": None,
                        "outcome": "not_dispatched",
                        "reason": "budget_busy",
                        "idempotent": False,
                    }).to_document()
                time.sleep(0.05)
                body = json.dumps({
                    "status": "completed",
                    "job_id": f"serialized-{ordinal}",
                    "result": {"data": {"element": None, "status": 200}},
                    "billing": {"credits_charged": 0.03},
                }, separators=(",", ":")).encode()
                return host.BrokerResult(
                    200,
                    {"content-type": "application/json"},
                    body,
                    {
                        "operation_id": frame["operation_id"],
                        "provider": "deepline",
                        "funding_source": "host",
                        "status": 200,
                        "provider_status": 200,
                        "outcome": "settled",
                        "actual_microusd": 3000,
                        "idempotent": False,
                    },
                ).to_document()
            finally:
                with self.lock:
                    self.active -= 1

    socket_path = Path("/tmp") / (
        "tyche-paid-gate-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16] + ".sock"
    )
    api = Api()
    state = host.RunState(
        lease={"run_id": "paid-gate-run", "kind": "execute"},
        lease_token="paid-gate-token",
    )
    worker = host.WorkerSocketServer(socket_path, api, state)
    worker.start()
    try:
        broker = Broker(
            socket_path,
            time.monotonic() + 30,
            response_deadline=time.monotonic() + 60,
        )
        research = ResearchTools(tmp_path / "research/results.json", execute=broker.execute)
        research.start(
            request=request_for(ICP, 1, 30),
            max_usd=0.01,
            verification_reserve_credits=0,
        )
        result = research.call("tyche_lookup", {"checks": [
            {"target": "alpha.example", "purpose": "Verify alpha", "phase": "account_verification",
             "tool": "harvestapi_get_company",
             "inputs": {"url": "https://www.linkedin.com/company/alpha"}},
            {"target": "beta.example", "purpose": "Verify beta", "phase": "account_verification",
             "tool": "harvestapi_get_company",
             "inputs": {"url": "https://www.linkedin.com/company/beta"}},
        ]})
    finally:
        worker.stop()

    assert api.calls == 2 and api.maximum_active == 1, (api.calls, api.maximum_active, result)
    assert len(state.calls) == 2
    assert all(call["outcome"] == "settled" for call in state.calls)
    assert not any(call.get("reason") == "budget_busy" for call in state.calls)
    assert [row["status"] for row in result["lookups"]] == ["no_results", "no_results"]
    ledger = budget_guard.load_ledger(research.path)
    assert len(ledger["calls"]) == 2
    assert {call["actual_credits"] for call in ledger["calls"].values()} == {"0.03"}


def test_paid_dispatch_gate_is_cross_provider_but_scoped_to_one_broker(
        monkeypatch, tmp_path):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    monkeypatch.setattr(
        budget_guard,
        "guarded_call",
        lambda _request, _provider, dispatch, *, tariff=None: dispatch(),
    )
    deepline_request = {
        "operation": "execute",
        "tool": "harvestapi_get_company",
        "payload": {"url": COMPANY_URL},
        "limit": 10,
        "timeout_seconds": 30.0,
        "spend": {"max_cost_credits": 0.03},
    }
    scrapingdog_request = {
        "operation": "google_search",
        "query": "Acme",
        "country": "us",
        "timeout_seconds": 30.0,
        "spend": {"max_cost_credits": 5},
    }
    broker = Broker(tmp_path / "same.sock", time.monotonic() + 30)
    entered = []
    first_entered = threading.Event()
    release_first = threading.Event()

    def serialized_request(operation, _parameters, **_kwargs):
        entered.append(operation)
        if len(entered) == 1:
            first_entered.set()
            assert release_first.wait(2)
        if operation == "deepline.execute":
            return 200, {}, {
                "status": "completed", "result": {"data": {"element": None, "status": 200}},
                "billing": {"credits_charged": 0.03},
            }
        return 200, {"content-type": "application/json"}, json.dumps({"organic_results": []})

    monkeypatch.setattr(broker, "request", serialized_request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(broker.execute, deepline_request, lambda _raw: None)
        assert first_entered.wait(1)
        second = pool.submit(broker.execute, scrapingdog_request, lambda _raw: None)
        time.sleep(0.05)
        assert entered == ["deepline.execute"]
        release_first.set()
        assert first.result(timeout=2)[0]["status"] == "no_results"
        assert second.result(timeout=2)[0]["status"] == "no_results"
    assert entered == ["deepline.execute", "scrapingdog.google"]

    overlap_lock = threading.Lock()
    overlap_barrier = threading.Barrier(2)
    active = 0
    maximum_active = 0

    def overlapping_request(_operation, _parameters, **_kwargs):
        nonlocal active, maximum_active
        with overlap_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            overlap_barrier.wait(timeout=2)
            return 200, {}, {
                "status": "completed", "result": {"data": {"element": None, "status": 200}},
                "billing": {"credits_charged": 0.03},
            }
        finally:
            with overlap_lock:
                active -= 1

    brokers = [Broker(tmp_path / f"distinct-{index}.sock", time.monotonic() + 30)
               for index in range(2)]
    for instance in brokers:
        monkeypatch.setattr(instance, "request", overlapping_request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda instance: instance.execute(deepline_request, lambda _raw: None),
            brokers,
        ))
    assert maximum_active == 2
    assert all(body["status"] == "no_results" and code == 0 for body, code in results)


@pytest.mark.parametrize("reason", ["deadline", "stopped"])
def test_waiting_paid_dispatch_refuses_before_admission_and_active_call_finishes(
        monkeypatch, tmp_path, reason):
    broker = Broker(
        tmp_path / "worker.sock",
        time.monotonic() + 30,
        response_deadline=time.monotonic() + 60,
    )
    request = {
        "operation": "execute",
        "tool": "harvestapi_get_company",
        "payload": {"url": COMPANY_URL},
        "limit": 10,
        "timeout_seconds": 30.0,
        "spend": {"max_cost_credits": 0.03},
    }
    guarded = []

    def guarded_call(_request, _provider, dispatch, *, tariff=None):
        guarded.append(True)
        return dispatch()

    first_entered = threading.Event()
    release_first = threading.Event()

    def provider_request(_operation, _parameters, **_kwargs):
        first_entered.set()
        assert release_first.wait(2)
        return 200, {}, {
            "status": "completed", "result": {"data": {"element": None, "status": 200}},
            "billing": {"credits_charged": 0.03},
        }

    monkeypatch.setattr(budget_guard, "guarded_call", guarded_call)
    monkeypatch.setattr(broker, "request", provider_request)
    second_capture = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(broker.execute, request, lambda _raw: None)
        assert first_entered.wait(1)
        if reason == "deadline":
            broker.deadline = time.monotonic() + 0.05
        second = pool.submit(broker.execute, request, second_capture.append)
        if reason == "stopped":
            time.sleep(0.05)
            broker.stopped.set()
        second_body, second_code = second.result(timeout=2)
        release_first.set()
        first_body, first_code = first.result(timeout=2)

    assert second_code in {0, 2}
    assert second_body["request_sent"] is False
    assert second_capture[0]["arena"]["error"] == (
        "deadline_reached" if reason == "deadline" else "stopped"
    )
    assert len(guarded) == 1
    assert broker.provider_calls("deepline") == 1
    assert first_code == 0 and first_body["status"] == "no_results"


def test_zero_cost_finalization_getter_bypasses_paid_dispatch_gate(
        monkeypatch, tmp_path):
    broker = Broker(
        tmp_path / "worker.sock",
        time.monotonic() - 1,
        response_deadline=time.monotonic() + 1,
    )
    request = {
        "operation": "execute",
        "tool": "bounceban_get_single_status",
        "payload": {"id": "saved-job"},
        "limit": 10,
        "timeout_seconds": 30.0,
        "spend": {"max_cost_credits": 0},
    }
    monkeypatch.setattr(
        budget_guard,
        "guarded_call",
        lambda _request, _provider, dispatch, *, tariff=None: dispatch(),
    )
    monkeypatch.setattr(broker, "request", lambda *_args, **_kwargs: (
        200,
        {},
        {"status": "success", "result": "deliverable",
         "email": "buyer@target.example",
         "billing": {"credits_charged": 0, "cost_usd": 0}},
    ))
    assert broker._requires_paid_dispatch(request, "deepline") is False
    assert broker._requires_paid_dispatch({
        **request,
        "tool": "harvestapi_get_company",
        "payload": {"url": COMPANY_URL},
    }, "deepline") is True
    broker._paid_dispatch_lock.acquire()
    try:
        body, code = broker.execute(
            request,
            lambda _raw: None,
            allow_after_deadline=True,
        )
    finally:
        broker._paid_dispatch_lock.release()

    assert code == 0 and body["status"] == "ok"
    assert broker.provider_calls("deepline") == 1


def test_unknown_worker_502_still_retains_the_model_reservation(
        tmp_path, arena_worker_runtime):
    host = arena_worker_runtime

    class Api:
        @staticmethod
        def provider(_run_id, _lease_token, frame):
            return host.error_result("provider_unavailable", {
                "operation_id": frame["operation_id"],
                "provider": "deepline",
                "funding_source": "host",
                "status": 502,
                "provider_status": None,
                "outcome": "uncertain",
                "idempotent": False,
            }).to_document()

    socket_path = Path("/tmp") / (
        "tyche-unknown-502-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16] + ".sock"
    )
    state = host.RunState(
        lease={"run_id": "unknown-run", "kind": "execute"},
        lease_token="unknown-token",
    )
    worker = host.WorkerSocketServer(socket_path, Api(), state)
    worker.start()
    try:
        broker = Broker(socket_path, time.monotonic() + 30)
        research = ResearchTools(tmp_path / "research/results.json", execute=broker.execute)
        research.start(
            request=request_for(ICP, 1, 30),
            max_usd=0.01,
            verification_reserve_credits=0,
        )
        result = research.call(
            "tyche_lookup",
            lookup("harvestapi_get_company", {"url": COMPANY_URL}),
        )
    finally:
        worker.stop()

    assert result["lookups"][0]["status"] == "provider_error"
    ledger = budget_guard.load_ledger(research.path)
    assert len(ledger["calls"]) == 1
    assert next(iter(ledger["calls"].values()))["actual_credits"] is None
    route_id = next(iter(ledger["calls"]))
    receipt = json.loads(
        (research.path.parent / "receipts" / f"{route_id}.json").read_text()
    )
    assert receipt["spend_receipt"]["state"] == "pending_billing"
    assert receipt.get("request_sent") is not False


def test_scrapingdog_native_google_params_map_to_existing_arena_frame_and_normalize(
        monkeypatch, tmp_path, arena_operations):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    monkeypatch.setattr(budget_guard, "guarded_call", lambda _request, provider, dispatch, *, tariff=None: (
        dispatch() if provider == "scrapingdog" else pytest.fail("wrong provider")))
    native_transport = scrapingdog._http_get
    frames = []
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 30)

    def request(operation, parameters, *, admitted=False, timeout_seconds=None):
        frames.append((operation, copy.deepcopy(parameters), admitted, timeout_seconds))
        payload = {"organic_results": [{"rank": 1, "title": "Acme", "link": "https://acme.test/about",
                                        "snippet": "Acme opened a new warehouse on September 1, 2026."}]}
        return 200, {"content-type": "application/json"}, json.dumps(payload)

    monkeypatch.setattr(broker, "request", request)
    captured = []
    body, code = broker.execute({"operation": "google_search", "query": "Acme warehouse",
                                 "country": "us"}, captured.append)

    assert code == 0 and body["status"] == "ok"
    assert frames == [("scrapingdog.google", {"query": "Acme warehouse", "country": "us"}, True, 30.0)]
    assert arena_operations.validate_operation_request(frames[0][0], frames[0][1]) == {
        "query": "Acme warehouse", "country": "us"}
    assert body["results"][0]["domain"] == "acme.test"
    assert body["results"][0]["evidence_date"] == "September 1, 2026"
    assert captured[0]["http_status"] == 200
    assert captured[0]["body"]["organic_results"][0]["title"] == "Acme"
    assert scrapingdog._http_get is native_transport


def test_scrapingdog_html_uses_native_visible_text_normalization(monkeypatch, tmp_path, arena_operations):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    monkeypatch.setattr(budget_guard, "guarded_call", lambda _request, _provider, dispatch, *, tariff=None: dispatch())
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    frames = []

    def request(operation, parameters, *, admitted=False, timeout_seconds=None):
        frames.append((operation, copy.deepcopy(parameters), admitted, timeout_seconds))
        return 200, {"content-type": "text/html"}, (
            "<html><head><script>secret state</script></head><body><h1>Acme careers</h1>"
            "<p>Hiring two sales leaders.</p></body></html>")

    monkeypatch.setattr(broker, "request", request)
    body, code = broker.execute({"operation": "scrape", "url": "https://acme.test/careers"}, lambda _raw: None)

    assert code == 0 and body["status"] == "ok"
    assert frames == [("scrapingdog.scrape", {"url": "https://acme.test/careers"}, True, 30.0)]
    assert arena_operations.validate_operation_request(frames[0][0], frames[0][1]) == {
        "url": "https://acme.test/careers", "dynamic": False}
    assert "Acme careers" in body["results"][0]["evidence_text"]
    assert "Hiring two sales leaders" in body["results"][0]["evidence_text"]
    assert "secret state" not in body["results"][0]["evidence_text"]


@pytest.mark.parametrize(
    "native_request,operation_id,parameters",
    [
        ({"operation": "linkedin_company", "id": "acme"}, "scrapingdog.profile",
         {"type": "company", "id": "acme"}),
        ({"operation": "linkedin_person", "id": "ada"}, "scrapingdog.profile",
         {"type": "profile", "id": "ada"}),
        ({"operation": "linkedin_job", "job_id": "123"}, "scrapingdog.jobs", {"job_id": "123"}),
        ({"operation": "google_jobs", "query": "Acme engineer", "country": "us"},
         "scrapingdog.google_jobs", {"query": "Acme engineer", "country": "us"}),
        ({"operation": "google_news", "query": "Acme launch", "country": "gb"},
         "scrapingdog.google_news", {"query": "Acme launch", "country": "gb"}),
        ({"operation": "linkedin_post", "id": "post-1"}, "scrapingdog.profile_post", {"id": "post-1"}),
        ({"operation": "x_profile", "profileId": "profile-1"}, "scrapingdog.x_profile",
         {"profileId": "profile-1"}),
        ({"operation": "x_post", "tweetId": "tweet-1"}, "scrapingdog.x_post", {"tweetId": "tweet-1"}),
        ({"operation": "youtube_search", "search_query": "Acme"}, "scrapingdog.youtube_search",
         {"search_query": "Acme"}),
        ({"operation": "youtube_video", "v": "video-1"}, "scrapingdog.youtube_video", {"v": "video-1"}),
        ({"operation": "youtube_transcript", "v": "video-1"}, "scrapingdog.youtube_transcripts",
         {"v": "video-1"}),
        ({"operation": "tiktok_profile", "username": "acme"}, "scrapingdog.tiktok_profile",
         {"username": "acme"}),
    ],
)
def test_scrapingdog_other_native_routes_match_existing_arena_operations(
        monkeypatch, tmp_path, arena_operations, native_request, operation_id, parameters):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    validated = scrapingdog.validate_request(native_request)
    validated["api_key"] = SCRAPINGDOG_RUNTIME_HANDLE

    _url, actual_operation, actual_parameters = broker._scrapingdog_frame(validated)

    assert actual_operation == operation_id
    assert actual_parameters == parameters
    authoritative = arena_operations.validate_operation_request(actual_operation, actual_parameters)
    assert all(authoritative[key] == value for key, value in actual_parameters.items())


def test_scrapingdog_local_parameter_bounds_are_coupled_to_authoritative_operations(arena_operations):
    from tyche_arena import broker as arena_broker

    kinds = {str: "str", int: "int", bool: "bool"}
    assert set(arena_broker._SCRAPINGDOG_FIELDS) == {
        route[1] for route in arena_broker._SCRAPINGDOG_ROUTES.values()}
    for operation_id, local_fields in arena_broker._SCRAPINGDOG_FIELDS.items():
        host_fields = arena_operations.OPERATIONS[operation_id].request_fields
        assert set(local_fields) <= set(host_fields)
        assert arena_broker._SCRAPINGDOG_REQUIRED_FIELDS[operation_id] == {
            name for name in local_fields if host_fields[name].required}
        for name, (kind, minimum, maximum) in local_fields.items():
            host = host_fields[name]
            assert host.kind == kinds[kind]
            if kind is str:
                assert (host.min_length, host.max_length) == (minimum, maximum)
                if name == "url":
                    assert host.format == "https_url"
            elif kind is int:
                assert (host.minimum, host.maximum) == (minimum, maximum)
        country = host_fields.get("country")
        if country is not None:
            assert set(country.choices) == arena_broker._GOOGLE_COUNTRIES


def test_scrapingdog_rate_and_mapped_costs_are_coupled_to_authoritative_arena_pricing(
        arena_operations):
    from decimal import Decimal
    from tyche_arena import broker as arena_broker

    provider_costs = importlib.import_module("lab_arena.provider_costs")
    assert runtime.SCRAPINGDOG_USD_PER_CREDIT == provider_costs.SCRAPINGDOG_USD_PER_CREDIT
    cases = [
        ("scrapingdog.google", {"query": "Acme"}),
        ("scrapingdog.scrape", {"url": "https://acme.test"}),
        ("scrapingdog.profile", {"type": "company", "id": "acme"}),
        ("scrapingdog.profile", {"type": "profile", "id": "ada"}),
        ("scrapingdog.jobs", {"job_id": "123"}),
        ("scrapingdog.google_jobs", {"query": "Acme"}),
        ("scrapingdog.google_news", {"query": "Acme"}),
        ("scrapingdog.profile_post", {"id": "post-1"}),
        ("scrapingdog.x_profile", {"profileId": "profile-1"}),
        ("scrapingdog.x_post", {"tweetId": "tweet-1"}),
        ("scrapingdog.youtube_search", {"search_query": "Acme"}),
        ("scrapingdog.youtube_video", {"v": "video-1"}),
        ("scrapingdog.youtube_transcripts", {"v": "video-1"}),
        ("scrapingdog.tiktok_profile", {"username": "acme"}),
    ]
    assert {operation for operation, _parameters in cases} == {
        route[1] for route in arena_broker._SCRAPINGDOG_ROUTES.values()}
    for operation, parameters in cases:
        authoritative = provider_costs.scrapingdog_cost(operation, parameters)
        assert Decimal(arena_broker.Broker._scrapingdog_minimum_credits(
            operation, parameters)) == authoritative.units


@pytest.mark.parametrize(
    "native_request,bound,minimum",
    [
        ({"operation": "google_search", "query": "Acme"}, 4, 5),
        ({"operation": "linkedin_company", "id": "acme"}, 9, 10),
        ({"operation": "linkedin_person", "id": "ada"}, 99, 100),
    ],
)
def test_scrapingdog_underestimated_host_cost_fails_before_admission_or_reservation(
        monkeypatch, tmp_path, native_request, bound, minimum):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    guarded = []
    monkeypatch.setattr(budget_guard, "guarded_call", lambda *_args, **_kwargs: guarded.append(True))
    instance = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    request = {**native_request, "spend": {"max_cost_credits": bound}}
    captured = []

    body, code = instance.execute(request, captured.append)

    assert code == 2
    assert {key: body[key] for key in (
        "status", "provider", "operation", "request_sent", "error_stage")} == {
            "status": "schema_error", "provider": "scrapingdog",
            "operation": native_request["operation"], "request_sent": False,
            "error_stage": "request"}
    assert f"cost of {minimum}" in body["error"]["message"]
    assert captured == [{"arena": {"dispatched": False, "error": "schema_error"},
                         "error": body["error"]}]
    assert guarded == []
    assert instance.provider_calls("scrapingdog") == 0


@pytest.mark.parametrize(
    "native_request,detail",
    [
        ({"operation": "google_search", "query": "Acme", "page": 2}, "page"),
        ({"operation": "google_search", "query": "Acme", "language": "en"}, "language"),
        ({"operation": "google_search", "query": "Acme", "domain": "google.co.uk"}, "domain"),
        ({"operation": "google_search", "query": "Acme", "advance_search": True}, "advance_search"),
        ({"operation": "google_search", "query": "Acme", "mob_search": True}, "mob_search"),
        ({"operation": "google_search", "query": "Acme", "country": "xx"}, "country"),
        ({"operation": "google_search", "query": "x" * 501}, "query"),
        ({"operation": "scrape", "url": "http://acme.test"}, "HTTPS URL"),
        ({"operation": "scrape", "url": "https://acme.test/" + "x" * 2000}, "url"),
        ({"operation": "scrape", "url": "https://acme.test", "wait": 15001}, "wait"),
        ({"operation": "scrape", "url": "https://acme.test", "country": "us"}, "country"),
        ({"operation": "x_post", "tweetId": "x" * 65}, "tweetId"),
        ({"operation": "google_maps", "query": "Acme"}, "supported operations"),
    ],
)
def test_scrapingdog_unsupported_semantics_fail_before_admission_or_paid_call(
        monkeypatch, tmp_path, native_request, detail):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    paid = []
    monkeypatch.setattr(budget_guard, "guarded_call", lambda *_args, **_kwargs: paid.append(True))
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    captured = []

    body, code = broker.execute(native_request, captured.append)

    assert code == 2 and body["status"] == "schema_error"
    assert body["provider"] == "scrapingdog" and body["operation"] == native_request["operation"]
    assert body["error_stage"] == "request" and body["request_sent"] is False
    assert detail in body["error"]["message"]
    assert captured == [{"arena": {"dispatched": False, "error": "schema_error"},
                         "error": body["error"]}]
    assert paid == []
    assert broker.provider_calls("scrapingdog") == 0


def test_scrapingdog_queued_calls_keep_per_call_transport_binding(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 30)
    caller_start = threading.Barrier(2)
    state_lock = threading.Lock()
    paid_lock_attempts = 0
    both_paid_lock_attempts = threading.Event()
    first_dispatch_entered = threading.Event()
    second_dispatch_entered = threading.Event()
    release_first_dispatch = threading.Event()
    active_dispatches = 0
    maximum_active_dispatches = 0
    caller_threads = {}
    transport_threads = {}
    cost_receipts = {}
    native_transport = scrapingdog._http_get
    acquire_paid_dispatch = broker._acquire_paid_dispatch

    def acquire(*, allow_after_deadline=False):
        nonlocal paid_lock_attempts
        with state_lock:
            paid_lock_attempts += 1
            if paid_lock_attempts == 2:
                both_paid_lock_attempts.set()
        return acquire_paid_dispatch(allow_after_deadline=allow_after_deadline)

    def guarded_call(request, provider, dispatch, *, tariff=None):
        body, code = dispatch()
        with state_lock:
            cost_receipts[request["query"]] = {
                "provider": provider,
                "tariff": copy.deepcopy(tariff),
                "billing": copy.deepcopy(body["billing"]),
                "billing_final": body["billing_final"],
            }
        return body, code

    monkeypatch.setattr(broker, "_acquire_paid_dispatch", acquire)
    monkeypatch.setattr(budget_guard, "guarded_call", guarded_call)

    def request(operation, parameters, *, admitted=False, timeout_seconds=None):
        nonlocal active_dispatches, maximum_active_dispatches
        assert operation == "scrapingdog.google" and admitted is True
        assert timeout_seconds == 30.0
        query = parameters["query"]
        with state_lock:
            transport_threads[query] = threading.get_ident()
            active_dispatches += 1
            maximum_active_dispatches = max(maximum_active_dispatches, active_dispatches)
            dispatch_number = len(transport_threads)
        try:
            if dispatch_number == 1:
                first_dispatch_entered.set()
                assert release_first_dispatch.wait(2)
            else:
                second_dispatch_entered.set()
            payload = {"organic_results": [{
                "title": query, "link": f"https://{query}.test/", "snippet": query,
            }]}
            return 200, {}, json.dumps(payload)
        finally:
            with state_lock:
                active_dispatches -= 1

    monkeypatch.setattr(broker, "request", request)

    def execute(query):
        caller_threads[query] = threading.get_ident()
        caller_start.wait(timeout=2)
        body, code = broker.execute({"operation": "google_search", "query": query, "country": "us"},
                                    lambda _raw: None)
        return code, body

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {query: pool.submit(execute, query) for query in ("alpha", "beta")}
        assert first_dispatch_entered.wait(1)
        assert both_paid_lock_attempts.wait(1)
        assert not second_dispatch_entered.is_set()
        release_first_dispatch.set()
        results = {query: future.result(timeout=2) for query, future in futures.items()}

    assert results["alpha"][1]["results"][0]["domain"] == "alpha.test"
    assert results["beta"][1]["results"][0]["domain"] == "beta.test"
    assert all(result[0] == 0 for result in results.values())
    assert second_dispatch_entered.is_set()
    assert maximum_active_dispatches == 1
    assert transport_threads == caller_threads
    assert cost_receipts == {
        query: {
            "provider": "scrapingdog",
            "tariff": {
                "version": "scrapingdog-2026-09-19",
                "operation": "google_search",
                "options": {"advance_search": False, "mob_search": False},
                "minimum_credits": 5,
                "maximum_credits": 5,
                "sources": ["https://www.scrapingdog.com/documentation/google-search-api/"],
            },
            "billing": {"credits_charged": 5, "basis": "documented_endpoint_tariff"},
            "billing_final": True,
        }
        for query in ("alpha", "beta")
    }
    assert broker.provider_calls("scrapingdog") == 2
    assert scrapingdog._http_get is native_transport


def test_scrapingdog_resume_is_provider_specific_and_preserves_uncertain_liability(tmp_path):
    run_file = tmp_path / "run" / "results.json"
    run_file.parent.mkdir()
    run_file.write_text(json.dumps({"routes": [{"route_id": "sd-one", "provider": "scrapingdog",
                                                 "paid_calls": 1, "request_fingerprint": "request-one"}]}))
    ledger = {"version": 1, "run_file": str(run_file.resolve()),
              "run_fingerprint": budget_guard.run_fingerprint(run_file),
              "calls": {"sd-one": {"provider": "scrapingdog"}}}
    budget_guard.ledger_path(run_file).write_text(json.dumps(ledger))
    receipts = run_file.parent / "receipts"
    receipts.mkdir()
    receipt = {"receipt_status": "complete", "run_fingerprint": budget_guard.run_fingerprint(run_file),
               "provider": "scrapingdog", "request_fingerprint": "request-one",
               "provider_response": {"timed_out": True, "incomplete": True}}
    (receipts / "sd-one.json").write_text(json.dumps(receipt))

    calls, blocked = broker_resume_state(run_file)
    assert calls == {"deepline": 0, "scrapingdog": 1}
    assert blocked == {"deepline": False, "scrapingdog": True}
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 30,
                    initial_calls=calls, provider_blocked=blocked)
    before = budget_guard.ledger_path(run_file).read_bytes()
    with pytest.raises(BrokerRefusal, match="scrapingdog_blocked_after_uncertain_call"):
        broker.request("scrapingdog.google", {"query": "Acme", "country": "us"})
    assert budget_guard.ledger_path(run_file).read_bytes() == before
    assert broker.provider_calls("scrapingdog") == 1
    assert broker.provider_calls("deepline") == 0


def test_scrapingdog_and_deepline_keep_independent_local_telemetry(tmp_path):
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 30,
                    initial_calls={"deepline": 29, "scrapingdog": 30})

    broker._admit("deepline")
    broker._admit("scrapingdog")

    budget = broker.local_dispatch_budget()["providers"]
    assert budget == {"deepline": {"used": 30}, "scrapingdog": {"used": 31}}


@pytest.mark.parametrize("case", ["google_json", "scrape_html"])
def test_native_research_lookup_uses_framed_scrapingdog_worker_and_real_ledger(
        monkeypatch, tmp_path, arena_operations, case):
    socket_path = Path("/tmp") / f"tyche-sd-{os.getpid()}-{abs(hash(tmp_path))}.sock"
    if case == "google_json":
        inputs = {"operation": "google_search", "query": "Acme warehouse", "country": "us",
                  "timeout_seconds": 2}
        body = json.dumps({"organic_results": [{"title": "Acme", "link": "https://acme.test/news",
                                                 "snippet": "Acme opened a warehouse in September 2026."}]}).encode()
        operation_id = "scrapingdog.google"
    else:
        inputs = {"operation": "scrape", "url": "https://acme.test/careers", "timeout_seconds": 2}
        body = b"<html><head><script>hidden</script></head><body>Acme is hiring sales leaders.</body></html>"
        operation_id = "scrapingdog.scrape"
    response = (200, {"content-type": "application/json" if case == "google_json" else "text/html"}, body)

    with FramedArenaWorker(socket_path, arena_operations, [response]) as worker:
        research, broker = native_scrapingdog_research(tmp_path, monkeypatch, socket_path)
        if case == "google_json":
            # The absolute phase cutoff bounds both the provider frame and the socket wait.
            broker.response_deadline = time.monotonic() + 1
        result = research.call("tyche_lookup", scrapingdog_lookup_request(inputs))

    lookup_result = result["lookups"][0]
    assert lookup_result["status"] == "ok"
    facts = lookup_result["results"][0]["facts"]
    assert (facts["domain"] == "acme.test" if case == "google_json"
            else "Acme is hiring sales leaders" in facts["evidence_text"])
    if case == "scrape_html":
        assert "hidden" not in facts["evidence_text"]
    assert len(worker.frames) == 1 and worker.frames[0]["operation_id"] == operation_id
    if case == "google_json":
        assert 1 <= worker.frames[0]["timeout_ms"] <= 1000
    else:
        assert worker.frames[0]["timeout_ms"] == 2000
    ledger = budget_guard.load_ledger(research.path)
    assert len(ledger["calls"]) == 1
    call = next(iter(ledger["calls"].values()))
    assert call["provider"] == "scrapingdog" and call["state"] == "settled"
    assert call["actual_credits"] == "5"  # Google search and default dynamic scrape.
    route = lookup_result["route"]
    receipt = json.loads((research.path.parent / "receipts" / (route + ".json")).read_text())
    assert receipt["provider"] == "scrapingdog" and receipt["receipt_status"] == "complete"
    calls, blocked = broker_resume_state(research.path)
    assert calls["scrapingdog"] == 1 and blocked["scrapingdog"] is False


def test_runtime_initialization_enables_real_labtools_scrapingdog_dispatch(
        monkeypatch, tmp_path, arena_operations):
    socket_path = Path("/tmp") / f"tyche-sd-runtime-{os.getpid()}-{abs(hash(tmp_path))}.sock"
    output = tmp_path / "companies.json"
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "1")
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(socket_path))
    monkeypatch.setenv("LAB_ARENA_OUTPUT_PATH", str(output))
    monkeypatch.setenv("LAB_ARENA_EVALUATION_DATE", "2026-09-17")
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    monkeypatch.setattr(runtime, "require_lab", lambda: SimpleNamespace())
    original_mkdtemp = runtime.tempfile.mkdtemp
    monkeypatch.setattr(runtime.tempfile, "mkdtemp",
                        lambda **_kwargs: original_mkdtemp(prefix="runtime-sd-", dir=tmp_path))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=lambda _rows: None, quota_usage=lambda: quota_snapshot(),
        QuotaUnavailable=QuotaUnavailable))
    monkeypatch.setattr(runtime, "checkpointed_companies", lambda *_args, **_kwargs: [])
    observed = {}

    def launch(_host, run_dir, deadline, response_deadline, _remaining, _quota_guard):
        tools = LabTools(run_dir / "results.json", deadline, response_deadline)
        observed["result"] = tools.call("tyche_lookup", scrapingdog_lookup_request({
            "operation": "google_search", "query": "Acme warehouse", "country": "us",
            "timeout_seconds": 2,
        }))
        observed["run_file"] = tools.research.path

    monkeypatch.setattr(runtime, "launch", launch)
    response = (200, {"content-type": "application/json"}, json.dumps({
        "organic_results": [{"title": "Acme", "link": "https://acme.test/news",
                             "snippet": "Acme opened a warehouse in September 2026."}],
    }).encode())

    with FramedArenaWorker(socket_path, arena_operations, [response]) as worker:
        assert runtime.run(ICP) == []

    lookup_result = observed["result"]["lookups"][0]
    assert lookup_result["status"] == "ok"
    assert worker.frames[0]["operation_id"] == "scrapingdog.google"
    assert worker.frames[0]["parameters"] == {"query": "Acme warehouse", "country": "us"}
    ledger = budget_guard.load_ledger(observed["run_file"])
    assert ledger["usd_limit"] == "0.80"
    assert ledger["credit_limits"] == {"deepline": "8.0", "scrapingdog": "16000.0"}
    assert ledger["usd_per_credit"] == {"deepline": "0.10", "scrapingdog": "0.00005"}
    call = next(iter(ledger["calls"].values()))
    assert call["provider"] == "scrapingdog"
    assert call["tariff"]["maximum_credits"] == 5
    assert call["actual_credits"] == "5"
    assert call["state"] == "settled"


def test_runtime_initializes_four_dollar_provider_allowance_for_five_companies(
        monkeypatch, tmp_path):
    output = tmp_path / "companies.json"
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", str(tmp_path / "worker.sock"))
    monkeypatch.setenv("LAB_ARENA_OUTPUT_PATH", str(output))
    monkeypatch.setenv("LAB_ARENA_EVALUATION_DATE", "2026-09-18")
    monkeypatch.delenv("SCRAPINGDOG_API_KEY", raising=False)
    monkeypatch.setattr(runtime, "require_lab", lambda: SimpleNamespace())
    original_mkdtemp = runtime.tempfile.mkdtemp
    monkeypatch.setattr(runtime.tempfile, "mkdtemp",
                        lambda **_kwargs: original_mkdtemp(prefix="runtime-budget-", dir=tmp_path))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=lambda _rows: None, quota_usage=lambda: quota_snapshot(),
        QuotaUnavailable=QuotaUnavailable))
    monkeypatch.setattr(runtime, "checkpointed_companies", lambda *_args, **_kwargs: [])
    observed = {}

    def launch(_host, run_dir, _deadline, _response_deadline, _remaining, _quota_guard):
        observed["ledger"] = budget_guard.load_ledger(run_dir / "results.json")

    monkeypatch.setattr(runtime, "launch", launch)

    assert runtime.run(ICP) == []
    assert observed["ledger"]["usd_limit"] == "4.00"
    assert observed["ledger"]["credit_limits"]["deepline"] == "40.0"


@pytest.mark.parametrize(
    "inputs,max_cost_credits,detail",
    [
        ({"operation": "google_search", "query": "Acme", "page": 2}, 5, "page"),
        ({"operation": "google_news", "query": "Acme", "limit": 20}, 5, "Arena-fixed results=10"),
        ({"operation": "google_maps", "query": "Acme"}, 5, "supported operations"),
    ],
)
def test_native_scrapingdog_predispatch_input_failure_completes_zero_cost_receipt(
        monkeypatch, tmp_path, inputs, max_cost_credits, detail):
    socket_path = tmp_path / "must-not-connect.sock"
    research, broker = native_scrapingdog_research(tmp_path, monkeypatch, socket_path)

    result = research.lookup(scrapingdog_lookup_request(
        inputs, max_cost_credits=max_cost_credits)["checks"])

    lookup_result = result["lookups"][0]
    assert lookup_result["status"] == "schema_error" and lookup_result["recorded"] is True
    assert detail in lookup_result["error"]["message"]
    document = json.loads(research.path.read_text())
    route = document["routes"][-1]
    assert route["provider"] == "scrapingdog" and route["operation"] == inputs["operation"]
    assert route["provider_status"] == "schema_error" and route["paid_calls"] == 0
    receipt = json.loads((research.path.parent / "receipts" / (route["route_id"] + ".json")).read_text())
    assert receipt["receipt_status"] == "complete"
    assert {key: receipt[key] for key in (
        "status", "provider", "operation", "request_sent", "error_stage")} == {
            "status": "schema_error", "provider": "scrapingdog",
            "operation": inputs["operation"], "request_sent": False,
            "error_stage": "request"}
    assert detail in receipt["error"]["message"]
    assert receipt["provider_response"] == {
        "arena": {"dispatched": False, "error": "schema_error"},
        "error": receipt["error"],
    }
    assert broker.provider_calls("scrapingdog") == 0
    assert broker.provider_is_blocked("scrapingdog") is False
    assert budget_guard.load_ledger(research.path)["calls"] == {}
    assert research._operational_block() is None
    calls, blocked = broker_resume_state(research.path)
    assert calls["scrapingdog"] == 0 and blocked["scrapingdog"] is False


def test_native_scrapingdog_predispatch_configuration_failure_completes_zero_cost_receipt(
        monkeypatch, tmp_path):
    research, broker = native_scrapingdog_research(
        tmp_path, monkeypatch, tmp_path / "must-not-connect.sock")
    monkeypatch.delenv("SCRAPINGDOG_API_KEY")
    inputs = {"operation": "google_search", "query": "Acme", "country": "us"}

    result = research.lookup(scrapingdog_lookup_request(inputs)["checks"])

    lookup_result = result["lookups"][0]
    assert lookup_result["status"] == "config_error" and lookup_result["recorded"] is True
    assert "runtime handle" in lookup_result["error"]["message"]
    document = json.loads(research.path.read_text())
    route = document["routes"][-1]
    assert route["provider_status"] == "config_error" and route["paid_calls"] == 0
    receipt = json.loads((research.path.parent / "receipts" / (route["route_id"] + ".json")).read_text())
    assert receipt["receipt_status"] == "complete"
    assert receipt["provider"] == "scrapingdog" and receipt["operation"] == "google_search"
    assert receipt["status"] == "config_error" and receipt["request_sent"] is False
    assert "error_stage" not in receipt
    assert receipt["provider_response"] == {
        "arena": {"dispatched": False, "error": "config_error"},
        "error": receipt["error"],
    }
    assert budget_guard.load_ledger(research.path)["calls"] == {}
    assert broker.provider_calls("scrapingdog") == 0
    assert broker.provider_is_blocked("scrapingdog") is False
    assert research._operational_block() is None
    calls, blocked = broker_resume_state(research.path)
    assert calls["scrapingdog"] == 0 and blocked["scrapingdog"] is False


def test_scrapingdog_unexpected_preflight_exception_is_not_normalized(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    monkeypatch.setattr(scrapingdog, "validate_request",
                        lambda _request: (_ for _ in ()).throw(RuntimeError("unexpected validator failure")))
    monkeypatch.setattr(budget_guard, "guarded_call",
                        lambda *_args, **_kwargs: pytest.fail("unexpected failure must not enter budget guard"))
    broker = Broker(tmp_path / "must-not-connect.sock", time.monotonic() + 30)
    captured = []

    with pytest.raises(RuntimeError, match="unexpected validator failure"):
        broker.execute({"operation": "google_search", "query": "Acme"}, captured.append)

    assert captured == []
    assert broker.provider_calls("scrapingdog") == 0


@pytest.mark.parametrize("spend", [None, {"max_cost_credits": "invalid"}])
def test_scrapingdog_missing_or_invalid_spend_keeps_native_no_send_refusal(
        monkeypatch, tmp_path, spend):
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)
    broker = Broker(tmp_path / "must-not-connect.sock", time.monotonic() + 30)
    request = {"operation": "google_search", "query": "Acme", "country": "us"}
    if spend is not None:
        request["spend"] = spend
    captured = []

    body, code = broker.execute(request, captured.append)

    assert code == 2 and body["status"] == "quota_exceeded"
    assert body["request_sent"] is False
    assert broker.provider_calls("scrapingdog") == 0
    assert captured == []


@pytest.mark.parametrize(
    "worker_response,expected_status,expected_blocked",
    [("budget_exhausted", "quota_exceeded", False), ("disconnect", "timeout", True)],
)
def test_native_scrapingdog_refusal_and_uncertainty_retain_real_reservation_without_replay(
        monkeypatch, tmp_path, arena_operations, worker_response, expected_status, expected_blocked):
    socket_path = Path("/tmp") / f"tyche-sd-{os.getpid()}-{abs(hash(tmp_path))}.sock"
    inputs = {"operation": "google_search", "query": "Acme uncertain", "country": "us"}

    with FramedArenaWorker(socket_path, arena_operations, [worker_response]) as worker:
        research, _broker = native_scrapingdog_research(tmp_path, monkeypatch, socket_path)
        result = research.call("tyche_lookup", scrapingdog_lookup_request(inputs))
        ledger_before = budget_guard.ledger_path(research.path).read_bytes()
        with pytest.raises(ValueError, match="request already attempted or pending"):
            research.call("tyche_lookup", scrapingdog_lookup_request(inputs))

    assert result["lookups"][0]["status"] == expected_status
    assert len(worker.frames) == 1
    assert budget_guard.ledger_path(research.path).read_bytes() == ledger_before
    ledger = budget_guard.load_ledger(research.path)
    assert len(ledger["calls"]) == 1
    route_id, call = next(iter(ledger["calls"].items()))
    assert call["provider"] == "scrapingdog"
    assert call["actual_credits"] is None and call["actual_usd"] is None
    receipt = json.loads((research.path.parent / "receipts" / (route_id + ".json")).read_text())
    assert receipt["spend_receipt"]["state"] == "reserved"
    assert "billing" not in receipt and "credits_charged" not in receipt
    calls, blocked = broker_resume_state(research.path)
    assert calls["scrapingdog"] == 1 and blocked["scrapingdog"] is expected_blocked
    assert calls["deepline"] == 0 and blocked["deepline"] is False


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
            assert sent["timeout_ms"] == 91_000

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


def test_provider_socket_wait_uses_existing_response_deadline_not_operation_window(
        monkeypatch):
    from tyche_arena import broker

    clock = [100.0]
    timeouts = []
    provider_body = json.dumps({"status": "ok", "results": []}).encode()
    reply = json.dumps({
        "status": 200,
        "headers": {},
        "body_b64": base64.b64encode(provider_body).decode(),
    }).encode()

    class Connection:
        def __init__(self):
            self.reply = len(reply).to_bytes(4, "big") + reply

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, value):
            timeouts.append(value)

        def connect(self, _path):
            return None

        def sendall(self, frame):
            size = int.from_bytes(frame[:4], "big")
            assert json.loads(frame[4:4 + size])["timeout_ms"] == 30_000

        def recv(self, size):
            part, self.reply = self.reply[:size], self.reply[size:]
            return part

    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker.socket, "socket", lambda *_args: Connection())
    instance = Broker(
        "/tmp/fixture.sock", 900.0, response_deadline=1000.0,
        catalog={"exa_search": {}},
    )

    assert instance.request(
        "deepline.execute", {"tool": "exa_search", "payload": {}},
        timeout_seconds=30,
    ) == (200, {}, {"status": "ok", "results": []})
    assert timeouts and all(value == 900.0 for value in timeouts)


@pytest.mark.parametrize(
    "native_timeout,response_at,response_deadline,expected_timeout_ms,accepted",
    [
        (None, 250.0, 400.0, 240_000, True),
        (30.0, 90.0, 400.0, 30_000, True),
        (None, 250.0, 245.0, 240_000, False),
    ],
)
def test_deepline_timeout_preserves_native_bound_and_absolute_response_phase(
        monkeypatch, native_timeout, response_at, response_deadline,
        expected_timeout_ms, accepted):
    from tyche_arena import broker

    clock = [0.0]
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
            clock[0] = 10.0

        def sendall(self, frame):
            size = int.from_bytes(frame[:4], "big")
            sent = json.loads(frame[4:4 + size])
            assert sent["operation_id"] == "deepline.execute"
            assert sent["timeout_ms"] == expected_timeout_ms

        def recv(self, size):
            clock[0] = response_at
            part, self.reply = self.reply[:size], self.reply[size:]
            return part

    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker.socket, "socket", lambda *_args: Connection())
    instance = Broker("/tmp/fixture.sock", 20.0, response_deadline=response_deadline,
                      catalog={"exa_search": {}})
    call = lambda: instance.request(
        "deepline.execute", {"tool": "exa_search", "payload": {}},
        timeout_seconds=native_timeout)

    if accepted:
        assert call() == (200, {}, {"status": "ok", "results": []})
    else:
        with pytest.raises(BrokerError, match="transport failed"):
            call()


def test_deepline_tight_response_phase_caps_frame_and_every_socket_wait(monkeypatch):
    from tyche_arena import broker

    clock = [100.0]
    timeouts = []
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
            assert 0 < value <= 5.0
            timeouts.append(value)

        def connect(self, _path):
            clock[0] += 1.0

        def sendall(self, frame):
            size = int.from_bytes(frame[:4], "big")
            sent = json.loads(frame[4:4 + size])
            assert 1 <= sent["timeout_ms"] <= 5_000
            clock[0] += 1.0

        def recv(self, size):
            clock[0] += 1.0
            part, self.reply = self.reply[:size], self.reply[size:]
            return part

    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker.socket, "socket", lambda *_args: Connection())
    instance = Broker("/tmp/fixture.sock", 104.0, response_deadline=105.0,
                      catalog={"exa_search": {}})

    assert instance.request("deepline.execute", {
        "tool": "exa_search", "payload": {},
    }, timeout_seconds=240) == (200, {}, {"status": "ok", "results": []})
    assert timeouts == [5.0, 4.0, 3.0, 2.0]


def test_expired_response_phase_fails_before_admission_or_socket(monkeypatch, tmp_path):
    from tyche_arena import broker

    clock = [100.0]
    connected = []
    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker.socket, "socket", lambda *_args: connected.append(True))
    instance = Broker(tmp_path / "must-not-connect.sock", 100.0, response_deadline=100.0,
                      catalog={"exa_search": {}})

    with pytest.raises(BrokerRefusal, match="response_deadline_reached"):
        instance.request("deepline.execute", {"tool": "exa_search", "payload": {}})

    assert instance.provider_calls("deepline") == 0
    assert connected == []


@pytest.mark.parametrize("timeout_seconds", [float("nan"), float("inf"), float("-inf")])
def test_deepline_nonfinite_timeout_fails_before_admission(
        monkeypatch, tmp_path, timeout_seconds):
    instance = Broker(tmp_path / "must-not-connect.sock", time.monotonic() + 30,
                      catalog={"exa_search": {}})
    connected = []
    monkeypatch.setattr("tyche_arena.broker.socket.socket", lambda *_args: connected.append(True))

    with pytest.raises(ValueError, match="finite and positive"):
        instance.request("deepline.execute", {"tool": "exa_search", "payload": {}},
                         timeout_seconds=timeout_seconds)

    assert instance.provider_calls("deepline") == 0
    assert connected == []


@pytest.mark.parametrize(
    "requested_timeout,expected_timeout_ms",
    [(None, 240_000), (30, 30_000), (780, 240_000)],
)
def test_validated_native_deepline_timeout_reaches_authoritative_framed_worker(
        monkeypatch, tmp_path, arena_operations, requested_timeout, expected_timeout_ms):
    assert arena_operations.OPERATIONS["deepline.execute"].timeout_seconds == 240
    socket_path = Path("/tmp") / (
        f"tyche-dl-timeout-{os.getpid()}-{abs(hash((tmp_path, requested_timeout)))}.sock")
    native_request = {"operation": "execute", "tool": "exa_search", "payload": {"query": "Acme"}}
    if requested_timeout is not None:
        native_request["timeout_seconds"] = requested_timeout
    validated = deepline._validate_request(native_request)
    response = (200, {"content-type": "application/json"}, json.dumps({
        "status": "completed", "result": {"data": []},
        "billing": {"credits_charged": 0.07},
    }).encode())
    monkeypatch.setattr(budget_guard, "guarded_call",
                        lambda _request, provider, dispatch, *, tariff=None: (
                            dispatch() if provider == "deepline" else pytest.fail("wrong provider")))

    with FramedArenaWorker(socket_path, arena_operations, [response]) as worker:
        instance = Broker(socket_path, time.monotonic() + 30,
                          response_deadline=time.monotonic() + 320)
        body, code = instance.execute(validated, lambda _raw: None)

    assert code == 0
    assert body["status"] == "no_results"
    assert len(worker.frames) == 1
    assert worker.frames[0]["operation_id"] == "deepline.execute"
    assert worker.frames[0]["timeout_ms"] == expected_timeout_ms


@pytest.mark.parametrize("response_deadline,accepted", [(100.0, True), (45.0, False)])
def test_scrapingdog_native_timeout_limits_frame_without_cutting_off_broker_overhead(
        monkeypatch, response_deadline, accepted):
    from tyche_arena import broker

    clock = [0.0]
    provider_body = b'{"organic_results":[]}'
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
            clock[0] = 10.0  # Admission can precede provider execution.

        def sendall(self, frame):
            size = int.from_bytes(frame[:4], "big")
            sent = json.loads(frame[4:4 + size])
            assert sent["operation_id"] == "scrapingdog.google"
            assert sent["timeout_ms"] == 30_000

        def recv(self, size):
            # The completed envelope arrives after the 30-second provider
            # budget, but still within admission/billing overhead when the
            # absolute response phase remains open.
            clock[0] = 50.0
            part, self.reply = self.reply[:size], self.reply[size:]
            return part

    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker.socket, "socket", lambda *_args: Connection())
    instance = Broker("/tmp/fixture.sock", 20.0, response_deadline=response_deadline)
    call = lambda: instance.request(
        "scrapingdog.google", {"query": "Acme", "country": "us"}, timeout_seconds=30)

    if accepted:
        assert call() == (200, {}, provider_body.decode())
        assert clock[0] == 50.0
    else:
        with pytest.raises(BrokerError, match="transport failed"):
            call()


@pytest.mark.parametrize("response_at,accepted", [(126.0, True), (201.0, False)])
def test_scrapingdog_keeps_60_second_provider_limit_and_original_response_deadline(
        monkeypatch, response_at, accepted):
    from tyche_arena import broker

    clock = [0.0]
    provider_body = b'{"organic_results":[]}'
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
            pass

        def sendall(self, frame):
            size = int.from_bytes(frame[:4], "big")
            sent = json.loads(frame[4:4 + size])
            assert sent["timeout_ms"] == 60_000

        def recv(self, size):
            clock[0] = response_at
            part, self.reply = self.reply[:size], self.reply[size:]
            return part

    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker.socket, "socket", lambda *_args: Connection())
    instance = Broker("/tmp/fixture.sock", 20.0, response_deadline=200.0)
    call = lambda: instance.request(
        "scrapingdog.google", {"query": "Acme", "country": "us"}, timeout_seconds=120)

    if accepted:
        assert call() == (200, {}, provider_body.decode())
    else:
        with pytest.raises(BrokerError, match="transport failed"):
            call()


def test_local_telemetry_does_not_duplicate_legacy_or_current_host_limit():
    instance = Broker("/tmp/fixture.sock", time.monotonic() + 30)

    def admit(_index):
        try:
            instance._admit()
            return "admitted"
        except BrokerRefusal:
            return "refused"

    outcomes = [admit(index) for index in range(202)]
    assert outcomes == ["admitted"] * 202
    assert instance.calls == 202


@pytest.mark.parametrize("reason", ["deadline"])
@pytest.mark.parametrize("tool,inputs,phase", [
    ("harvestapi_get_company",
     {"url": "https://www.linkedin.com/company/late-example"}, "account_verification"),
    ("harvestapi_get_profile",
     {"url": "https://www.linkedin.com/in/late-example", "main": "true"}, "contact_verification"),
])
def test_no_send_refusal_preserves_native_finish_semantics(
        lab, monkeypatch, reason, tool, inputs, phase):
    """A no-send receipt fixes accounting; native stop policy still decides delivery."""

    from datetime import datetime, timedelta
    from harness import run_icp
    import run_attempt
    import validate_run

    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario(None)

    def refuse_then_finish(tools):
        # The accepted transition now requires its reviewed host checkpoint
        # before any later paid lookup, including this deliberate no-send call.
        accepted_packet = tools.call("tyche_checkpoint", {})
        accepted_checkpoint = tools.call(
            "tyche_review", {
                "review_ref": accepted_packet["review_ref"],
                "review_findings": review_findings(accepted_packet, tools),
            })
        assert accepted_checkpoint["checkpoint_saved"]
        before = budget_guard.load_ledger(tools.research.path)
        if reason == "deadline":
            tools.broker.deadline = time.monotonic() - 1
        refused = tools.call("tyche_lookup", lookup(tool, inputs, phase))
        assert refused["lookups"][0]["status"] == "config_error"
        after = budget_guard.load_ledger(tools.research.path)
        assert after["calls"] == before["calls"]
        document = json.loads(tools.research.path.read_text())
        route = document["routes"][-1]
        assert route["paid_calls"] == 0
        assert tools.research._operational_block() is None
        receipt = run_attempt.read_receipt(tools.research.path, route["route_id"])["result"]
        assert receipt["request_sent"] is False
        assert receipt["provider_response"]["arena"] == {
            "dispatched": False,
            "error": "deadline_reached" if reason == "deadline" else "deepline_quota_exceeded",
        }
        stop = run_attempt.evaluate_stop(document, execution_budget=after)
        assert stop["decision"] not in {"provider_stop", "input_or_configuration_stop"}
        checkpoint_packet = tools.call("tyche_checkpoint", {})
        assert checkpoint_packet["status"] == "review_required"
        checkpointed = tools.call(
            "tyche_checkpoint", {
                "review_ref": checkpoint_packet["review_ref"],
                "review_findings": review_findings(checkpoint_packet, tools),
            })
        assert checkpointed["checkpoint_saved"] and checkpointed["delivery_allowed"] is False
        before_deadline = tools.call("tyche_finish", {})
        assert before_deadline["status"] == "needs_research"
        assert before_deadline["delivery_allowed"] is False

        started = datetime.fromisoformat(
            document["stop_check"]["started_at"].replace("Z", "+00:00"))
        finished = started + timedelta(seconds=runtime.RESEARCH_SECONDS + 1)
        real_datetime = validate_run.datetime

        class FinishedClock(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return finished if tz is None else finished.astimezone(tz)

        assert run_attempt.evaluate_stop is validate_run.evaluate_stop
        monkeypatch.setattr(validate_run, "datetime", FinishedClock)
        packet = tools.call("tyche_finish", {})
        assert packet["status"] == "review_required", json.dumps(packet, sort_keys=True)
        delivered = tools.call("tyche_finish", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet),
        })
        assert delivered["checkpoint_saved"] and delivered["delivery_allowed"]

    lab.after_program = refuse_then_finish
    assert len(run_icp(ICP)) == 1


def test_host_quota_refusal_preserves_reviewed_checkpoint(lab, monkeypatch):
    """An authoritative host cap stops new work without losing reviewed output."""

    from harness import run_icp

    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario(None)

    def refuse_after_checkpoint(tools):
        packet = tools.call("tyche_checkpoint", {})
        saved = tools.call("tyche_review", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet, tools),
        })
        assert saved["checkpoint_saved"]
        checkpoint = lab.output.read_bytes()

        def host_refusal(*_args, **_kwargs):
            raise BrokerRefusal("budget_exhausted")

        tools.broker.request = host_refusal
        request = lookup(
            "harvestapi_get_company",
            {"url": "https://www.linkedin.com/company/host-capped-example"},
        )
        refused = tools.call("tyche_lookup", request)
        assert refused["lookups"][0]["status"] == "quota_exceeded"
        assert refused["lookups"][0]["request_sent"] is True
        assert lab.output.read_bytes() == checkpoint
        with pytest.raises(ValueError, match="request already attempted or pending"):
            tools.call("tyche_lookup", request)

    lab.after_program = refuse_after_checkpoint
    rows = run_icp(ICP)

    assert len(rows) == 1
    assert rows == json.loads(lab.output.read_text())["companies"]


def test_host_headroom_finishes_reviewed_partial_while_native_budget_can_continue(
        lab, monkeypatch):
    """The Arena research cap is a real stop even when native provider budget remains."""

    import run_coordination as coordination

    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario("tyche_checkpoint")
    observed = []

    def finish_after_headroom(tools):
        assert tools.research._overview()["stop"] == "continue"
        before_calls = len(lab.frames)
        coordination.configure(tools.research.path, 1)
        coordination.update(tools.research.path, lambda state: state.update(
            phase="finalization"))
        final = LabTools(tools.research.path, tools.broker.deadline,
                         tools.broker.response_deadline)
        final.research.environment.update(
            TYCHE_FINALIZATION_ONLY="1",
            TYCHE_HOST_RESEARCH_STOP="finalization_headroom",
        )
        assert final.research._overview()["stop"] == "continue"
        packet = final.call("tyche_finish", {})
        observed.append(packet)
        if packet["status"] != "review_required":
            return
        delivered = final.call("tyche_finish", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet),
        })
        observed.append(delivered)
        assert delivered["checkpoint_saved"] and delivered["delivery_allowed"]
        assert len(lab.frames) == before_calls

    lab.after_program = finish_after_headroom
    rows = runtime.run(ICP)
    assert len(observed) == 2 and observed[0]["status"] == "review_required"
    assert observed[1]["checkpoint_saved"] and observed[1]["delivery_allowed"]
    assert len(rows) == 1
    assert json.loads(lab.output.read_text()) == {"companies": rows}


def test_host_headroom_needs_current_review_before_empty_delivery(lab, monkeypatch):
    """A host cutoff permits reviewed empty output, not an unreviewed shortcut."""

    import run_coordination as coordination

    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")

    def inspect_only():
        yield "tyche_inspect", {}

    lab.program = inspect_only
    observed = []

    def finish_after_headroom(tools):
        assert tools.research._overview()["stop"] == "continue"
        coordination.configure(tools.research.path, 1)
        coordination.update(tools.research.path, lambda state: state.update(
            phase="finalization"))
        final = LabTools(tools.research.path, tools.broker.deadline,
                         tools.broker.response_deadline)
        final.research.environment["TYCHE_FINALIZATION_ONLY"] = "1"
        final.research.environment["TYCHE_HOST_RESEARCH_STOP"] = "host_limit"
        unchanged = final.call("tyche_finish", {})
        assert unchanged["status"] == "needs_research"
        assert not lab.output.exists()
        final.research.environment["TYCHE_HOST_RESEARCH_STOP"] = "finalization_headroom"
        packet = final.call("tyche_finish", {})
        observed.append(packet)
        assert packet["status"] == "review_required" and packet["companies"] == []
        assert not lab.output.exists()
        delivered = final.call("tyche_finish", {
            "review_ref": packet["review_ref"], "review_findings": [],
        })
        observed.append(delivered)

    lab.after_program = finish_after_headroom
    assert runtime.run(ICP) == []
    assert len(observed) == 2 and observed[1]["delivery_allowed"]
    assert json.loads(lab.output.read_text()) == {"companies": []}
    assert lab.frames == []


def test_host_headroom_does_not_close_unknown_billing(lab, monkeypatch):
    """The host research cutoff never converts an operational bill into delivery."""

    import run_coordination as coordination

    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario("tyche_checkpoint")
    observed = []

    def finish_after_headroom(tools):
        checkpoint = lab.output.read_bytes()
        coordination.configure(tools.research.path, 1)
        coordination.update(tools.research.path, lambda state: state.update(
            phase="finalization"))
        with budget_guard.transaction(
                tools.research.path.with_name("results.json.budget.json")) as ledger:
            ledger["blocked"] = "Later call billing is uncertain; do not retry"
        final = LabTools(tools.research.path, tools.broker.deadline,
                         tools.broker.response_deadline)
        final.research.environment.update(
            TYCHE_FINALIZATION_ONLY="1",
            TYCHE_HOST_RESEARCH_STOP="finalization_headroom",
        )
        result = final.call("tyche_finish", {})
        observed.append(result)
        assert result["status"] == "operationally_blocked"
        assert result["delivery_allowed"] is False
        assert lab.output.read_bytes() == checkpoint

    lab.after_program = finish_after_headroom
    rows = runtime.run(ICP)
    assert len(rows) == 1 and len(observed) == 1


def test_scrapingdog_predispatch_failures_reach_valid_empty_deadline_review(lab, monkeypatch):
    """No-send validation receipts close the route without inventing early completion."""

    from datetime import datetime, timedelta
    from harness import run_icp
    import run_attempt
    import validate_run

    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "1")
    monkeypatch.setenv("SCRAPINGDOG_API_KEY", SCRAPINGDOG_RUNTIME_HANDLE)

    def inspect_only():
        yield "tyche_inspect", {}

    lab.program = inspect_only

    def refuse_then_finish(tools):
        failures = [
            ({"operation": "google_search", "query": "Acme", "page": 2}, 5, "page"),
            ({"operation": "google_news", "query": "Acme", "limit": 20}, 5, "Arena-fixed results=10"),
            ({"operation": "google_maps", "query": "Acme"}, 5, "supported operations"),
                ]
        for inputs, bound, detail in failures:
            refused = tools.call(
                "tyche_lookup", scrapingdog_lookup_request(inputs, max_cost_credits=bound))
            lookup_result = refused["lookups"][0]
            assert lookup_result["status"] == "schema_error" and lookup_result["recorded"] is True
            assert detail in lookup_result["error"]["message"]
        document = json.loads(tools.research.path.read_text())
        scrapingdog_routes = [route for route in document["routes"]
                              if route["provider"] == "scrapingdog"]
        assert len(scrapingdog_routes) == 3
        assert all(route["paid_calls"] == 0 and route["provider_status"] == "schema_error"
                   for route in scrapingdog_routes)
        assert budget_guard.load_ledger(tools.research.path)["calls"] == {}
        assert tools.broker.provider_calls("scrapingdog") == 0
        assert tools.broker.provider_is_blocked("scrapingdog") is False
        assert tools.research._operational_block() is None
        calls, blocked = broker_resume_state(tools.research.path)
        assert calls["scrapingdog"] == 0 and blocked["scrapingdog"] is False
        assert tools.call("tyche_finish", {})["status"] == "needs_research"

        started = datetime.fromisoformat(
            document["stop_check"]["started_at"].replace("Z", "+00:00"))
        finished = started + timedelta(seconds=runtime.RESEARCH_SECONDS + 1)
        real_datetime = validate_run.datetime

        class FinishedClock(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return finished if tz is None else finished.astimezone(tz)

        assert run_attempt.evaluate_stop is validate_run.evaluate_stop
        monkeypatch.setattr(validate_run, "datetime", FinishedClock)
        checked, preflight = run_attempt.delivery_preflight(
            tools.research.path, json.loads(tools.research.path.read_text()), check_review=False)
        assert checked["accepted"] == []
        assert preflight["valid"] is True and preflight["stop_decision"]["decision"] == "time_limit_reached"
        packet = tools.call("tyche_finish", {})
        assert packet["status"] == "review_required" and packet["companies"] == []
        delivered = tools.call("tyche_finish", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet),
        })
        assert delivered["checkpoint_saved"] and delivered["delivery_allowed"]

    lab.after_program = refuse_then_finish
    assert run_icp(ICP) == []
    assert lab.frames == []


@pytest.mark.parametrize("provider_status", ["quota_exceeded", "auth_failed"])
def test_real_deepline_access_failure_still_stops_model_entry(
        lab, monkeypatch, provider_status):
    """A dispatched provider refusal remains a provider stop."""

    from harness import run_icp

    def access_failure(_self, operation, parameters, *, admitted=False, timeout_seconds=None):
        assert operation == "deepline.execute" and admitted is True
        lab.frames.append(copy.deepcopy(parameters))
        return 429 if provider_status == "quota_exceeded" else 401, {}, {
            "status": provider_status,
            "error": {"code": provider_status, "message": "fixture provider access failure"},
            "billing": {"credits_charged": 0, "cost_usd": 0},
        }

    monkeypatch.setattr(Broker, "request", access_failure)

    def provider_failure():
        result = yield "tyche_lookup", lookup(
            "harvestapi_get_company", {"url": "https://www.linkedin.com/company/provider-failure"})
        assert result["lookups"][0]["status"] == provider_status
        assert result["status"] == "operationally_blocked"

    lab.program = provider_failure
    with pytest.raises(RuntimeError, match="harvestapi_get_company"):
        run_icp(ICP)
    assert len(lab.frames) == 1
    assert not lab.output.exists()


def test_trickled_response_uses_one_absolute_wait_limit(monkeypatch):
    from tyche_arena import broker

    clock = iter([0.0, 0.4, 0.8, 1.2])
    monkeypatch.setattr(broker.time, "monotonic", lambda: next(clock))
    connection = SimpleNamespace(settimeout=lambda value: None, recv=lambda size: b"x")
    with pytest.raises(TimeoutError, match="wait limit"):
        Broker._receive(connection, 4, deadline=1.0)


def test_connect_time_does_not_extend_the_absolute_response_deadline(monkeypatch):
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
            assert self.timeout == 1185.0
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
    with pytest.raises(RuntimeError, match="invalid_or_unavailable_runtime_state"):
        runtime.run(ICP)
    assert not lab.output.exists()
    assert lab.research[0].delivered is False
    status = json.loads((lab.research[0].research.path.parent / "worker-status.json").read_text())
    assert "output mount is unavailable" in status["detail"]


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
    with pytest.raises(RuntimeError, match="repeated_worker_failure"):
        runtime.run(ICP)
    assert not lab.output.exists()
    assert len(json.loads(lab.research[0].research.path.read_text())["accepted"]) == 1


def test_accepted_review_returns_packet_and_review_approval_saves_atomically(lab, monkeypatch):
    import run_attempt

    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    captured = []
    lab.program = lambda: capture_accepted_review(captured)
    lab.mode = "partial_timeout"

    def approve_from_review(tools):
        assert len(captured) == 1
        packet = captured[0]
        assert packet["status"] == "review_required"
        assert packet["saved_companies"] == ["example.com"]
        assert packet["review_scope"] == "confirmed_leads"
        assert "tyche_review(review_ref=..., review_findings=...)" in packet["next"]
        assert packet["companies"][0]["sources"]
        assert not lab.output.exists()
        assert not tools.research.path.with_name("checkpoint-results.json").exists()

        calls_before = len(lab.frames)
        resumed = LabTools(tools.research.path, tools.broker.deadline, tools.broker.response_deadline)
        with pytest.raises(ValueError, match="review_findings"):
            resumed.call("tyche_review", {"review_ref": packet["review_ref"]})
        with pytest.raises(ValueError, match="source_refs absent from.*current packet"):
            resumed.call("tyche_review", {
                "review_ref": packet["review_ref"],
                "review_findings": [{
                    "target": "example.com",
                    "source_refs": ["route:does-not-exist:0"],
                    "finding": "This finding cites no source in the reviewed company packet.",
                }],
            })
        assert not lab.output.exists()
        assert json.loads(tools.research.path.with_name(
            "leads.json").read_text())["confirmed_count"] == 0
        blocked = resumed.call("tyche_lookup", lookup(
            "harvestapi_get_company", {"url": "https://www.linkedin.com/company/later-example"}))
        assert blocked["status"] == "review_required"
        assert blocked["review_ref"] == packet["review_ref"]
        assert len(lab.frames) == calls_before

        opened = []
        resumed.public_web.open = lambda **arguments: opened.append(arguments) or {"status": "ok"}
        exact = resumed.call("tyche_open", {
            "target": "example.com", "purpose": "Corroborate the reviewed signal",
            "url": "https://example.com/news/wms-project",
        })
        assert exact["status"] == "ok" and len(opened) == 1
        unrelated = resumed.call("tyche_open", {
            "target": "example.com", "purpose": "Start unrelated research",
            "url": "https://unrelated.example/new",
        })
        assert unrelated["status"] == "review_required" and len(opened) == 1

        document = json.loads(tools.research.path.read_text())
        document["accepted"][0]["intent_details"] += " Updated after the first packet."
        tools.research.path.write_text(json.dumps(document))
        stale = resumed.call("tyche_review", {"review_ref": packet["review_ref"]})
        assert stale["status"] == "review_required"
        assert stale["review_ref"] == confirmed_leads.review_ref(tools.research.path, document)
        assert stale["review_ref"] != packet["review_ref"]
        assert not lab.output.exists()

        # Restore the valid reviewed prose through the native correction path;
        # that change returns the current packet instead of requiring a separate
        # checkpoint tool call.
        fresh = resumed.call("tyche_review", {"companies": [{
            "target": "example.com", "decision": "accept",
            "reason": "Restore the evidence-reviewed writing",
            "intent_details": PARAGRAPH,
        }]})
        assert fresh["status"] == "review_required"
        saved = resumed.call("tyche_review", {
            "review_ref": fresh["review_ref"],
            "review_findings": review_findings(fresh),
        })
        assert saved["status"] == "confirmed_leads_saved"
        assert saved["checkpoint_saved"]
        assert saved["delivery_allowed"] is False
        assert len(json.loads(lab.output.read_text())["companies"]) == 1
        snapshot = json.loads(tools.research.path.with_name("checkpoint-results.json").read_text())
        assert snapshot["accepted"] == json.loads(tools.research.path.read_text())["accepted"]

    lab.after_program = approve_from_review
    assert len(runtime.run(ICP)) == 1


def test_one_then_two_receipt_backed_leads_are_reviewed_and_checkpointed(lab, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = two_company_checkpoint_scenario
    lab.mode = "partial_timeout"

    rows = runtime.run(ICP)

    assert len(rows) == 2
    assert {row["company_website"] for row in rows} == {
        "https://example.com", "https://second.example"}
    assert {row["contact"]["email"] for row in rows} == {
        "ada@example.com", "bob@second.example"}
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    snapshot = json.loads(
        lab.research[0].research.path.with_name("checkpoint-results.json").read_text())
    assert len(snapshot["accepted"]) == 2


@pytest.mark.parametrize("failed_file", [
    "companies.json", "validation.json", "checkpoint-results.json",
])
def test_host_commit_survives_failed_local_diagnostics(
        lab, monkeypatch, failed_file):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: incremental_checkpoint_scenario(approve_count=1)
    lab.mode = "partial_timeout"

    def interrupt(tools):
        packet = tools.call("tyche_review", {})
        path = tools.research.path.with_name(failed_file)
        write = confirmed_leads.write_snapshot

        def fail_local(destination, document):
            if Path(destination) == path:
                raise OSError("fixture local disk failure after host commit")
            return write(destination, document)

        monkeypatch.setattr(confirmed_leads, "write_snapshot", fail_local)
        with pytest.raises(OSError, match="after host commit"):
            tools.call("tyche_review", {
                "review_ref": packet["review_ref"],
                "review_findings": review_findings(packet, tools),
            })
        assert len(json.loads(lab.output.read_text())["companies"]) == 2
        assert len(json.loads(tools.research.path.with_name(
            "checkpoint-results.json").read_text())["accepted"]) == 1
        lab.calls_before_recovery = len(lab.frames)

    lab.after_program = interrupt
    rows = runtime.run(ICP)
    assert len(rows) == 2
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert len(lab.frames) == lab.calls_before_recovery
    assert [len(rows) for rows in lab.checkpoints] == [1, 2]


def test_first_host_commit_survives_missing_or_corrupt_local_snapshot(
        lab, monkeypatch):
    from tyche_arena.output import checkpointed_companies

    lab.program = lambda: scenario(None)
    lab.mode = "partial_timeout"

    def interrupt(tools):
        packet = tools.call("tyche_review", {})
        snapshot = tools.research.path.with_name("checkpoint-results.json")
        write = confirmed_leads.write_snapshot

        def fail_snapshot(path, document):
            if Path(path) == snapshot:
                raise OSError("fixture snapshot unavailable")
            return write(path, document)

        monkeypatch.setattr(confirmed_leads, "write_snapshot", fail_snapshot)
        with pytest.raises(OSError, match="snapshot unavailable"):
            tools.call("tyche_review", {
                "review_ref": packet["review_ref"],
                "review_findings": review_findings(packet, tools),
            })
        assert not snapshot.exists()
        assert len(checkpointed_companies(
            tools.research.path, ICP, lab.output)) == 1
        snapshot.write_text("interrupted diagnostic data")
        lab.calls_before_recovery = len(lab.frames)

    lab.after_program = interrupt
    rows = runtime.run(ICP)
    assert len(rows) == 1
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert len(lab.frames) == lab.calls_before_recovery


@pytest.mark.parametrize("failure", ["host", "native_sync"])
def test_recovery_removes_withdrawn_lead_and_keeps_other_confirmed_lead(
        lab, monkeypatch, failure):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = incremental_checkpoint_scenario
    lab.mode = "partial_timeout"

    def withdraw(tools):
        def fail(_rows):
            raise OSError("fixture withdrawal save failed")

        if failure == "host":
            tools.write_checkpoint = fail
        else:
            write = confirmed_leads.write_snapshot

            def fail_native(path, document):
                if Path(path) == tools.research.path.with_name("leads.json"):
                    raise OSError("fixture withdrawal save failed")
                return write(path, document)

            monkeypatch.setattr(confirmed_leads, "write_snapshot", fail_native)
        with pytest.raises(OSError, match="withdrawal save failed"):
            tools.call("tyche_review", {"companies": [{
                "target": "example.com", "decision": "hold_contact",
                "reason": "New evidence invalidates this buyer; another review is needed",
            }]})
        assert len(json.loads(tools.research.path.read_text())["accepted"]) == 1
        assert len(json.loads(lab.output.read_text())["companies"]) == 2
        lab.calls_before_recovery = len(lab.frames)

    lab.after_program = withdraw
    rows = runtime.run(ICP)
    assert [row["company_name"] for row in rows] == ["Second Products"]
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert len(lab.frames) == lab.calls_before_recovery
    assert [len(rows) for rows in lab.checkpoints] == [1, 2, 1]


def test_recovery_fails_closed_when_host_cannot_remove_withdrawn_lead(
        lab, monkeypatch):
    lab.program = lambda: scenario("tyche_checkpoint")
    lab.mode = "partial_timeout"

    def withdraw(tools):
        def fail(_rows):
            raise OSError("fixture host remains unavailable")

        tools.write_checkpoint = fail
        monkeypatch.setattr(sys.modules["lab_arena_checkpoint"], "write", fail)
        with pytest.raises(OSError, match="host remains unavailable"):
            tools.call("tyche_review", {"companies": [{
                "target": "example.com", "decision": "hold_contact",
                "reason": "Buyer needs new evidence",
            }]})
        lab.calls_before_recovery = len(lab.frames)

    lab.after_program = withdraw
    with pytest.raises(OSError, match="host remains unavailable"):
        runtime.run(ICP)
    assert len(lab.frames) == lab.calls_before_recovery
    assert len(lab.checkpoints) == 1
    assert json.loads(lab.research[0].research.path.with_name(
        "leads.json").read_text())["leads"] == []


def test_recovery_does_not_return_newer_approval_that_never_reached_host(
        lab, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: incremental_checkpoint_scenario(approve_count=1)
    lab.mode = "partial_timeout"

    def fail_new_publication(tools):
        packet = tools.call("tyche_review", {})

        def fail(_rows):
            raise OSError("fixture new approval not published")

        tools.write_checkpoint = fail
        with pytest.raises(OSError, match="not published"):
            tools.call("tyche_review", {
                "review_ref": packet["review_ref"],
                "review_findings": review_findings(packet, tools),
            })
        assert json.loads(tools.research.path.with_name(
            "leads.json").read_text())["confirmed_count"] == 2
        lab.calls_before_recovery = len(lab.frames)

    lab.after_program = fail_new_publication
    rows = runtime.run(ICP)
    assert [row["company_name"] for row in rows] == ["Example Products"]
    assert json.loads(lab.output.read_text()) == {"companies": rows}
    assert len(lab.frames) == lab.calls_before_recovery
    assert len(lab.checkpoints) == 1


def test_checkpoint_recovery_rejects_wrong_icp_and_foreign_host_output(
        lab, tmp_path):
    from tyche_arena.output import checkpointed_companies

    rows = runtime.run(ICP)
    run_file = lab.research[0].research.path
    wrong_icp = {**ICP, "industry": "Financial Services"}
    with pytest.raises(ValueError, match="Lab output differs"):
        checkpointed_companies(run_file, wrong_icp, lab.output)

    foreign = tmp_path / "foreign-host.json"
    foreign.write_text(json.dumps({
        "companies": [{**rows[0], "company_name": "Foreign Run Company"}],
    }))
    with pytest.raises(ValueError, match="Lab output differs"):
        checkpointed_companies(run_file, ICP, foreign)


def test_checkpoint_recovery_rejects_cross_run_local_snapshot(lab):
    from tyche_arena.output import checkpointed_companies

    runtime.run(ICP)
    run_file = lab.research[0].research.path
    document = json.loads(run_file.read_text())
    confirmed = confirmed_leads.read(run_file, document)
    confirmed_leads.write_snapshot(confirmed_leads.output_path(run_file), {
        **confirmed, "confirmed_count": 0, "leads": [],
    })
    snapshot = run_file.with_name("checkpoint-results.json")
    foreign = json.loads(snapshot.read_text())
    foreign["run_id"] = "foreign-run"
    snapshot.write_text(json.dumps(foreign))

    with pytest.raises(ValueError, match="Lab output differs"):
        checkpointed_companies(run_file, ICP, lab.output)


@pytest.mark.parametrize("payload", [
    {}, {"companies": {}}, {"companies": ["not-an-object"]},
    {"companies": [], "extra": True},
])
def test_host_output_reader_rejects_non_companies_shapes(tmp_path, payload):
    from tyche_arena.output import read_output

    path = tmp_path / "host-output.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="companies list"):
        read_output(path)


def test_host_output_reader_rejects_more_than_512_kib(tmp_path):
    from tyche_arena.output import read_output

    path = tmp_path / "host-output.json"
    path.write_bytes(b" " * (512 * 1024 + 1))
    with pytest.raises(ValueError, match="512 KiB"):
        read_output(path)


def install_actual_checkpoint_writer(lab, monkeypatch):
    reference = Path(os.environ["LAB_ARENA_REFERENCE_SOURCE"])
    module_path = reference / "lab_arena" / "lab_arena_checkpoint.py"
    spec = importlib.util.spec_from_file_location("arena_checkpoint_reference", module_path)
    checkpoint = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(checkpoint)
    current = sys.modules["lab_arena_checkpoint"]
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=lambda rows: checkpoint.write(rows, output_path=lab.output),
        quota_usage=current.quota_usage,
        QuotaUnavailable=current.QuotaUnavailable,
    ))
    return checkpoint


def expire_native_research_clock(tools, monkeypatch):
    from datetime import datetime, timedelta
    import validate_run

    document = tools.research._document()
    started = datetime.fromisoformat(
        document["stop_check"]["started_at"].replace("Z", "+00:00"))
    finished = started + timedelta(seconds=runtime.RESEARCH_SECONDS + 1)
    real_datetime = validate_run.datetime

    class FinishedClock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return finished if tz is None else finished.astimezone(tz)

    monkeypatch.setattr(validate_run, "datetime", FinishedClock)


def test_real_ready_contact_gate_accepts_and_delivers_nonempty_checkpoint(
        lab, monkeypatch):
    install_actual_checkpoint_writer(lab, monkeypatch)
    lab.program = lambda: scenario("tyche_checkpoint")
    completed = []

    def finalize_ready_contact(tools):
        assert len(json.loads(lab.output.read_text())["companies"]) == 1
        demoted = tools.call("tyche_review", {"companies": [{
            "target": "example.com", "decision": "hold_contact",
            "reason": "Run a bounded final review of the completed buyer",
        }]})
        assert demoted["status"] == "confirmed_leads_saved"
        assert json.loads(lab.output.read_text())["companies"] == []
        tools.research.environment["TYCHE_FINALIZATION_ONLY"] = "1"
        expire_native_research_clock(tools, monkeypatch)

        blocked = tools.call("tyche_finish", {})
        assert blocked["status"] == "completion_review_required"
        assert blocked["completion_candidates"][0]["target"] == "example.com"
        accepted = tools.call("tyche_review", {"companies": [{
            "target": "example.com", "decision": "accept",
            "reason": "Saved company, buyer, and email evidence satisfy the request",
        }]})
        assert accepted["status"] == "review_required"
        checkpointed = tools.call("tyche_review", {
            "review_ref": accepted["review_ref"],
            "review_findings": review_findings(accepted, tools),
        })
        assert checkpointed["checkpoint_saved"]
        assert len(json.loads(lab.output.read_text())["companies"]) == 1
        packet = tools.call("tyche_finish", {})
        delivered = tools.call("tyche_finish", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet, tools),
        })
        assert delivered["delivery_allowed"] and delivered["checkpoint_saved"]
        completed.append(True)

    lab.after_program = finalize_ready_contact
    rows = runtime.run(ICP)
    assert len(rows) == 1 and completed == [True]
    assert len(json.loads(lab.output.read_text())["companies"]) == 1


def test_real_ready_contact_explicit_hold_allows_reviewed_empty_finish(
        lab, monkeypatch):
    install_actual_checkpoint_writer(lab, monkeypatch)
    lab.program = lambda: scenario("tyche_checkpoint")
    completed = []

    def hold_ready_contact(tools):
        tools.call("tyche_review", {"companies": [{
            "target": "example.com", "decision": "hold_contact",
            "reason": "Run a bounded final review of the completed buyer",
        }]})
        tools.research.environment["TYCHE_FINALIZATION_ONLY"] = "1"
        expire_native_research_clock(tools, monkeypatch)
        assert tools.call("tyche_finish", {})["status"] == "completion_review_required"

        held = tools.call("tyche_review", {"companies": [{
            "target": "example.com", "decision": "hold_contact",
            "reason": "Keep the buyer held because its function does not satisfy the requested role",
        }]})
        assert held["status"] == "confirmed_leads_saved"
        packet = tools.call("tyche_finish", {})
        assert packet["status"] == "review_required" and packet["companies"] == []
        delivered = tools.call("tyche_finish", {
            "review_ref": packet["review_ref"], "review_findings": [],
        })
        assert delivered["delivery_allowed"] and delivered["checkpoint_saved"]
        completed.append(True)

    lab.after_program = hold_ready_contact
    assert runtime.run(ICP) == []
    assert completed == [True]
    assert json.loads(lab.output.read_text())["companies"] == []


def test_ready_contact_change_and_restart_regate_without_losing_checkpoint(
        lab, monkeypatch):
    install_actual_checkpoint_writer(lab, monkeypatch)
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: incremental_checkpoint_scenario(approve_count=1)
    completed = []

    def mutate_and_restart(tools):
        assert len(json.loads(lab.output.read_text())["companies"]) == 1
        tools.call("tyche_review", {"companies": [{
            "target": "second.example", "decision": "hold_contact",
            "reason": "Review the second completed buyer before final delivery",
        }]})
        tools.research.environment["TYCHE_FINALIZATION_ONLY"] = "1"
        expire_native_research_clock(tools, monkeypatch)
        first = tools.call("tyche_finish", {})
        assert first["status"] == "completion_review_required"
        tools.call("tyche_review", {"companies": [{
            "target": "second.example", "decision": "hold_contact",
            "reason": "The second buyer remains legitimately held after review",
        }]})
        assert tools.call("tyche_finish", {})["status"] == "review_required"

        run_attempt.save_review(tools.research.path, {"companies": [{
            "scope": "second.example", "state": "unresolved", "stage": "contact",
            "reason_text": "Persisted candidate evidence needs a new bounded review",
        }]})
        changed = tools.call("tyche_finish", {})
        assert changed["status"] == "completion_review_required"
        assert changed["completion_candidates"][0]["assessment_ref"] != first["completion_candidates"][0]["assessment_ref"]
        assert len(json.loads(lab.output.read_text())["companies"]) == 1

        fresh = LabTools(
            tools.research.path, tools.broker.deadline, tools.broker.response_deadline)
        fresh.research.environment["TYCHE_FINALIZATION_ONLY"] = "1"
        restarted = fresh.call("tyche_finish", {})
        assert restarted["status"] == "completion_review_required"
        assert restarted["completion_candidates"][0]["assessment_ref"] == changed["completion_candidates"][0]["assessment_ref"]
        assert len(json.loads(lab.output.read_text())["companies"]) == 1
        fresh.call("tyche_review", {"companies": [{
            "target": "second.example", "decision": "hold_contact",
            "reason": "Fresh finalizer reviewed and retained the legitimate hold",
        }]})
        packet = fresh.call("tyche_finish", {})
        delivered = fresh.call("tyche_finish", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet, fresh),
        })
        assert delivered["delivery_allowed"] and delivered["checkpoint_saved"]
        completed.append(True)

    lab.after_program = mutate_and_restart
    rows = runtime.run(ICP)
    assert len(rows) == 1 and rows[0]["company_name"] == "Example Products"
    assert completed == [True]
    assert len(json.loads(lab.output.read_text())["companies"]) == 1


def test_reviewed_checkpoint_uses_real_arena_atomic_writer_and_v5_validation(
        lab, monkeypatch):
    reference = Path(os.environ["LAB_ARENA_REFERENCE_SOURCE"])
    checkpoint = install_actual_checkpoint_writer(lab, monkeypatch)
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    captured = []
    lab.program = lambda: capture_accepted_review(captured)
    lab.mode = "partial_timeout"

    def approve_then_fail_replacement(tools):
        saved = tools.call("tyche_review", {
            "review_ref": captured[0]["review_ref"],
            "review_findings": review_findings(captured[0]),
        })
        assert saved["checkpoint_saved"] and not saved["delivery_allowed"]
        prior_output = lab.output.read_bytes()
        prior_local = tools.research.path.with_name("companies.json").read_bytes()
        prior_snapshot = tools.research.path.with_name("checkpoint-results.json").read_bytes()

        validator = (
            "import json,sys; from pathlib import Path; "
            "from lab_arena.output import output_document_from_bytes; "
            "doc=output_document_from_bytes(Path(sys.argv[1]).read_bytes(), "
            "expected_schema_version='leadpoet.lab_arena.output.v5'); "
            "print(json.dumps({'count':len(doc['companies']),'schema':doc['schema_version']}))"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(reference) + os.pathsep + environment.get("PYTHONPATH", "")
        validated = subprocess.run(
            [sys.executable, "-c", validator, str(lab.output)],
            capture_output=True, text=True, env=environment, timeout=20,
        )
        assert validated.returncode == 0, validated.stderr
        assert json.loads(validated.stdout) == {
            "count": 1, "schema": "leadpoet.lab_arena.output.v5"}

        fresh = tools.call("tyche_review", {"companies": [{
            "target": "example.com", "decision": "accept",
            "reason": "Revise the reviewed relevance",
            "intent_details": PARAGRAPH + " This revision remains evidence-bound.",
        }]})
        assert fresh["status"] == "review_required"
        real_os = checkpoint.os
        def interrupted_replace(_source, _target):
            raise OSError("fixture interrupted at atomic replace")
        checkpoint.os = SimpleNamespace(
            fdopen=real_os.fdopen, replace=interrupted_replace, unlink=real_os.unlink)
        tools.write_checkpoint = lambda rows: checkpoint.write(rows, output_path=lab.output)
        try:
            with pytest.raises(OSError, match="atomic replace"):
                tools.call("tyche_review", {
                    "review_ref": fresh["review_ref"],
                    "review_findings": review_findings(fresh),
                })
        finally:
            checkpoint.os = real_os
        assert lab.output.read_bytes() == prior_output
        assert tools.research.path.with_name("companies.json").read_bytes() == prior_local
        assert tools.research.path.with_name("checkpoint-results.json").read_bytes() == prior_snapshot
        assert list(lab.output.parent.glob(".arena-checkpoint-*")) == []

    lab.after_program = approve_then_fail_replacement
    rows = runtime.run(ICP)
    assert len(rows) == 1
    assert json.loads(lab.output.read_text()) == {"companies": rows}


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
        assert packet["unchanged"] and packet["review_scope"] == "confirmed_leads"
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
        fresh = tools.call("tyche_checkpoint", {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet, tools),
        })
        assert fresh["status"] == "review_required" and fresh["review_ref"] != packet["review_ref"]
        assert not lab.output.exists()
        saved = tools.call("tyche_checkpoint", {
            "review_ref": fresh["review_ref"],
            "review_findings": review_findings(fresh, tools),
        })
        assert saved["checkpoint_saved"] and not saved["delivery_allowed"]
        assert json.loads(lab.output.read_text())["companies"][0]["intent_details"] == changed
        tools.call("tyche_review", {"companies": [{"target": "example.com", "decision": "accept",
            "reason": "Use the concise reviewed description", "intent_details": PARAGRAPH}]})
        newer = tools.call("tyche_checkpoint", {})
        assert newer["status"] == "review_required"
        # A changed confirmed lead is withdrawn until its revision is reviewed.
        assert json.loads(lab.output.read_text())["companies"] == []
        final_checkpoint = tools.call("tyche_checkpoint", {
            "review_ref": newer["review_ref"],
            "review_findings": review_findings(newer, tools),
        })
        assert final_checkpoint["checkpoint_saved"]
        assert len(json.loads(lab.output.read_text())["companies"]) == 1
        assert json.loads(tools.research.path.with_name(
            "leads.json").read_text())["confirmed_count"] == 1
        assert len(json.loads(tools.research.path.with_name(
            "checkpoint-results.json").read_text())["accepted"]) == 1

    lab.after_program = approve_incrementally
    rows = runtime.run(ICP)
    assert len(rows) == 1, (rows, lab.checkpoints, json.loads(lab.output.read_text()))
    assert rows[0]["intent_details"] == PARAGRAPH


@pytest.mark.parametrize("finish_tool", ["tyche_checkpoint", "tyche_finish"])
def test_finalization_projects_before_review_then_accepts_provider_backed_repair(lab, monkeypatch, finish_tool):
    lab.program = lambda: scenario(None)

    def repair_missing_hq(tools):
        document = json.loads(tools.research.path.read_text())
        document["accepted"][0]["company"].pop("hq_country")
        tools.research.path.write_text(json.dumps(document))

        # A row that cannot satisfy the Arena projection remains repairable,
        # but native confirmation blocks more paid research until it is fixed.
        calls = len(lab.frames)
        lookup_result = tools.call("tyche_lookup", lookup(
            "harvestapi_get_company", {"url": COMPANY_URL}))
        assert lookup_result["status"] == "needs_repair"
        assert len(lab.frames) == calls

        blocked = tools.call(finish_tool, {})
        assert blocked["status"] == "needs_repair"
        assert any("company country must be nonempty text" in error for error in blocked["errors"])
        assert "final_review" not in json.loads(tools.research.path.read_text())
        assert not lab.output.exists()

        source = document["accepted"][0]["company"]["employee_range_evidence"]["source"]
        company_ref = source["route_id"] + ":0"
        repaired = tools.call("tyche_review", {"companies": [{"target": "example.com", "decision": "accept",
            "reason": "Restore the provider-backed headquarters field", "company": {"ref": company_ref}}]})
        packet = repaired if finish_tool == "tyche_checkpoint" else tools.call(finish_tool, {})
        assert packet["status"] == "review_required" and not lab.output.exists()
        if finish_tool == "tyche_finish":
            assert "agent_recorded_web are discovery notes; required web facts need captured source bodies" in packet["instructions"]
            assert "reopen the exact saved source URL once" in packet["instructions"]
            assert "preserve the captured qualification ref" in packet["instructions"]
            assert "No new searches, new source URLs or provider lookups" in packet["instructions"]
            assert "tyche_review (operation=open" not in packet["instructions"]
        saved = tools.call(finish_tool, {
            "review_ref": packet["review_ref"],
            "review_findings": review_findings(packet, tools),
        })
        assert saved["checkpoint_saved"]
        assert saved["delivery_allowed"] == (finish_tool == "tyche_finish")
        assert json.loads(lab.output.read_text())["companies"][0]["country"] == "United States"

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
    with pytest.raises(RuntimeError, match="repeated_worker_failure"):
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
    with pytest.raises(RuntimeError, match="repeated_worker_failure"):
        runtime.run(ICP)


def test_failed_changed_checkpoint_is_revoked_during_host_recovery(lab, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario("tyche_checkpoint")

    def failed_update(tools):
        previous = lab.output.read_bytes()
        local_previous = tools.research.path.with_name("companies.json").read_bytes()
        snapshot = tools.research.path.with_name("checkpoint-results.json").read_bytes()
        def fail(rows):
            raise OSError("fixture output mount is unavailable")
        tools.write_checkpoint = fail
        with pytest.raises(OSError, match="output mount"):
            tools.call("tyche_review", {"companies": [{"target": "example.com", "decision": "accept",
                "reason": "Clarified the relevance", "intent_details": PARAGRAPH + " Better coordination may help."}]})
        packet = tools.call("tyche_checkpoint", {})
        with pytest.raises(OSError, match="output mount"):
            tools.call("tyche_checkpoint", {
                "review_ref": packet["review_ref"],
                "review_findings": review_findings(packet, tools),
            })
        assert lab.output.read_bytes() == previous
        assert tools.research.path.with_name("companies.json").read_bytes() == local_previous
        assert tools.research.path.with_name("checkpoint-results.json").read_bytes() == snapshot
        assert not tools.delivered
        lab.calls_before_recovery = len(lab.frames)

    lab.after_program = failed_update
    assert runtime.run(ICP) == []
    assert json.loads(lab.output.read_text()) == {"companies": []}
    assert len(lab.frames) == lab.calls_before_recovery


def test_confirmed_publication_retries_after_restart_without_provider_dispatch(lab, monkeypatch):
    monkeypatch.setenv("LAB_ARENA_COMPANY_LIMIT", "5")
    lab.program = lambda: scenario("tyche_checkpoint")

    def lose_host_acknowledgement(tools):
        packet = tools.call("tyche_review", {"companies": [{
            "target": "example.com", "decision": "accept",
            "reason": "Clarify the reviewed relevance",
            "intent_details": PARAGRAPH + " The revised relevance remains conditional.",
        }]})
        assert packet["status"] == "review_required"
        assert json.loads(lab.output.read_text())["companies"] == []
        provider_calls = len(lab.frames)

        def fail(_rows):
            raise OSError("fixture host write failed")

        tools.write_checkpoint = fail
        with pytest.raises(OSError, match="host write failed"):
            tools.call("tyche_review", {
                "review_ref": packet["review_ref"],
                "review_findings": review_findings(packet),
            })
        assert json.loads(tools.research.path.with_name("leads.json").read_text())["confirmed_count"] == 1
        assert json.loads(tools.research.path.with_name("leads.json").read_text())["review_findings"] == review_findings(packet)
        assert json.loads(lab.output.read_text())["companies"] == []

        resumed = LabTools(tools.research.path, tools.broker.deadline, tools.broker.response_deadline)
        saved = resumed.call("tyche_review", {"review_ref": packet["review_ref"]})
        assert saved["arena_checkpoint"]["confirmed_count"] == 1
        assert len(lab.frames) == provider_calls
        assert "revised relevance" in json.loads(lab.output.read_text())["companies"][0]["intent_details"]

    lab.after_program = lose_host_acknowledgement
    assert len(runtime.run(ICP)) == 1


def test_employee_range_stays_prose_to_preserve_observed_legacy_aliases():
    # Arena treats 50-200 as 51-200; native numeric bounds compare literally.
    # Do not add a stricter numeric gate while these evidence semantics differ.
    request = request_for({**ICP, "employee_count": ["51-200"]}, 1, 30)
    assert "company_size" not in request["icp"]
    assert 'Employee range is one of: ["51-200"]' in request["icp"]["required_attributes"]


@pytest.mark.parametrize("free_pages", [False, True])
def test_paid_and_free_captured_pages_keep_native_approval_atomic_checkpoint_and_recovery(
        lab, monkeypatch, free_pages, arena_operations):
    """Hermetic decisions, real captured HTTP pages, host writer and frozen V5 parser."""
    from lab_arena.output import output_document_from_bytes
    from test_arena_public_web import proxy
    from tyche_arena import public_web
    from source_receipts import content_kind, read_receipt

    reference = Path(os.environ["LAB_ARENA_REFERENCE_SOURCE"])
    spec = importlib.util.spec_from_file_location(
        "free_page_checkpoint_reference", reference / "lab_arena" / "lab_arena_checkpoint.py")
    checkpoint = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(checkpoint)
    current = sys.modules["lab_arena_checkpoint"]
    def real_checkpoint(rows):
        checkpoint.write(rows, output_path=lab.output)
        lab.checkpoints.append(copy.deepcopy(rows))
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", SimpleNamespace(
        write=real_checkpoint, quota_usage=current.quota_usage,
        QuotaUnavailable=current.QuotaUnavailable,
    ))

    def program():
        native = scenario("tyche_checkpoint")
        command = next(native)
        free_routes = []
        while True:
            if (free_pages and command[0] == "tyche_lookup"
                    and command[1]["checks"][0]["tool"] == "generic_http_request"):
                refs = []
                for path in ("fit", "signal"):
                    page = yield "tyche_open", {
                        "target": "example.com", "purpose": "Read captured fixture evidence",
                        "url": "http://public.example/" + path,
                    }
                    refs.append({"ref": page["ref"]})
                    free_routes.append(page["ref"].split(":")[0])
                result = {"lookups": [{"results": refs, "route": free_routes[0]}]}
            else:
                if free_pages and command[0] == "tyche_review" and command[1].get("sources"):
                    command[1]["sources"].append({"ref": free_routes[1], "state": "exhausted",
                                                  "reason": "Reviewed captured signal fixture"})
                result = yield command
            try:
                command = native.send(result)
            except StopIteration:
                return

    def response(path, count):
        text = ("Example Products manufactures products for retailers." if path.endswith("/fit")
                else "Example Products connected its acquired warehouse to a shared WMS on August 12, 2026. "
                     "The project covers inventory visibility and fulfillment across the combined operation.")
        return 0, 200, {"Content-Type": "text/plain"}, text.encode()

    lab.program = program
    lab.mode = "partial_timeout"
    def remember_calls(tools):
        lab.calls_before_recovery = copy.deepcopy(lab.frames)
        assert len(lab.checkpoints) == 1
        assert len(json.loads(lab.output.read_text())["companies"]) == 1
    lab.after_program = remember_calls
    with proxy(response) as (proxy_url, calls):
        monkeypatch.setenv(public_web.PROXY_ENV, proxy_url)
        # The runtime fixture replaces Popen. Keep real HTTP capture here;
        # the separate public-web suite exercises the isolated child boundary.
        monkeypatch.setattr(public_web, "_fetch_isolated", lambda url, proxy, deadline, **kwargs:
                            public_web._fetch(url, proxy, deadline, **kwargs))
        rows = runtime.run(ICP)
    assert len(rows) == 1
    for payload in (lab.output.read_bytes(), json.dumps({"companies": rows}).encode()):
        parsed = output_document_from_bytes(
            payload, expected_schema_version="leadpoet.lab_arena.output.v5")
        assert parsed["schema_version"] == "leadpoet.lab_arena.output.v5"
        assert len(parsed["companies"]) == 1
    assert list(lab.output.parent.glob(".arena-checkpoint-*")) == []
    assert lab.frames == lab.calls_before_recovery
    assert len(lab.checkpoints) == 1
    assert rows[0]["intent_signals"][0]["date"] == "2026-08-12"
    assert len(calls) == (2 if free_pages else 0)
    research = lab.research[0].research
    pages = [route for route in research._document()["routes"]
             if route.get("provider") == "public_web"]
    assert len(pages) == (2 if free_pages else 0)
    for route in pages:
        saved = read_receipt(research.path, route["route_id"])["result"]
        assert saved["attempt"]["action"]["paid_calls"] == 0
        assert content_kind(saved["results"][0], saved) == "captured_page"
    assert any(frame["tool"] == "generic_http_request" for frame in lab.frames) is (not free_pages)


@pytest.mark.parametrize("name,payload,provider_data", [
    ("contextdev_post_web_search",
     {"query": "site:example.com product announcement", "numResults": 10},
     {"query": "site:example.com product announcement", "results": [
         {"title": "Product announcement", "url": "https://example.com/news",
          "description": "Example announced a product."}]}),
    ("contextdev_post_news_search",
     {"searchBy": {"type": "entity", "entity": {"type": "domain", "domain": "example.com"}}, "limit": 3},
     [{"title": "Product announcement", "url": "https://example.com/news",
       "description": "Example announced a product.", "published_at": "2026-09-18T12:00:00.000Z"}]),
])
def test_free_contextdev_search_uses_existing_native_broker(
        name, payload, provider_data, tmp_path, monkeypatch, arena_operations):
    """The restored native research tools need no alternate transport or parser."""
    import research_input

    contract = json.loads((ROOT / "tyche_arena/catalog.json").read_text())["tools"][name]
    request = {"operation": "execute", "tool": name, "payload": payload,
               "limit": 10, "timeout_seconds": 30, "spend": {"max_cost_credits": 0}}
    research_input.check_tool_contract({"results": [contract]}, request)
    frame = {"tool": name, "payload": payload}
    assert arena_operations.validate_operation_request("deepline.execute", frame) == frame
    assert contract["provider"] == "contextdev" and contract["callable"] is True
    assert contract["pricing"]["creditsPerUnit"] == contract["pricing"]["usdPerUnit"] == 0
    broker = Broker(tmp_path / "unused.sock", time.monotonic() + 30, catalog={name: contract})
    dispatched = []
    raw = {"status": "completed", "job_id": "public-search-test",
           "result": {"data": provider_data}}

    def send(operation, parameters, *, admitted=False, timeout_seconds=None):
        dispatched.append((operation, copy.deepcopy(parameters), admitted))
        return 200, {}, copy.deepcopy(raw)

    monkeypatch.setattr(broker, "request", send)
    monkeypatch.setattr(budget_guard, "guarded_call", lambda _request, _provider, dispatch, *, tariff=None: dispatch())
    captured = []
    assert broker._requires_paid_dispatch(request, "deepline") is False
    result, code = broker.execute(request, captured.append)
    assert code == 0 and result["status"] == "ok"
    assert dispatched == [("deepline.execute", frame, True)]
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert row["evidence_url"] == "https://example.com/news"
    assert row["evidence_text"] == "Example announced a product."
    if name == "contextdev_post_news_search":
        assert row["evidence_date"] == "2026-09-18T12:00:00.000Z"
    assert captured[0]["body"] == raw
    assert broker.provider_calls("deepline") == 1
    invalid = copy.deepcopy(request)
    invalid["payload"]["unknown_option"] = True
    with pytest.raises(ValueError, match="Additional properties are not allowed.*unknown_option"):
        research_input.check_tool_contract({"results": [contract]}, invalid)
    assert len(dispatched) == 1


def test_bundle_contains_the_main_runner_and_no_duplicate(tmp_path):
    from scripts.build_arena_bundle import build
    destination = build(tmp_path / 'bundle')
    assert (destination / 'scripts/codex_tyche.py').read_bytes() == (ROOT / 'scripts/codex_tyche.py').read_bytes()
    assert (destination / 'scripts/run_costs.py').is_file()
    assert (destination / 'tyche_arena/host.py').is_file()
    assert not (destination / 'tyche_arena/runtime.py').exists()
    assert not (destination / '.runtime').exists()
    assert not (destination / 'reports').exists()

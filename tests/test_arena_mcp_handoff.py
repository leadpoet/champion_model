"""Offline checks of the actual Arena MCP contract and saved lookup handoff."""

import copy
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".agents/skills/lead-sourcing/tests"))

from tyche_arena.mcp import LAB_TOOLS, MODEL_RESULT_MAX_CHARACTERS, LabTools, lookup_model_result
from research_tools import ResearchTools, TOOLS, validate
import test_research_tools as native_tests


def session(research):
    tools = LabTools.__new__(LabTools)
    tools.lock = threading.Lock()
    tools.delivered = False
    tools.research = research
    tools._publish_confirmed = lambda: None
    tools.broker = SimpleNamespace(local_dispatch_budget=lambda: {
        "scope": "local_adapter_dispatch_count", "providers": {}})
    return tools


@pytest.mark.parametrize("name", list(LAB_TOOLS))
def test_inspect_returns_exact_served_schema_without_catalog_dispatch(name):
    research = ResearchTools(Path("/nonexistent/arena-schema-test/results.json"),
        execute=lambda *_args: pytest.fail("Own-tool inspection dispatched a provider request"))
    result = session(research).call("tyche_inspect", {"tool": name, "field": "inputSchema"})
    assert result["tool"] == LAB_TOOLS[name][1]
    result["tool"]["unexpected_mutation"] = True
    assert "unexpected_mutation" not in LAB_TOOLS[name][1]


def test_served_review_contract_and_native_inspect_field_semantics():
    tools = session(ResearchTools(Path("/nonexistent/arena-schema-test/results.json")))
    schema = tools.call("tyche_inspect", {"tool": "tyche_review", "field": "inputSchema"})["tool"]
    assert "web" not in schema["properties"]
    assert "company_stage" in json.dumps(schema)
    for arguments in ({"web": []}, {"invented_field": True}):
        with pytest.raises(ValueError):
            validate(arguments, schema)
        with pytest.raises(ValueError):
            tools.call("tyche_review", arguments)
    assert tools.call("tyche_inspect", {"tool": "tyche_open"})["tool"]["toolId"] == "tyche_open"
    required = tools.call("tyche_inspect", {
        "tool": "tyche_open", "field": "inputSchema.required", "offset": 1, "limit": 1})
    assert required["tool"] == ["purpose"] and required["total"] == 3 and required["next_offset"] == 2
    description = tools.call("tyche_inspect", {"tool": "tyche_open", "field": "description", "offset": 5})
    assert description["tool"] == LAB_TOOLS["tyche_open"][0][5:1805]
    for extra in ({"target": "example.test"}, {"unexpected": True}, {"offset": -1}):
        with pytest.raises(ValueError):
            tools.call("tyche_inspect", {"tool": "tyche_open", **extra})


def test_email_schema_clarifies_selection_without_changing_native_draft_types():
    schema = session(ResearchTools(Path("/nonexistent/schema/results.json"))).call(
        "tyche_inspect", {"tool": "tyche_review", "field": "inputSchema"})["tool"]
    def shape(value):
        return schema["$defs"][value["$ref"].split("/")[-1]] if "$ref" in value else value
    company = shape(shape(schema["properties"]["companies"])["items"])["properties"]
    for contact in (shape(company["primary_contact"]), shape(shape(company["backup_contacts"])["items"])):
        assert set(contact["properties"]["email"]) == {"description"}
        assert "same-address" in contact["properties"]["email_ref"]["description"]
        assert "finder" in contact["properties"]["email_ref"]["description"]
        for email in (None, "", "chosen@example.test"):
            validate({"email": email}, contact)
    native_contact = TOOLS["tyche_review"][1]["properties"]["companies"]["items"]["properties"]["primary_contact"]
    assert "email" not in native_contact["properties"]


def test_arena_email_selection_still_requires_matching_validation_and_provenance():
    case = native_tests.ResearchToolTests("runTest")
    case.setUp()
    try:
        case.start()
        profile = case.selected_contact()
        tools = session(case.tools)
        case.provider.raw = {"status": "completed", "toolResponse": {"rawV2": {"email": "ada@example.test"}}}
        finder = tools.call("tyche_lookup", {"checks": [native_tests.check(
            tool="fixture_email_finder", phase="contact_discovery", contact_ref=profile, inputs={})]
        })["lookups"][0]["results"][0]["ref"]
        def review(contact):
            return tools.call("tyche_review", {"companies": [{"target": "example.test",
                "decision": "hold_contact", "reason": "Select saved exact address", "primary_contact": contact}]})
        for contact in ({"email_source": {"ref": finder}}, {"email_ref": finder}):
            before = case.path.read_bytes(), len(case.provider.requests)
            with pytest.raises(ValueError):
                review(contact)
            assert (case.path.read_bytes(), len(case.provider.requests)) == before
        review({"email": "ada@example.test", "email_source": {"ref": finder}})
        selected = json.loads(case.path.read_text())["unresolved"][0]["primary_contact"]
        assert selected["email"] == "ada@example.test" and not selected.get("email_validation")
        case.provider.raw = {"status": "ok", "data": {"address": "ada@example.test", "status": "valid"}}
        verdict = tools.call("tyche_lookup", {"checks": [native_tests.check(
            tool="zerobounce_validate", phase="email_validation", contact_ref=profile,
            inputs={"email": "ada@example.test"})]})["lookups"][0]["results"][0]["ref"]
        with pytest.raises(ValueError, match="conflicts"):
            review({"email": "other@example.test", "email_ref": verdict})
        review({"email": "ada@example.test", "email_ref": verdict, "email_source": {"ref": finder}})
        selected = json.loads(case.path.read_text())["unresolved"][0]["primary_contact"]
        assert selected["email_validation"]["status"] == "valid"
    finally:
        case.doCleanups()


def test_large_route_field_catalog_and_provider_failure_keep_bounded_handoff(tmp_path):
    native = ResearchTools.__new__(ResearchTools)
    native._document = lambda: {"routes": [{"route_id": "failed-route"}]}
    wide = native._lookup_view({"route_id": "wide-route", "result": {
        "status": "ok", "results": [{f"field{index}": "v" for index in range(5000)}]}})
    failed = native._lookup_view({"route_id": "failed-route", "result": {
        "status": "provider_error", "error": {"message": "x" * 3000, "code": "quota_exceeded"}, "results": []}})
    document = {"request": {"target_count": 1}, "accepted": [], "unresolved": []}
    run_file = tmp_path / "results.json"
    run_file.write_text(json.dumps(document))
    tools = session(SimpleNamespace(path=run_file,
        _document=lambda: document,
        call=lambda name, arguments: wide if name == "tyche_inspect" else {"lookups": [wide, failed]}))
    route = tools.call("tyche_inspect", {"ref": "wide-route", "limit": 1})
    assert route["route"] == "wide-route" and route["result_count"] == 1 and route["next_offset"] is None
    assert route["results"][0]["ref"] == "wide-route:0"
    assert route["results"][0]["omitted_field_count"] > 0
    assert len(json.dumps(route, ensure_ascii=True)) <= MODEL_RESULT_MAX_CHARACTERS
    packet = tools.call("tyche_lookup", {"checks": []})
    failure = packet["lookups"][1]
    assert failure["status"] == "provider_error" and failure["recorded"] is True
    assert failure["error"]["code"] == "quota_exceeded"
    assert failure["recovery_note"] == failed["recovery_note"]
    assert failure["result_count"] == 0 and failure["next_offset"] is None
    assert len(json.dumps(packet, ensure_ascii=True)) <= MODEL_RESULT_MAX_CHARACTERS


def test_native_prepare_refusal_and_saved_success_keep_stop_errors_and_safe_next_steps():
    case = native_tests.ResearchToolTests("runTest")
    case.setUp()
    try:
        case.start(max_usd=2)
        refused = case.tools._lookup_view({
            "route_id": "offline-prepare-refused",
            "error": "OFFLINE_PREPARE_FAILURE " + ("x" * 1600),
        })
        success = {
            "route": "offline-success", "status": "ok", "recorded": True,
            "results": [{"ref": f"offline-success:{index}", "facts": {"text": "界" * 1800}}
                        for index in range(10)],
            "result_count": 10, "next_offset": None, "pending_verification": None,
        }
        original = {
            "lookups": [refused, success],
            "progress": {
                "summary": {}, "budget": {"cap_usd": 2, "costs": {"status": "exact"}},
                "errors": ["OFFLINE_STOP_ERROR", "界" * 10000] + ["x" * 2000] * 40,
                "stop": "continue",
            },
        }
        wrapped = lookup_model_result(original, {})
        assert wrapped.get("truncated") is not True
        assert len(json.dumps(wrapped, ensure_ascii=True)) <= MODEL_RESULT_MAX_CHARACTERS
        assert wrapped["progress"]["errors"][0] == "OFFLINE_STOP_ERROR"
        assert wrapped["progress"]["errors"][1].startswith("界")
        assert wrapped["progress"]["stop"] == "continue"
        refused_view = wrapped["lookups"][0]
        assert refused_view["recorded"] is False
        assert isinstance(refused_view["next"], str)
        assert "tyche_inspect(ref=" not in refused_view["next"]
        assert "recorded=false does not prove request_sent=false" in wrapped["next"]
        assert wrapped["lookups"][1]["results"] == [
            {"ref": f"offline-success:{index}"} for index in range(10)]
    finally:
        case.doCleanups()


def test_unrecorded_dispatched_failure_preserves_uncertainty_and_bounds_long_error():
    case = native_tests.ResearchToolTests("runTest")
    case.setUp()
    try:
        case.start(max_usd=2)
        uncertain = case.tools._lookup_view({
            "route_id": "uncertain-dispatch",
            "error": {
                "code": "transport_unknown", "request_sent": True,
                "message": "dispatch outcome is unknown " + ("x" * 2000),
            },
        })
        original = {
            "lookups": [uncertain],
            "progress": {"budget": {"cap_usd": 2, "costs": {}}, "stop": "provider_stop",
                          "errors": ["native stop error", "x" * 30000]},
        }
        wrapped = lookup_model_result(original, {})
        assert wrapped.get("truncated") is not True
        assert len(json.dumps(wrapped, ensure_ascii=True)) <= MODEL_RESULT_MAX_CHARACTERS
        view = wrapped["lookups"][0]
        assert view["recorded"] is False
        assert view["error"]["request_sent"] is True
        assert isinstance(wrapped["next"], str)
        assert "tyche_inspect(ref=" not in wrapped["next"]
        assert "never retry an uncertain paid call" in wrapped["next"]
    finally:
        case.doCleanups()


@pytest.mark.parametrize("text,checks,fields", [("界" * 4000, 1, 1), ("x" * 4000, 3, 1),
    ("界" * 4000, 1, 4)], ids=["unicode", "three-ascii", "unicode-wide-row"])
def test_oversized_lookup_keeps_saved_handoff_and_inspect_recovers_full_text(text, checks, fields):
    case = native_tests.ResearchToolTests("runTest")
    case.setUp()
    try:
        case.start(max_usd=2)
        row_fields = {"text" if index == 0 else f"passage{index}": text for index in range(fields)}
        case.provider.raw = {"status": "ok", "results": [
            {**row_fields, "index": index} for index in range(10)]}
        tools = session(case.tools)
        result = tools.call("tyche_lookup", {"checks": [
            native_tests.check(target=f"example{index}.test", tool="fixture-search", inputs={"query": str(index)})
            for index in range(checks)]})
        assert result["preview_omitted"] is True
        assert result.get("truncated") is not True
        assert len(json.dumps(result, ensure_ascii=True)) <= MODEL_RESULT_MAX_CHARACTERS
        assert len(result["lookups"]) == checks
        assert "budget" in result["progress"]
        provider_requests = copy.deepcopy(case.provider.requests)
        saved_bytes = case.path.read_bytes()
        receipts = {path: path.read_bytes() for path in (case.path.parent / "receipts").glob("*.json")}
        for lookup in result["lookups"]:
            route = lookup["route"]
            assert lookup["recorded"] is True and lookup["status"] == "ok"
            assert lookup["result_count"] == 10 and lookup["next_offset"] is None
            assert lookup["results"] == [{"ref": f"{route}:{index}"} for index in range(10)]
            assert lookup["next"] == {"tool": "tyche_inspect", "arguments": {
                "ref": route, "offset": 0, "limit": 1}}
            page = tools.call("tyche_inspect", {"ref": route, "offset": 5, "limit": 1})
            assert page["result_count"] == 10 and page["next_offset"] == 6
            assert page["results"][0]["ref"] == f"{route}:5"
            assert len(json.dumps(page, ensure_ascii=True)) <= MODEL_RESULT_MAX_CHARACTERS
            if fields == 4:
                assert page["preview_omitted"] is True and page.get("truncated") is not True
                assert set(row_fields) <= set(page["results"][0]["available_fields"])
            for field in row_fields:
                recovered_text, offset = "", 0
                while True:
                    recovered = tools.call("tyche_inspect", {
                        "ref": f"{route}:0", "field": field, "offset": offset})
                    assert recovered["total_characters"] == len(text)
                    assert len(json.dumps(recovered, ensure_ascii=True)) <= MODEL_RESULT_MAX_CHARACTERS
                    recovered_text += recovered["text"]
                    if recovered["next_offset"] is None:
                        break
                    offset = recovered["next_offset"]
                assert recovered_text == text
        assert case.provider.requests == provider_requests
        assert case.path.read_bytes() == saved_bytes
        assert {path: path.read_bytes() for path in receipts} == receipts
    finally:
        case.doCleanups()

"""Offline integration checks for the native public-page bridge."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tyche_arena.input import request_for
from tyche_arena.mcp import LAB_TOOLS, LabTools
from tyche_arena.output import public_url
from tyche_arena.public_web import (MAX_RAW_BYTES, MAX_TEXT_CHARACTERS, PROXY_ENV,
                                    PublicWeb)
from tyche_arena import public_web, runtime
from research_tools import ResearchTools, validate
from source_receipts import read_receipt


ICP = {
    "intent_details_policy": "intent_details_v1", "contact_policy": "contacts_v1",
    "industry": "Manufacturing", "required_attribute": "Manufactures products",
    "intent_signals": ["Recently expanded a warehouse"], "intent_max_age_days": 365,
    "target_roles": ["Director of Supply Chain"], "target_seniority": "Director+",
    "contact_geography": {"countries": ["US"]}, "excluded_companies": [],
}
URL = "http://public.example/page"


def catalog_provider(request, _capture):
    tool = request.get("tool")
    key = "email" if tool == "zerobounce_validate" else "url"
    properties = {key: {"type": "string"}}
    if tool == "harvestapi_get_profile":
        properties["findEmail"] = {"type": "string"}
    return {"provider": "deepline", "operation": "describe", "status": "ok", "results": [{
        "toolId": tool, "callable": True, "connected": True,
        "inputSchema": {
            "fields": [{"name": key, "required": True, "type": "string"}],
            "jsonSchema": {"properties": properties, "additionalProperties": False},
        },
        "pricing": {"creditsPerUnit": .2, "unit": "call"},
    }]}, 0


def native_run(tmp_path, monkeypatch, *, target_count=1, duration=300):
    monkeypatch.setenv("TYCHE_FINALIZATION_ONLY", "0")
    monkeypatch.setenv("LAB_ARENA_EVALUATION_DATE", "2026-09-17")
    path = tmp_path / ("run-" + str(len(list(tmp_path.glob("run-*"))))) / "results.json"
    tools = ResearchTools(path, execute=catalog_provider)
    tools.start(request=request_for(ICP, target_count, duration), max_usd=1)
    return tools


def accept(tools, url=URL, target="example.com"):
    document = json.loads(tools.path.read_text())
    document["accepted"].append({
        "company": {"domain": target},
        "account_fit": {"evidence_url": url},
    })
    tools.path.write_text(json.dumps(document))


@contextmanager
def proxy(response):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            delay, status, headers, body = response
            if delay:
                time.sleep(delay)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def public_routes(tools):
    document = json.loads(tools.path.read_text())
    return [route for route in document["routes"] if route.get("provider") == "public_web"]


def test_proxy_observation_cannot_replace_captured_qualification(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)
    with proxy((0, 200, {"Content-Type": "text/html; charset=utf-8"},
                b"<html><body>Example manufactures products.</body></html>")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        page = PublicWeb(tools, time.monotonic() + 10).open(
            "example.com", "Discover manufacturing evidence", URL,
        )
        assert page["status"] == "ok" and len(calls) == 1
    before = tools.path.read_bytes()
    with pytest.raises(ValueError, match="tool-captured page, not an agent-recorded passage"):
        tools.review(companies=[{
            "target": "example.com", "decision": "qualify_account", "reason": "Check industry",
            "qualification_checks": [{
                "requirement_ref": "icp:industries", "status": "pass",
                "claim": "Manufactures products", "evidence": [{"ref": page["ref"]}],
            }, {
                "requirement_ref": "attribute:0", "status": "pass",
                "claim": "Manufactures products", "evidence": [{"ref": page["ref"]}],
            }, {
                "requirement_ref": "signal:0", "status": "pass",
                "claim": "Recently expanded a warehouse",
                "evidence": [{"ref": page["ref"], "event_date": "2026-09-17"}],
            }],
        }])
    assert tools.path.read_bytes() == before


def test_real_native_research_read_final_reread_and_phase_cache(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch, duration=1)
    with proxy((0, 200, {"Content-Type": "text/html; charset=utf-8"},
                b"<html><style>hidden</style><body>Observed public page</body></html>")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        bridge = PublicWeb(tools, time.monotonic() + 10)
        research = bridge.open("example.com", "Read the source", URL)
        cached = bridge.open("example.com", "Read the source", URL)
        assert research["status"] == "ok" and research["text"] == "Observed public page"
        assert cached["cached"] is True and cached["ref"] == research["ref"]
        assert len(calls) == 1

        accept(tools)
        document = json.loads(tools.path.read_text())
        document["stop_check"]["started_at"] = "2026-09-17T00:00:00+00:00"
        tools.path.write_text(json.dumps(document))
        monkeypatch.setenv("TYCHE_FINALIZATION_ONLY", "1")
        final = bridge.open("example.com", "Reread the accepted source", URL)
        assert final["status"] == "ok" and final["ref"] != research["ref"]
        assert len(calls) == 2

    routes = public_routes(tools)
    assert len(routes) == 2
    assert routes[0]["request_fingerprint"] != routes[1]["request_fingerprint"]
    first = read_receipt(tools.path, routes[0]["route_id"])["result"]
    second = read_receipt(tools.path, routes[1]["route_id"])["result"]
    assert first["attempt"]["request"]["arena_review_phase"] == "research"
    assert second["attempt"]["request"]["arena_review_phase"] == "finalization"
    assert first["attempt"]["request"]["arena_target_scope"] == "example.com"
    page = tools.inspect(ref=final["ref"], field="text", offset=0)
    assert page["text"] == "Observed public page" and page["next_offset"] is None


def test_invalid_capability_and_expired_response_deadline_do_not_plan(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)
    before = tools.path.read_bytes()
    bridge = PublicWeb(tools, time.monotonic() - 1)
    with pytest.raises(ValueError, match="proxy is unavailable"):
        bridge.open("example.com", "Read", URL)
    assert tools.path.read_bytes() == before

    monkeypatch.setenv(PROXY_ENV, "http://127.0.0.1:12345")
    for url in ("http://127.0.0.1/private", "https://user:secret@public.example/page"):
        with pytest.raises(ValueError, match="public HTTP|bounded HTTP"):
            bridge.open("example.com", "Read", url)
        assert tools.path.read_bytes() == before
    with pytest.raises(ValueError, match="deadline has passed"):
        bridge.open("example.com", "Read", URL)
    assert tools.path.read_bytes() == before


def test_redirect_and_empty_page_save_fixed_failures_on_planned_route(tmp_path, monkeypatch):
    cases = [
        ((0, 302, {"Content-Type": "text/plain", "Location": "http://public.example/other"}, b"go"),
         "provider_error", "arena_public_web_http_302"),
        ((0, 200, {"Content-Type": "text/html"}, b"<html><script>only hidden</script></html>"),
         "schema_error", "arena_public_web_no_readable_text"),
    ]
    for index, (response, status, code) in enumerate(cases):
        tools = native_run(tmp_path, monkeypatch)
        with proxy(response) as (proxy_url, _calls):
            monkeypatch.setenv(PROXY_ENV, proxy_url)
            result = PublicWeb(tools, time.monotonic() + 10).open(
                "example" + str(index) + ".com", "Read", URL)
        assert result["status"] == status and result["error"] == code
        route = public_routes(tools)[0]
        receipt = read_receipt(tools.path, route["route_id"])["result"]
        assert receipt["receipt_status"] == "complete" and receipt["status"] == status


@pytest.mark.parametrize("status", [301, 403, 404, 429, 503])
def test_http_status_survives_real_child_receipt_and_cache_without_payload(tmp_path, monkeypatch, status):
    tools = native_run(tmp_path, monkeypatch)
    secret_marker = "do-not-emit-http-error-payload"
    headers = {"Content-Type": "text/plain", "X-Private": secret_marker,
               "Location": "http://public.example/" + secret_marker}
    with proxy((0, status, headers, secret_marker.encode())) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        bridge = PublicWeb(tools, time.monotonic() + 10)
        result = bridge.open("example.com", "Read", URL)
        cached = bridge.open("example.com", "Read", URL)
        assert len(calls) == 1  # No redirect or retry; the same native receipt is reused.
    receipt = read_receipt(tools.path, public_routes(tools)[0]["route_id"])["result"]
    for value in (result, cached, receipt):
        assert value["status"] == "provider_error"
        assert "http_status" not in value  # Preserve the native receipt schema.
        assert value["error"] == "arena_public_web_http_" + str(status)
        assert secret_marker not in json.dumps(value)
    assert cached["cached"] is True and cached["ref"] == result["ref"]
    assert receipt["receipt_status"] == "complete" and receipt["results"] == []


@pytest.mark.parametrize("invalid", [True, False, 299, 600, 404.0, "404", None, {"secret": "marker"}])
def test_malformed_child_http_status_remains_generic_without_payload(tmp_path, monkeypatch, invalid):
    tools = native_run(tmp_path, monkeypatch)
    monkeypatch.setenv(PROXY_ENV, "http://127.0.0.1:12345")
    child = {"status": "provider_error", "value": {"error": "http_error", "http_status": invalid}}
    monkeypatch.setattr(public_web.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(child)))
    result = PublicWeb(tools, time.monotonic() + 10).open("example.com", "Read", URL)
    receipt = read_receipt(tools.path, public_routes(tools)[0]["route_id"])["result"]
    for value in (result, receipt):
        assert value["status"] == "provider_error"
        assert value["error"] == "arena_public_web_fetch_failed"
        assert "http_status" not in value and "marker" not in json.dumps(value)


@pytest.mark.parametrize("child", [
    {"status": "provider_error", "value": {"error": "http_error", "http_status": 403, "headers": "private-marker"}},
    {"status": "provider_error", "value": {"error": "private-marker", "http_status": 403}},
    {"status": "provider_error", "value": "private-marker"},
    {"status": "schema_error", "value": {"private-marker": 1}},
])
def test_child_error_envelope_rejects_unknown_or_private_fields(monkeypatch, child):
    monkeypatch.setattr(public_web.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(child)))
    with pytest.raises(OSError, match="^fetch_child_failed$"):
        public_web._fetch_isolated(URL, "http://127.0.0.1:12345", time.monotonic() + 10)


def test_raw_and_text_bounds_are_truthful_for_unicode(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)
    body = ("界" * (MAX_TEXT_CHARACTERS + 1000)).encode("utf-8")
    with proxy((0, 200, {"Content-Type": "text/plain; charset=utf-8"}, body)) as (proxy_url, _calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        result = PublicWeb(tools, time.monotonic() + 10).open("example.com", "Read", URL)
    assert result["status"] == "partial" and result["truncated"] is True
    assert len(result["text"]) == 8000
    assert result["next"] == {"tool": "tyche_inspect", "arguments": {
        "ref": result["ref"], "field": "text", "offset": 8000}}
    receipt = read_receipt(tools.path, public_routes(tools)[0]["route_id"])["result"]
    row = receipt["results"][0]
    assert len(row["text"]) == MAX_TEXT_CHARACTERS
    assert row["observed_characters"] == MAX_TEXT_CHARACTERS + 1000
    assert row["text_truncated"] is True and row["raw_truncated"] is False

    other = native_run(tmp_path, monkeypatch)
    with proxy((0, 200, {"Content-Type": "text/plain"}, b"a" * (MAX_RAW_BYTES + 1))) as (proxy_url, _calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        PublicWeb(other, time.monotonic() + 10).open("other.com", "Read", URL)
    raw = read_receipt(other.path, public_routes(other)[0]["route_id"])["result"]["results"][0]
    assert raw["raw_bytes_observed"] == MAX_RAW_BYTES and raw["raw_truncated"] is True


def test_child_wall_deadline_completes_timeout_receipt(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)
    with proxy((.5, 200, {"Content-Type": "text/plain"}, b"late")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        started = time.monotonic()
        result = PublicWeb(tools, started + .12).open("example.com", "Read", URL)
        elapsed = time.monotonic() - started
    assert result["status"] == "timeout" and elapsed < .45
    receipt = read_receipt(tools.path, public_routes(tools)[0]["route_id"])["result"]
    assert receipt["receipt_status"] == "complete"
    assert receipt["error"] == "arena_public_web_timeout" and len(calls) == 1


def test_finalization_native_guards_refuse_before_fetch_or_mutation(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch, target_count=2, duration=1)
    accept(tools)
    document = json.loads(tools.path.read_text())
    document["stop_check"]["started_at"] = "2026-09-17T00:00:00+00:00"
    tools.path.write_text(json.dumps(document))
    monkeypatch.setenv("TYCHE_FINALIZATION_ONLY", "1")
    with proxy((0, 200, {"Content-Type": "text/plain"}, b"must not fetch")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        before = tools.path.read_bytes()
        bridge = PublicWeb(tools, time.monotonic() + 10)
        with pytest.raises(ValueError, match="action not eligible"):
            bridge.open("example.com", "Reread", URL)
        assert tools.path.read_bytes() == before and calls == []
        with pytest.raises(ValueError, match="exact saved source URL"):
            bridge.open("example.com", "Reread", "http://public.example/other")
        assert tools.path.read_bytes() == before and calls == []


def test_cache_is_run_local_and_target_is_part_of_native_identity(tmp_path, monkeypatch):
    with proxy((0, 200, {"Content-Type": "text/plain"}, b"one observation")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        first = native_run(tmp_path, monkeypatch)
        second = native_run(tmp_path, monkeypatch)
        PublicWeb(first, time.monotonic() + 10).open("first.com", "Read", URL)
        PublicWeb(second, time.monotonic() + 10).open("second.com", "Read", URL)
        assert len(calls) == 2
    first_receipt = read_receipt(first.path, public_routes(first)[0]["route_id"])["result"]
    second_receipt = read_receipt(second.path, public_routes(second)[0]["route_id"])["result"]
    assert first_receipt["request_fingerprint"] != second_receipt["request_fingerprint"]


def test_tool_schema_prompt_and_child_proxy_forwarding_are_narrow():
    assert "web" not in LAB_TOOLS["tyche_review"][1]["properties"]
    schema = LAB_TOOLS["tyche_open"][1]
    assert set(schema["properties"]) == {"target", "purpose", "url"}
    with pytest.raises(ValueError):
        validate({"target": "example.com", "purpose": "Read", "url": URL,
                  "response": "fabricated"}, schema)
    config = runtime.tool_configuration(Path("/tmp/results.json"), 10, 20)
    assert "LAB_ARENA_WEB_PROXY_URL" in config
    prompt = runtime.instructions()
    assert "tyche_open only to read an exact public page URL" in prompt
    assert "paid search remain brokered" in prompt
    assert public_url("https://openrouter.ai/docs") == "https://openrouter.ai/docs"


def test_lab_tool_keeps_ref_when_unicode_preview_exceeds_model_result_limit():
    ref = "lookup-unicode-page:0"
    session = LabTools.__new__(LabTools)
    session.lock = threading.Lock()
    session.delivered = False
    session.public_web = SimpleNamespace(open=lambda **_arguments: {
        "status": "ok", "ref": ref, "cached": False,
        "url": "https://public.example/" + "escaped/" * 400,
        "text": "界" * 8000,
        "content_sha256": "a" * 64,
        "saved_characters": 8000, "observed_characters": 8000,
        "truncated": False, "next_offset": None, "next": None,
    })
    session.broker = SimpleNamespace(local_dispatch_budget=lambda: {
        "scope": "local_adapter_dispatch_count", "used": 0, "limit": 30, "remaining": 30})
    result = session.call("tyche_open", {
        "target": "example.com", "purpose": "Read", "url": "https://public.example/page"})
    assert result["ref"] == ref and result["preview_omitted"] is True
    assert result["next_offset"] == 0
    assert result["next"] == {"tool": "tyche_inspect", "arguments": {
        "ref": ref, "field": "text", "offset": 0}}
    assert "text" not in result and "url" not in result

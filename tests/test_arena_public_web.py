"""Offline integration checks for the native public-page bridge."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
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
from tyche_arena import public_web
from tyche_arena import host as runtime
from research_tools import ResearchTools, validate
from source_receipts import arena_public_web_capture, content_kind, read_receipt, web_passage
import run_attempt


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
            delay, status, headers, body = (response(self.path, len(calls))
                                            if callable(response) else response)
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


@pytest.mark.parametrize("legacy", [False, True])
def test_host_capture_qualification_and_legacy_observation_rejection(tmp_path, monkeypatch, legacy):
    tools = native_run(tmp_path, monkeypatch)
    with proxy((0, 200, {"Content-Type": "text/html; charset=utf-8"},
                b"<html><body>Example manufactures products.</body></html>")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        page = PublicWeb(tools, time.monotonic() + 10).open(
            "example.com", "Discover manufacturing evidence", URL,
        )
        assert page["status"] == "ok" and len(calls) == 1
    if legacy:
        receipt = read_receipt(tools.path, page["ref"].split(":")[0])
        saved = receipt["result"]
        saved.pop("provider_response")
        saved["results"][0]["content_kind"] = "captured_page"  # A marker cannot promote a note.
        Path(receipt["receipt_file"]).write_text(json.dumps(saved))
    arguments = [{
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
        }]
    before = tools.path.read_bytes()
    if legacy:
        with pytest.raises(ValueError, match="tool-captured page, not an agent-recorded passage"):
            tools.review(companies=arguments)
        assert tools.path.read_bytes() == before
    else:
        tools.review(companies=arguments)
        packet = tools.inspect(target="example.com", field="evidence_review")
        source = packet["sources"][page["ref"]]
        assert source["capture_method"] == "arena_host_public_web"
        assert source["content_kind"] == "captured_page"
        assert source["date_basis"] == "observed_current"


def test_real_native_research_read_final_reread_and_phase_cache(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)
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
        if response[1] == 302:
            assert result["redirect_url"] == "http://public.example/other"
            assert result["next"]["tool"] == "tyche_open"
        route = public_routes(tools)[0]
        receipt = read_receipt(tools.path, route["route_id"])["result"]
        assert receipt["receipt_status"] == "complete" and receipt["status"] == status


@pytest.mark.parametrize("status", [403, 404, 429, 503])
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


@pytest.mark.parametrize(
    "status,location,expected",
    [
        (301, "https://public.example/new", "https://public.example/new"),
        (302, "/new", "http://public.example/new"),
        (303, "next", "http://public.example/next"),
        (307, "//other.example/new", "http://other.example/new"),
        (308, "../new", "http://public.example/new"),
    ],
)
def test_public_redirect_exposes_validated_explicit_next_without_following(
        tmp_path, monkeypatch, status, location, expected):
    tools = native_run(tmp_path, monkeypatch)
    private_marker = "do-not-emit-other-header-or-body"
    headers = {"Content-Type": "text/plain", "Location": location,
               "X-Private": private_marker}
    with proxy((0, status, headers, private_marker.encode())) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        bridge = PublicWeb(tools, time.monotonic() + 10)
        result = bridge.open("example.com", "Read", URL)
        cached = bridge.open("example.com", "Read", URL)
        assert len(calls) == 1

    receipt = read_receipt(tools.path, public_routes(tools)[0]["route_id"])["result"]
    assert receipt["error"] == {
        "code": "arena_public_web_http_" + str(status), "redirect_url": expected}
    for value in (result, cached):
        assert value["error"] == "arena_public_web_http_" + str(status)
        assert value["redirect_url"] == expected
        assert value["next"] == {"tool": "tyche_open", "arguments": {
            "target": "example.com", "purpose": "Open the observed public redirect target",
            "url": expected}}
        assert private_marker not in json.dumps(value)


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/private",
    "https://user:secret@public.example/private",
    "https://public.example/" + "x" * 4096,
    "/line\nbreak",
    "/delete\x7fcharacter",
    "http://[invalid",
])
def test_redirect_location_rejects_private_credentials_oversize_controls_and_invalid(location):
    assert public_web._redirect_url(URL, location) is None


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/private",
    "https://user:secret@public.example/private",
    "https://public.example/" + "x" * 4096,
    "http://[invalid",
])
def test_invalid_redirect_location_keeps_status_only(tmp_path, monkeypatch, location):
    tools = native_run(tmp_path, monkeypatch)
    with proxy((0, 302, {"Content-Type": "text/plain", "Location": location}, b"go")) \
            as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        result = PublicWeb(tools, time.monotonic() + 10).open("example.com", "Read", URL)
    assert len(calls) == 1
    assert result["error"] == "arena_public_web_http_302"
    assert "redirect_url" not in result and "next" not in result


def test_explicit_redirect_open_uses_normal_proxy_gate_and_own_receipt(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)

    def responses(_path, call_number):
        if call_number == 1:
            return 0, 302, {"Content-Type": "text/plain", "Location": "/other"}, b"go"
        return 0, 200, {"Content-Type": "text/plain"}, b"redirect destination"

    with proxy(responses) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        bridge = PublicWeb(tools, time.monotonic() + 10)
        first = bridge.open("example.com", "Read", URL)
        assert len(calls) == 1
        second = bridge.open(**first["next"]["arguments"])
        assert len(calls) == 2

    assert second["status"] == "ok" and second["text"] == "redirect destination"
    routes = public_routes(tools)
    assert len(routes) == 2 and routes[0]["route_id"] != routes[1]["route_id"]
    assert routes[0]["request_fingerprint"] != routes[1]["request_fingerprint"]
    second_receipt = read_receipt(tools.path, routes[1]["route_id"])["result"]
    assert second_receipt["attempt"]["request"]["query"] == "http://public.example/other"
    assert second_receipt["receipt_status"] == "complete"


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
    {"status": "provider_error", "value": {"error": "http_error", "http_status": 302,
                                              "redirect_url": "https://public.example/next",
                                              "headers": "private-marker"}},
    {"status": "provider_error", "value": {"error": "private-marker", "http_status": 403}},
    {"status": "provider_error", "value": "private-marker"},
    {"status": "schema_error", "value": {"private-marker": 1}},
])
def test_child_error_envelope_rejects_unknown_or_private_fields(monkeypatch, child):
    monkeypatch.setattr(public_web.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(child)))
    with pytest.raises(OSError, match="^fetch_child_failed$"):
        public_web._fetch_isolated(URL, "http://127.0.0.1:12345", time.monotonic() + 10)


@pytest.mark.parametrize("redirect_url", [
    "http://127.0.0.1/private", "https://user:secret@public.example/private",
    "https://public.example/" + "x" * 4096, "https://public.example/line\nbreak",
])
def test_parent_revalidates_untrusted_child_redirect_as_status_only(monkeypatch, redirect_url):
    child = {"status": "provider_error", "value": {
        "error": "http_error", "http_status": 302, "redirect_url": redirect_url}}
    monkeypatch.setattr(public_web.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(child)))
    with pytest.raises(public_web._HTTPStatusError) as raised:
        public_web._fetch_isolated(URL, "http://127.0.0.1:12345", time.monotonic() + 10)
    assert raised.value.status == 302 and raised.value.redirect_url is None


def test_saved_redirect_error_is_revalidated_before_view():
    saved = {
        "status": "provider_error", "results": [],
        "error": {"code": "arena_public_web_http_302",
                  "redirect_url": "http://127.0.0.1/private"},
        "attempt": {"request": {
            "arena_review_phase": "research",
            "arena_target_scope": "example.com",
        }},
    }
    result = PublicWeb._view("route-one", saved)
    assert result["error"] == "arena_public_web_http_302"
    assert "redirect_url" not in result and "next" not in result


@pytest.mark.parametrize("saved_error", [
    {"code": "arena_public_web_http_0302", "redirect_url": "https://public.example/next"},
    {"code": "arena_public_web_http_302", "redirect_url": "https://public.example/next",
     "headers": "private-marker"},
])
def test_saved_redirect_error_rejects_noncanonical_or_extra_fields(saved_error):
    result = PublicWeb._view("route-one", {
        "status": "provider_error", "results": [], "error": saved_error})
    assert result["error"] == "arena_public_web_fetch_failed"
    assert "redirect_url" not in result and "next" not in result


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
    assert receipt["error"] == "arena_public_web_timeout"
    # Process startup may consume the deadline before the HTTP request begins.
    assert len(calls) <= 1


def test_finalization_native_guards_refuse_before_fetch_or_mutation(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch, target_count=2)
    accept(tools)
    document = json.loads(tools.path.read_text())
    document["stop_check"]["started_at"] = "2026-09-17T00:00:00+00:00"
    tools.path.write_text(json.dumps(document))
    with proxy((0, 200, {"Content-Type": "text/plain"}, b"must not fetch")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        before = tools.path.read_bytes()
        bridge = PublicWeb(tools, time.monotonic() + 10)
        with pytest.raises(ValueError, match="action not eligible"):
            bridge.open("example.com", "Reread", URL)
        assert tools.path.read_bytes() == before and calls == []
        monkeypatch.setenv("TYCHE_FINALIZATION_ONLY", "1")
        with pytest.raises(ValueError, match="exact saved source URL"):
            bridge.open("example.com", "Reread", "http://public.example/other")
        assert tools.path.read_bytes() == before and calls == []
        with pytest.raises(ValueError, match="exact saved source URL"):
            bridge.open("unaccepted.example", "Reread", URL)
        assert tools.path.read_bytes() == before and calls == []


def test_finalization_refuses_unseen_redirect_target_and_offers_no_new_url_action(
        tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)
    with proxy((0, 302, {"Content-Type": "text/plain", "Location": "/other"}, b"go")) \
            as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        bridge = PublicWeb(tools, time.monotonic() + 10)
        research = bridge.open("example.com", "Read", URL)
        assert research["next"]["arguments"]["url"] == "http://public.example/other"

        accept(tools)
        document = json.loads(tools.path.read_text())
        document["stop_check"]["started_at"] = "2026-09-17T00:00:00+00:00"
        tools.path.write_text(json.dumps(document))
        monkeypatch.setenv("TYCHE_FINALIZATION_ONLY", "1")
        with pytest.raises(ValueError, match="exact saved source URL"):
            bridge.open("example.com", "Reread", research["redirect_url"])
        final = bridge.open("example.com", "Reread", URL)

    assert len(calls) == 2
    assert final["redirect_url"] == "http://public.example/other"
    assert "next" not in final


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
    assert "successful research page captured by tyche_open or tyche_lookup" in LAB_TOOLS["tyche_review"][0]
    assert "web observations are discovery notes, not qualifying evidence" not in LAB_TOOLS["tyche_review"][0]
    schema = LAB_TOOLS["tyche_open"][1]
    assert set(schema["properties"]) == {"target", "purpose", "url"}
    with pytest.raises(ValueError):
        validate({"target": "example.com", "purpose": "Read", "url": URL,
                  "response": "fabricated"}, schema)
    config = runtime.tool_configuration(Path("/tmp/results.json"), 10, 20)
    assert "LAB_ARENA_WEB_PROXY_URL" in config
    prompt = runtime.instructions()
    assert runtime.runner.ISOLATION_INSTRUCTIONS in prompt
    assert (runtime.SKILL / "SKILL.md").read_text().replace(
        "(references/", "(" + str(runtime.SKILL / "references") + "/") in prompt
    assert "The host owns credentials, provider billing, quotas and the hard deadline" in prompt
    assert "tyche_finish writes reviewed /output/companies.json" in prompt
    assert public_url("https://openrouter.ai/docs") == "https://openrouter.ai/docs"


def test_lab_tool_keeps_ref_when_unicode_preview_exceeds_model_result_limit(tmp_path):
    ref = "lookup-unicode-page:0"
    session = LabTools.__new__(LabTools)
    session.lock = threading.Lock()
    session.delivered = False
    session.icp = {}
    run_file = tmp_path / "results.json"
    document = {"request": {"target_count": 1}, "accepted": []}
    run_file.write_text(json.dumps(document))
    session.research = SimpleNamespace(
        path=run_file, _document=lambda: document)
    session._publish_confirmed = lambda: None
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


def test_strict_capture_envelope_rejects_forgery_and_preserves_quote_date_rules(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)
    with proxy((0, 200, {"Content-Type": "text/plain"}, b"Captured activity on September 17, 2026.")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        page = PublicWeb(tools, time.monotonic() + 10).open("example.com", "Read proof", URL)
    receipt = read_receipt(tools.path, page["ref"].split(":")[0])
    original = receipt["result"]
    evidence = tools._evidence({"ref": page["ref"]})
    web_passage(tools.path, tools._document(), evidence)
    assert arena_public_web_capture(original["results"][0], original)
    mutations = [
        lambda s: s.pop("provider_response"),
        lambda s: s["provider_response"].update(capture="authored_observation"),
        lambda s: s["provider_response"].update(http_status=302),
        lambda s: s["provider_response"].update(http_status=200.0),
        lambda s: s["provider_response"].update(body="Invented page"),
        lambda s: s["provider_response"].update(content_sha256="0" * 64),
        lambda s: s["provider_response"].update(url=URL + "/other"),
        lambda s: s["provider_response"].update(run_fingerprint="0" * 64),
        lambda s: s["provider_response"].update(request_fingerprint="0" * 64),
        lambda s: s["provider_response"]["request"].update(arena_review_phase="finalization"),
        lambda s: s["provider_response"].update(headers={"Location": URL}),
        lambda s: s["results"][0].update(http_status=True),
        lambda s: s["results"][0].update(url=URL + "/other"),
        lambda s: s["results"][0].update(text="Invented page"),
        lambda s: s["results"][0].update(content_kind="captured_page"),
        lambda s: s["results"][0].update(saved_characters=0),
        lambda s: s["results"][0].update(raw_bytes_observed=1024 * 1024 + 1),
        lambda s: s["results"].append(copy.deepcopy(s["results"][0])),
        lambda s: s.update(status="provider_error"),
        lambda s: s.update(receipt_status="pending"),
        lambda s: s.update(pending_verification=True),
        lambda s: s.update(attempt=None),
        lambda s: s.update(run_fingerprint="0" * 64),
    ]
    for mutate in mutations:
        saved = copy.deepcopy(original)
        mutate(saved)
        assert not arena_public_web_capture(saved["results"][0], saved)
        assert content_kind(saved["results"][0], saved) == "unverified"
        Path(receipt["receipt_file"]).write_text(json.dumps(saved))
        with pytest.raises(ValueError):
            web_passage(tools.path, tools._document(), evidence)
    for bad_url in ("http://127.0.0.1/private", "http://user:secret@public.example/page",
                    "http://public.example:99999/page", "http://public.example/page\n"):
        saved = copy.deepcopy(original)
        saved["attempt"]["request"]["query"] = bad_url
        saved["provider_response"]["request"]["query"] = bad_url
        saved["provider_response"]["url"] = bad_url
        saved["results"][0]["url"] = bad_url
        fingerprint = public_web.request_fingerprint("public_web", saved["attempt"]["request"])
        saved["request_fingerprint"] = saved["provider_response"]["request_fingerprint"] = fingerprint
        assert not arena_public_web_capture(saved["results"][0], saved)
    Path(receipt["receipt_file"]).write_text(json.dumps(original))
    for forged in ({**evidence, "text": "Uncaptured quote"},
                   {**evidence, "url": URL + "/other"},
                   {**evidence, "date_basis": "published"}):
        with pytest.raises(ValueError):
            web_passage(tools.path, tools._document(), forged)
    with pytest.raises(ValueError, match="helper-owned"):
        run_attempt._public_web_observation(original, {
            "status": "ok", "operation": "open", "results": original["results"],
            "provider_response": original["provider_response"],
        })
    assert len(calls) == 1


def test_finalization_capture_is_not_new_qualification(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)
    accept(tools)
    monkeypatch.setenv("TYCHE_FINALIZATION_ONLY", "1")
    with proxy((0, 200, {"Content-Type": "text/plain"}, b"Current corroboration")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        page = PublicWeb(tools, time.monotonic() + 10).open("example.com", "Reread", URL)
    saved = read_receipt(tools.path, page["ref"].split(":")[0])["result"]
    assert saved["provider_response"]["http_status"] == 200
    assert content_kind(saved["results"][0], saved) == "unverified"
    with pytest.raises(ValueError, match="agent-recorded passage"):
        web_passage(tools.path, tools._document(), tools._evidence({"ref": page["ref"]}))
    assert len(calls) == 1


def test_saved_capture_recovers_after_completion_interrupt_without_redispatch(tmp_path, monkeypatch):
    tools = native_run(tmp_path, monkeypatch)
    bridge = PublicWeb(tools, time.monotonic() + 10)
    finish = run_attempt.finish_attempt
    def interrupt(*args, **kwargs):
        raise OSError("offline completion interruption")
    monkeypatch.setattr(run_attempt, "finish_attempt", interrupt)
    with proxy((0, 200, {"Content-Type": "text/plain"}, b"Saved page proof")) as (proxy_url, calls):
        monkeypatch.setenv(PROXY_ENV, proxy_url)
        with pytest.raises(OSError, match="completion interruption"):
            bridge.open("example.com", "Read", URL)
        monkeypatch.setattr(run_attempt, "finish_attempt", finish)
        cached = bridge.open("example.com", "Read", URL)
        assert cached["cached"] is True and len(calls) == 1
        tools.inspect(recover=cached["ref"])
    saved = read_receipt(tools.path, cached["ref"].split(":")[0])["result"]
    assert arena_public_web_capture(saved["results"][0], saved)
    web_passage(tools.path, tools._document(), tools._evidence({"ref": cached["ref"]}))
    assert saved["attempt"]["action"]["paid_calls"] == 0


def test_success_child_envelope_revalidates_status_url_body_and_bounds(monkeypatch):
    with proxy((0, 200, {"Content-Type": "text/plain"}, b"Controlled child body")) as (proxy_url, calls):
        row = public_web._fetch(URL, proxy_url, time.monotonic() + 10)
    mutations = [lambda r: r.pop("http_status"),
                 lambda r: r.update(http_status=302),
                 lambda r: r.update(http_status=200.0),
                 lambda r: r.update(url=URL + "/other"),
                 lambda r: r.update(content_sha256="0" * 64),
                 lambda r: r.update(headers={"Location": URL}),
                 lambda r: r.update(text="x" * (MAX_TEXT_CHARACTERS + 1)),
                 lambda r: r.update(text="\ud800")]
    for mutate in mutations:
        forged = copy.deepcopy(row)
        mutate(forged)
        monkeypatch.setattr(public_web.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps({"status": "ok", "value": forged})))
        with pytest.raises(OSError, match="fetch_child_failed"):
            public_web._fetch_isolated(URL, "http://127.0.0.1:12345", time.monotonic() + 10)
    assert len(calls) == 1

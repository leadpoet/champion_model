"""Optional native Codex audit. No Leadpoet execution or paid requests.

The original PR #198 incompatibility at 2558d4bc is an explicit expected failure,
not validation of the updated upstream protocol. See docs/leadpoet-codex-audit.md.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tyche_arena import runtime
from tyche_arena.mcp import LAB_TOOLS


def unsupported_fields(body):
    """Inspect the relevant closed PR #198 fields; not a full gateway validator."""
    errors = []
    if set(body.get("reasoning", {})) - {"effort", "summary"}:
        errors.append("reasoning.context")
    for item in body.get("input", []):
        if item.get("type", "message") not in {"message", "function_call", "function_call_output",
                "custom_tool_call", "custom_tool_call_output", "reasoning"}:
            errors.append("input." + str(item.get("type")))
        if item.get("type") in {"function_call", "custom_tool_call"} and "namespace" in item:
            errors.append("input.call.namespace")
        if item.get("type") in {"function_call_output", "custom_tool_call_output"} and not isinstance(item.get("output"), str):
            errors.append("input.call_output.content_array")
    for tool in body.get("tools") or []:
        if tool.get("type") not in {"function", "custom"} or "defer_loading" in tool:
            errors.append("tools." + str(tool.get("type")))
    if nesting_depth(body) > 12:
        errors.append("operation.depth>12")
    return errors


def nesting_depth(value):
    if isinstance(value, dict):
        return max((1 + nesting_depth(child) for child in value.values()), default=0)
    if isinstance(value, list):
        return max((1 + nesting_depth(child) for child in value), default=0)
    return 0


@pytest.mark.skipif(not os.environ.get("TYCHE_TEST_CODEX_BINARY"),
                    reason="set TYCHE_TEST_CODEX_BINARY to Codex 0.154.0 for the offline wire audit")
@pytest.mark.parametrize("admit_native", [False, True])
def test_native_codex_lab_boundary(tmp_path, monkeypatch, admit_native):
    binary = os.environ["TYCHE_TEST_CODEX_BINARY"]
    assert subprocess.check_output([binary, "--version"], text=True).strip() == "codex-cli 0.154.0"
    assert Path(binary).resolve().with_name("codex-code-mode-host").is_file(), "Install the full Codex package, including its code-mode companion"
    observed = []
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            if size > 1_000_000:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(size))
            (tmp_path / ("request-" + str(len(observed)) + ".json")).write_text(json.dumps(body, indent=2))
            observed.append({"path": self.path, "body": body})
            errors = unsupported_fields(body)
            if not admit_native:
                # Stop before inference even if future configuration fits this
                # subset. This case only audits the native request boundary.
                payload = json.dumps({"error": {"message": "; ".join(errors) or "offline audit complete"}}).encode()
                self.send_response(400)
            else:
                # A hypothetical native-compatible upstream, not PR #198.
                # Replies are scripted; no model inference occurs.
                if len(calls) < 2:
                    calls.append(len(calls) + 1)
                    code = "const required = ['tyche_inspect','tyche_lookup','tyche_review','tyche_finish']; const missing = required.filter(name => !ALL_TOOLS.some(t => t.name.endsWith(name))); if (missing.length) throw new Error('TYCHE MCP tools missing: ' + missing.join(',')); const t = ALL_TOOLS.find(t => t.name.endsWith('tyche_inspect')); text(await tools[t.name]({}));"
                    output = [{"type": "custom_tool_call", "id": "ct-" + str(len(calls)),
                               "call_id": "call-" + str(len(calls)), "name": "exec", "namespace": "functions",
                               "input": code, "status": "completed"}]
                else:
                    output = [{"type": "message", "id": "final-msg", "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": "TYCHE_CODEX_WIRE_OK", "annotations": []}]}]
                usage = ({"input_tokens": 17020, "output_tokens": 20, "total_tokens": 17040}
                         if len(calls) == 1 else
                         {"input_tokens": 17020, "output_tokens": 16020, "total_tokens": 33040}
                         if len(calls) == 2 and output[0]["type"] == "custom_tool_call" else
                         {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
                document = {"id": "resp-" + str(len(observed)), "object": "response", "created_at": 1789488000,
                            "model": runtime.MODEL, "status": "completed", "output": output,
                            "usage": usage}
                events = [("response.created", {"response": {**document, "status": "in_progress", "output": []}})]
                for index, item in enumerate(output):
                    events.append(("response.output_item.added", {"output_index": index, "item": item}))
                    if item["type"] == "message":
                        events.append(("response.output_text.delta", {"item_id": item["id"], "output_index": index,
                                                                     "content_index": 0, "delta": item["content"][0]["text"]}))
                    events.append(("response.output_item.done", {"output_index": index, "item": item}))
                events.append(("response.completed", {"response": document}))
                payload = "".join("event: " + kind + "\ndata: " + json.dumps({"type": kind, "sequence_number": n, **data}) + "\n\n"
                                  for n, (kind, data) in enumerate(events)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fixture = tmp_path / "mcp_fixture.py"
    advertised = [{"name": name, "description": description, "inputSchema": schema,
                   "annotations": {"destructiveHint": False, "openWorldHint": True}}
                  for name, (description, schema) in LAB_TOOLS.items()]
    fixture.write_text("\n".join([
        "import json, sys",
        "TOOLS = " + repr(advertised),
        "PADDING = 'alpha beta gamma delta ' * 1200",
        "for line in sys.stdin:",
        "    request = json.loads(line)",
        "    if 'id' not in request: continue",
        "    method = request.get('method')",
        "    if method == 'initialize':",
        "        result = {'protocolVersion': request['params']['protocolVersion'], 'capabilities': {'tools': {}, 'experimental': {'codex/sandbox-state-meta': {}}}, 'serverInfo': {'name': 'tyche-fixture', 'version': '1'}}",
        "    elif method == 'tools/list': result = {'tools': TOOLS}",
        "    elif method == 'tools/call': result = {'content': [{'type': 'text', 'text': json.dumps({'status': 'TYCHE_OFFLINE_TOOL_OK', 'padding': PADDING, 'tail': 'TYCHE_TOOL_OUTPUT_TAIL_OK'})}], 'isError': False}",
        "    else: result = {}",
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
    ]) + "\n")
    config = ["model = " + json.dumps(runtime.MODEL), 'model_reasoning_effort = "xhigh"',
              'model_provider = "fixture"', 'approval_policy = "never"', 'sandbox_mode = "read-only"',
              'web_search = "disabled"', 'check_for_update_on_startup = false',
              '[features]', 'apps = false', 'multi_agent = false', 'shell_snapshot = false',
              'enable_request_compression = false', '[model_providers.fixture]', 'name = "Fixture"',
              'base_url = "http://127.0.0.1:' + str(server.server_port) + '/v1"',
              'wire_api = "responses"', 'requires_openai_auth = false', 'supports_websockets = false',
              'request_max_retries = 0', 'stream_max_retries = 0']
    (codex_home / "config.toml").write_text("\n".join(config) + "\n")
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(codex_home), "CODEX_HOME": str(codex_home),
                   "PYTHONPATH": str(ROOT), "LANG": "en_US.UTF-8", "NO_PROXY": "127.0.0.1,localhost"}

    @contextmanager
    def session(**kwargs):
        assert kwargs == {"model": runtime.MODEL, "reasoning_effort": runtime.REASONING_EFFORT}
        yield environment

    # Replace only the host-bound MCP startup guard, which intentionally refuses
    # a local invocation. Keep the production launch, CLI flags and LAB_TOOLS.
    original_configuration = runtime.tool_configuration

    def fixture_configuration(run_file, deadline, response_deadline):
        return original_configuration(run_file, deadline, response_deadline).replace(
            json.dumps(["-B", "-m", "tyche_arena.mcp", "--run-file", str(run_file), "--deadline", str(deadline),
                        "--response-deadline", str(response_deadline)]),
            json.dumps([str(fixture)]))

    monkeypatch.setattr(runtime, "tool_configuration", fixture_configuration)
    monkeypatch.setattr(runtime, "progress", lambda _path: {"stop": "continue", "operational_block": None})
    monkeypatch.setattr(runtime, "full_delivery", lambda directory: (
        (directory / "final.txt").exists()
        and (directory / "final.txt").read_text().strip() == "TYCHE_CODEX_WIRE_OK"))
    now = runtime.time.monotonic()
    try:
        if admit_native:
            runtime.launch(SimpleNamespace(session=session, CODEX_BINARY=binary), tmp_path,
                           now + 40, now + 40, 40)
        else:
            with pytest.raises(RuntimeError, match="Lab Codex failed twice before delivery"):
                runtime.launch(SimpleNamespace(session=session, CODEX_BINARY=binary), tmp_path,
                               now + 40, now + 40, 40)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    log = (tmp_path / "codex.log").read_text()
    config_text = (codex_home / "config.toml").read_text()
    assert "model_auto_compact_token_limit" not in config_text
    assert "model_auto_compact_token_limit_scope" not in config_text
    assert "tool_output_token_limit" not in config_text
    assert observed, log
    assert "Code Mode is unavailable" not in log, log
    assert all(row["path"] == "/v1/responses" for row in observed)
    body = observed[0]["body"]
    additional = [item for item in body["input"] if item.get("type") == "additional_tools"]
    tools = [tool for item in additional for tool in item.get("tools", [])] + (body.get("tools") or [])
    namespaces = {tool["name"]: [child["name"] for child in tool.get("tools", [])]
                  for tool in tools if tool.get("type") == "namespace"}
    assert "multi_agent_v1" not in namespaces and "collaboration" not in namespaces
    assert "image_gen" not in namespaces
    # Luna discovers MCP tools inside the code-mode host. The synthetic
    # successful response above verifies all required names through ALL_TOOLS.
    assert any(child == "exec" for children in namespaces.values() for child in children)
    summary = {"codex": runtime.CODEX_VERSION, "model": body["model"], "reasoning": body.get("reasoning"),
               "input_types": sorted({item.get("type", "message") for item in body["input"]}),
               "tool_namespaces": namespaces, "request_bytes": len(json.dumps(body).encode()),
               "operation_depth": nesting_depth(body),
               "unsupported_fields": sorted({error for row in observed for error in unsupported_fields(row["body"])})}
    (tmp_path / "wire-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    errors = unsupported_fields(body)
    if admit_native:
        assert (tmp_path / "final.txt").read_text().strip() == "TYCHE_CODEX_WIRE_OK"
        assert len(calls) == 2
        assert sum(any(item.get("type") == "custom_tool_call_output"
                       and "TYCHE_TOOL_OUTPUT_TAIL_OK" in json.dumps(item)
                       and "truncated" not in json.dumps(item).lower()
                       for item in row["body"]["input"]) for row in observed[1:]) >= 2, (
                           "Native Codex did not preserve both >4000-token MCP results")
        assert not any("Another language model started to solve this problem" in json.dumps(row["body"])
                       for row in observed), "Native model defaults compacted the high-usage fixture prematurely"
        assert len(observed) == 3
    elif errors:
        assert set(errors) == {"reasoning.context", "input.additional_tools", "operation.depth>12"}, summary
        pytest.xfail("PR #198 rejects native Luna: " + ", ".join(errors))

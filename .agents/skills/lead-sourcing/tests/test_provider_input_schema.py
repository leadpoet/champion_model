"""Reject malformed provider inputs before creating paid work or reservations."""
import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import budget_guard as budget
import deepline
import research_input
import run_attempt as runner
from research_tools import ResearchTools
from test_research_interface import setup_request
from test_research_tools import FixtureProvider


def object_schema(**properties):
    return {"type": "object", "properties": properties, "additionalProperties": False}


# Relevant schema from the saved September 19 ai_ark_company_search description.
# PRODUCT_AND_SERVICES caused a remote 422 with no bill despite this saved enum.
SOURCES = {"type": "array", "minItems": 1, "items": {
    **object_schema(mode={"type": "string", "enum": ["WORD", "SMART", "STRICT"]},
                    source={"type": "string", "enum": ["NAME", "KEYWORD", "SEO", "DESCRIPTION", "INDUSTRY"]}),
    "required": ["mode", "source"]}}
SCHEMA = object_schema(account=object_schema(keyword=object_schema(any=object_schema(
    include=object_schema(sources=SOURCES)))))
TOOL = "ai_ark_company_search"


def payload(source="DESCRIPTION"):
    return {"account": {"keyword": {"any": {"include": {"sources": [
        {"mode": "SMART", "source": "DESCRIPTION"}, {"mode": "SMART", "source": source}]}}}}}


def validate(schema, value):
    research_input.check_tool_contract({"results": [{"toolId": TOOL,
        "inputSchema": {"jsonSchema": schema}}]}, {"tool": TOOL, "payload": value})


class SchemaTests(unittest.TestCase):
    def test_nested_enum_reports_exact_path_and_allowed_values_without_coercion(self):
        value = payload("PRODUCT_AND_SERVICES")
        before = copy.deepcopy(value)
        with self.assertRaisesRegex(ValueError, r"payload.account.keyword.any.include.sources\[1\].source.*NAME.*INDUSTRY"):
            validate(SCHEMA, value)
        self.assertEqual(value, before)
        validate(SCHEMA, payload())

    def test_nested_required_additional_properties_arrays_and_numeric_constraints(self):
        for change in (lambda sources: sources.clear(),
                       lambda sources: sources[0].pop("mode"),
                       lambda sources: sources[0].update(extra=True)):
            value = payload()
            change(value["account"]["keyword"]["any"]["include"]["sources"])
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(SCHEMA, value)
        schema = object_schema(page={"type": "integer", "minimum": 1, "maximum": 5})
        for value in (0, 6, True, "2", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(schema, {"page": value})
        validate(schema, {"page": 2})

    def test_local_references_unions_and_declared_draft(self):
        schema = {"$schema": "http://json-schema.org/draft-07/schema#",
            "definitions": {"choice": {"anyOf": [{"type": "null"}, {"type": "string", "enum": ["NAME", "SEO"]}]}},
            **object_schema(source={"$ref": "#/definitions/choice"})}
        for source in (None, "NAME", "SEO"):
            validate(schema, {"source": source})
        with self.assertRaisesRegex(ValueError, "payload.source.*NAME.*SEO"):
            validate(schema, {"source": "PRODUCT_AND_SERVICES"})

    def test_bad_schemas_fail_closed_and_external_references_never_access_network(self):
        for schema in ({"type": "object", "required": "wrong"}, {"$schema": []}, {"$schema": 9}):
            with self.subTest(schema=schema), self.assertRaisesRegex(ValueError, "malformed"):
                validate(schema, {})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            validate({"$schema": "https://example.test/unknown-draft"}, {})
        for reference in ("https://example.test/schema", "file:///tmp/provider-schema.json", "#/$defs/missing"):
            with self.subTest(reference=reference), patch("urllib.request.urlopen", side_effect=AssertionError("No fetch")):
                with self.assertRaisesRegex(ValueError, "unresolved reference"):
                    validate({"$ref": reference}, {})

    def test_field_only_catalogs_keep_their_existing_checks(self):
        receipt = {"results": [{"toolId": TOOL, "inputSchema": {
            "fields": [{"name": "query", "type": "string", "required": True}]}}]}
        research_input.check_tool_contract(receipt, {"tool": TOOL, "payload": {"query": "companies"}})
        with self.assertRaisesRegex(ValueError, "missing required fields: query"):
            research_input.check_tool_contract(receipt, {"tool": TOOL, "payload": {}})


class DispatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "run/results.json"
        self.provider = FixtureProvider()
        self.tools = ResearchTools(self.path, execute=self.execute)
        self.tools.start(setup_request()["request"])
        self.tools.inspect(tool=TOOL)

    def execute(self, request, capture):
        result, status = self.provider(request, capture)
        if request["operation"] == "describe" and request["tool"] == TOOL:
            result["results"][0]["inputSchema"] = {"jsonSchema": copy.deepcopy(SCHEMA)}
        return result, status

    def lookup(self, value):
        return {"target": "example.test", "phase": "account_discovery", "purpose": "Find matching companies",
                "tool": TOOL, "inputs": value}

    def snapshot(self):
        return self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), {
            path.name: path.read_bytes() for path in self.path.parent.joinpath("receipts").glob("*.json")}

    def test_native_batch_rejection_preserves_state_then_corrected_input_dispatches_once(self):
        before = self.snapshot()
        bad = self.lookup(payload("PRODUCT_AND_SERVICES"))
        with self.assertRaisesRegex(ValueError, r"input.checks\[1\].inputs.*sources\[1\].source.*No paid call"):
            self.tools.lookup([self.lookup(payload()), bad])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(r["operation"] == "execute" for r in self.provider.requests))
        self.tools.lookup([self.lookup(payload())])
        calls = [r for r in self.provider.requests if r["operation"] == "execute"]
        self.assertEqual([r["payload"] for r in calls], [payload()])
        self.assertEqual(budget.actual_cost_summary(budget.load_ledger(self.path))["provider_usd"], .02)

    def test_concise_cli_uses_same_gate_before_any_batch_member_dispatches(self):
        def lookup(value):
            return {"scope": "example.test", "phase": "account_discovery", "purpose": "Find matching companies",
                    "request": {"operation": "execute", "tool": TOOL, "payload": value}}
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, r"sources\[1\].source"):
            runner.run_lookup(self.path, [lookup(payload()), lookup(payload("PRODUCT_AND_SERVICES"))], execute=self.execute)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(r["operation"] == "execute" for r in self.provider.requests))

    def test_legacy_cli_formats_share_preflight_and_preserve_state(self):
        def spec(source):
            return research_input.prepare_lookup({"scope": "example.test", "phase": "account_verification",
                "purpose": "Check company fit", "request": {"operation": "execute", "tool": TOOL,
                                                            "payload": payload(source)}})
        good, bad = spec("DESCRIPTION"), spec("PRODUCT_AND_SERVICES")
        good["action"]["scope"] = "other.test"
        single, batch = self.path.parent / "single.json", self.path.parent / "batch.json"
        single.write_text(json.dumps(bad))
        batch.write_text(json.dumps([good, bad]))
        for args in (["--input-file", str(single)], ["--input-file", str(batch)], ["--batch-files", str(batch)]):
            before = self.snapshot()
            errors = io.StringIO()
            with self.subTest(args=args), patch.object(sys, "argv", [runner.__file__, str(self.path), *args]), \
                    patch.object(runner, "_dispatch", side_effect=AssertionError("No paid dispatch")), \
                    contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
                runner.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("sources[1].source", errors.getvalue())
            self.assertEqual(self.snapshot(), before)

        single.write_text(json.dumps(good))
        with patch.object(sys, "argv", [runner.__file__, str(self.path), "--input-file", str(single)]), \
                patch.object(deepline, "run", side_effect=self.execute), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 0)
        self.assertEqual(sum(r["operation"] == "execute" for r in self.provider.requests), 1)

    def test_remote_validation_error_without_bill_remains_pending_and_cannot_replay(self):
        def remote_error(request, capture):
            if request["operation"] != "execute":
                return self.execute(request, capture)
            def dispatch():
                raw = {"exit_code": 1, "body": {"statusCode": 422, "code": "VALIDATION_ERROR",
                    "message": "Provider schema changed", "job_id": "validation-job"}, "stderr": ""}
                capture(raw)
                return deepline.normalize_response(request, raw)
            return budget.guarded_call(request, "deepline", dispatch)
        self.tools.execute = remote_error
        outcome = self.tools.lookup([self.lookup(payload())])["lookups"][0]
        call = budget.load_ledger(self.path)["calls"][outcome["route"]]
        self.assertIsNone(call["actual_credits"])
        self.assertEqual(call["state"], "pending_billing")
        before = self.snapshot()
        self.tools.execute = lambda *_: self.fail("Never replay an uncertain call")
        with self.assertRaisesRegex(ValueError, "already attempted"):
            self.tools.lookup([self.lookup(payload())])
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()

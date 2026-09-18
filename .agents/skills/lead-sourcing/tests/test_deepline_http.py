"""API execution retains attributable errors without retries or assumed charges."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import budget_guard as budget
import deepline
import deepline_http as transport
from provider_output import ResponseFile


class DeeplineHttpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "results.json"
        self.path.write_text(json.dumps({"request": {"target_count": 5, "contact_fields": []},
            "accepted": [], "routes": [], "budget": {"policy": "actual_cost", "paid_calls": 0,
                "limits": {"deepline_credits": 25, "scrapingdog_credits": 0}}}))
        budget.initialize(self.path, max_usd=2.5)
        self.request = {"operation": "execute", "tool": "hunter_email_finder", "payload": {"first_name": "Ada"},
                        "spend": {"run_file": str(self.path), "route_id": "call-1"}, "timeout_seconds": 7}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"DEEPLINE_API_KEY": "fixture-private-key"}).start()
        self.opener = patch.object(transport, "build_opener").start().return_value
        self.cli = patch.object(deepline, "_invoke", side_effect=AssertionError("API execution must not call CLI")).start()

    def response(self, body, status=200, headers=None):
        response = io.BytesIO(json.dumps(body).encode())
        response.code = status
        response.headers = headers or {}
        return response

    def run_call(self):
        receipt = ResponseFile(self.path.parent / "response.json", deepline.redact)
        body, code = deepline.run(self.request, receipt.capture)
        self.assertTrue(receipt.finish(body))
        return body, json.loads(receipt.path.read_text()), budget.load_ledger(self.path)["calls"]["call-1"]

    def test_explicit_zero_charge_rejection_settles_and_keeps_raw_error(self):
        error = {"error": {"code": "VALIDATION_ERROR", "message": "Invalid input"},
                 "tool_error": {"requestId": "request-1"}, "billing": {"credits_charged": 0}}
        self.opener.open.side_effect = HTTPError("https://code.deepline.com/fixture", 422, "Invalid input",
            {"x-vercel-id": "request-1", "set-cookie": "private-cookie"}, io.BytesIO(json.dumps(error).encode()))
        body, saved, call = self.run_call()
        self.assertEqual((body["request_id"], call["state"], call["actual_credits"]), ("request-1", "settled", "0"))
        self.assertEqual(saved["provider_response"]["body"], error)
        self.assertEqual(saved["provider_response"]["http_status"], 422)
        self.assertEqual(saved["provider_response"]["headers"], {"x-vercel-id": "request-1"})
        self.assertNotIn("fixture-private-key", json.dumps(saved))
        self.assertEqual(self.opener.open.call_count, 1)

    def test_error_header_id_is_retained_but_missing_billing_stays_unknown(self):
        self.opener.open.return_value = self.response({"error": "Invalid input"}, 422, {"x-request-id": "request-2"})
        body, saved, call = self.run_call()
        self.assertEqual(body["request_id"], "request-2")
        self.assertEqual(call["state"], "pending_billing")
        self.assertIsNone(call["actual_credits"])
        self.assertNotIn("billing", body)

    def test_structured_id_takes_precedence_and_paid_charge_is_kept(self):
        self.opener.open.return_value = self.response({"error": {"message": "Provider failed"},
            "tool_error": {"requestId": "provider-request"}, "billing": {"credits_charged": .28}}, 500,
            {"x-vercel-id": "edge-request"})
        body, saved, call = self.run_call()
        self.assertEqual(body["request_id"], "provider-request")
        self.assertEqual(call["actual_credits"], "0.28")
        self.assertEqual(self.opener.open.call_count, 1)

    def test_timeout_or_connection_error_does_not_retry_or_settle(self):
        for failure in [TimeoutError(), URLError("private network detail")]:
            with self.subTest(failure=type(failure).__name__):
                self.opener.open.side_effect = failure
                response = transport.execute(deepline._validate_request(self.request))
                body, _ = deepline.normalize_response(deepline._validate_request(self.request), response)
                self.assertNotIn("billing", body)
                self.assertNotIn("private network detail", json.dumps(response))
                self.assertEqual(self.opener.open.call_count, 1)
                self.opener.open.reset_mock()

    def test_api_payload_contract_and_cli_only_fallback(self):
        self.opener.open.return_value = self.response({"status": "completed", "job_id": "job-1",
            "toolResponse": {"rawV2": {"email": "ada@example.test"}}, "billing": {"credits_charged": .3}})
        body, saved, call = self.run_call()
        wire = self.opener.open.call_args.args[0]
        self.assertEqual(json.loads(wire.data), {"payload": {"first_name": "Ada"}})
        self.assertEqual(wire.full_url, "https://code.deepline.com/api/v2/integrations/hunter_email_finder/execute")
        self.assertEqual(wire.get_header("X-deepline-tool-error-schema"), "1")
        self.assertEqual(call["actual_credits"], "0.3")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 7)
        self.assertIsNone(transport.NoRedirect().redirect_request(wire, None, 307, "Redirect", {}, "https://elsewhere.test"))
        with patch.dict(os.environ, {"DEEPLINE_API_KEY": ""}), patch.object(deepline, "_run_command", return_value=({}, 0)) as cli:
            deepline._run_validated(deepline._validate_request(self.request))
        self.assertEqual(cli.call_args.args[1][1:4], ["tools", "execute", "hunter_email_finder"])


if __name__ == "__main__":
    unittest.main()

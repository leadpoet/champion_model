"""Raw API replies share the CLI and saved-receipt normalization path."""

import copy
import json
import unittest
from unittest import mock

from test_provider_scripts import DEEPLINE


def completed(data):
    return {"status": "completed", "job_id": "fixture-job",
            "billing": {"cost_usd": 0.003, "credits_charged": 0.03},
            "result": {"data": data}}


def cli_completed(data):
    raw = completed(data)
    raw["toolResponse"] = {"rawV2": raw.pop("result")["data"], "view": "rawV2"}
    return raw


def answer():
    return completed({"answer": "Generated interpretation; not a source passage.",
                      "requestId": "fixture-exa-request", "citations": [
        {"url": "https://example.com/news", "title": "Warehouse project",
         "text": "The company connected its acquired warehouse on August 12.",
         "publishedDate": "2026-08-20"},
        {"url": "https://example.com/about", "title": "About the company"}]})


class RawDeeplineResultsTests(unittest.TestCase):
    def request(self, tool):
        return {"operation": "execute", "tool": tool, "payload": {}, "limit": 10}

    def normalize(self, tool, raw, **transport):
        result, _ = DEEPLINE.normalize_response(
            self.request(tool), {"body": raw, "exit_code": 0, **transport})
        return result

    def test_exa_citations_keep_generated_answers_separate_and_preserve_receipt(self):
        raw = answer()
        before = copy.deepcopy(raw)
        result = self.normalize("exa_answer", raw)
        self.assertEqual(raw, before)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["billing"], raw["billing"])
        self.assertEqual(result["job_id"], raw["job_id"])
        cited, title_only = result["evidence"]
        self.assertEqual(cited["evidence_url"], "https://example.com/news")
        self.assertEqual(cited["evidence_text"], raw["result"]["data"]["citations"][0]["text"])
        self.assertEqual(cited["evidence_date"], "2026-08-20")
        self.assertEqual(cited["provider_answer"], raw["result"]["data"]["answer"])
        self.assertEqual(cited["provider_request_id"], "fixture-exa-request")
        self.assertFalse(title_only.get("evidence_text"))

    def test_cli_and_saved_response_replay_agree_without_repeating_dispatch(self):
        raw = answer()
        captured = []
        with mock.patch.object(DEEPLINE, "_invoke", return_value=(0, json.dumps(raw), "")) as dispatch:
            live = DEEPLINE._run_command(self.request("exa_answer"), ["fixture"], 10, captured.append)
            replay = DEEPLINE.normalize_response(self.request("exa_answer"), captured[0])
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(live, replay)
        self.assertEqual(captured[0]["body"], raw)

    def test_empty_harvest_company_is_not_evidence_and_keeps_billing(self):
        for data, expected in [
            ({"status": 200, "element": None}, "no_results"),
            ({"status": 200, "element": None, "error": None}, "no_results"),
            ({"status": 400, "element": None,
              "error": [{"status": 404, "error": "Company not found"}]}, "provider_error"),
        ]:
            for wrap in (completed, cli_completed):
                with self.subTest(data=data, wrapper=wrap.__name__):
                    raw = wrap(data)
                    before = copy.deepcopy(raw)
                    result = self.normalize("harvestapi_get_company", raw)
                    self.assertEqual(raw, before)
                    self.assertEqual(result["status"], expected)
                    self.assertEqual(result["evidence"], [])
                    self.assertEqual(result["results"], [])
                    self.assertEqual(result["billing"], raw["billing"])
                    self.assertEqual(result["job_id"], raw["job_id"])
                    if expected == "provider_error":
                        self.assertIn("Company not found", result["error"]["message"])

    def test_cli_company_failure_replay_preserves_unknown_billing_without_dispatch(self):
        raw = cli_completed({"status": 400, "element": None,
                             "error": [{"status": 404, "error": "Company not found"}]})
        del raw["billing"]
        captured = []
        with mock.patch.object(DEEPLINE, "_invoke", return_value=(0, json.dumps(raw), "")) as dispatch:
            live = DEEPLINE._run_command(self.request("harvestapi_get_company"), ["fixture"], 10, captured.append)
            replay = DEEPLINE.normalize_response(self.request("harvestapi_get_company"), captured[0])
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(live, replay)
        self.assertEqual(live[0]["status"], "provider_error")
        self.assertNotIn("billing", live[0])
        self.assertEqual(captured[0]["body"], raw)

    def test_cli_company_wrapper_does_not_change_other_or_uncertain_results(self):
        raw = cli_completed({"status": 400, "element": None,
                             "error": [{"status": 404, "error": "Company not found"}]})
        variants = [("another_tool", raw)]
        for field, value in (("status", "running"), ("job_id", ""), ("extra", True),
                             ("result", {"data": {"name": "Unrelated result"}})):
            variants.append(("harvestapi_get_company", dict(raw, **{field: value})))
        for view in ("other", None):
            variants.append(("harvestapi_get_company", dict(raw, toolResponse={**raw["toolResponse"], "view": view})))
        for data in ({"status": 200, "element": {"name": "Example"}},
                     {"status": 400, "element": None, "error": [{"status": 429, "error": "Rate limited"}]}):
            variants.append(("harvestapi_get_company", cli_completed(data)))
        for tool, variant in variants:
            with self.subTest(tool=tool, raw=variant):
                self.assertIs(DEEPLINE._completed_execute_output(variant, tool), variant)
        for transport in ({"timed_out": True}, {"exit_code": 2, "stderr": "upstream failed"}):
            with self.subTest(transport=transport):
                result = self.normalize("harvestapi_get_company", raw, **transport)
                self.assertNotEqual(result["status"], "ok")
                self.assertFalse(result.get("results"))

    def test_unrecognized_shapes_keep_existing_parser_semantics(self):
        malformed = answer()
        malformed["result"]["data"]["citations"][0]["url"] = "not-a-url"
        extra = answer()
        extra["unrecognized"] = True
        empty = answer()
        empty["result"]["data"]["citations"] = []
        for tool, raw in [
            ("exa_answer", malformed), ("exa_answer", extra), ("exa_answer", empty),
            ("another_tool", answer()),
            ("harvestapi_get_company", completed({"status": 200, "element": {"name": "Example"}})),
            ("harvestapi_get_company", completed({"status": 400, "element": None,
                                                  "error": [{"status": 429, "error": "Rate limited"}]})),
        ]:
            with self.subTest(tool=tool, raw=raw):
                before = copy.deepcopy(raw)
                expected = DEEPLINE._execute_output(raw, tool)
                if expected["status"] == "schema_error":
                    expected["error_stage"] = "response"
                self.assertEqual(self.normalize(tool, raw), expected)
                self.assertEqual(raw, before)

    def test_failed_or_uncertain_transport_cannot_become_citation_success(self):
        for transport in ({"timed_out": True}, {"exit_code": 2, "stderr": "upstream failed"}):
            with self.subTest(transport=transport):
                result = self.normalize("exa_answer", answer(), **transport)
                self.assertNotEqual(result["status"], "ok")
                self.assertFalse(result.get("results"))


if __name__ == "__main__":
    unittest.main()

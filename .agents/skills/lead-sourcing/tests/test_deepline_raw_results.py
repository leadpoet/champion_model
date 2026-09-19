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

    def test_crustdata_job_lists_preserve_company_context_without_inventing_event_dates(self):
        job = {"company": {"basic_info": {"name": "Example Manufacturing", "primary_domain": "example.test"},
                           "headcount": {"range": "51-200", "total": 72},
                           "revenue": {"estimated": {"lower_bound_usd": 5000000}}},
               "job_details": {"title": "Office Administrator", "url": "https://example.test/jobs/123"},
               "metadata": {"date_added": "2026-09-18T21:45:22"}}
        for rows in ([job], []):
            with self.subTest(rows=len(rows)):
                raw = cli_completed({"job_listings": rows, "total_count": len(rows)})
                raw['billing'].update(pricing_status='final', settlement_status='queued')
                before = copy.deepcopy(raw)
                with mock.patch.object(DEEPLINE, '_invoke', side_effect=AssertionError('No paid replay')):
                    result = self.normalize('crustdata_v3_job_search', raw)
                self.assertEqual(result['status'], 'ok' if rows else 'no_results')
                self.assertEqual(result['billing'], raw['billing'])
                self.assertEqual(result['job_id'], raw['job_id'])
                self.assertEqual(raw, before)
                if rows:
                    row = result['results'][0]
                    self.assertEqual(row['company'], 'Example Manufacturing')
                    self.assertEqual(row['domain'], 'example.test')
                    self.assertEqual(row['company_details'], job['company'])
                    self.assertEqual(row['job_details'], job['job_details'])
                    self.assertEqual(row['metadata'], job['metadata'])
                    self.assertEqual(row['evidence_url'], job['job_details']['url'])
                    self.assertIsNone(row['evidence_date'])
                    self.assertNotIn('event_date', row)
                    self.assertEqual(row['content_kind'], 'unverified')

    def test_crustdata_job_lists_do_not_hide_malformed_or_failed_responses(self):
        for payload in ({}, {'job_listings': None}, {'job_listings': 'bad'},
                        {'job_listings': ['bad']}, {'job_listings': [], 'success': False}):
            with self.subTest(payload=payload):
                result = self.normalize('crustdata_v3_job_search', cli_completed(payload))
                self.assertNotIn(result['status'], {'ok', 'no_results'})
                self.assertFalse(result['results'])
        raw = cli_completed({'job_listings': []})
        raw['status'] = 'failed'
        self.assertEqual(self.normalize('crustdata_v3_job_search', raw)['status'], 'provider_error')

    def test_firecrawl_search_lists_preserve_pages_snippets_and_exact_billing(self):
        web = {"url": "https://example.com/funding", "title": "Funding",
               "description": "Search summary", "markdown": "The company raised funding.",
               "metadata": {"sourceURL": "https://example.com/funding", "statusCode": 200}}
        news = {"url": "https://example.com/news", "title": "News", "snippet": "Discovery only"}
        for data in ({"web": [web]}, {"web": [], "news": [news]}, {"web": [web], "news": [news]},
                     {"web": [], "news": []}):
            for billing in ({"credits_charged": 0.13001, "cost_usd": 0.013001,
                             "pricing_status": "final", "settlement_status": "queued"}, None):
                with self.subTest(data=list(data), billing=billing):
                    raw = cli_completed({"data": data, "meta": {"status": 200, "success": True}})
                    raw["request_id"] = "original-request"
                    if billing is None:
                        del raw["billing"]
                    else:
                        raw["billing"] = billing
                    # A bounded CLI preview must not hide news or other full rows.
                    raw["output_preview"] = {"listSourcePath": "toolResponse.rawV2.data.web",
                                             "preview": data["web"][:1]}
                    before = copy.deepcopy(raw)
                    with mock.patch.object(DEEPLINE, "_invoke", side_effect=AssertionError("No paid replay")):
                        result = self.normalize("firecrawl_search", raw)
                    self.assertEqual(raw, before)
                    expected = data["web"] + data.get("news", [])
                    self.assertEqual(result["status"], "ok" if expected else "no_results")
                    self.assertEqual([r["evidence_url"] for r in result["results"]], [r["url"] for r in expected])
                    self.assertEqual(result.get("billing"), billing)
                    self.assertEqual(result["job_id"], raw["job_id"])
                    self.assertEqual(result["request_id"], raw["request_id"])
                    for original, row in zip(expected, result["results"]):
                        self.assertEqual(row["content_kind"], "captured_page" if "markdown" in original else "search_excerpt")
                        self.assertEqual(row["evidence_text"], original.get("markdown", original.get("snippet")))
                    del raw["output_preview"]
                    native = self.normalize("firecrawl_search", raw)
                    for key in ("status", "results", "billing", "job_id", "request_id"):
                        self.assertEqual(native.get(key), result.get(key))

    def test_firecrawl_search_malformed_and_failed_responses_do_not_become_success(self):
        row = {"url": "https://example.com/news", "title": "News"}
        for data in ({}, {"images": [row]}, {"web": None}, {"web": ["bad"]},
                     {"web": [{"title": "Missing URL"}]}, {"web": [dict(row, url="not-a-url")]},
                     {"news": [dict(row, title="")]}, {"web": [], "news": "bad"}):
            with self.subTest(data=data):
                result = self.normalize("firecrawl_search", cli_completed({
                    "data": data, "meta": {"status": 200, "success": True}}))
                self.assertNotIn(result["status"], {"ok", "no_results"})
                self.assertFalse(result["results"])
        for meta in ({"status": 500, "success": False, "error": "Upstream failed"},
                     {"status": 200, "success": False}, {}):
            raw = cli_completed({"data": {"web": [row]}, "meta": meta})
            self.assertNotIn(self.normalize("firecrawl_search", raw)["status"], {"ok", "no_results"})
        for level in ("wrapper", "response", "raw"):
            raw = cli_completed({"data": {"web": [row]}, "meta": {"status": 200, "success": True}})
            part = raw if level == "wrapper" else raw["toolResponse"] if level == "response" else raw["toolResponse"]["rawV2"]
            part["success"] = False
            self.assertNotIn(self.normalize("firecrawl_search", raw)["status"], {"ok", "no_results"})
        raw = cli_completed({"data": {"web": [row]}, "meta": {"status": 200, "success": True}})
        for transport in ({"timed_out": True}, {"exit_code": 2, "stderr": "upstream failed"}):
            result = self.normalize("firecrawl_search", raw, **transport)
            self.assertNotEqual(result["status"], "ok")
            self.assertEqual(result["billing"], raw["billing"])
        raw["status"] = "failed"
        self.assertEqual(self.normalize("firecrawl_search", raw)["status"], "provider_error")

    def test_firecrawl_failures_preserve_diagnostics_and_billing_without_replay(self):
        for failure, status in (({"status": 500, "error": "Upstream failed"}, "provider_error"),
                                ({"status": 429, "error": "Quota exceeded"}, "rate_limited"),
                                ({"success": False, "message": "Upstream failed"}, "provider_error")):
            for level in ("meta", "raw", "response"):
                for billed in (True, False):
                    with self.subTest(failure=failure, level=level, billed=billed):
                        raw = cli_completed({"data": {"web": []}, "meta": {"status": 200, "success": True}})
                        part = raw["toolResponse"] if level == "response" else raw["toolResponse"]["rawV2"]
                        if level == "meta":
                            part = part["meta"]
                        part.update(failure)
                        if not billed:
                            del raw["billing"]
                        before = copy.deepcopy(raw)
                        with mock.patch.object(DEEPLINE, "_invoke", side_effect=AssertionError("No paid replay")):
                            result = self.normalize("firecrawl_search", raw)
                        self.assertEqual(result["status"], status)
                        self.assertEqual(result["error"]["message"], failure.get("error", failure.get("message")))
                        self.assertEqual(result["results"], [])
                        self.assertEqual(result.get("billing"), raw.get("billing"))
                        self.assertEqual(result["job_id"], raw["job_id"])
                        self.assertEqual(raw, before)
                        if level != "meta":
                            del raw["toolResponse"]["rawV2"]["meta"]
                            self.assertEqual(self.normalize("firecrawl_search", raw), result)

    def test_serper_organic_results_remain_search_evidence_and_keep_billing(self):
        rows = [{"title": "Example announces partnership", "link": "https://example.com/news",
                 "snippet": "Example announced a planned partnership.", "position": 1}]
        for organic in (rows, []):
            raw = cli_completed({"data": {"organic": organic}, "meta": {"status": 200}})
            before = copy.deepcopy(raw)
            with mock.patch.object(DEEPLINE, "_invoke", side_effect=AssertionError("Replay must not dispatch")):
                result = self.normalize("serper_google_search", raw)
            self.assertEqual(raw, before)
            self.assertEqual(result["status"], "ok" if organic else "no_results")
            self.assertEqual(result["billing"], raw["billing"])
            self.assertEqual(result["job_id"], raw["job_id"])
            if organic:
                row = result["results"][0]
                self.assertEqual(row["evidence_url"], rows[0]["link"])
                self.assertEqual(row["evidence_text"], rows[0]["snippet"])
                self.assertEqual(row["content_kind"], "search_excerpt")

    def test_serper_failed_or_unknown_envelopes_do_not_become_search_success(self):
        for data, meta in [(None, {"status": 200}), ({"organic": "bad"}, {"status": 200}),
                           ({"organic": ["bad"]}, {"status": 200}), ({"organic": []}, {}),
                           ({"organic": []}, {"status": 500}),
                           ({"organic": []}, {"status": 200, "success": False})]:
            with self.subTest(data=data, meta=meta):
                result = self.normalize("serper_google_search", cli_completed({"data": data, "meta": meta}))
                self.assertNotIn(result["status"], {"ok", "no_results"})
                self.assertFalse(result["results"])

    def test_discolike_domain_response_is_bound_to_the_requested_homepage(self):
        raw = cli_completed({"language": "en", "text": "Example provides electrical construction services."})
        before = copy.deepcopy(raw)
        request = dict(self.request("discolike_extract"), payload={"domain": "Example.com"})
        result, _ = DEEPLINE.normalize_response(request, {"body": raw, "exit_code": 0})
        self.assertEqual(raw, before)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results"][0]["evidence_url"], "https://example.com")
        self.assertEqual(result["results"][0]["content_kind"], "captured_page")
        self.assertEqual(result["billing"], raw["billing"])
        for domain in (None, "", "example.com/page", "user@example.com", "example.com?url=other.test", "bad domain.com"):
            request["payload"] = {"domain": domain}
            result, _ = DEEPLINE.normalize_response(request, {"body": raw, "exit_code": 0})
            self.assertNotEqual(result["status"], "ok")
        request["payload"] = {"domain": "example.com"}
        raw["status"] = "failed"
        result, _ = DEEPLINE.normalize_response(request, {"body": raw, "exit_code": 0})
        self.assertNotEqual(result["status"], "ok")

    def test_fullenrich_people_preserve_current_identity_pagination_and_billing(self):
        person = {"full_name": "Ada Example", "employment": {"current": {
            "is_current": True, "title": "Head of Claims",
            "company": {"name": "Example", "domain": "example.test"}}},
            "social_profiles": {"professional_network": {"url": "https://www.linkedin.com/in/ada-example"}}}
        for count in (0, 5, 20):
            with self.subTest(count=count):
                raw = cli_completed({"people": [person] * count,
                    "metadata": {"total": 30, "offset": 0, "search_after": "fixture-cursor"}})
                before = copy.deepcopy(raw)
                request = dict(self.request("fullenrich_people_search"), limit=25)
                with mock.patch.object(DEEPLINE, "_invoke", side_effect=AssertionError("Replay must not dispatch")):
                    result, _ = DEEPLINE.normalize_response(request, {"body": raw, "exit_code": 0})
                self.assertEqual(raw, before)
                self.assertEqual(result["status"], "ok" if count else "no_results")
                self.assertEqual(len(result["results"]), count)
                self.assertEqual(result["billing"], raw["billing"])
                self.assertEqual(result["job_id"], raw["job_id"])
                self.assertEqual(result["pagination"]["next_cursor"], "fixture-cursor")
                if count:
                    row = result["results"][0]
                    self.assertEqual(row["contact_name"], "Ada Example")
                    self.assertEqual(row["contact_title"], "Head of Claims")
                    self.assertEqual(row["domain"], "example.test")
                    self.assertEqual(row["employment"], person["employment"])

    def test_fullenrich_past_employment_is_not_a_current_role(self):
        person = {"full_name": "Ada Example", "headline": "Claims Director",
                  "employment": {"current": {"is_current": False, "title": "Claims Director",
                      "company": {"name": "Former Employer", "domain": "former.test"}}}}
        result = self.normalize("fullenrich_people_search", cli_completed({"people": [person]}))
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["results"][0]["contact_title"])
        self.assertIsNone(result["results"][0]["domain"])
        self.assertEqual(result["results"][0]["employment"], person["employment"])

    def test_fullenrich_malformed_and_failed_results_do_not_become_people(self):
        for people in (None, "not-a-list", ["not-a-person"]):
            raw = cli_completed({"people": people})
            result = self.normalize("fullenrich_people_search", raw)
            self.assertEqual(result["status"], "schema_error")
            self.assertFalse(result["results"])
        raw = cli_completed({"people": [{"full_name": "Ada Example"}]})
        raw["status"] = "failed"
        self.assertEqual(self.normalize("fullenrich_people_search", raw)["status"], "provider_error")
        raw["status"] = "completed"
        for transport in ({"timed_out": True}, {"exit_code": 2, "stderr": "upstream failed"}):
            self.assertNotEqual(self.normalize("fullenrich_people_search", raw, **transport)["status"], "ok")

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

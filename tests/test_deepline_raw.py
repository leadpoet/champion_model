"""Exact, offline contracts for Arena's observed Deepline raw envelopes."""

import copy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / ".agents/skills/lead-sourcing/scripts")]

import deepline
from tyche_arena.deepline_raw import for_normalizer


def response(data, *, billing=True):
    body = {"status": "completed", "job_id": "job-observed", "result": {"data": data}}
    if billing:
        body["billing"] = {"credits_charged": 0.07, "cost_usd": 0.007}
    return {"body": body, "exit_code": 0, "stderr": "", "arena": {"status": 200, "headers": {}}}


def request(tool, payload=None):
    return {"operation": "execute", "tool": tool, "payload": payload or {}, "limit": 10}


def test_exa_answer_maps_only_citations_to_evidence_and_keeps_answer_separate():
    answer = {"conclusion": "Generated synthesis", "facility_evidence": [{"finding": "Generated"}]}
    raw = response({"answer": answer, "citations": [
        {"id": "source-1", "url": "https://example.com/news", "title": "News",
         "text": "Primary source text", "publishedDate": "2026-08-12"},
        {"id": "source-2", "url": "https://example.com/about", "title": "About"},
    ], "requestId": "request-observed"})
    before = copy.deepcopy(raw)

    adapted = for_normalizer(request("exa_answer", {"query": "question"}), raw)

    assert raw == before and adapted is not raw
    assert adapted["body"]["billing"] == raw["body"]["billing"]
    assert adapted["body"]["job_id"] == "job-observed"
    first, second = adapted["body"]["results"]
    assert first["evidence_url"] == "https://example.com/news"
    assert first["evidence_text"] == "Primary source text"
    assert first["evidence_date"] == "2026-08-12"
    assert first["provider_answer"] == answer
    assert first["evidence_text"] != answer["conclusion"]
    assert "evidence_text" not in second
    assert second["title"] == "About"
    assert all(row["source_kind"] == "provider_citation" for row in (first, second))
    assert all("company" not in row and "domain" not in row for row in (first, second))

    normalized, _ = deepline.normalize_response(request("exa_answer", {"query": "question"}), adapted)
    title_only = normalized["results"][1]
    assert title_only["evidence_text"] is None
    assert title_only["company"] is None and title_only["domain"] is None
    assert normalized["results"][0]["provider_answer"] == answer
    assert normalized["results"][0]["evidence_text"] != answer["conclusion"]


def test_generic_http_raw_body_without_upstream_status_remains_fail_closed():
    raw = response("<html>Access denied</html>", billing=False)
    req = request("generic_http_request", {"url": "https://example.com/source", "method": "GET"})

    assert for_normalizer(req, raw) is raw
    normalized, _ = deepline.normalize_response(req, raw)
    assert normalized["status"] == "schema_error"
    assert normalized["results"] == []


@pytest.mark.parametrize(
    "data,status,rows,normalized_status",
    [
        ({"status": 200, "element": None}, "no_results", [], "no_results"),
        ({"status": 200, "element": None, "error": None}, "no_results", [], "no_results"),
        ({"status": 400, "element": None, "error": [{"status": 404, "error": "not found"}]},
         "ok", [{"status": 400, "error": [{"status": 404, "error": "not found"}]}], "provider_error"),
    ],
)
def test_harvest_null_outcomes_are_determinate_without_positive_company_evidence(
        data, status, rows, normalized_status):
    raw = response(data)
    req = request("harvestapi_get_company", {"url": "https://linkedin.com/company/example"})
    adapted = for_normalizer(req, raw)
    assert adapted["body"]["status"] == status
    assert adapted["body"]["results"] == rows
    assert adapted["body"]["billing"] == raw["body"]["billing"]
    normalized, _ = deepline.normalize_response(req, adapted)
    assert normalized["status"] == normalized_status
    assert normalized["results"] == []


@pytest.mark.parametrize(
    "tool,payload,data",
    [
        ("exa_answer", {"query": "question"},
         {"answer": "answer", "citations": [], "requestId": "request"}),
        ("exa_answer", {"query": "question"},
         {"answer": "answer", "citations": [{"url": "https://example.com", "text": "text", "extra": True}], "requestId": "request"}),
        ("harvestapi_get_company", {"url": "https://linkedin.com/company/example"},
         {"status": 500, "element": None, "error": [{"status": 500, "error": "failed"}]}),
        ("harvestapi_get_company", {"url": "https://linkedin.com/company/example"},
         {"status": 200, "element": {"name": "Do not intercept valid rows"}, "error": None}),
        ("harvestapi_get_profile", {"url": "https://linkedin.com/in/example"},
         {"status": 200, "element": None, "error": None}),
    ],
)
def test_unproved_or_out_of_scope_shapes_are_unchanged(tool, payload, data):
    raw = response(data)
    assert for_normalizer(request(tool, payload), raw) is raw


def test_non_success_response_and_nonexact_outer_envelope_are_unchanged():
    raw = response("body")
    raw["exit_code"] = 2
    assert for_normalizer(request("generic_http_request", {"url": "https://example.com"}), raw) is raw
    extra = response("body")
    extra["body"]["unexpected"] = True
    assert for_normalizer(request("generic_http_request", {"url": "https://example.com"}), extra) is extra
    catalog = response("body")
    assert for_normalizer({"operation": "describe", "tool": "generic_http_request"}, catalog) is catalog


@pytest.mark.parametrize(
    "tool,data,expected_status,expected_count",
    [
        ("firecrawl_scrape", {"markdown": "Observed page", "metadata": {
            "statusCode": 200, "sourceURL": "https://example.com/page", "url": "https://example.com/page"}}, "ok", 1),
        ("exa_search", {"results": [{"url": "https://example.com/a", "text": "Observed excerpt"}]}, "ok", 1),
        ("harvestapi_get_company", {"status": 200, "element": {
            "name": "Example", "linkedinUrl": "https://www.linkedin.com/company/example",
            "employeeCountRange": {"start": 11, "end": 50}}, "error": None}, "ok", 1),
        ("harvestapi_get_profile", {"status": 200, "element": {
            "firstName": "A", "lastName": "Person", "linkedinUrl": "https://www.linkedin.com/in/a-person",
            "headline": "Director"}, "error": None}, "ok", 1),
    ],
)
def test_existing_recognized_result_data_shapes_bypass_adapter_and_still_normalize(
        tool, data, expected_status, expected_count):
    raw = response(data)
    req = request(tool, {"url": "https://example.com"})
    assert for_normalizer(req, raw) is raw
    normalized, _ = deepline.normalize_response(req, raw)
    assert normalized["status"] == expected_status
    assert len(normalized["results"]) == expected_count

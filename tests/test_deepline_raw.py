"""Exact, offline contracts for Arena's observed Deepline raw envelopes."""

import copy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / ".agents/skills/lead-sourcing/scripts")]

import deepline


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

    normalized, _ = deepline.normalize_response(
        request("exa_answer", {"query": "question"}), raw)

    assert raw == before
    assert normalized["billing"] == raw["body"]["billing"]
    assert normalized["job_id"] == "job-observed"
    first, second = normalized["results"]
    assert first["evidence_url"] == "https://example.com/news"
    assert first["evidence_text"] == "Primary source text"
    assert first["evidence_date"] == "2026-08-12"
    assert first["provider_answer"] == answer
    assert first["evidence_text"] != answer["conclusion"]
    assert second["evidence_text"] is None
    assert second["title"] == "About"
    assert all(row["source_kind"] == "provider_citation" for row in (first, second))
    assert all(row["company"] is None and row["domain"] is None for row in (first, second))

    title_only = normalized["results"][1]
    assert title_only["evidence_text"] is None
    assert title_only["company"] is None and title_only["domain"] is None
    assert normalized["results"][0]["provider_answer"] == answer
    assert normalized["results"][0]["evidence_text"] != answer["conclusion"]


def test_generic_http_raw_body_without_upstream_status_remains_fail_closed():
    raw = response("<html>Access denied</html>", billing=False)
    req = request("generic_http_request", {"url": "https://example.com/source", "method": "GET"})
    before = copy.deepcopy(raw)

    normalized, _ = deepline.normalize_response(req, raw)
    assert raw == before
    assert normalized["status"] == "schema_error"
    assert normalized["results"] == []


@pytest.mark.parametrize(
    "data,normalized_status",
    [
        ({"status": 200, "element": None}, "no_results"),
        ({"status": 200, "element": None, "error": None}, "no_results"),
        ({"status": 400, "element": None, "error": [{"status": 404, "error": "not found"}]},
         "provider_error"),
    ],
)
def test_harvest_null_outcomes_are_determinate_without_positive_company_evidence(
        data, normalized_status):
    raw = response(data)
    req = request("harvestapi_get_company", {"url": "https://linkedin.com/company/example"})
    before = copy.deepcopy(raw)
    normalized, _ = deepline.normalize_response(req, raw)
    assert raw == before
    assert normalized["status"] == normalized_status
    assert normalized["results"] == []
    assert normalized["billing"] == raw["body"]["billing"]
    assert normalized["job_id"] == "job-observed"


@pytest.mark.parametrize(
    "tool,payload,data,expected_status",
    [
        ("exa_answer", {"query": "question"},
         {"answer": "answer", "citations": [], "requestId": "request"}, "schema_error"),
        ("exa_answer", {"query": "question"},
         {"answer": "answer", "citations": [{"url": "https://example.com", "text": "text", "extra": True}], "requestId": "request"}, "schema_error"),
        ("harvestapi_get_company", {"url": "https://linkedin.com/company/example"},
         {"status": 500, "element": None, "error": [{"status": 500, "error": "failed"}]}, "schema_error"),
        ("harvestapi_get_company", {"url": "https://linkedin.com/company/example"},
         {"status": 200, "element": {"name": "Do not intercept valid rows"}, "error": None}, "schema_error"),
        ("harvestapi_get_profile", {"url": "https://linkedin.com/in/example"},
         {"status": 200, "element": None, "error": None}, "no_results"),
    ],
)
def test_unproved_or_out_of_scope_shapes_keep_existing_parser_outcomes(
        tool, payload, data, expected_status):
    raw = response(data)
    before = copy.deepcopy(raw)
    normalized, _ = deepline.normalize_response(request(tool, payload), raw)
    assert raw == before
    assert normalized["status"] == expected_status
    assert normalized["results"] == []


def test_non_success_response_and_nonexact_outer_envelope_keep_existing_outcomes():
    raw = response("body")
    raw["exit_code"] = 2
    before = copy.deepcopy(raw)
    normalized, _ = deepline.normalize_response(
        request("generic_http_request", {"url": "https://example.com"}), raw)
    assert raw == before and normalized["status"] == "provider_error"
    extra = response("body")
    extra["body"]["unexpected"] = True
    before = copy.deepcopy(extra)
    normalized, _ = deepline.normalize_response(
        request("generic_http_request", {"url": "https://example.com"}), extra)
    assert extra == before and normalized["status"] == "schema_error"
    catalog = response("body")
    before = copy.deepcopy(catalog)
    normalized, _ = deepline.normalize_response(
        {"operation": "describe", "tool": "generic_http_request"}, catalog)
    assert catalog == before and normalized["status"] == "schema_error"


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
def test_existing_recognized_result_data_shapes_still_normalize(
        tool, data, expected_status, expected_count):
    raw = response(data)
    req = request(tool, {"url": "https://example.com"})
    before = copy.deepcopy(raw)
    normalized, _ = deepline.normalize_response(req, raw)
    assert raw == before
    assert normalized["status"] == expected_status
    assert len(normalized["results"]) == expected_count

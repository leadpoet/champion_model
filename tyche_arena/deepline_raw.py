"""Adapt exact Arena Deepline raw envelopes for the existing model normalizer."""

import copy
from urllib.parse import urlparse


_OUTER_KEYS = {"billing", "job_id", "result", "status"}
_EXA_DATA_KEYS = {"answer", "citations", "requestId"}
_EXA_CITATION_KEYS = {
    "author", "favicon", "id", "image", "publishedDate", "text", "title", "url",
}


def _http_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value.strip()


def _raw_envelope(response):
    if not isinstance(response, dict) or response.get("exit_code") != 0:
        return None
    body = response.get("body")
    if (not isinstance(body, dict) or set(body) - _OUTER_KEYS
            or body.get("status") != "completed"
            or not isinstance(body.get("job_id"), str) or not body["job_id"].strip()
            or not isinstance(body.get("result"), dict)
            or set(body["result"]) != {"data"}):
        return None
    return body, body["result"]["data"]


def _metadata(body):
    return {key: copy.deepcopy(body[key]) for key in ("billing", "job_id") if key in body}


def _exa_rows(data):
    if not isinstance(data, dict) or set(data) != _EXA_DATA_KEYS:
        return None
    answer, citations, request_id = data["answer"], data["citations"], data["requestId"]
    if (not isinstance(answer, (str, dict)) or not answer
            or not isinstance(request_id, str) or not request_id.strip()
            or not isinstance(citations, list) or not citations or len(citations) > 100):
        return None
    rows = []
    for citation in citations:
        if (not isinstance(citation, dict) or set(citation) - _EXA_CITATION_KEYS
                or not (url := _http_url(citation.get("url")))):
            return None
        text, title = citation.get("text"), citation.get("title")
        if not any(isinstance(value, str) and value.strip() for value in (text, title)):
            return None
        row = copy.deepcopy(citation)
        row.update(evidence_url=url, source_kind="provider_citation")
        if isinstance(text, str) and text.strip():
            row["evidence_text"] = text.strip()
        published = citation.get("publishedDate")
        if isinstance(published, str) and published.strip():
            row["evidence_date"] = published.strip()
        rows.append(row)
    # Keep the generated answer available for research context, but separate
    # from citation text so review cannot attribute it to a cited page.
    rows[0]["provider_answer"] = copy.deepcopy(answer)
    rows[0]["provider_request_id"] = request_id
    return rows


def _harvest_outcome(data):
    if not isinstance(data, dict) or set(data) not in ({"element", "status"}, {"element", "error", "status"}):
        return None
    if data.get("element") is None and data.get("status") == 200 and data.get("error") is None:
        return "no_results", []
    errors = data.get("error")
    if (data.get("element") is None and data.get("status") == 400
            and isinstance(errors, list) and len(errors) == 1
            and isinstance(errors[0], dict) and set(errors[0]) == {"error", "status"}
            and errors[0].get("status") == 404
            and isinstance(errors[0].get("error"), str) and errors[0]["error"].strip()):
        # Preserve the proven provider failure for the existing Harvest
        # company-failure classifier.  It is not positive company evidence.
        return "ok", [{"status": 400, "error": copy.deepcopy(errors)}]
    return None


def for_normalizer(request, response):
    """Return a derived response for normalization, or the original object."""

    envelope = _raw_envelope(response)
    if envelope is None or not isinstance(request, dict) or request.get("operation") != "execute":
        return response
    body, data = envelope
    tool = request.get("tool")
    if tool == "exa_answer":
        rows = _exa_rows(data)
        outcome = ("ok", rows) if rows is not None else None
    elif tool == "harvestapi_get_company":
        outcome = _harvest_outcome(data)
    else:
        outcome = None
    if outcome is None:
        return response
    status, rows = outcome
    adapted = copy.deepcopy(response)
    adapted["body"] = {"status": status, "results": rows, **_metadata(body)}
    return adapted


def install_normalizer(module):
    """Apply the adapter to every first-pass and saved-receipt normalization."""

    original = module.normalize_response
    if getattr(original, "_tyche_raw_result_adapter", False):
        return

    def normalize_response(request, response):
        return original(request, for_normalizer(request, response))

    normalize_response._tyche_raw_result_adapter = True
    module.normalize_response = normalize_response

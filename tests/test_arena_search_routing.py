from __future__ import annotations

import time
import json

import httpx
import pytest

from arena_transport import ArenaToolClient


def test_discovery_query_preserves_employee_bands_for_natural_language_filters() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={"result": {"data": []}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        tools = ArenaToolClient(client=client)
        tools.search_companies({
            "query": "connected device manufacturer",
            "industry": "Hardware",
            "geography": "United States, South",
            "employee_count": ["51-200", "201-500"],
        })
        tools.search_companies({"query": "connected device manufacturer"})

    first = json.loads(requests[0].content)["payload"]
    assert first["query"].endswith("Employees: 51-200 or 201-500")
    assert "Industry: Hardware" in first["query"]
    assert "Headquarters: United States, South" in first["query"]
    assert first["headcount"] == ["51-200", "201-500"]
    assert "Employees:" not in json.loads(requests[1].content)["payload"]["query"]
    assert tools.deepline_calls == 2


@pytest.mark.parametrize(
    "mode",
    [
        "search",
        "news",
        "jobs",
    ],
)
def test_search_web_uses_scrapingdog_without_spending_deepline(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "organic_results": [
                    {
                        "title": "Verified event",
                        "link": "https://example.com/event#details",
                        "snippet": "Published evidence",
                        "date": "2026-09-01",
                    }
                ]
            },
        )

    monkeypatch.setenv("LAB_ARENA_EVALUATION_DATE", "2026-09-13")
    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))

    result = tools.search_web(
        {"query": "Example event", "mode": mode, "limit": 1, "recency_days": 30}
    )

    assert result == {
        "results": [
            {
                "title": "Verified event",
                "date": "2026-09-01",
                "snippet": "Published evidence",
                "source": "ScrapingDog",
                "url": "https://example.com/event",
            }
        ],
        "count": 1,
        "mode": mode,
    }
    assert tools.scrapingdog_calls == 1
    assert tools.deepline_calls == 0
    assert requests[0].url.scheme == "http"
    assert requests[0].url.host == "api.scrapingdog.com"
    assert requests[0].url.path == "/google"
    params = dict(requests[0].url.params)
    assert set(params) == {"query", "country"}
    assert params["country"] == "us"
    assert params["query"].endswith(
        "after:2026-08-14 before:2026-09-14"
    )
    if mode == "jobs":
        assert "(jobs OR careers OR hiring)" in params["query"]
    assert "api_key" not in request_headers(requests[0])
    assert "authorization" not in request_headers(requests[0])


def request_headers(request: httpx.Request) -> set[str]:
    return {name.lower() for name in request.headers}


@pytest.mark.parametrize("limit, expected_count", [(None, 10), (10, 10), (99, 10), (2, 2)])
def test_search_retains_lower_ranked_candidates_in_one_bounded_call(
    limit: int | None, expected_count: int,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"organic_results": [
            {"title": f"Candidate {index}", "link": f"https://example.com/{index}"}
            for index in range(1, 13)
        ]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        tools = ArenaToolClient(client=client)
        arguments = {"query": "candidate discovery"}
        if limit is not None:
            arguments["limit"] = limit
        result = tools.search_web(arguments)

    assert result["count"] == expected_count
    assert result["results"][-1]["url"] == f"https://example.com/{expected_count}"
    if expected_count == 10:
        assert result["results"][8]["title"] == "Candidate 9"
    assert tools.scrapingdog_calls == 1
    assert tools.deepline_calls == 0


@pytest.mark.parametrize("scrapingdog_outcome", ["empty", "error"])
def test_search_web_falls_back_to_exa_after_scrapingdog_miss(
    scrapingdog_outcome: str,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.scrapingdog.com":
            if scrapingdog_outcome == "error":
                return httpx.Response(
                    503,
                    request=request,
                    json={"error": {"code": "provider_unavailable"}},
                )
            return httpx.Response(200, request=request, json={"organic_results": []})
        assert request.url.path.endswith("/exa_search/execute")
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "title": "Fallback evidence",
                        "url": "https://example.com/fallback",
                        "publishedDate": "2026-09-02",
                        "highlights": ["Verified fallback"],
                    }
                ]
            },
        )

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))

    result = tools.search_web({"query": "Example event", "limit": 1})

    assert result["results"] == [
        {
            "title": "Fallback evidence",
            "date": "2026-09-02",
            "snippet": "Verified fallback",
            "source": "Exa",
            "url": "https://example.com/fallback",
        }
    ]
    assert [request.url.host for request in requests] == [
        "api.scrapingdog.com",
        "code.deepline.com",
    ]
    assert tools.scrapingdog_calls == 1
    assert tools.deepline_calls == 1


@pytest.mark.parametrize("error_code", ["budget_refused", "budget_exhausted"])
def test_scrapingdog_budget_refusal_stops_both_provider_routes(error_code: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            402,
            request=request,
            json={"error": {"code": error_code}},
        )

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))

    with pytest.raises(RuntimeError, match=error_code):
        tools.search_web({"query": "Example event"})
    with pytest.raises(RuntimeError, match=error_code):
        tools.search_companies({"query": "Example companies"})

    assert len(requests) == 1
    assert tools.scrapingdog_calls == 1
    assert tools.deepline_calls == 0
    assert tools.scrapingdog_limit_reached is True
    assert tools.deepline_limit_reached is True


def test_search_web_preserves_expired_arena_deadline() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={"organic_results": []})

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
    tools.request_deadline = time.monotonic() - 1

    with pytest.raises(RuntimeError, match="Arena provider deadline reached"):
        tools.search_web({"query": "Example event"})

    assert requests == []
    assert tools.scrapingdog_calls == 1
    assert tools.deepline_calls == 0


def test_scrapingdog_local_cap_falls_back_to_available_deepline() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/exa_search/execute")
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "title": "Exa result",
                        "url": "https://example.com/exa-result",
                    }
                ]
            },
        )

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
    tools.scrapingdog_calls = 30

    result = tools.search_web({"query": "Example event"})

    assert result["results"][0]["source"] == "Exa"
    assert len(requests) == 1
    assert tools.scrapingdog_limit_reached is True
    assert tools.deepline_calls == 1


def test_empty_scrapingdog_result_does_not_spend_reserved_deepline_calls() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.scrapingdog.com"
        return httpx.Response(200, request=request, json={"organic_results": []})

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
    tools.deepline_call_limit = 20
    tools.deepline_calls = 20

    with pytest.raises(RuntimeError, match="Arena Deepline call limit reached"):
        tools.search_web({"query": "Example event"})

    assert len(requests) == 1
    assert tools.scrapingdog_calls == 1
    assert tools.deepline_calls == 20


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/html", b"%PDF-1.7\n\x00\x7f binary document"),
        ("application/pdf", b"not a text page"),
    ],
)
def test_fetch_page_rejects_binary_scrapingdog_response(
    content_type: str, body: bytes
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": content_type},
            content=body,
        )

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
    tools.deepline_calls = tools.deepline_call_limit

    with pytest.raises(RuntimeError, match="non-text page content"):
        tools.fetch_page({"url": "https://example.com/document"})

    assert len(requests) == 1
    assert requests[0].url.path == "/scrape"
    assert tools.scrapingdog_calls == 1


def test_fetch_page_removes_strict_control_characters_from_html() -> None:
    article = (("Verified launch evidence \x00 with details. " * 12) + "\x7f").encode()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><title>Launch\x7f update</title><body>" + article + b"</body></html>",
        )

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
    tools.deepline_calls = tools.deepline_call_limit

    page = tools.fetch_page({"url": "https://example.com/launch"})

    assert page["source"] == "ScrapingDog"
    assert page["title"] == "Launch update"
    assert "Verified launch evidence" in page["text"]
    for value in (page["title"], page["text"]):
        assert not any(
            ord(character) < 32 and character not in "\t\n\r"
            or ord(character) == 127
            for character in value
        )


def test_fetch_page_exposes_only_five_valid_untrusted_company_hints() -> None:
    article = "Verified first-party company evidence with current details. " * 12
    hrefs = [
        "https://evil.linkedin.com/company/wrong-host",
        "https://linkedin.com.evil.example/company/wrong-host",
        "https://www.linkedin.com/in/a-person",
        "https://www.linkedin.com/company/query-is-not-allowed?view=all",
        "https://www.linkedin.com/company/nium-global/",
        "https://linkedin.com/company/example-two",
        "https://www.linkedin.com/company/example-three",
        "https://www.linkedin.com/company/example-four",
        "https://www.linkedin.com/company/example-five",
        "https://www.linkedin.com/company/example-six",
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "code.deepline.com":
            return httpx.Response(
                200,
                request=request,
                json={"result": {"data": {"results": []}}},
            )
        links = "".join(f'<a href="{href}">source</a>' for href in hrefs)
        return httpx.Response(
            200,
            request=request,
            text=f"<html><title>Nium resources</title><body>{article}{links}</body></html>",
        )

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
    page = tools.fetch_page({"url": "https://www.nium.com/resources"})

    assert page["untrusted_linkedin_company_url_hints"] == [
        "https://www.linkedin.com/company/nium-global",
        "https://www.linkedin.com/company/example-two",
        "https://www.linkedin.com/company/example-three",
        "https://www.linkedin.com/company/example-four",
        "https://www.linkedin.com/company/example-five",
    ]


def test_fetch_page_exa_company_url_is_an_untrusted_hint() -> None:
    evidence = (
        "Verified company profile evidence with current details. " * 12
        + " Source https://www.linkedin.com/company/example/"
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "result": {
                    "data": {
                        "results": [
                            {
                                "url": "https://example.com/company-profile",
                                "title": "Example | LinkedIn",
                                "text": evidence,
                            }
                        ]
                    }
                }
            },
        )

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
    page = tools.fetch_page({"url": "https://example.com/company-profile"})

    assert page["source"] == "Exa"
    assert page["untrusted_linkedin_company_url_hints"] == [
        "https://www.linkedin.com/company/example"
    ]


def test_fetch_page_removes_exa_controls_and_preserves_paragraphs() -> None:
    evidence = (
        ("First paragraph has verified company evidence. " * 6)
        + "\n\n"
        + ("Second paragraph has current intent evidence. " * 6)
        + "\x00\x7f"
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/exa_contents/execute")
        return httpx.Response(
            200,
            request=request,
            json={
                "result": {
                    "data": {
                        "results": [
                            {
                                "url": "https://example.com/launch",
                                "title": "Launch\x7f update",
                                "text": evidence,
                            }
                        ]
                    }
                }
            },
        )

    tools = ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))
    page = tools.fetch_page({"url": "https://example.com/launch"})

    assert page["source"] == "Exa"
    assert page["title"] == "Launch  update"
    assert "\n\n" in page["text"]
    for value in (page["title"], page["text"]):
        assert not any(
            ord(character) < 32 and character not in "\t\n\r"
            or ord(character) == 127
            for character in value
        )

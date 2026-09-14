from __future__ import annotations

import time

import httpx
import pytest

from arena_transport import ArenaToolClient


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

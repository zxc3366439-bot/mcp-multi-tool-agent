"""Provider contracts, source preservation, bounded requests, and secret-safe errors."""

import json
import traceback

import httpx
import pytest
from ddgs.exceptions import TimeoutException
from pydantic import ValidationError

from mcp_agent import web_search
from mcp_agent.web_search import WebSearchService, WebSearchSettings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in (
        "WEB_SEARCH_PROVIDER",
        "TAVILY_API_KEY",
        "WEB_SEARCH_TIMEOUT_SECONDS",
        "WEB_SEARCH_MAX_RESULTS",
        "WEB_SEARCH_REGION",
        "WEB_SEARCH_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)


def settings(**kwargs):
    return WebSearchSettings(_env_file=None, **kwargs)


def mock_tavily(monkeypatch, handler):
    real_client = httpx.Client
    seen = {}

    def make_client(**kwargs):
        seen.update(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(web_search.httpx, "Client", make_client)
    return seen


def test_provider_selection_and_secret_representation():
    assert settings().provider == "ddgs"
    assert settings(tavily_api_key="  ").provider == "ddgs"
    configured = settings(tavily_api_key="tvly-test-secret")
    assert configured.provider == "tavily"
    assert "tvly-test-secret" not in repr(configured)
    assert settings(web_search_provider="ddgs", tavily_api_key="secret").provider == "ddgs"


def test_settings_load_env_file_without_llm_configuration(tmp_path):
    env = tmp_path / "search.env"
    env.write_text(
        "WEB_SEARCH_PROVIDER=ddgs\nWEB_SEARCH_MAX_RESULTS=3\nLLM_MODEL=ignored\n", encoding="utf-8"
    )
    configured = WebSearchSettings(_env_file=env)
    assert configured.provider == "ddgs"
    assert configured.web_search_max_results == 3


@pytest.mark.parametrize(
    "options",
    [
        {"web_search_provider": "invented"},
        {"web_search_timeout_seconds": 0},
        {"web_search_timeout_seconds": float("inf")},
        {"web_search_max_results": 11},
        {"web_search_backend": " "},
        {"web_search_region": "\x00"},
    ],
)
def test_invalid_configuration_fails(options):
    with pytest.raises(ValidationError):
        settings(**options)


@pytest.mark.parametrize(
    "query,count",
    [
        ("", 5),
        ("   ", 5),
        ("x" * 1001, 5),
        ("a\x00b", 5),
        ("valid", 0),
        ("valid", 11),
        ("valid", True),
    ],
)
def test_invalid_search_input_never_reaches_provider(query, count, monkeypatch):
    service = WebSearchService(settings())
    monkeypatch.setattr(
        service, "_search_ddgs", lambda *args: pytest.fail("network must not be called")
    )
    with pytest.raises(ValueError):
        service.search(query, count)


def test_ddgs_passes_options_caps_results_and_preserves_real_links(monkeypatch):
    seen = {}

    class FakeDDGS:
        def __init__(self, **kwargs):
            seen["constructor"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def text(self, query, **kwargs):
            seen["query"] = query
            seen["search"] = kwargs
            return [
                {
                    "title": "Python",
                    "href": "https://www.python.org/?a=b#docs",
                    "body": " Official site ",
                },
                {
                    "title": "duplicate",
                    "href": "https://www.python.org/?a=b#docs",
                    "body": "duplicate",
                },
                {"title": "Local file", "href": "file:///secret", "body": "bad"},
                {"title": "SQLite", "href": "https://www.sqlite.org", "body": "SQLite source"},
                {"title": "Third", "href": "https://example.org", "body": "above limit"},
            ]

    monkeypatch.setattr(web_search, "DDGS", FakeDDGS)
    service = WebSearchService(
        settings(web_search_max_results=2, web_search_backend="bing", web_search_region="cn-zh")
    )
    result = service.search("  Python SQLite  ", 10)
    assert result == {
        "query": "Python SQLite",
        "provider": "ddgs",
        "results": [
            {
                "title": "Python",
                "url": "https://www.python.org/?a=b#docs",
                "snippet": "Official site",
            },
            {"title": "SQLite", "url": "https://www.sqlite.org", "snippet": "SQLite source"},
        ],
    }
    assert seen["constructor"] == {"timeout": 15, "verify": True}
    assert seen["search"] == {
        "max_results": 2,
        "backend": "bing",
        "region": "cn-zh",
        "safesearch": "moderate",
    }


def test_tavily_uses_fixed_endpoint_bearer_auth_and_source_snippets(monkeypatch):
    def handler(request):
        assert str(request.url) == "https://api.tavily.com/search"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer tvly-test-secret"
        payload = json.loads(request.content)
        assert payload == {
            "query": "Python",
            "max_results": 2,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "auto_parameters": False,
        }
        return httpx.Response(
            200,
            json={
                "answer": "Ignored",
                "results": [
                    {
                        "title": "Python",
                        "url": "https://www.python.org",
                        "content": "Official source",
                        "raw_content": "Ignored",
                    },
                ],
            },
        )

    seen = mock_tavily(monkeypatch, handler)
    result = WebSearchService(
        settings(tavily_api_key="tvly-test-secret", web_search_timeout_seconds=7)
    ).search("Python", 2)
    assert result["provider"] == "tavily"
    assert result["results"] == [
        {"title": "Python", "url": "https://www.python.org", "snippet": "Official source"}
    ]
    assert seen == {"timeout": 7, "follow_redirects": False}


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "limit"),
        (432, "limit"),
        (433, "limit"),
        (500, "HTTP 500"),
        (302, "HTTP 302"),
    ],
)
def test_tavily_http_errors_never_echo_provider_body_or_secrets(monkeypatch, status, expected):
    mock_tavily(monkeypatch, lambda request: httpx.Response(status, text="LEAK tvly-test-secret"))
    with pytest.raises(ValueError, match=expected) as caught:
        WebSearchService(settings(tavily_api_key="tvly-test-secret")).search("Python")
    assert "tvly-test-secret" not in str(caught.value)
    assert "LEAK" not in str(caught.value)


def test_tavily_missing_key_never_calls_http(monkeypatch):
    monkeypatch.setattr(
        web_search.httpx, "Client", lambda **kwargs: pytest.fail("no network without key")
    )
    with pytest.raises(ValueError, match="TAVILY_API_KEY is required"):
        WebSearchService(settings(web_search_provider="tavily")).search("Python")


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ReadTimeout("LEAK tvly-test-secret"), "timed out"),
        (httpx.ConnectError("LEAK tvly-test-secret"), "request failed"),
    ],
)
def test_tavily_transport_errors_hide_sensitive_exception_chain(monkeypatch, exc, expected):
    def handler(request):
        raise exc

    mock_tavily(monkeypatch, handler)
    service = WebSearchService(settings(tavily_api_key="tvly-test-secret"))
    with pytest.raises(ValueError, match=expected) as caught:
        service.search("Python")
    assert "tvly-test-secret" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"results": "bad"}),
    ],
)
def test_tavily_malformed_responses_are_errors(monkeypatch, response):
    mock_tavily(monkeypatch, lambda request: response)
    with pytest.raises(ValueError, match="invalid"):
        WebSearchService(settings(tavily_api_key="secret")).search("Python")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"title": "Bad", "href": "javascript:alert(1)"}],
        [
            {"href": "https://user:password@example.org"},
            {"href": "https://example.org:wrong"},
            {"href": "https://"},
            {"href": "https://example.org/new line"},
            {"href": "https://[wrong"},
        ],
    ],
)
def test_empty_or_invalid_sources_do_not_report_success(monkeypatch, rows):
    service = WebSearchService(settings())
    monkeypatch.setattr(service, "_search_ddgs", lambda *args: rows)
    with pytest.raises(ValueError, match="no usable source"):
        service.search("Python")


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TimeoutException("private proxy password"), "timed out"),
        (RuntimeError("private proxy password"), "search failed"),
    ],
)
def test_ddgs_errors_are_clear_and_do_not_echo_exception_details(monkeypatch, exc, expected):
    def failing_ddgs(**kwargs):
        raise exc

    monkeypatch.setattr(web_search, "DDGS", failing_ddgs)
    with pytest.raises(ValueError, match=expected) as caught:
        WebSearchService(settings()).search("Python")
    assert "private proxy password" not in "".join(traceback.format_exception(caught.value))

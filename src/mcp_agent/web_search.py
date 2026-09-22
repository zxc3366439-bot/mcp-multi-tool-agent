"""Source-preserving Web Search providers shared by the MCP server and CLI.

DDGS >= 9.16 uses direct HTTP search engines without the DHT/P2P cache present
in some earlier releases. Keep that minimum version when updating dependencies.
"""

from __future__ import annotations

import math
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from ddgs import DDGS
from ddgs.exceptions import TimeoutException
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_QUERY_LENGTH = 1000
HARD_MAX_RESULTS = 10
TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class WebSearchSettings(BaseSettings):
    """Read configuration without requiring or exposing LLM credentials."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    web_search_provider: Literal["auto", "ddgs", "tavily"] = "auto"
    tavily_api_key: SecretStr | None = None
    web_search_timeout_seconds: float = Field(default=15, gt=0, le=120, allow_inf_nan=False)
    web_search_max_results: int = Field(default=5, ge=1, le=HARD_MAX_RESULTS)
    web_search_region: str = "wt-wt"
    web_search_backend: str = "auto"

    @field_validator("web_search_region", "web_search_backend")
    @classmethod
    def nonempty_option(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
            raise ValueError("Search region/backend must be a short, nonempty string.")
        return value

    @property
    def provider(self) -> Literal["ddgs", "tavily"]:
        if self.web_search_provider != "auto":
            return self.web_search_provider
        return (
            "tavily"
            if self.tavily_api_key and self.tavily_api_key.get_secret_value().strip()
            else "ddgs"
        )


def _normalize_results(rows: Any, limit: int) -> list[dict[str, str]]:
    """Keep real source URLs, discard invalid rows, and bound tool context size."""
    if not isinstance(rows, list):
        raise ValueError("Search provider returned an invalid results format.")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("url") or row.get("href")
        if not isinstance(url, str):
            continue
        url = url.strip()
        if len(url) > 4096 or any(char.isspace() or ord(char) < 32 for char in url):
            continue
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme in {"https", "http"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
            )
            # Accessing port also rejects malformed ports without rewriting the source URL.
            parsed.port
        except ValueError:
            continue
        if not valid or url in seen:
            continue
        title = row.get("title")
        snippet = row.get("content", row.get("body", ""))
        if not isinstance(title, str) or not title.strip():
            title = url
        if not isinstance(snippet, str):
            snippet = ""
        results.append(
            {"title": title.strip()[:500], "url": url, "snippet": snippet.strip()[:1500]}
        )
        seen.add(url)
        if len(results) >= limit:
            break
    if not results:
        raise ValueError(
            "Web Search returned no usable source results. "
            "Try a more specific query or another provider."
        )
    return results


class WebSearchService:
    """Execute a search and return citations, never a generated answer."""

    def __init__(self, settings: WebSearchSettings | None = None) -> None:
        self.settings = settings or WebSearchSettings()

    def search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonempty string.")
        query = query.strip()
        if len(query) > MAX_QUERY_LENGTH or "\x00" in query:
            raise ValueError(
                f"query must contain at most {MAX_QUERY_LENGTH} characters and no null bytes."
            )
        if type(max_results) is not int or not 1 <= max_results <= HARD_MAX_RESULTS:
            raise ValueError(f"max_results must be an integer between 1 and {HARD_MAX_RESULTS}.")
        limit = min(max_results, self.settings.web_search_max_results)
        provider = self.settings.provider
        rows = (
            self._search_tavily(query, limit)
            if provider == "tavily"
            else self._search_ddgs(query, limit)
        )
        return {"query": query, "provider": provider, "results": _normalize_results(rows, limit)}

    def _search_ddgs(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            # 9.16+ contains no distributed cache path; do not use ddgs[api] or spawn a service.
            with DDGS(
                timeout=math.ceil(self.settings.web_search_timeout_seconds), verify=True
            ) as client:
                return client.text(
                    query,
                    max_results=limit,
                    region=self.settings.web_search_region,
                    backend=self.settings.web_search_backend,
                    safesearch="moderate",
                )
        except (TimeoutException, TimeoutError):
            raise ValueError(
                "DDGS search timed out. Retry or configure another WEB_SEARCH_PROVIDER."
            ) from None
        except Exception:
            # Upstream exceptions may contain request URLs or proxy credentials.
            raise ValueError(
                "DDGS search failed or found no results. Check network access, try another query, "
                "set WEB_SEARCH_BACKEND, or configure Tavily."
            ) from None

    def _search_tavily(self, query: str, limit: int) -> list[dict[str, Any]]:
        key = self.settings.tavily_api_key
        if not key or not key.get_secret_value().strip():
            raise ValueError("TAVILY_API_KEY is required when WEB_SEARCH_PROVIDER=tavily.")
        try:
            with httpx.Client(
                timeout=self.settings.web_search_timeout_seconds, follow_redirects=False
            ) as client:
                response = client.post(
                    TAVILY_SEARCH_URL,
                    headers={"Authorization": f"Bearer {key.get_secret_value().strip()}"},
                    json={
                        "query": query,
                        "max_results": limit,
                        "search_depth": "basic",
                        "include_answer": False,
                        "include_raw_content": False,
                        "include_images": False,
                        "auto_parameters": False,
                    },
                )
        except httpx.TimeoutException:
            raise ValueError("Tavily search timed out. Check network access and retry.") from None
        except Exception:
            raise ValueError(
                "Tavily search request failed. Check network access and proxy settings."
            ) from None
        if response.status_code in {401, 403}:
            raise ValueError("Tavily authentication failed. Check TAVILY_API_KEY.")
        if response.status_code in {429, 432, 433}:
            raise ValueError(
                "Tavily rate or usage limit reached. Check your account quota and retry later."
            )
        if not 200 <= response.status_code < 300:
            raise ValueError(f"Tavily search failed with HTTP {response.status_code}. Retry later.")
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            rows = payload["results"]
        except (ValueError, KeyError):
            raise ValueError("Tavily returned an invalid search response.") from None
        return rows

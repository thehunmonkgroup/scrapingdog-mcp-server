"""Tests for the Scrapingdog HTTP client."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from scrapingdog_mcp_server.core import (
    AIOHTTP_TIMEOUT_ENV_VAR,
    DEFAULT_AIOHTTP_TIMEOUT_SECONDS,
    GOOGLE_SCRAPINGDOG_URL,
    SCRAPINGDOG_API_KEY_ENV_VAR,
    ScrapingdogClient,
    ScrapingdogConfigurationError,
)
from scrapingdog_mcp_server.metrics import MetricEvent
from scrapingdog_mcp_server.schemas import GoogleSearchRequest, WebpageRequest


class FakeResponse:
    """Async response test double."""

    def __init__(
        self,
        *,
        status: int = 200,
        text: str = "Example Domain",
        json_body: dict[str, Any] | None = None,
    ) -> None:
        self.status: int = status
        self._text: str = text
        self._json_body: dict[str, Any] = json_body or {"organic_results": []}

    async def __aenter__(self) -> FakeResponse:
        """Enter the async context manager.

        :return: The fake response.
        :rtype: FakeResponse
        """

        return self

    async def __aexit__(self, *_args: object) -> None:
        """Exit the async context manager.

        :return: None.
        :rtype: None
        """

    async def text(self) -> str:
        """Return fake response text.

        :return: Fake response text.
        :rtype: str
        """

        return self._text

    async def json(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
        """Return fake response JSON.

        :return: Fake JSON body.
        :rtype: dict[str, Any]
        """

        return self._json_body


class FakeSession:
    """Async session test double."""

    closed: bool = False

    def __init__(self, response: FakeResponse) -> None:
        self.response: FakeResponse = response
        self.last_url: str | None = None
        self.last_params: dict[str, str] | None = None

    def get(self, url: str, *, params: dict[str, str]) -> FakeResponse:
        """Record a fake GET request.

        :param url: Request URL.
        :type url: str
        :param params: Request query parameters.
        :type params: dict[str, str]
        :return: Fake response.
        :rtype: FakeResponse
        """

        self.last_url = url
        self.last_params = params
        return self.response


class FakeMetricsRecorder:
    """Metrics recorder test double."""

    def __init__(self) -> None:
        self.events: list[MetricEvent] = []

    async def record_request(self, event: MetricEvent) -> None:
        """Record a metric event in memory.

        :param event: Metric event.
        :type event: MetricEvent
        :return: None.
        :rtype: None
        """

        self.events.append(event)


def run_async(awaitable: Any) -> Any:
    """Run an awaitable for tests.

    :param awaitable: Awaitable object.
    :type awaitable: Any
    :return: Awaitable result.
    :rtype: Any
    """

    return asyncio.run(awaitable)


def test_api_key_is_read_lazily_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client reads API key configuration after construction."""

    monkeypatch.delenv(SCRAPINGDOG_API_KEY_ENV_VAR, raising=False)
    client = ScrapingdogClient()

    monkeypatch.setenv(SCRAPINGDOG_API_KEY_ENV_VAR, " env-key ")

    assert client.api_key == "env-key"


def test_timeout_default_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default timeout is used when no environment value exists."""

    monkeypatch.delenv(AIOHTTP_TIMEOUT_ENV_VAR, raising=False)
    client = ScrapingdogClient(api_key="test-key")

    assert client.timeout_seconds == DEFAULT_AIOHTTP_TIMEOUT_SECONDS


def test_invalid_timeout_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid timeout configuration is reported clearly."""

    monkeypatch.setenv(AIOHTTP_TIMEOUT_ENV_VAR, "invalid")
    client = ScrapingdogClient(api_key="test-key")

    with pytest.raises(
        ScrapingdogConfigurationError,
        match="must be an integer",
    ):
        _ = client.timeout_seconds


def test_query_params_include_api_key_and_serialized_values() -> None:
    """Scrapingdog request models serialize to documented query parameters."""

    client = ScrapingdogClient(api_key="test-key")
    request = GoogleSearchRequest(
        query="openai",
        advance_search=True,
        mob_search=False,
        html=True,
        domain="google.com",
        country="us",
        location="New York",
        language="en",
        safe="active",
        nfpr=True,
        filter=False,
        results=10,
        page=2,
    )

    assert client.build_query_params(request) == {
        "api_key": "test-key",
        "advance_search": "true",
        "html": "true",
        "query": "openai",
        "domain": "google.com",
        "country": "us",
        "location": "New York",
        "language": "en",
        "safe": "active",
        "nfpr": "1",
        "filter": "0",
        "results": "10",
        "page": "2",
    }


def test_query_params_omit_false_mob_search() -> None:
    """A false mobile search flag is omitted from Scrapingdog requests."""

    client = ScrapingdogClient(api_key="test-key")
    request = GoogleSearchRequest(
        query="openai",
        advance_search=None,
        mob_search=False,
        html=None,
        domain=None,
        country=None,
        location=None,
        language=None,
        safe=None,
        nfpr=None,
        filter=None,
        results=None,
        page=None,
    )

    assert client.build_query_params(request) == {
        "api_key": "test-key",
        "query": "openai",
    }


def test_query_params_include_true_mob_search() -> None:
    """A true mobile search flag is sent to Scrapingdog requests."""

    client = ScrapingdogClient(api_key="test-key")
    request = GoogleSearchRequest(
        query="openai",
        advance_search=None,
        mob_search=True,
        html=None,
        domain=None,
        country=None,
        location=None,
        language=None,
        safe=None,
        nfpr=None,
        filter=None,
        results=None,
        page=None,
    )

    assert client.build_query_params(request) == {
        "api_key": "test-key",
        "mob_search": "true",
        "query": "openai",
    }


def test_google_uses_scrapingdog_get_endpoint() -> None:
    """Google Search calls Scrapingdog with query-string authentication."""

    session = FakeSession(FakeResponse(json_body={"organic_results": []}))
    client = ScrapingdogClient(api_key="test-key", session=cast(Any, session))

    response = run_async(
        client.google(
            GoogleSearchRequest(
                query="openai",
                advance_search=None,
                mob_search=None,
                html=None,
                domain=None,
                country=None,
                location=None,
                language=None,
                safe=None,
                nfpr=None,
                filter=None,
                results=None,
                page=None,
            )
        )
    )

    assert response == {"organic_results": []}
    assert session.last_url == GOOGLE_SCRAPINGDOG_URL
    assert session.last_params == {"api_key": "test-key", "query": "openai"}


def test_google_records_search_metric() -> None:
    """Google Search records a portable search metric event."""

    session = FakeSession(
        FakeResponse(
            json_body={
                "organic_results": [{"title": "Example", "link": "https://example.com"}]
            }
        )
    )
    metrics = FakeMetricsRecorder()
    client = ScrapingdogClient(
        api_key="test-key",
        session=cast(Any, session),
        metrics=metrics,
    )

    run_async(
        client.google(
            GoogleSearchRequest(
                query="openai",
                advance_search=None,
                mob_search=None,
                html=None,
                domain=None,
                country=None,
                location=None,
                language=None,
                safe=None,
                nfpr=None,
                filter=None,
                results=None,
                page=None,
            )
        )
    )

    assert len(metrics.events) == 1
    event = metrics.events[0]
    assert event.tool == "google_search"
    assert event.request_type == "search"
    assert event.succeeded is True
    assert event.status_code == 200
    assert event.query == "openai"
    assert event.result_count == 1


def test_scrape_wraps_raw_response_text() -> None:
    """Webpage scrape wraps raw Scrapingdog text into structured content."""

    session = FakeSession(FakeResponse(text="# Example Domain"))
    client = ScrapingdogClient(api_key="test-key", session=cast(Any, session))

    response = run_async(
        client.scrape(
            WebpageRequest(
                url="https://example.com",
                dynamic=False,
                formats="markdown",
            )
        )
    )

    assert response == {
        "format": "markdown",
        "content": "# Example Domain",
        "status": 200,
    }
    assert session.last_params == {
        "api_key": "test-key",
        "url": "https://example.com",
        "dynamic": "false",
        "formats": "markdown",
    }


def test_scrape_records_scrape_metric() -> None:
    """Webpage scrape records a portable scrape metric event."""

    session = FakeSession(FakeResponse(text="# Example Domain"))
    metrics = FakeMetricsRecorder()
    client = ScrapingdogClient(
        api_key="test-key",
        session=cast(Any, session),
        metrics=metrics,
    )

    run_async(
        client.scrape(
            WebpageRequest(
                url="https://example.com",
                dynamic=False,
                formats="markdown",
            )
        )
    )

    assert len(metrics.events) == 1
    event = metrics.events[0]
    assert event.tool == "webpage_scrape"
    assert event.request_type == "scrape"
    assert event.succeeded is True
    assert event.status_code == 200
    assert event.url == "https://example.com"
    assert event.response_format == "markdown"
    assert event.returned_bytes == len("# Example Domain".encode("utf-8"))

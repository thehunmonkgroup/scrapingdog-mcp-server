"""Tests for the Scrapingdog FastMCP server."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import types
import pytest
from typing_extensions import override

from scrapingdog_mcp_server.core import (
    ScrapingdogClient,
    ScrapingdogConfigurationError,
)
from scrapingdog_mcp_server.enums import ScrapingdogTools
from scrapingdog_mcp_server.schemas import GoogleSearchRequest, WebpageRequest
from scrapingdog_mcp_server.server import (
    ScrapingdogMcpApplication,
    create_mcp_server,
)


class FakeScrapingdogClient(ScrapingdogClient):
    """Scrapingdog client test double returning deterministic responses."""

    def __init__(self) -> None:
        super().__init__(api_key="test-key")
        self.last_search_payload: dict[str, Any] | None = None
        self.last_scrape_payload: dict[str, Any] | None = None

    @override
    async def google(self, request: GoogleSearchRequest) -> dict[str, Any]:
        """Return a fake Google response.

        :param request: Validated search request model.
        :type request: GoogleSearchRequest
        :return: Fake Scrapingdog response.
        :rtype: dict[str, Any]
        """

        self.last_search_payload = request.model_dump()
        return {
            "organic_results": [{"title": "Example", "link": "https://example.com"}],
            "search_information": {
                "query_displayed": self.last_search_payload["query"]
            },
        }

    @override
    async def scrape(self, request: WebpageRequest) -> dict[str, Any]:
        """Return a fake scrape response.

        :param request: Validated webpage request.
        :type request: WebpageRequest
        :return: Fake wrapped Scrapingdog scrape response.
        :rtype: dict[str, Any]
        """

        self.last_scrape_payload = request.model_dump()
        return {
            "format": request.formats or "html",
            "content": "# Example Domain",
            "status": 200,
        }


class FailingScrapingdogClient(ScrapingdogClient):
    """Scrapingdog client test double raising expected configuration errors."""

    @override
    async def google(self, request: GoogleSearchRequest) -> dict[str, Any]:
        """Raise a configuration error.

        :param request: Validated search request model.
        :type request: GoogleSearchRequest
        :return: Never returns.
        :rtype: dict[str, Any]
        :raises ScrapingdogConfigurationError: Always raised.
        """

        raise ScrapingdogConfigurationError("SCRAPINGDOG_API_KEY is empty")


def run_async(awaitable: Any) -> Any:
    """Run an awaitable for tests.

    :param awaitable: Awaitable object.
    :type awaitable: Any
    :return: Awaitable result.
    :rtype: Any
    """

    return asyncio.run(awaitable)


def test_tool_list_contains_expected_metadata() -> None:
    """All public tools expose useful metadata and annotations."""

    mcp_server = create_mcp_server(FakeScrapingdogClient())
    tools = run_async(mcp_server.list_tools())
    tools_by_name = {tool.name: tool for tool in tools}

    assert set(tools_by_name) == {tool.value for tool in ScrapingdogTools}

    search_tool = tools_by_name[ScrapingdogTools.GOOGLE_SEARCH.value]
    assert search_tool.title == "Google Search"
    assert search_tool.description == ("Search Google web results through Scrapingdog.")
    assert search_tool.outputSchema is not None
    assert search_tool.annotations is not None
    assert search_tool.annotations.readOnlyHint is True
    assert search_tool.annotations.destructiveHint is False
    assert search_tool.annotations.idempotentHint is False
    assert search_tool.annotations.openWorldHint is True
    assert search_tool.inputSchema["properties"]["query"]["type"] == "string"
    advance_search_schema = search_tool.inputSchema["properties"]["advance_search"]
    assert {"type": "boolean"} in advance_search_schema["anyOf"]

    scrape_tool = tools_by_name[ScrapingdogTools.WEBPAGE_SCRAPE.value]
    assert scrape_tool.title == "Webpage Scrape"
    assert scrape_tool.description == ("Scrape a webpage URL through Scrapingdog.")
    dynamic_schema = scrape_tool.inputSchema["properties"]["dynamic"]
    assert {"type": "boolean"} in dynamic_schema["anyOf"]
    assert "formats" in scrape_tool.inputSchema["properties"]


def test_google_search_returns_structured_content() -> None:
    """Successful search calls return structured content."""

    client = FakeScrapingdogClient()
    mcp_server = create_mcp_server(client)

    content, structured_content = run_async(
        mcp_server.call_tool(
            ScrapingdogTools.GOOGLE_SEARCH.value,
            {
                "query": "openai",
                "country": "us",
                "language": "en",
                "advance_search": True,
            },
        )
    )

    assert structured_content["organic_results"][0]["title"] == "Example"
    assert content[0].type == "text"
    assert client.last_search_payload is not None
    assert client.last_search_payload["query"] == "openai"
    assert client.last_search_payload["country"] == "us"
    assert client.last_search_payload["advance_search"] is True
    assert client.last_search_payload["results"] == 10
    assert client.last_search_payload["page"] == 0


def test_forced_env_var_name_converts_parameter_names() -> None:
    """Forced env var names are derived from tool parameter names."""

    assert ScrapingdogMcpApplication.force_env_var_name("country") == (
        "SCRAPINGDOG_FORCE_COUNTRY"
    )
    assert ScrapingdogMcpApplication.force_env_var_name("advance_search") == (
        "SCRAPINGDOG_FORCE_ADVANCE_SEARCH"
    )
    assert ScrapingdogMcpApplication.force_env_var_name("mob_search") == (
        "SCRAPINGDOG_FORCE_MOB_SEARCH"
    )


def test_google_search_uses_forced_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search calls prefer forced environment values over caller arguments."""

    monkeypatch.setenv("SCRAPINGDOG_FORCE_COUNTRY", "us")
    monkeypatch.setenv("SCRAPINGDOG_FORCE_LANGUAGE", "en")
    monkeypatch.setenv("SCRAPINGDOG_FORCE_ADVANCE_SEARCH", "true")
    client = FakeScrapingdogClient()
    mcp_server = create_mcp_server(client)

    run_async(
        mcp_server.call_tool(
            ScrapingdogTools.GOOGLE_SEARCH.value,
            {
                "query": "openai",
                "country": "ca",
                "language": "fr",
                "advance_search": False,
            },
        )
    )

    assert client.last_search_payload is not None
    assert client.last_search_payload["country"] == "us"
    assert client.last_search_payload["language"] == "en"
    assert client.last_search_payload["advance_search"] is True


def test_webpage_scrape_returns_structured_content() -> None:
    """Successful scrape calls return structured content."""

    client = FakeScrapingdogClient()
    mcp_server = create_mcp_server(client)

    content, structured_content = run_async(
        mcp_server.call_tool(
            ScrapingdogTools.WEBPAGE_SCRAPE.value,
            {
                "url": "https://example.com",
                "dynamic": False,
                "formats": "markdown",
            },
        )
    )

    assert structured_content["format"] == "markdown"
    assert structured_content["content"] == "# Example Domain"
    assert content[0].type == "text"
    assert client.last_scrape_payload is not None
    assert client.last_scrape_payload["dynamic"] is False
    assert client.last_scrape_payload["formats"] == "markdown"


def test_webpage_scrape_uses_forced_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scrape calls prefer forced environment values over caller arguments."""

    monkeypatch.setenv("SCRAPINGDOG_FORCE_DYNAMIC", "true")
    monkeypatch.setenv("SCRAPINGDOG_FORCE_FORMATS", "summary")
    client = FakeScrapingdogClient()
    mcp_server = create_mcp_server(client)

    run_async(
        mcp_server.call_tool(
            ScrapingdogTools.WEBPAGE_SCRAPE.value,
            {
                "url": "https://example.com",
                "dynamic": False,
                "formats": "markdown",
            },
        )
    )

    assert client.last_scrape_payload is not None
    assert client.last_scrape_payload["dynamic"] is True
    assert client.last_scrape_payload["formats"] == "summary"


def test_expected_tool_failure_sets_is_error() -> None:
    """Expected execution failures are exposed as MCP tool errors."""

    mcp_server = create_mcp_server(FailingScrapingdogClient())
    low_level_server = getattr(mcp_server, "_mcp_server")
    handler = low_level_server.request_handlers[types.CallToolRequest]

    result = run_async(
        handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name=ScrapingdogTools.GOOGLE_SEARCH.value,
                    arguments={"query": "openai"},
                )
            )
        )
    )

    call_result = result.root
    assert call_result.isError is True
    assert "SCRAPINGDOG_API_KEY is empty" in call_result.content[0].text


def test_invalid_format_fails_validation() -> None:
    """Invalid scrape output formats are rejected before handler execution."""

    mcp_server = create_mcp_server(FakeScrapingdogClient())
    low_level_server = getattr(mcp_server, "_mcp_server")
    handler = low_level_server.request_handlers[types.CallToolRequest]

    result = run_async(
        handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name=ScrapingdogTools.WEBPAGE_SCRAPE.value,
                    arguments={
                        "url": "https://example.com",
                        "formats": "pdf",
                    },
                )
            )
        )
    )

    call_result = result.root
    assert call_result.isError is True
    assert "validation error" in call_result.content[0].text

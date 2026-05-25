"""Tests for the Scrapingdog FastMCP server."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import types
import pytest
from typing_extensions import override

from scrapingdog_mcp_server.core import (
    ScrapingdogClient,
    ScrapingdogClientError,
    ScrapingdogConfigurationError,
)
from scrapingdog_mcp_server.enums import ScrapingdogTools
from scrapingdog_mcp_server.schemas import GoogleSearchRequest, WebpageRequest
from scrapingdog_mcp_server.server import (
    GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR,
    ScrapingdogMcpApplication,
    WEBPAGE_SCRAPE_SESSION_LIMIT_ENV_VAR,
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


class IntermittentFailingScrapingdogClient(FakeScrapingdogClient):
    """Scrapingdog client test double that fails once before succeeding."""

    def __init__(self) -> None:
        super().__init__()
        self.should_fail_search: bool = True

    @override
    async def google(self, request: GoogleSearchRequest) -> dict[str, Any]:
        """Fail once, then return a fake Google response.

        :param request: Validated search request model.
        :type request: GoogleSearchRequest
        :return: Fake Scrapingdog response.
        :rtype: dict[str, Any]
        :raises ScrapingdogClientError: On the first search call.
        """

        if self.should_fail_search:
            self.should_fail_search = False
            raise ScrapingdogClientError("Scrapingdog API returned HTTP 500")
        return await super().google(request)


@pytest.fixture(autouse=True)
def clear_session_limit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear session limit configuration unless a test sets it explicitly.

    :param monkeypatch: Pytest monkeypatch fixture.
    :type monkeypatch: pytest.MonkeyPatch
    :return: None.
    :rtype: None
    """

    monkeypatch.delenv(GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR, raising=False)
    monkeypatch.delenv(WEBPAGE_SCRAPE_SESSION_LIMIT_ENV_VAR, raising=False)


def run_async(awaitable: Any) -> Any:
    """Run an awaitable for tests.

    :param awaitable: Awaitable object.
    :type awaitable: Any
    :return: Awaitable result.
    :rtype: Any
    """

    return asyncio.run(awaitable)


def call_tool_result(
    mcp_server: Any,
    tool_name: ScrapingdogTools,
    arguments: dict[str, Any],
) -> types.CallToolResult:
    """Call a tool through the low-level MCP request handler.

    :param mcp_server: FastMCP server instance.
    :type mcp_server: Any
    :param tool_name: Tool to call.
    :type tool_name: ScrapingdogTools
    :param arguments: Tool arguments.
    :type arguments: dict[str, Any]
    :return: MCP call tool result.
    :rtype: types.CallToolResult
    """

    low_level_server = getattr(mcp_server, "_mcp_server")
    handler = low_level_server.request_handlers[types.CallToolRequest]
    result = run_async(
        handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name=tool_name.value,
                    arguments=arguments,
                )
            )
        )
    )
    return result.root


def call_tool_text(call_result: types.CallToolResult) -> str:
    """Return the first text content item from a tool result.

    :param call_result: MCP call tool result.
    :type call_result: types.CallToolResult
    :return: First text content string.
    :rtype: str
    """

    content = call_result.content[0]
    assert isinstance(content, types.TextContent)
    return content.text


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


def test_google_search_session_limit_errors_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search limits apply after the configured number of successful calls."""

    monkeypatch.setenv(GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR, "1")
    mcp_server = create_mcp_server(FakeScrapingdogClient())

    first_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.GOOGLE_SEARCH,
        {"query": "openai"},
    )
    second_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.GOOGLE_SEARCH,
        {"query": "openai"},
    )

    assert first_result.isError is False
    assert second_result.isError is True
    second_result_text = call_tool_text(second_result)
    assert "usage limit reached" in second_result_text
    assert "Do not call google_search again" in second_result_text


def test_webpage_scrape_session_limit_errors_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scrape limits apply after the configured number of successful calls."""

    monkeypatch.setenv(WEBPAGE_SCRAPE_SESSION_LIMIT_ENV_VAR, "1")
    mcp_server = create_mcp_server(FakeScrapingdogClient())

    first_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.WEBPAGE_SCRAPE,
        {"url": "https://example.com"},
    )
    second_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.WEBPAGE_SCRAPE,
        {"url": "https://example.com"},
    )

    assert first_result.isError is False
    assert second_result.isError is True
    second_result_text = call_tool_text(second_result)
    assert "usage limit reached" in second_result_text
    assert "Do not call webpage_scrape again" in second_result_text


def test_session_limits_are_independent_by_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausting one tool limit does not block the other tool."""

    monkeypatch.setenv(GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR, "1")
    monkeypatch.setenv(WEBPAGE_SCRAPE_SESSION_LIMIT_ENV_VAR, "1")
    mcp_server = create_mcp_server(FakeScrapingdogClient())

    search_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.GOOGLE_SEARCH,
        {"query": "openai"},
    )
    limited_search_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.GOOGLE_SEARCH,
        {"query": "openai"},
    )
    scrape_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.WEBPAGE_SCRAPE,
        {"url": "https://example.com"},
    )

    assert search_result.isError is False
    assert limited_search_result.isError is True
    assert scrape_result.isError is False


def test_validation_failures_do_not_consume_session_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calls rejected by MCP validation do not count against the limit."""

    monkeypatch.setenv(WEBPAGE_SCRAPE_SESSION_LIMIT_ENV_VAR, "1")
    mcp_server = create_mcp_server(FakeScrapingdogClient())

    invalid_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.WEBPAGE_SCRAPE,
        {
            "url": "https://example.com",
            "formats": "pdf",
        },
    )
    successful_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.WEBPAGE_SCRAPE,
        {"url": "https://example.com"},
    )
    limited_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.WEBPAGE_SCRAPE,
        {"url": "https://example.com"},
    )

    assert invalid_result.isError is True
    assert successful_result.isError is False
    assert limited_result.isError is True
    assert "usage limit reached" in call_tool_text(limited_result)


def test_scrapingdog_failures_do_not_consume_session_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scrapingdog client failures do not count against the limit."""

    monkeypatch.setenv(GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR, "1")
    mcp_server = create_mcp_server(IntermittentFailingScrapingdogClient())

    failed_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.GOOGLE_SEARCH,
        {"query": "openai"},
    )
    successful_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.GOOGLE_SEARCH,
        {"query": "openai"},
    )
    limited_result = call_tool_result(
        mcp_server,
        ScrapingdogTools.GOOGLE_SEARCH,
        {"query": "openai"},
    )

    assert failed_result.isError is True
    failed_result_text = call_tool_text(failed_result)
    assert "Scrapingdog API returned HTTP 500" in failed_result_text
    assert successful_result.isError is False
    assert limited_result.isError is True
    assert "usage limit reached" in call_tool_text(limited_result)


@pytest.mark.parametrize("limit_value", ["invalid", "0", "-1"])
def test_invalid_session_limit_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
    limit_value: str,
) -> None:
    """Invalid configured session limits fail server creation clearly."""

    monkeypatch.setenv(GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR, limit_value)

    with pytest.raises(
        ScrapingdogConfigurationError,
        match=(f"{GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR} " "must be a positive integer"),
    ):
        create_mcp_server(FakeScrapingdogClient())


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

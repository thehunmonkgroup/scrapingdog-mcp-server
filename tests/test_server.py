"""Tests for the Scrapingdog FastMCP server."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import types
import pytest
from typing_extensions import override

from scrapingdog_mcp_server.core import (
    SCRAPINGDOG_MAX_CONCURRENT_REQUESTS_ENV_VAR,
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
            "format": request.format or "html",
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


class BlockingScrapingdogClient(FakeScrapingdogClient):
    """Client test double that exposes simultaneous tool execution.

    :param expected_active_calls: Number of active calls that signals readiness.
    :type expected_active_calls: int
    """

    def __init__(self, expected_active_calls: int) -> None:
        super().__init__()
        self.expected_active_calls: int = expected_active_calls
        self.active_calls: int = 0
        self.maximum_active_calls: int = 0
        self.expected_calls_started: asyncio.Event = asyncio.Event()
        self.release_calls: asyncio.Event = asyncio.Event()

    async def wait_for_release(self) -> None:
        """Run one tracked call until the test releases it.

        :return: None.
        :rtype: None
        """

        with self.concurrent_request_limiter.claim_request_slot():
            self.active_calls += 1
            self.maximum_active_calls = max(
                self.maximum_active_calls,
                self.active_calls,
            )
            if self.active_calls >= self.expected_active_calls:
                self.expected_calls_started.set()
            try:
                await self.release_calls.wait()
            finally:
                self.active_calls -= 1

    @override
    async def google(self, request: GoogleSearchRequest) -> dict[str, Any]:
        """Block and then return a fake Google response.

        :param request: Validated search request model.
        :type request: GoogleSearchRequest
        :return: Fake Scrapingdog response.
        :rtype: dict[str, Any]
        """

        await self.wait_for_release()
        return await super().google(request)

    @override
    async def scrape(self, request: WebpageRequest) -> dict[str, Any]:
        """Block and then return a fake scrape response.

        :param request: Validated webpage request model.
        :type request: WebpageRequest
        :return: Fake wrapped Scrapingdog response.
        :rtype: dict[str, Any]
        """

        await self.wait_for_release()
        return await super().scrape(request)


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
    monkeypatch.delenv(
        SCRAPINGDOG_MAX_CONCURRENT_REQUESTS_ENV_VAR,
        raising=False,
    )


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

    return run_async(call_tool_result_async(mcp_server, tool_name, arguments))


async def call_tool_result_async(
    mcp_server: Any,
    tool_name: ScrapingdogTools,
    arguments: dict[str, Any],
) -> types.CallToolResult:
    """Call a tool asynchronously through the low-level request handler.

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
    result = await handler(
        types.CallToolRequest(
            params=types.CallToolRequestParams(
                name=tool_name.value,
                arguments=arguments,
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
    assert search_tool.description == "Search Google web results."
    assert search_tool.outputSchema is not None
    assert search_tool.annotations is not None
    assert search_tool.annotations.readOnlyHint is True
    assert search_tool.annotations.destructiveHint is False
    assert search_tool.annotations.idempotentHint is False
    assert search_tool.annotations.openWorldHint is True
    search_properties = search_tool.inputSchema["properties"]
    assert search_properties["query"]["type"] == "string"
    advance_search_schema = search_tool.inputSchema["properties"]["advance_search"]
    assert {"type": "boolean"} in advance_search_schema["anyOf"]
    assert search_properties["results"]["anyOf"][0]["minimum"] == 1
    assert search_properties["page"]["anyOf"][0]["minimum"] == 0
    expected_search_descriptions = {
        "query": (
            "Google search query. Supports operators like site:, inurl:, and intitle:."
        ),
        "advance_search": "Include advanced Google result features and snippets.",
        "mob_search": "Return mobile Google search results.",
        "html": "Return raw Google results-page HTML instead of parsed JSON.",
        "domain": "Google domain to search, such as google.com or google.co.uk.",
        "country": (
            "Two-letter country code for localized results, such as us, uk, or fr."
        ),
        "location": "Search origin location; city-level values usually work best.",
        "language": "Language code for results, such as en, es, fr, or de.",
        "safe": (
            "SafeSearch setting: active filters adult content, off disables filtering."
        ),
        "nfpr": "Exclude results from Google's auto-corrected spelling.",
        "filter": "Enable Google's similar and omitted-results filters.",
        "results": "Number of Google results to request.",
        "page": "Zero-based results page: 0 is the first page, 1 is the second.",
    }
    assert {
        name: schema["description"] for name, schema in search_properties.items()
    } == expected_search_descriptions

    scrape_tool = tools_by_name[ScrapingdogTools.WEBPAGE_SCRAPE.value]
    assert scrape_tool.title == "Webpage Scrape"
    assert scrape_tool.description == "Scrape a webpage URL."
    scrape_properties = scrape_tool.inputSchema["properties"]
    dynamic_schema = scrape_properties["dynamic"]
    assert {"type": "boolean"} in dynamic_schema["anyOf"]
    assert "format" in scrape_properties
    assert "formats" not in scrape_properties
    expected_scrape_descriptions = {
        "url": "Decoded absolute URL of the page to scrape.",
        "dynamic": "Render JavaScript before scraping. Defaults to true when omitted.",
        "format": (
            "Output format: markdown, summary, links, or images. Omit for HTML."
        ),
    }
    assert {
        name: schema["description"] for name, schema in scrape_properties.items()
    } == expected_scrape_descriptions


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
                "format": "markdown",
            },
        )
    )

    assert structured_content["format"] == "markdown"
    assert structured_content["content"] == "# Example Domain"
    assert content[0].type == "text"
    assert client.last_scrape_payload is not None
    assert client.last_scrape_payload["dynamic"] is False
    assert client.last_scrape_payload["format"] == "markdown"


def test_webpage_scrape_uses_forced_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scrape calls prefer forced environment values over caller arguments."""

    monkeypatch.setenv("SCRAPINGDOG_FORCE_DYNAMIC", "true")
    monkeypatch.setenv("SCRAPINGDOG_FORCE_FORMAT", "summary")
    client = FakeScrapingdogClient()
    mcp_server = create_mcp_server(client)

    run_async(
        mcp_server.call_tool(
            ScrapingdogTools.WEBPAGE_SCRAPE.value,
            {
                "url": "https://example.com",
                "dynamic": False,
                "format": "markdown",
            },
        )
    )

    assert client.last_scrape_payload is not None
    assert client.last_scrape_payload["dynamic"] is True
    assert client.last_scrape_payload["format"] == "summary"


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


def test_session_limit_allows_calls_to_run_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session quota reservations do not serialize outbound tool calls."""

    monkeypatch.setenv(GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR, "2")
    client = BlockingScrapingdogClient(expected_active_calls=2)
    mcp_server = create_mcp_server(client)

    async def exercise_session_limit() -> list[types.CallToolResult]:
        first_call = asyncio.create_task(
            call_tool_result_async(
                mcp_server,
                ScrapingdogTools.GOOGLE_SEARCH,
                {"query": "first"},
            )
        )
        second_call = asyncio.create_task(
            call_tool_result_async(
                mcp_server,
                ScrapingdogTools.GOOGLE_SEARCH,
                {"query": "second"},
            )
        )
        await asyncio.wait_for(client.expected_calls_started.wait(), timeout=1)
        limited_result = await asyncio.wait_for(
            call_tool_result_async(
                mcp_server,
                ScrapingdogTools.GOOGLE_SEARCH,
                {"query": "third"},
            ),
            timeout=0.1,
        )
        client.release_calls.set()
        completed_results = await asyncio.gather(first_call, second_call)
        return [*completed_results, limited_result]

    first_result, second_result, limited_result = run_async(exercise_session_limit())

    assert client.maximum_active_calls == 2
    assert first_result.isError is False
    assert second_result.isError is False
    assert limited_result.isError is True
    assert "usage limit reached" in call_tool_text(limited_result)


def test_cancelled_call_releases_session_limit_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation returns reserved session quota for a later call."""

    monkeypatch.setenv(GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR, "1")
    client = BlockingScrapingdogClient(expected_active_calls=1)
    mcp_server = create_mcp_server(client)

    async def cancel_and_retry() -> types.CallToolResult:
        cancelled_call = asyncio.create_task(
            call_tool_result_async(
                mcp_server,
                ScrapingdogTools.GOOGLE_SEARCH,
                {"query": "cancelled"},
            )
        )
        await asyncio.wait_for(client.expected_calls_started.wait(), timeout=1)
        cancelled_call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_call

        client.release_calls.set()
        return await call_tool_result_async(
            mcp_server,
            ScrapingdogTools.GOOGLE_SEARCH,
            {"query": "retry"},
        )

    retry_result = run_async(cancel_and_retry())

    assert retry_result.isError is False


def test_global_concurrency_limit_replies_with_warning_without_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed tools share one fail-fast concurrent request limit."""

    monkeypatch.setenv(SCRAPINGDOG_MAX_CONCURRENT_REQUESTS_ENV_VAR, "2")
    client = BlockingScrapingdogClient(expected_active_calls=2)
    mcp_server = create_mcp_server(client)

    async def exercise_concurrency_limit() -> tuple[
        types.CallToolResult,
        tuple[types.CallToolResult, types.CallToolResult],
        types.CallToolResult,
    ]:
        search_call = asyncio.create_task(
            call_tool_result_async(
                mcp_server,
                ScrapingdogTools.GOOGLE_SEARCH,
                {"query": "first"},
            )
        )
        scrape_call = asyncio.create_task(
            call_tool_result_async(
                mcp_server,
                ScrapingdogTools.WEBPAGE_SCRAPE,
                {"url": "https://example.com"},
            )
        )
        await asyncio.wait_for(client.expected_calls_started.wait(), timeout=1)
        rejected_result = await asyncio.wait_for(
            call_tool_result_async(
                mcp_server,
                ScrapingdogTools.GOOGLE_SEARCH,
                {"query": "rejected"},
            ),
            timeout=0.1,
        )
        client.release_calls.set()
        completed_results = await asyncio.gather(search_call, scrape_call)
        retry_result = await call_tool_result_async(
            mcp_server,
            ScrapingdogTools.GOOGLE_SEARCH,
            {"query": "retry"},
        )
        return rejected_result, completed_results, retry_result

    rejected_result, completed_results, retry_result = run_async(
        exercise_concurrency_limit()
    )

    assert client.maximum_active_calls == 2
    assert rejected_result.isError is True
    warning_text = call_tool_text(rejected_result)
    assert "WARNING: The maximum of 2 simultaneous" in warning_text
    assert "not submitted or queued" in warning_text
    assert "Submit no more than 2" in warning_text
    assert all(result.isError is False for result in completed_results)
    assert retry_result.isError is False


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
            "format": "pdf",
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
    """Invalid scrape output format values are rejected before handler execution."""

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
                        "format": "pdf",
                    },
                )
            )
        )
    )

    call_result = result.root
    assert call_result.isError is True
    assert "validation error" in call_result.content[0].text

"""FastMCP server exposing Scrapingdog search and scrape tools."""

from __future__ import annotations

import logging
import os
from asyncio import Lock
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar
from weakref import WeakKeyDictionary

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .core import ScrapingdogClient, ScrapingdogConfigurationError
from .enums import ScrapingdogTools
from .metrics import (
    MetricsConfigurationError,
    MetricsService,
    NullMetricsRecorder,
    get_metrics_host,
    get_metrics_port,
    metrics_enabled,
)
from .schemas import GoogleSearchRequest, WebpageRequest

SERVER_INSTRUCTIONS = (
    "Search Google and scrape webpages through Scrapingdog. Tools call "
    "external Scrapingdog endpoints and return Scrapingdog responses as "
    "structured content."
)

FORCE_ENV_PREFIX = "SCRAPINGDOG_FORCE_"
GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR = "SCRAPINGDOG_GOOGLE_SEARCH_SESSION_LIMIT"
WEBPAGE_SCRAPE_SESSION_LIMIT_ENV_VAR = "SCRAPINGDOG_WEBPAGE_SCRAPE_SESSION_LIMIT"

READ_ONLY_OPEN_WEB_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

logger = logging.getLogger(__name__)
RequestModelT = TypeVar("RequestModelT", bound=BaseModel)


class SessionLimitFallbackKey:
    """Weak-referenceable fallback key for direct in-process tool calls."""


class UsageLimitReachedError(Exception):
    """Error raised when a tool reaches its configured session usage limit."""


class ToolUsageState:
    """Mutable usage state for one tool in one MCP client session."""

    def __init__(self) -> None:
        self.successful_calls: int = 0
        self.lock: Lock = Lock()


class ScrapingdogMcpApplication:
    """Factory and registry for the Scrapingdog FastMCP server.

    :param client: Scrapingdog API client used by tool handlers.
    :type client: ScrapingdogClient | None
    """

    def __init__(self, client: ScrapingdogClient | None = None) -> None:
        self.client: ScrapingdogClient = client or ScrapingdogClient()
        self.metrics_service: MetricsService | None = None
        self.session_limits: dict[ScrapingdogTools, int] = self.load_session_limits()
        self.session_usage: WeakKeyDictionary[
            object,
            dict[ScrapingdogTools, ToolUsageState],
        ] = WeakKeyDictionary()
        self.direct_call_session_key: SessionLimitFallbackKey = (
            SessionLimitFallbackKey()
        )
        self.mcp: FastMCP = FastMCP(
            "Scrapingdog",
            instructions=SERVER_INSTRUCTIONS,
            lifespan=self.lifespan,
        )
        self.register_tools()

    @asynccontextmanager
    async def lifespan(self, _server: FastMCP) -> AsyncIterator[None]:
        """Manage reusable Scrapingdog client resources.

        :param _server: FastMCP server instance.
        :type _server: FastMCP
        :return: Async lifespan iterator.
        :rtype: AsyncIterator[None]
        """

        try:
            await self.start_metrics()
            yield
        finally:
            await self.client.close()
            await self.close_metrics()

    async def start_metrics(self) -> None:
        """Start the portable metrics service when enabled.

        :return: None.
        :rtype: None
        """

        if not metrics_enabled():
            self.client.metrics = NullMetricsRecorder()
            return

        try:
            self.metrics_service = MetricsService()
        except Exception as exc:
            logger.warning(
                "MCP metrics disabled after initialization failure: %s",
                exc,
            )
            self.client.metrics = NullMetricsRecorder()
            self.metrics_service = None
            return

        self.client.metrics = self.metrics_service
        try:
            await self.metrics_service.start_http_server(
                get_metrics_host(),
                get_metrics_port(),
            )
        except MetricsConfigurationError:
            await self.close_metrics()
            raise
        except Exception as exc:
            logger.warning(
                "MCP metrics HTTP sidecar disabled after failure: %s",
                exc,
            )

    async def close_metrics(self) -> None:
        """Close the metrics service when this process owns it.

        :return: None.
        :rtype: None
        """

        if self.metrics_service is not None:
            await self.metrics_service.close()
            self.metrics_service = None
            self.client.metrics = NullMetricsRecorder()

    def register_tools(self) -> None:
        """Register all public MCP tools.

        :return: None.
        :rtype: None
        """

        self._register_google_search_tool()
        self._register_scrape_tool()

    def load_session_limits(self) -> dict[ScrapingdogTools, int]:
        """Load configured per-session tool limits from the environment.

        :return: Tool limits keyed by tool name.
        :rtype: dict[ScrapingdogTools, int]
        :raises ScrapingdogConfigurationError: If a limit is invalid.
        """

        env_vars = {
            ScrapingdogTools.GOOGLE_SEARCH: (GOOGLE_SEARCH_SESSION_LIMIT_ENV_VAR),
            ScrapingdogTools.WEBPAGE_SCRAPE: (WEBPAGE_SCRAPE_SESSION_LIMIT_ENV_VAR),
        }
        limits: dict[ScrapingdogTools, int] = {}
        for tool_name, env_var_name in env_vars.items():
            if env_var_name not in os.environ:
                continue
            raw_limit = os.environ[env_var_name]
            try:
                limit = int(raw_limit)
            except ValueError as exc:
                message = f"{env_var_name} must be a positive integer"
                raise ScrapingdogConfigurationError(message) from exc
            if limit <= 0:
                message = f"{env_var_name} must be a positive integer"
                raise ScrapingdogConfigurationError(message)
            limits[tool_name] = limit
        return limits

    def get_session_key(self, ctx: Context[Any, Any, Any]) -> object:
        """Return the key used for per-session tool usage state.

        :param ctx: FastMCP request context.
        :type ctx: Context[Any, Any, Any]
        :return: MCP session object or direct-call fallback key.
        :rtype: object
        """

        try:
            return ctx.session
        except ValueError:
            return self.direct_call_session_key

    def get_usage_state(
        self,
        ctx: Context[Any, Any, Any],
        tool_name: ScrapingdogTools,
    ) -> ToolUsageState:
        """Return mutable usage state for a tool in the current session.

        :param ctx: FastMCP request context.
        :type ctx: Context[Any, Any, Any]
        :param tool_name: Tool whose usage is being tracked.
        :type tool_name: ScrapingdogTools
        :return: Tool usage state.
        :rtype: ToolUsageState
        """

        session_key = self.get_session_key(ctx)
        usage_by_tool = self.session_usage.setdefault(session_key, {})
        if tool_name not in usage_by_tool:
            usage_by_tool[tool_name] = ToolUsageState()
        return usage_by_tool[tool_name]

    async def call_with_session_limit(
        self,
        ctx: Context[Any, Any, Any],
        tool_name: ScrapingdogTools,
        call: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Call a tool implementation while enforcing successful-call limits.

        :param ctx: FastMCP request context.
        :type ctx: Context[Any, Any, Any]
        :param tool_name: Tool being called.
        :type tool_name: ScrapingdogTools
        :param call: Awaitable Scrapingdog client call.
        :type call: Callable[[], Awaitable[dict[str, Any]]]
        :return: Scrapingdog client response.
        :rtype: dict[str, Any]
        :raises UsageLimitReachedError: If the session limit is exhausted.
        """

        limit = self.session_limits.get(tool_name)
        if limit is None:
            return await call()

        usage_state = self.get_usage_state(ctx, tool_name)
        async with usage_state.lock:
            if usage_state.successful_calls >= limit:
                raise UsageLimitReachedError(
                    self.build_usage_limit_message(tool_name, limit)
                )
            response = await call()
            usage_state.successful_calls += 1
            return response

    @staticmethod
    def build_usage_limit_message(
        tool_name: ScrapingdogTools,
        limit: int,
    ) -> str:
        """Build a model-directed usage limit error message.

        :param tool_name: Tool whose limit has been reached.
        :type tool_name: ScrapingdogTools
        :param limit: Configured successful-call limit.
        :type limit: int
        :return: Usage limit error message.
        :rtype: str
        """

        return (
            f"usage limit reached: {tool_name.value} has reached its session "
            f"limit of {limit} successful calls. Do not call "
            f"{tool_name.value} "
            "again in this MCP client session; further calls will fail until "
            "a new session is started."
        )

    def build_request(
        self,
        model_type: type[RequestModelT],
        **values: Any,
    ) -> RequestModelT:
        """Build a request model after applying forced environment overrides.

        :param model_type: Pydantic request model type to instantiate.
        :type model_type: type[RequestModelT]
        :param values: Request field values from the MCP tool call.
        :type values: Any
        :return: Validated request model.
        :rtype: RequestModelT
        """

        forced_values = {
            name: self.resolve_forced_parameter(name, value)
            for name, value in values.items()
        }
        return model_type(**forced_values)

    def resolve_forced_parameter(
        self,
        parameter_name: str,
        value: Any,
    ) -> Any:
        """Resolve a value from a forced env var or the tool argument.

        :param parameter_name: Tool parameter name.
        :type parameter_name: str
        :param value: Value supplied by the MCP tool caller.
        :type value: Any
        :return: Forced environment value, otherwise the caller value.
        :rtype: Any
        """

        env_var_name = self.force_env_var_name(parameter_name)
        if env_var_name in os.environ:
            logger.debug(
                "Using forced Scrapingdog parameter from %s",
                env_var_name,
            )
            return os.environ[env_var_name]
        return value

    @staticmethod
    def force_env_var_name(parameter_name: str) -> str:
        """Return the forced environment variable name for a parameter.

        :param parameter_name: Tool parameter name.
        :type parameter_name: str
        :return: Environment variable name with Scrapingdog force prefix.
        :rtype: str
        """

        return (
            f"{FORCE_ENV_PREFIX}"
            f"{ScrapingdogMcpApplication.to_env_name(parameter_name)}"
        )

    @staticmethod
    def to_env_name(parameter_name: str) -> str:
        """Convert a Scrapingdog parameter name into env-var format.

        :param parameter_name: Parameter name such as ``advance_search``.
        :type parameter_name: str
        :return: Upper snake-case parameter name.
        :rtype: str
        """

        env_name_parts: list[str] = []
        for index, character in enumerate(parameter_name):
            previous_character = parameter_name[index - 1] if index > 0 else ""
            next_character = ""
            if index + 1 < len(parameter_name):
                next_character = parameter_name[index + 1]
            should_add_separator = (
                index > 0
                and character.isupper()
                and (
                    previous_character.islower()
                    or previous_character.isdigit()
                    or (previous_character.isupper() and next_character.islower())
                )
            )
            if should_add_separator:
                env_name_parts.append("_")
            if character in {"-", " "}:
                env_name_parts.append("_")
            else:
                env_name_parts.append(character.upper())
        return "".join(env_name_parts)

    def _register_google_search_tool(self) -> None:
        """Register the Google Search tool.

        :return: None.
        :rtype: None
        """

        async def google_search(
            query: Annotated[
                str,
                Field(
                    description=(
                        "Google search query. Supports operators like site:, "
                        "inurl:, and intitle:."
                    ),
                ),
            ],
            ctx: Context[Any, Any, Any],
            advance_search: Annotated[
                bool | None,
                Field(
                    description="Include advanced Google result features and snippets.",
                ),
            ] = None,
            mob_search: Annotated[
                bool | None,
                Field(description="Return mobile Google search results."),
            ] = None,
            html: Annotated[
                bool | None,
                Field(
                    description=(
                        "Return raw Google results-page HTML instead of parsed JSON."
                    ),
                ),
            ] = None,
            domain: Annotated[
                str | None,
                Field(
                    description=(
                        "Google domain to search, such as google.com or google.co.uk."
                    ),
                ),
            ] = None,
            country: Annotated[
                str | None,
                Field(
                    description=(
                        "Two-letter country code for localized results, such as "
                        "us, uk, or fr."
                    ),
                ),
            ] = None,
            location: Annotated[
                str | None,
                Field(
                    description=(
                        "Search origin location; city-level values usually work best."
                    ),
                ),
            ] = None,
            language: Annotated[
                str | None,
                Field(
                    description=(
                        "Language code for results, such as en, es, fr, or de."
                    ),
                ),
            ] = None,
            safe: Annotated[
                Literal["active", "off"] | None,
                Field(
                    description=(
                        "SafeSearch setting: active filters adult content, off "
                        "disables filtering."
                    ),
                ),
            ] = None,
            nfpr: Annotated[
                bool | None,
                Field(
                    description=(
                        "Exclude results from Google's auto-corrected spelling."
                    ),
                ),
            ] = None,
            filter: Annotated[
                bool | None,
                Field(
                    description=(
                        "Enable Google's similar and omitted-results filters."
                    ),
                ),
            ] = None,
            results: Annotated[
                int | None,
                Field(
                    ge=1,
                    description="Number of Google results to request.",
                ),
            ] = 10,
            page: Annotated[
                int | None,
                Field(
                    ge=0,
                    description=(
                        "Zero-based results page: 0 is the first page, 1 is the "
                        "second."
                    ),
                ),
            ] = 0,
        ) -> dict[str, Any]:
            """Search Google web results."""

            request = self.build_request(
                GoogleSearchRequest,
                advance_search=advance_search,
                mob_search=mob_search,
                html=html,
                query=query,
                domain=domain,
                country=country,
                location=location,
                language=language,
                safe=safe,
                nfpr=nfpr,
                filter=filter,
                results=results,
                page=page,
            )
            return await self.call_with_session_limit(
                ctx,
                ScrapingdogTools.GOOGLE_SEARCH,
                lambda: self.client.google(request),
            )

        self.mcp.add_tool(
            google_search,
            name=ScrapingdogTools.GOOGLE_SEARCH.value,
            title="Google Search",
            description="Search Google web results.",
            annotations=READ_ONLY_OPEN_WEB_ANNOTATIONS,
            structured_output=True,
        )

    def _register_scrape_tool(self) -> None:
        """Register the webpage scrape tool.

        :return: None.
        :rtype: None
        """

        async def webpage_scrape(
            url: Annotated[
                str,
                Field(description="Decoded absolute URL of the page to scrape."),
            ],
            ctx: Context[Any, Any, Any],
            dynamic: Annotated[
                bool | None,
                Field(
                    description=(
                        "Render JavaScript before scraping. Defaults to true "
                        "when omitted."
                    ),
                ),
            ] = None,
            format: Annotated[
                Literal["markdown", "summary", "links", "images"] | None,
                Field(
                    description=(
                        "Output format: markdown, summary, links, or images. "
                        "Omit for HTML."
                    ),
                ),
            ] = None,
        ) -> dict[str, Any]:
            """Scrape a webpage URL."""

            request = self.build_request(
                WebpageRequest,
                url=url,
                dynamic=dynamic,
                format=format,
            )
            return await self.call_with_session_limit(
                ctx,
                ScrapingdogTools.WEBPAGE_SCRAPE,
                lambda: self.client.scrape(request),
            )

        self.mcp.add_tool(
            webpage_scrape,
            name=ScrapingdogTools.WEBPAGE_SCRAPE.value,
            title="Webpage Scrape",
            description="Scrape a webpage URL.",
            annotations=READ_ONLY_OPEN_WEB_ANNOTATIONS,
            structured_output=True,
        )


def create_mcp_server(client: ScrapingdogClient | None = None) -> FastMCP:
    """Create the Scrapingdog FastMCP server.

    :param client: Optional Scrapingdog client for tests.
    :type client: ScrapingdogClient | None
    :return: Configured FastMCP server.
    :rtype: FastMCP
    """

    load_dotenv()
    application = ScrapingdogMcpApplication(client=client)
    return application.mcp


server = create_mcp_server()


def main() -> None:
    """Run the Scrapingdog MCP server over stdio.

    :return: None.
    :rtype: None
    """

    server.run(transport="stdio")

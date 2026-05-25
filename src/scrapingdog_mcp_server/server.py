"""FastMCP server exposing Scrapingdog search and scrape tools."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, TypeVar

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from .core import ScrapingdogClient
from .enums import ScrapingdogTools
from .schemas import GoogleSearchRequest, WebpageRequest

SERVER_INSTRUCTIONS = (
    "Search Google and scrape webpages through Scrapingdog. Tools call "
    "external Scrapingdog endpoints and return Scrapingdog responses as "
    "structured content."
)

FORCE_ENV_PREFIX = "SCRAPINGDOG_FORCE_"

READ_ONLY_OPEN_WEB_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

logger = logging.getLogger(__name__)
RequestModelT = TypeVar("RequestModelT", bound=BaseModel)


class ScrapingdogMcpApplication:
    """Factory and registry for the Scrapingdog FastMCP server.

    :param client: Scrapingdog API client used by tool handlers.
    :type client: ScrapingdogClient | None
    """

    def __init__(self, client: ScrapingdogClient | None = None) -> None:
        self.client: ScrapingdogClient = client or ScrapingdogClient()
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
            yield
        finally:
            await self.client.close()

    def register_tools(self) -> None:
        """Register all public MCP tools.

        :return: None.
        :rtype: None
        """

        self._register_google_search_tool()
        self._register_scrape_tool()

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
            next_character = (
                parameter_name[index + 1] if index + 1 < len(parameter_name) else ""
            )
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
            query: str,
            advance_search: bool | None = None,
            mob_search: bool | None = None,
            html: bool | None = None,
            domain: str | None = None,
            country: str | None = None,
            location: str | None = None,
            language: str | None = None,
            safe: Literal["active", "off"] | None = None,
            nfpr: bool | None = None,
            filter: bool | None = None,
            results: int | None = 10,
            page: int | None = 0,
        ) -> dict[str, Any]:
            """Search Google web results through Scrapingdog."""

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
            return await self.client.google(request)

        self.mcp.add_tool(
            google_search,
            name=ScrapingdogTools.GOOGLE_SEARCH.value,
            title="Google Search",
            description="Search Google web results through Scrapingdog.",
            annotations=READ_ONLY_OPEN_WEB_ANNOTATIONS,
            structured_output=True,
        )

    def _register_scrape_tool(self) -> None:
        """Register the webpage scrape tool.

        :return: None.
        :rtype: None
        """

        async def webpage_scrape(
            url: str,
            dynamic: bool | None = None,
            formats: Literal["markdown", "summary", "links", "images"] | None = None,
        ) -> dict[str, Any]:
            """Scrape a webpage through Scrapingdog."""

            request = self.build_request(
                WebpageRequest,
                url=url,
                dynamic=dynamic,
                formats=formats,
            )
            return await self.client.scrape(request)

        self.mcp.add_tool(
            webpage_scrape,
            name=ScrapingdogTools.WEBPAGE_SCRAPE.value,
            title="Webpage Scrape",
            description="Scrape a webpage URL through Scrapingdog.",
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

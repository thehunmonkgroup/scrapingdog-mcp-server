"""Scrapingdog HTTP client used by the MCP server tools."""

from __future__ import annotations

import logging
import os
import ssl
from time import perf_counter
from typing import Any

import aiohttp
import certifi
from pydantic import BaseModel

from .metrics import MetricEvent, MetricsRecorder, NullMetricsRecorder
from .schemas import GoogleSearchRequest, WebpageRequest

DEFAULT_AIOHTTP_TIMEOUT_SECONDS = 30
GOOGLE_SCRAPINGDOG_URL = "https://api.scrapingdog.com/google/"
SCRAPE_SCRAPINGDOG_URL = "https://api.scrapingdog.com/scrape"
SCRAPINGDOG_API_KEY_ENV_VAR = "SCRAPINGDOG_API_KEY"
SCRAPINGDOG_REQUEST_TIMEOUT_ENV_VAR = "SCRAPINGDOG_REQUEST_TIMEOUT"

BOOLEAN_QUERY_FIELDS = frozenset({"advance_search", "mob_search", "html", "dynamic"})
INTEGER_BOOLEAN_QUERY_FIELDS = frozenset({"nfpr", "filter"})
OMIT_FALSE_QUERY_FIELDS = frozenset({"mob_search"})

logger = logging.getLogger(__name__)


class ScrapingdogClientError(Exception):
    """Error raised for expected Scrapingdog client failures."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize a Scrapingdog client error.

        :param message: Error message.
        :type message: str
        :param status_code: Optional HTTP status code.
        :type status_code: int | None
        """

        super().__init__(message)
        self.status_code: int | None = status_code


class ScrapingdogConfigurationError(ScrapingdogClientError):
    """Error raised when server configuration is invalid or incomplete."""


class ScrapingdogClient:
    """Reusable asynchronous Scrapingdog API client.

    :param api_key: Scrapingdog API key. When omitted, it is read lazily from
        the environment.
    :type api_key: str | None
    :param timeout_seconds: Request timeout in seconds. When omitted, it is
        read from the environment.
    :type timeout_seconds: int | None
    :param session: Optional injected aiohttp session for tests.
    :type session: aiohttp.ClientSession | None
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout_seconds: int | None = None,
        session: aiohttp.ClientSession | None = None,
        metrics: MetricsRecorder | None = None,
    ) -> None:
        self._api_key: str | None = api_key
        self._timeout_seconds: int | None = timeout_seconds
        self._session: aiohttp.ClientSession | None = session
        self._owns_session: bool = session is None
        self.metrics: MetricsRecorder = metrics or NullMetricsRecorder()

    async def google(self, request: GoogleSearchRequest) -> dict[str, Any]:
        """Search Google through Scrapingdog.

        :param request: Validated search request.
        :type request: GoogleSearchRequest
        :return: Scrapingdog JSON response.
        :rtype: dict[str, Any]
        :raises ScrapingdogClientError: If the API call fails.
        """

        started_at = perf_counter()
        try:
            response, status_code = await self.fetch_json_response(
                GOOGLE_SCRAPINGDOG_URL,
                request,
            )
        except ScrapingdogClientError as exc:
            await self.record_search_metric(
                request=request,
                started_at=started_at,
                succeeded=False,
                status_code=exc.status_code,
                error=str(exc),
            )
            raise
        await self.record_search_metric(
            request=request,
            started_at=started_at,
            succeeded=True,
            status_code=status_code,
            response=response,
        )
        return response

    async def scrape(self, request: WebpageRequest) -> dict[str, Any]:
        """Scrape a webpage through Scrapingdog.

        :param request: Validated webpage scrape request.
        :type request: WebpageRequest
        :return: Wrapped Scrapingdog text response.
        :rtype: dict[str, Any]
        :raises ScrapingdogClientError: If the API call fails.
        """

        started_at = perf_counter()
        try:
            response = await self.fetch_text(SCRAPE_SCRAPINGDOG_URL, request)
        except ScrapingdogClientError as exc:
            await self.record_scrape_metric(
                request=request,
                started_at=started_at,
                succeeded=False,
                status_code=exc.status_code,
                error=str(exc),
            )
            raise
        await self.record_scrape_metric(
            request=request,
            started_at=started_at,
            succeeded=True,
            status_code=int(response["status"]),
            response=response,
        )
        return response

    async def fetch_json(self, url: str, request: BaseModel) -> dict[str, Any]:
        """Get a Scrapingdog JSON endpoint and return its object body.

        :param url: Scrapingdog endpoint URL.
        :type url: str
        :param request: Validated request model.
        :type request: BaseModel
        :return: Scrapingdog JSON response.
        :rtype: dict[str, Any]
        :raises ScrapingdogClientError: If the API call fails.
        """

        json_body, _status_code = await self.fetch_json_response(url, request)
        return json_body

    async def fetch_json_response(
        self,
        url: str,
        request: BaseModel,
    ) -> tuple[dict[str, Any], int]:
        """Get a Scrapingdog JSON endpoint and return body with status.

        :param url: Scrapingdog endpoint URL.
        :type url: str
        :param request: Validated request model.
        :type request: BaseModel
        :return: Scrapingdog JSON response and HTTP status.
        :rtype: tuple[dict[str, Any], int]
        :raises ScrapingdogClientError: If the API call fails.
        """

        session = await self.get_session()
        params = self.build_query_params(request)
        logger.debug("Getting Scrapingdog request from %s", url)

        try:
            async with session.get(url, params=params) as response:
                await self.raise_for_error_status(response)
                try:
                    json_body = await response.json(content_type=None)
                except aiohttp.ContentTypeError as exc:
                    raise ScrapingdogClientError(
                        "Scrapingdog API returned a non-JSON response",
                        response.status,
                    ) from exc
                status_code = response.status
        except TimeoutError as exc:
            logger.warning("Scrapingdog request timed out: %s", url)
            raise ScrapingdogClientError("Scrapingdog API request timed out") from exc
        except aiohttp.ClientError as exc:
            logger.warning("Scrapingdog request failed: %s", url)
            raise ScrapingdogClientError(
                f"Scrapingdog API request failed: {exc}"
            ) from exc
        if not isinstance(json_body, dict):
            raise ScrapingdogClientError(
                "Scrapingdog API returned an unexpected JSON shape",
                status_code,
            )
        return json_body, status_code

    async def fetch_text(
        self,
        url: str,
        request: WebpageRequest,
    ) -> dict[str, Any]:
        """Get a Scrapingdog text endpoint and wrap its response.

        :param url: Scrapingdog endpoint URL.
        :type url: str
        :param request: Validated webpage request model.
        :type request: WebpageRequest
        :return: Structured wrapper around the raw response text.
        :rtype: dict[str, Any]
        :raises ScrapingdogClientError: If the API call fails.
        """

        session = await self.get_session()
        params = self.build_query_params(request)
        logger.debug("Getting Scrapingdog request from %s", url)

        try:
            async with session.get(url, params=params) as response:
                await self.raise_for_error_status(response)
                response_text = await response.text()
                return {
                    "format": request.formats or "html",
                    "content": response_text,
                    "status": response.status,
                }
        except TimeoutError as exc:
            logger.warning("Scrapingdog request timed out: %s", url)
            raise ScrapingdogClientError("Scrapingdog API request timed out") from exc
        except aiohttp.ClientError as exc:
            logger.warning("Scrapingdog request failed: %s", url)
            raise ScrapingdogClientError(
                f"Scrapingdog API request failed: {exc}"
            ) from exc

    async def raise_for_error_status(
        self,
        response: aiohttp.ClientResponse,
    ) -> None:
        """Raise a client error for unsuccessful Scrapingdog HTTP responses.

        :param response: Scrapingdog HTTP response.
        :type response: aiohttp.ClientResponse
        :return: None.
        :rtype: None
        :raises ScrapingdogClientError: If the API call fails.
        """

        if response.status >= 400:
            response_text = await response.text()
            message = (
                f"Scrapingdog API returned HTTP {response.status}: "
                f"{response_text[:500]}"
            )
            raise ScrapingdogClientError(
                message,
                response.status,
            )

    def build_query_params(self, request: BaseModel) -> dict[str, str]:
        """Build Scrapingdog query parameters for a request model.

        :param request: Validated request model.
        :type request: BaseModel
        :return: Query parameters including the API key.
        :rtype: dict[str, str]
        """

        params = {"api_key": self.api_key}
        for name, value in request.model_dump(exclude_none=True).items():
            if name in OMIT_FALSE_QUERY_FIELDS and value is False:
                continue
            params[name] = self.serialize_query_value(name, value)
        return params

    def serialize_query_value(self, name: str, value: Any) -> str:
        """Serialize a model field value for Scrapingdog query parameters.

        :param name: Request field name.
        :type name: str
        :param value: Request field value.
        :type value: Any
        :return: Serialized query value.
        :rtype: str
        """

        if name in BOOLEAN_QUERY_FIELDS and isinstance(value, bool):
            return str(value).lower()
        if name in INTEGER_BOOLEAN_QUERY_FIELDS and isinstance(value, bool):
            return "1" if value else "0"
        return str(value)

    async def record_search_metric(
        self,
        *,
        request: GoogleSearchRequest,
        started_at: float,
        succeeded: bool,
        status_code: int | None = None,
        response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Record a Google Search metric event.

        :param request: Search request.
        :type request: GoogleSearchRequest
        :param started_at: Monotonic start time.
        :type started_at: float
        :param succeeded: Whether the request succeeded.
        :type succeeded: bool
        :param status_code: Optional HTTP status code.
        :type status_code: int | None
        :param response: Optional response body.
        :type response: dict[str, Any] | None
        :param error: Optional error detail.
        :type error: str | None
        :return: None.
        :rtype: None
        """

        await self.metrics.record_request(
            MetricEvent(
                tool="google_search",
                request_type="search",
                succeeded=succeeded,
                latency_ms=elapsed_ms(started_at),
                status_code=status_code,
                query=request.query,
                result_count=count_organic_results(response),
                returned_bytes=count_json_bytes(response),
                error=error,
            )
        )

    async def record_scrape_metric(
        self,
        *,
        request: WebpageRequest,
        started_at: float,
        succeeded: bool,
        status_code: int | None = None,
        response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Record a webpage scrape metric event.

        :param request: Webpage scrape request.
        :type request: WebpageRequest
        :param started_at: Monotonic start time.
        :type started_at: float
        :param succeeded: Whether the request succeeded.
        :type succeeded: bool
        :param status_code: Optional HTTP status code.
        :type status_code: int | None
        :param response: Optional response body.
        :type response: dict[str, Any] | None
        :param error: Optional error detail.
        :type error: str | None
        :return: None.
        :rtype: None
        """

        await self.metrics.record_request(
            MetricEvent(
                tool="webpage_scrape",
                request_type="scrape",
                succeeded=succeeded,
                latency_ms=elapsed_ms(started_at),
                status_code=status_code,
                url=request.url,
                returned_bytes=count_response_content_bytes(response),
                response_format=request.formats or "html",
                error=error,
            )
        )

    async def get_session(self) -> aiohttp.ClientSession:
        """Return the reusable aiohttp session.

        :return: Active aiohttp client session.
        :rtype: aiohttp.ClientSession
        :raises ScrapingdogConfigurationError: If required configuration is
            missing or invalid.
        """

        if not self.api_key:
            raise ScrapingdogConfigurationError(
                f"{SCRAPINGDOG_API_KEY_ENV_VAR} is empty"
            )

        if self._session is None or self._session.closed:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Close the owned aiohttp session.

        :return: None.
        :rtype: None
        """

        if self._owns_session and self._session is not None:
            await self._session.close()

    @property
    def api_key(self) -> str:
        """Return the configured Scrapingdog API key.

        :return: Scrapingdog API key, or an empty string if unset.
        :rtype: str
        """

        return (self._api_key or os.getenv(SCRAPINGDOG_API_KEY_ENV_VAR, "")).strip()

    @property
    def timeout_seconds(self) -> int:
        """Return the configured timeout.

        :return: Request timeout in seconds.
        :rtype: int
        :raises ScrapingdogConfigurationError: If timeout configuration is
            invalid.
        """

        if self._timeout_seconds is not None:
            return self._timeout_seconds

        value = os.getenv(
            SCRAPINGDOG_REQUEST_TIMEOUT_ENV_VAR,
            str(DEFAULT_AIOHTTP_TIMEOUT_SECONDS),
        ).strip()
        try:
            timeout_seconds = int(value)
        except ValueError as exc:
            raise ScrapingdogConfigurationError(
                f"{SCRAPINGDOG_REQUEST_TIMEOUT_ENV_VAR} must be an integer"
            ) from exc
        if timeout_seconds <= 0:
            raise ScrapingdogConfigurationError(
                f"{SCRAPINGDOG_REQUEST_TIMEOUT_ENV_VAR} must be greater than 0"
            )
        return timeout_seconds


def elapsed_ms(started_at: float) -> float:
    """Return elapsed milliseconds since a monotonic start time.

    :param started_at: Monotonic start time.
    :type started_at: float
    :return: Elapsed milliseconds.
    :rtype: float
    """

    return (perf_counter() - started_at) * 1000


def count_organic_results(response: dict[str, Any] | None) -> int | None:
    """Count organic search results in a Scrapingdog response.

    :param response: Scrapingdog response.
    :type response: dict[str, Any] | None
    :return: Organic result count when available.
    :rtype: int | None
    """

    if response is None:
        return None
    organic_results = response.get("organic_results")
    if isinstance(organic_results, list):
        return len(organic_results)
    return None


def count_json_bytes(response: dict[str, Any] | None) -> int | None:
    """Count serialized JSON response bytes.

    :param response: JSON response.
    :type response: dict[str, Any] | None
    :return: Response byte count.
    :rtype: int | None
    """

    if response is None:
        return None
    return len(str(response).encode("utf-8"))


def count_response_content_bytes(
    response: dict[str, Any] | None,
) -> int | None:
    """Count wrapped scrape response content bytes.

    :param response: Wrapped scrape response.
    :type response: dict[str, Any] | None
    :return: Content byte count.
    :rtype: int | None
    """

    if response is None:
        return None
    content = response.get("content")
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    return None

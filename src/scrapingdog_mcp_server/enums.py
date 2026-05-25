"""Enum values used by the Scrapingdog MCP server."""

from __future__ import annotations

from enum import StrEnum


class ScrapingdogTools(StrEnum):
    """Public MCP tool names exposed by the server."""

    GOOGLE_SEARCH = "google_search"
    WEBPAGE_SCRAPE = "webpage_scrape"

    @classmethod
    def has_value(cls, value: str) -> bool:
        """Return whether a string is a known tool value.

        :param value: Candidate tool value.
        :type value: str
        :return: Whether the value is known.
        :rtype: bool
        """

        return value in cls._value2member_map_

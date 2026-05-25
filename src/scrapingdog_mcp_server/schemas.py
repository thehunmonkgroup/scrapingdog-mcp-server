"""Request models for Scrapingdog MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GoogleSearchRequest(BaseModel):
    """Request fields for Scrapingdog Google Search."""

    advance_search: bool | None = Field(
        None,
        description="Enable Scrapingdog advanced Google Search mode.",
    )
    mob_search: bool | None = Field(
        None,
        description="Use mobile Google Search results.",
    )
    html: bool | None = Field(
        None,
        description="Return the raw Google Search HTML response.",
    )
    query: str = Field(..., description="The Google query to search for.")
    domain: str | None = Field(
        None,
        description="The Google domain to search, such as google.com.",
    )
    country: str | None = Field(
        None,
        description="The country code to search in, such as us or uk.",
    )
    location: str | None = Field(
        None,
        description="The geographic location to search from.",
    )
    language: str | None = Field(
        None,
        description="The language code for search results, such as en.",
    )
    safe: Literal["active", "off"] | None = Field(
        None,
        description="Google safe search setting.",
    )
    nfpr: bool | None = Field(
        None,
        description="Exclude results from auto-corrected queries.",
    )
    filter: bool | None = Field(
        None,
        description="Enable or disable duplicate result filtering.",
    )
    results: int | None = Field(
        None,
        ge=1,
        description="The number of Google Search results to return.",
    )
    page: int | None = Field(
        None,
        ge=0,
        description="The Google Search results page to return.",
    )


class WebpageRequest(BaseModel):
    """Request fields for Scrapingdog webpage scraping."""

    url: str = Field(..., description="The URL to scrape.")
    dynamic: bool | None = Field(
        None,
        description="Enable dynamic browser rendering.",
    )
    formats: Literal["markdown", "summary", "links", "images"] | None = Field(
        None,
        description="Optional output format. When omitted, HTML is returned.",
    )

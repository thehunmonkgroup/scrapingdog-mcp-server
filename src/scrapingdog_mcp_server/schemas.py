"""Request models for Scrapingdog MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GoogleSearchRequest(BaseModel):
    """Request fields for Scrapingdog Google Search."""

    advance_search: bool | None = Field(
        None,
        description="Include advanced Google result features and snippets.",
    )
    mob_search: bool | None = Field(
        None,
        description="Return mobile Google search results.",
    )
    html: bool | None = Field(
        None,
        description="Return raw Google results-page HTML instead of parsed JSON.",
    )
    query: str = Field(
        ...,
        description="Google search query. Supports operators like site:, inurl:, and intitle:.",
    )
    domain: str | None = Field(
        None,
        description="Google domain to search, such as google.com or google.co.uk.",
    )
    country: str | None = Field(
        None,
        description="Two-letter country code for localized results, such as us, uk, or fr.",
    )
    location: str | None = Field(
        None,
        description="Search origin location; city-level values usually work best.",
    )
    language: str | None = Field(
        None,
        description="Language code for results, such as en, es, fr, or de.",
    )
    safe: Literal["active", "off"] | None = Field(
        None,
        description="SafeSearch setting: active filters adult content, off disables filtering.",
    )
    nfpr: bool | None = Field(
        None,
        description="Exclude results from Google's auto-corrected spelling.",
    )
    filter: bool | None = Field(
        None,
        description="Enable Google's similar and omitted-results filters.",
    )
    results: int | None = Field(
        None,
        ge=1,
        description="Number of Google results to request.",
    )
    page: int | None = Field(
        None,
        ge=0,
        description="Zero-based results page: 0 is the first page, 1 is the second.",
    )


class WebpageRequest(BaseModel):
    """Request fields for Scrapingdog webpage scraping."""

    url: str = Field(..., description="Decoded absolute URL of the page to scrape.")
    dynamic: bool | None = Field(
        None,
        description="Render JavaScript before scraping. Defaults to true when omitted.",
    )
    format: Literal["markdown", "summary", "links", "images"] | None = Field(
        None,
        description="Output format: markdown, summary, links, or images. Omit for HTML.",
    )

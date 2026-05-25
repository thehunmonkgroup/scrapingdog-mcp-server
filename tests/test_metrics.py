"""Tests for the portable SQLite metrics sidecar."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any
from typing_extensions import override

import pytest

from scrapingdog_mcp_server.metrics import (
    DEFAULT_METRICS_PORT,
    METRICS_PORT_ENV_VAR,
    MetricEvent,
    MetricsPortConflictError,
    MetricsProbeResult,
    MetricsService,
    get_metrics_port,
    hash_value,
)


class MutableClock:
    """Mutable clock test double."""

    def __init__(self, value: datetime) -> None:
        self.value: datetime = value

    def __call__(self) -> datetime:
        """Return the configured datetime.

        :return: Current fake datetime.
        :rtype: datetime
        """

        return self.value


class ProbeMetricsService(MetricsService):
    """Metrics service test double with configurable probe result."""

    def __init__(
        self,
        db_path: Path,
        probe_result: MetricsProbeResult,
    ) -> None:
        super().__init__(db_path)
        self.probe_result: MetricsProbeResult = probe_result

    @override
    async def probe_metrics_service(
        self,
        host: str,
        port: int,
    ) -> MetricsProbeResult:
        """Return the configured probe result.

        :param host: Host to probe.
        :type host: str
        :param port: Port to probe.
        :type port: int
        :return: Configured probe result.
        :rtype: MetricsProbeResult
        """

        return self.probe_result


def run_async(awaitable: Coroutine[Any, Any, Any]) -> Any:
    """Run an awaitable for tests.

    :param awaitable: Awaitable object.
    :type awaitable: object
    :return: Awaitable result.
    :rtype: object
    """

    return asyncio.run(awaitable)


def test_metrics_port_defaults_to_3004(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default metrics port is 3004."""

    monkeypatch.delenv(METRICS_PORT_ENV_VAR, raising=False)

    assert get_metrics_port() == DEFAULT_METRICS_PORT == 3004


def test_daily_runs_are_created_from_event_dates(tmp_path: Path) -> None:
    """Metrics events on different local dates use different runs."""

    clock = MutableClock(datetime(2026, 5, 24, 10, 30, 0))
    service = MetricsService(tmp_path / "metrics.sqlite3", clock=clock)

    run_async(
        service.record_request(
            MetricEvent(
                tool="google_search",
                request_type="search",
                succeeded=True,
                latency_ms=10,
                status_code=200,
                query="openai",
                result_count=7,
                returned_bytes=1024,
            )
        )
    )
    first_run = service.resolve_run(clock())

    clock.value = datetime(2026, 5, 24, 23, 59, 0)
    run_async(
        service.record_request(
            MetricEvent(
                tool="webpage_scrape",
                request_type="scrape",
                succeeded=True,
                latency_ms=20,
                status_code=200,
                url="https://example.com",
                returned_bytes=2048,
                response_format="markdown",
            )
        )
    )
    second_same_day_run = service.resolve_run(clock())

    clock.value = datetime(2026, 5, 25, 0, 1, 0)
    run_async(
        service.record_request(
            MetricEvent(
                tool="google_search",
                request_type="search",
                succeeded=False,
                latency_ms=30,
                status_code=400,
                query="openai",
                error="bad request",
            )
        )
    )
    next_day_run = service.resolve_run(clock())

    assert first_run.id == second_same_day_run.id
    assert next_day_run.id != first_run.id
    assert [run.run_key for run in [first_run, next_day_run]] == [
        "2026-05-24",
        "2026-05-25",
    ]

    run_async(service.close())


def test_metrics_report_aggregates_requests(tmp_path: Path) -> None:
    """Metrics reports include totals, tool rows, statuses, and failures."""

    clock = MutableClock(datetime(2026, 5, 24, 12, 0, 0))
    service = MetricsService(tmp_path / "metrics.sqlite3", clock=clock)

    run_async(
        service.record_request(
            MetricEvent(
                tool="google_search",
                request_type="search",
                succeeded=True,
                latency_ms=100,
                status_code=200,
                query="openai",
                result_count=8,
                returned_bytes=1024 * 1024,
            )
        )
    )
    run_async(
        service.record_request(
            MetricEvent(
                tool="webpage_scrape",
                request_type="scrape",
                succeeded=False,
                latency_ms=200,
                status_code=500,
                url="https://example.com",
                error="server error",
            )
        )
    )

    report = service.get_metrics_report("current")
    search_row = next(
        row for row in report["per_tool"] if row["tool"] == "google_search"
    )

    assert report["run"]["run_key"] == "2026-05-24"
    assert report["totals"]["attempted_requests"] == 2
    assert report["totals"]["successful_requests"] == 1
    assert report["totals"]["failed_requests"] == 1
    assert report["status_codes"] == [
        {"status_code": 200, "count": 1},
        {"status_code": 500, "count": 1},
    ]
    assert report["recent_failures"][0]["tool"] == "webpage_scrape"
    assert search_row["average_result_count"] == 8

    stored_query_hash = service.db.execute("""
        SELECT query_hash
        FROM request_events
        WHERE tool = 'google_search'
        """).fetchone()["query_hash"]
    assert stored_query_hash == hash_value("openai")

    run_async(service.close())


def test_metrics_sidecar_skips_start_when_existing_service_is_present(
    tmp_path: Path,
) -> None:
    """An existing portable metrics sidecar is reused instead of replaced."""

    service = ProbeMetricsService(tmp_path / "metrics.sqlite3", "metrics")

    run_async(service.start_http_server("127.0.0.1", 3004))

    assert service.runner is None

    run_async(service.close())


def test_metrics_sidecar_raises_when_port_belongs_to_another_service(
    tmp_path: Path,
) -> None:
    """A non-metrics service on the configured port raises a conflict."""

    service = ProbeMetricsService(tmp_path / "metrics.sqlite3", "other")

    with pytest.raises(MetricsPortConflictError):
        run_async(service.start_http_server("127.0.0.1", 3004))

    run_async(service.close())


def test_metrics_sidecar_starts_when_port_is_available(tmp_path: Path) -> None:
    """The sidecar starts when no service is already listening."""

    service = ProbeMetricsService(tmp_path / "metrics.sqlite3", "unavailable")

    run_async(service.start_http_server("127.0.0.1", 0))

    assert service.runner is not None

    run_async(service.close())

"""Portable SQLite metrics sidecar for search and scrape MCP servers."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from aiohttp import ClientError, ClientSession, ClientTimeout, web

DEFAULT_METRICS_DATA_DIR = "data"
DEFAULT_METRICS_DB_NAME = "mcp-metrics.sqlite3"
DEFAULT_METRICS_HOST = "127.0.0.1"
DEFAULT_METRICS_PORT = 3004
METRICS_DATA_DIR_ENV_VAR = "MCP_METRICS_DATA_DIR"
METRICS_DB_PATH_ENV_VAR = "MCP_METRICS_DB_PATH"
METRICS_ENABLED_ENV_VAR = "MCP_METRICS_ENABLED"
METRICS_HOST_ENV_VAR = "MCP_METRICS_HOST"
METRICS_PORT_ENV_VAR = "MCP_METRICS_PORT"
METRICS_HEALTH_PATH = "/health"
METRICS_SERVICE_ID = "mcp-metrics-sidecar"
METRICS_HEALTH_VERSION = "1"
METRICS_PROBE_TIMEOUT_SECONDS = 0.5
MAX_RECENT_FAILURES = 10

MetricRequestType = Literal["search", "scrape"]
MetricsScope = Literal["current", "all_time", "run"]
MetricsProbeResult = Literal["metrics", "other", "unavailable", "unresponsive"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricEvent:
    """Request metric event persisted by the metrics recorder.

    :param tool: Public tool name.
    :type tool: str
    :param request_type: Generic request type.
    :type request_type: MetricRequestType
    :param succeeded: Whether the request completed successfully.
    :type succeeded: bool
    :param latency_ms: Request latency in milliseconds.
    :type latency_ms: float
    :param status_code: Optional HTTP status code.
    :type status_code: int | None
    :param query: Optional raw search query. It is hashed before persistence.
    :type query: str | None
    :param url: Optional raw URL. It is hashed before persistence.
    :type url: str | None
    :param result_count: Optional count of returned search results.
    :type result_count: int | None
    :param returned_bytes: Optional number of returned response bytes.
    :type returned_bytes: int | None
    :param response_format: Optional response format name.
    :type response_format: str | None
    :param error: Optional error detail.
    :type error: str | None
    :param occurred_at: Optional event timestamp.
    :type occurred_at: datetime | None
    """

    tool: str
    request_type: MetricRequestType
    succeeded: bool
    latency_ms: float
    status_code: int | None = None
    query: str | None = None
    url: str | None = None
    result_count: int | None = None
    returned_bytes: int | None = None
    response_format: str | None = None
    error: str | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class RunRow:
    """SQLite run row.

    :param id: Run primary key.
    :type id: int
    :param run_key: Daily run key.
    :type run_key: str
    :param started_at: Run start timestamp.
    :type started_at: str
    :param observed_at: First observed timestamp.
    :type observed_at: str
    """

    id: int
    run_key: str
    started_at: str
    observed_at: str


class MetricsRecorder(Protocol):
    """Interface implemented by metrics recorders."""

    async def record_request(self, event: MetricEvent) -> None:
        """Record one request event.

        :param event: Metric event to record.
        :type event: MetricEvent
        :return: None.
        :rtype: None
        """


class NullMetricsRecorder:
    """Metrics recorder that intentionally discards events."""

    async def record_request(self, event: MetricEvent) -> None:
        """Discard one request event.

        :param event: Metric event to discard.
        :type event: MetricEvent
        :return: None.
        :rtype: None
        """

        del event


class MetricsConfigurationError(Exception):
    """Error raised for invalid metrics configuration."""


class MetricsPortConflictError(MetricsConfigurationError):
    """Error raised when the metrics port is occupied by another service."""


class MetricsUnresponsivePortError(MetricsConfigurationError):
    """Error raised when the metrics port is occupied but unresponsive."""


class MetricsService:
    """SQLite metrics recorder and sidecar HTTP service.

    :param db_path: Optional SQLite database path.
    :type db_path: Path | str | None
    :param clock: Optional clock used by tests.
    :type clock: Callable[[], datetime] | None
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path: Path = (
            Path(db_path) if db_path is not None else get_metrics_db_path()
        )
        self.clock: Callable[[], datetime] = clock or datetime.now
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db: sqlite3.Connection = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.initialize_schema()

    async def record_request(self, event: MetricEvent) -> None:
        """Record one request metric event.

        :param event: Metric event to persist.
        :type event: MetricEvent
        :return: None.
        :rtype: None
        """

        try:
            occurred_at = event.occurred_at or self.clock()
            run = self.resolve_run(occurred_at)
            self.db.execute(
                """
                INSERT INTO request_events (
                    run_id, occurred_at, tool, request_type, succeeded,
                    status_code, query_hash, url_hash, result_count,
                    returned_bytes, response_format, latency_ms, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    to_isoformat(occurred_at),
                    event.tool,
                    event.request_type,
                    1 if event.succeeded else 0,
                    event.status_code,
                    hash_value(event.query) if event.query else None,
                    hash_value(event.url) if event.url else None,
                    event.result_count,
                    event.returned_bytes,
                    event.response_format,
                    event.latency_ms,
                    truncate_error(event.error),
                ),
            )
            self.db.commit()
        except Exception as exc:
            logger.warning("Unable to record MCP metric event: %s", exc)

    async def start_http_server(self, host: str, port: int) -> None:
        """Start the sidecar HTTP server unless another one is already present.

        :param host: Host interface to bind.
        :type host: str
        :param port: TCP port to bind.
        :type port: int
        :return: None.
        :rtype: None
        :raises MetricsPortConflictError: If another service owns the port.
        :raises MetricsUnresponsivePortError: If the port owner is unresponsive.
        """

        existing_service = await self.probe_metrics_service(host, port)
        if existing_service == "metrics":
            logger.info(
                "MCP metrics endpoint already listening on %s:%s",
                host,
                port,
            )
            return
        if existing_service == "other":
            raise self.metrics_port_conflict_error(host, port)
        if existing_service == "unresponsive":
            raise self.metrics_unresponsive_port_error(host, port)

        app = self.create_app()
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host=host, port=port)
        try:
            await self.site.start()
        except OSError:
            await self.close_http_server()
            race_winner = await self.probe_metrics_service(host, port)
            if race_winner == "metrics":
                logger.info(
                    "MCP metrics endpoint already listening on %s:%s",
                    host,
                    port,
                )
                return
            if race_winner == "unresponsive":
                raise self.metrics_unresponsive_port_error(host, port)
            raise self.metrics_port_conflict_error(host, port)
        logger.info("MCP metrics endpoint listening on %s:%s", host, port)

    async def close(self) -> None:
        """Close the HTTP sidecar and SQLite connection.

        :return: None.
        :rtype: None
        """

        await self.close_http_server()
        self.db.close()

    async def close_http_server(self) -> None:
        """Close only the HTTP sidecar resources.

        :return: None.
        :rtype: None
        """

        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None

    def create_app(self) -> web.Application:
        """Create the aiohttp sidecar application.

        :return: Configured aiohttp application.
        :rtype: web.Application
        """

        app = web.Application()
        app.router.add_get(METRICS_HEALTH_PATH, self.handle_health)
        app.router.add_get("/metrics", self.handle_metrics)
        return app

    async def handle_health(self, _request: web.Request) -> web.Response:
        """Return sidecar health information.

        :param _request: HTTP request.
        :type _request: web.Request
        :return: JSON response.
        :rtype: web.Response
        """

        return web.json_response(
            {
                "service": METRICS_SERVICE_ID,
                "status": "ok",
                "version": METRICS_HEALTH_VERSION,
            }
        )

    async def handle_metrics(self, request: web.Request) -> web.Response:
        """Return a metrics report.

        :param request: HTTP request.
        :type request: web.Request
        :return: JSON response.
        :rtype: web.Response
        """

        try:
            scope = request.query.get("scope", "current")
            run_id = request.query.get("run_id")
            report = self.get_metrics_report(scope, run_id)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(report)

    async def probe_metrics_service(
        self,
        host: str,
        port: int,
    ) -> MetricsProbeResult:
        """Probe a host and port for an existing portable metrics sidecar.

        :param host: Host to probe.
        :type host: str
        :param port: Port to probe.
        :type port: int
        :return: Probe result.
        :rtype: MetricsProbeResult
        """

        timeout = ClientTimeout(total=METRICS_PROBE_TIMEOUT_SECONDS)
        url = f"http://{host}:{port}{METRICS_HEALTH_PATH}"
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    body = await response.json(content_type=None)
        except TimeoutError:
            return "unresponsive"
        except (ClientError, OSError):
            return "unavailable"
        except ValueError:
            return "other"
        if isinstance(body, dict) and body.get("service") == METRICS_SERVICE_ID:
            return "metrics"
        return "other"

    def get_metrics_report(
        self,
        scope: str,
        run_id_value: str | None = None,
    ) -> dict[str, Any]:
        """Build a metrics report for a scope.

        :param scope: Report scope.
        :type scope: str
        :param run_id_value: Optional run id query string value.
        :type run_id_value: str | None
        :return: Metrics report.
        :rtype: dict[str, Any]
        :raises ValueError: If the scope or run id is invalid.
        """

        if scope == "current":
            run = self.resolve_run(self.clock())
            return self.build_report("current", [run.id], run)
        if scope == "all_time":
            return self.build_report("all_time", self.get_run_ids(), None)
        if scope == "run":
            run_id = parse_run_id(run_id_value)
            run = self.get_run_by_id(run_id)
            if run is None:
                raise ValueError(f"No run found for run_id={run_id}")
            return self.build_report("run", [run.id], run)
        raise ValueError("scope must be current, all_time, or run")

    def build_report(
        self,
        scope: MetricsScope,
        run_ids: list[int],
        current_run: RunRow | None,
    ) -> dict[str, Any]:
        """Build a metrics report from run ids.

        :param scope: Report scope.
        :type scope: MetricsScope
        :param run_ids: Run ids to aggregate.
        :type run_ids: list[int]
        :param current_run: Optional current run metadata.
        :type current_run: RunRow | None
        :return: Metrics report.
        :rtype: dict[str, Any]
        """

        return {
            "scope": scope,
            "generated_at": to_isoformat(self.clock()),
            "run": serialize_run(current_run) if current_run else None,
            "totals": self.get_total_metrics(run_ids),
            "per_tool": self.get_per_tool_metrics(run_ids),
            "status_codes": self.get_status_code_metrics(run_ids),
            "recent_failures": self.get_recent_failures(run_ids),
        }

    def get_total_metrics(self, run_ids: list[int]) -> dict[str, Any]:
        """Return aggregate metrics for run ids.

        :param run_ids: Run ids to aggregate.
        :type run_ids: list[int]
        :return: Aggregate metrics.
        :rtype: dict[str, Any]
        """

        row = self.get_aggregate_row(run_ids)
        attempted = int(row["attempted_requests"] or 0)
        successful = int(row["successful_requests"] or 0)
        failed = attempted - successful
        returned_bytes = int(row["returned_bytes"] or 0)
        return {
            "attempted_requests": attempted,
            "successful_requests": successful,
            "failed_requests": failed,
            "success_percentage": percentage(successful, attempted),
            "average_latency_ms": round_value(row["average_latency_ms"] or 0),
            "max_latency_ms": round_value(row["max_latency_ms"] or 0),
            "total_returned_mb": bytes_to_mb(returned_bytes),
            "average_returned_mb": (
                bytes_to_mb(returned_bytes / successful) if successful > 0 else 0
            ),
        }

    def get_per_tool_metrics(self, run_ids: list[int]) -> list[dict[str, Any]]:
        """Return per-tool metrics for run ids.

        :param run_ids: Run ids to aggregate.
        :type run_ids: list[int]
        :return: Per-tool metric rows.
        :rtype: list[dict[str, Any]]
        """

        if not run_ids:
            return []
        rows = self.db.execute(
            f"""
            SELECT
                tool,
                request_type,
                COUNT(*) AS attempted_requests,
                SUM(succeeded) AS successful_requests,
                COALESCE(SUM(returned_bytes), 0) AS returned_bytes,
                AVG(latency_ms) AS average_latency_ms,
                MAX(latency_ms) AS max_latency_ms,
                AVG(CASE WHEN succeeded = 1 THEN result_count END)
                    AS average_result_count
            FROM request_events
            WHERE run_id IN ({placeholders(run_ids)})
            GROUP BY tool, request_type
            ORDER BY tool
            """,
            run_ids,
        ).fetchall()
        return [self.serialize_tool_metrics(row) for row in rows]

    def get_status_code_metrics(
        self,
        run_ids: list[int],
    ) -> list[dict[str, Any]]:
        """Return status-code distribution for run ids.

        :param run_ids: Run ids to aggregate.
        :type run_ids: list[int]
        :return: Status-code metrics.
        :rtype: list[dict[str, Any]]
        """

        if not run_ids:
            return []
        rows = self.db.execute(
            f"""
            SELECT status_code, COUNT(*) AS count
            FROM request_events
            WHERE run_id IN ({placeholders(run_ids)})
              AND status_code IS NOT NULL
            GROUP BY status_code
            ORDER BY status_code
            """,
            run_ids,
        ).fetchall()
        return [
            {
                "status_code": int(row["status_code"]),
                "count": int(row["count"]),
            }
            for row in rows
        ]

    def get_recent_failures(self, run_ids: list[int]) -> list[dict[str, Any]]:
        """Return recent failure summaries for run ids.

        :param run_ids: Run ids to inspect.
        :type run_ids: list[int]
        :return: Recent failure summaries.
        :rtype: list[dict[str, Any]]
        """

        if not run_ids:
            return []
        rows = self.db.execute(
            f"""
            SELECT occurred_at, tool, request_type, status_code, error
            FROM request_events
            WHERE run_id IN ({placeholders(run_ids)})
              AND succeeded = 0
            ORDER BY occurred_at DESC, id DESC
            LIMIT ?
            """,
            [*run_ids, MAX_RECENT_FAILURES],
        ).fetchall()
        return [
            {
                "occurred_at": row["occurred_at"],
                "tool": row["tool"],
                "request_type": row["request_type"],
                "status_code": row["status_code"],
                "error": row["error"],
            }
            for row in rows
        ]

    def serialize_tool_metrics(self, row: sqlite3.Row) -> dict[str, Any]:
        """Serialize one per-tool aggregate row.

        :param row: SQLite aggregate row.
        :type row: sqlite3.Row
        :return: Serialized metrics.
        :rtype: dict[str, Any]
        """

        attempted = int(row["attempted_requests"] or 0)
        successful = int(row["successful_requests"] or 0)
        returned_bytes = int(row["returned_bytes"] or 0)
        metrics: dict[str, Any] = {
            "tool": row["tool"],
            "request_type": row["request_type"],
            "attempted_requests": attempted,
            "successful_requests": successful,
            "failed_requests": attempted - successful,
            "success_percentage": percentage(successful, attempted),
            "average_latency_ms": round_value(row["average_latency_ms"] or 0),
            "max_latency_ms": round_value(row["max_latency_ms"] or 0),
            "average_returned_mb": (
                bytes_to_mb(returned_bytes / successful) if successful > 0 else 0
            ),
        }
        if row["request_type"] == "search":
            metrics["average_result_count"] = round_value(
                row["average_result_count"] or 0
            )
        return metrics

    def get_aggregate_row(self, run_ids: list[int]) -> sqlite3.Row:
        """Return one aggregate row for run ids.

        :param run_ids: Run ids to aggregate.
        :type run_ids: list[int]
        :return: SQLite aggregate row.
        :rtype: sqlite3.Row
        """

        if not run_ids:
            return self.db.execute("""
                SELECT
                    0 AS attempted_requests,
                    0 AS successful_requests,
                    0 AS returned_bytes,
                    0 AS average_latency_ms,
                    0 AS max_latency_ms
                """).fetchone()
        return self.db.execute(
            f"""
            SELECT
                COUNT(*) AS attempted_requests,
                SUM(succeeded) AS successful_requests,
                COALESCE(SUM(returned_bytes), 0) AS returned_bytes,
                AVG(latency_ms) AS average_latency_ms,
                MAX(latency_ms) AS max_latency_ms
            FROM request_events
            WHERE run_id IN ({placeholders(run_ids)})
            """,
            run_ids,
        ).fetchone()

    def resolve_run(self, occurred_at: datetime) -> RunRow:
        """Return the daily run for a timestamp, creating it if needed.

        :param occurred_at: Event timestamp.
        :type occurred_at: datetime
        :return: Daily run row.
        :rtype: RunRow
        """

        run_key = occurred_at.date().isoformat()
        existing = self.db.execute(
            """
            SELECT id, run_key, started_at, observed_at
            FROM runs
            WHERE run_key = ?
            """,
            (run_key,),
        ).fetchone()
        if existing is not None:
            return run_from_row(existing)

        started_at = to_isoformat(
            datetime.combine(occurred_at.date(), datetime.min.time())
        )
        observed_at = to_isoformat(occurred_at)
        result = self.db.execute(
            """
            INSERT INTO runs (run_key, started_at, observed_at)
            VALUES (?, ?, ?)
            """,
            (run_key, started_at, observed_at),
        )
        self.db.commit()
        last_row_id = result.lastrowid
        if last_row_id is None:
            raise MetricsConfigurationError("Unable to create metrics run")
        run_id = int(last_row_id)
        return self.get_run_by_id(run_id) or RunRow(
            id=run_id,
            run_key=run_key,
            started_at=started_at,
            observed_at=observed_at,
        )

    def get_run_by_id(self, run_id: int) -> RunRow | None:
        """Return one run by id.

        :param run_id: Run id.
        :type run_id: int
        :return: Run row when present.
        :rtype: RunRow | None
        """

        row = self.db.execute(
            """
            SELECT id, run_key, started_at, observed_at
            FROM runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        return run_from_row(row) if row is not None else None

    def get_run_ids(self) -> list[int]:
        """Return all run ids.

        :return: All run ids.
        :rtype: list[int]
        """

        rows = self.db.execute("""
            SELECT id
            FROM runs
            ORDER BY started_at, id
            """).fetchall()
        return [int(row["id"]) for row in rows]

    def initialize_schema(self) -> None:
        """Initialize the SQLite schema.

        :return: None.
        :rtype: None
        """

        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_key TEXT NOT NULL UNIQUE,
                started_at TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                occurred_at TEXT NOT NULL,
                tool TEXT NOT NULL,
                request_type TEXT NOT NULL CHECK (
                    request_type IN ('search', 'scrape')
                ),
                succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1)),
                status_code INTEGER,
                query_hash TEXT,
                url_hash TEXT,
                result_count INTEGER,
                returned_bytes INTEGER,
                response_format TEXT,
                latency_ms REAL NOT NULL,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_request_events_run_tool
                ON request_events(run_id, tool, request_type);

            CREATE INDEX IF NOT EXISTS idx_request_events_run_status
                ON request_events(run_id, status_code);

            CREATE INDEX IF NOT EXISTS idx_request_events_run_failure
                ON request_events(run_id, succeeded, occurred_at, id);
            """)
        self.db.commit()

    @staticmethod
    def metrics_port_conflict_error(
        host: str,
        port: int,
    ) -> MetricsPortConflictError:
        """Create a port conflict error.

        :param host: Host that was probed.
        :type host: str
        :param port: Port that was probed.
        :type port: int
        :return: Port conflict error.
        :rtype: MetricsPortConflictError
        """

        message = (
            f"MCP metrics port {host}:{port} is already in use by a non-MCP "
            "metrics service. Set MCP_METRICS_PORT to another port or "
            "disable "
            f"metrics with MCP_METRICS_ENABLED=false."
        )
        return MetricsPortConflictError(message)

    @staticmethod
    def metrics_unresponsive_port_error(
        host: str,
        port: int,
    ) -> MetricsUnresponsivePortError:
        """Create an unresponsive port owner error.

        :param host: Host that was probed.
        :type host: str
        :param port: Port that was probed.
        :type port: int
        :return: Unresponsive port error.
        :rtype: MetricsUnresponsivePortError
        """

        message = (
            f"MCP metrics port {host}:{port} is already in use, but its "
            "health endpoint did not respond. Kill the hung process that "
            "owns this port, then restart the MCP server."
        )
        return MetricsUnresponsivePortError(message)


def metrics_enabled() -> bool:
    """Return whether metrics are enabled by environment.

    :return: Whether metrics are enabled.
    :rtype: bool
    """

    return parse_boolean_env(METRICS_ENABLED_ENV_VAR, True)


def get_metrics_host() -> str:
    """Return the configured metrics host.

    :return: Metrics host.
    :rtype: str
    """

    return os.getenv(METRICS_HOST_ENV_VAR, DEFAULT_METRICS_HOST).strip()


def get_metrics_port() -> int:
    """Return the configured metrics port.

    :return: Metrics port.
    :rtype: int
    :raises MetricsConfigurationError: If the port is invalid.
    """

    return parse_positive_integer_env(
        METRICS_PORT_ENV_VAR,
        DEFAULT_METRICS_PORT,
    )


def get_metrics_db_path() -> Path:
    """Return the configured metrics database path.

    :return: Metrics database path.
    :rtype: Path
    """

    if METRICS_DB_PATH_ENV_VAR in os.environ:
        return Path(os.environ[METRICS_DB_PATH_ENV_VAR])
    return (
        Path(os.getenv(METRICS_DATA_DIR_ENV_VAR, DEFAULT_METRICS_DATA_DIR))
        / DEFAULT_METRICS_DB_NAME
    )


def parse_boolean_env(name: str, fallback: bool) -> bool:
    """Parse a boolean environment variable.

    :param name: Environment variable name.
    :type name: str
    :param fallback: Fallback value.
    :type fallback: bool
    :return: Parsed boolean.
    :rtype: bool
    """

    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return fallback
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


def parse_positive_integer_env(name: str, fallback: int) -> int:
    """Parse a positive integer environment variable.

    :param name: Environment variable name.
    :type name: str
    :param fallback: Fallback value.
    :type fallback: int
    :return: Parsed positive integer.
    :rtype: int
    :raises MetricsConfigurationError: If the environment value is invalid.
    """

    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return fallback
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise MetricsConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise MetricsConfigurationError(f"{name} must be greater than 0")
    return value


def parse_run_id(run_id_value: str | None) -> int:
    """Parse a run id query parameter.

    :param run_id_value: Query parameter value.
    :type run_id_value: str | None
    :return: Parsed run id.
    :rtype: int
    :raises ValueError: If the run id is invalid.
    """

    if run_id_value is None:
        raise ValueError("scope=run requires a positive run_id")
    try:
        run_id = int(run_id_value)
    except ValueError as exc:
        raise ValueError("scope=run requires a positive run_id") from exc
    if run_id <= 0:
        raise ValueError("scope=run requires a positive run_id")
    return run_id


def hash_value(value: str) -> str:
    """Hash a potentially sensitive metric value.

    :param value: Raw value.
    :type value: str
    :return: SHA-256 hex digest.
    :rtype: str
    """

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def truncate_error(error: str | None) -> str | None:
    """Truncate an error message for persistence.

    :param error: Error message.
    :type error: str | None
    :return: Truncated error message.
    :rtype: str | None
    """

    if error is None:
        return None
    return error[:500]


def placeholders(values: list[int]) -> str:
    """Return SQLite placeholders for values.

    :param values: Values requiring placeholders.
    :type values: list[int]
    :return: Placeholder string.
    :rtype: str
    """

    return ", ".join("?" for _value in values)


def percentage(successful: int, attempted: int) -> float:
    """Return a rounded success percentage.

    :param successful: Successful count.
    :type successful: int
    :param attempted: Attempted count.
    :type attempted: int
    :return: Percentage.
    :rtype: float
    """

    if attempted == 0:
        return 0
    return round_value((successful / attempted) * 100)


def bytes_to_mb(value: float) -> float:
    """Convert bytes to megabytes.

    :param value: Byte count.
    :type value: float
    :return: Megabytes.
    :rtype: float
    """

    return round_value(value / 1024 / 1024)


def round_value(value: float) -> float:
    """Round a metric value.

    :param value: Metric value.
    :type value: float
    :return: Rounded value.
    :rtype: float
    """

    return round(float(value), 4)


def to_isoformat(value: datetime) -> str:
    """Serialize a datetime with second precision.

    :param value: Datetime value.
    :type value: datetime
    :return: ISO formatted timestamp.
    :rtype: str
    """

    return value.isoformat(timespec="seconds")


def run_from_row(row: sqlite3.Row) -> RunRow:
    """Build a run dataclass from a SQLite row.

    :param row: SQLite row.
    :type row: sqlite3.Row
    :return: Run row dataclass.
    :rtype: RunRow
    """

    return RunRow(
        id=int(row["id"]),
        run_key=str(row["run_key"]),
        started_at=str(row["started_at"]),
        observed_at=str(row["observed_at"]),
    )


def serialize_run(run: RunRow | None) -> dict[str, Any] | None:
    """Serialize run metadata.

    :param run: Run row.
    :type run: RunRow | None
    :return: Serialized run metadata.
    :rtype: dict[str, Any] | None
    """

    if run is None:
        return None
    return {
        "id": run.id,
        "run_key": run.run_key,
        "started_at": run.started_at,
        "observed_at": run.observed_at,
    }

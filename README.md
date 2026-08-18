# Scrapingdog MCP Server

A Model Context Protocol server that provides Google Search and webpage
scraping through [Scrapingdog](https://www.scrapingdog.com/).

## Available Tools

- `google_search` - Search Google web results.
- `webpage_scrape` - Scrape a webpage URL.

## Configuration

Set `SCRAPINGDOG_API_KEY` to your Scrapingdog API key.

Any tool parameter can be forced with a `SCRAPINGDOG_FORCE_` environment
variable. Forced values take precedence over values passed by the MCP client.
Parameter names are converted to upper snake case, so `advance_search` becomes
`SCRAPINGDOG_FORCE_ADVANCE_SEARCH`, `format` becomes
`SCRAPINGDOG_FORCE_FORMAT`, and `country` becomes
`SCRAPINGDOG_FORCE_COUNTRY`.

For example:

```json
{
    "SCRAPINGDOG_API_KEY": "<Your Scrapingdog API key>",
    "SCRAPINGDOG_FORCE_COUNTRY": "us",
    "SCRAPINGDOG_FORCE_LANGUAGE": "en",
    "SCRAPINGDOG_FORCE_DYNAMIC": "false"
}
```

Per-client-session successful tool call limits can be set with
`SCRAPINGDOG_GOOGLE_SEARCH_SESSION_LIMIT` and
`SCRAPINGDOG_WEBPAGE_SCRAPE_SESSION_LIMIT`. When provided, each value must be a
positive integer. The limit applies separately to each MCP client session and
only successful Scrapingdog-backed calls count against it. Once a tool reaches
its limit, further calls to that tool in the same session return a clear
`usage limit reached` tool error.

Set `SCRAPINGDOG_MAX_CONCURRENT_REQUESTS` to a positive integer to limit active
Scrapingdog API requests across all tools and all MCP client sessions in the
server process. When every request slot is active, an additional tool call is
rejected immediately with a `WARNING` tool error instead of being submitted or
queued. The response asks the caller to submit no more than the configured
number of Scrapingdog tool calls at a time. When this setting is omitted, no
application-level concurrency limit is applied.

Scrapingdog API requests default to a 30-second timeout. Set
`SCRAPINGDOG_REQUEST_TIMEOUT` to a positive integer number of seconds to
override it.

### Metrics Sidecar

The server records portable SQLite metrics for search and scrape requests and
starts a local HTTP sidecar when one is not already running. The default
metrics endpoint is `http://127.0.0.1:3004`.

The sidecar exposes:

- `GET /health` - Identify the portable MCP metrics sidecar.
- `GET /metrics?scope=current` - Report the current local-date run.
- `GET /metrics?scope=all_time` - Report all recorded runs.
- `GET /metrics?scope=run&run_id=1` - Report one run by ID.

Metrics use one run per local `YYYY-MM-DD` date. Queries and URLs are stored
only as SHA-256 hashes.

Metrics configuration:

- `MCP_METRICS_ENABLED` - Enable metrics. Defaults to `true`.
- `MCP_METRICS_HOST` - Sidecar bind host. Defaults to `127.0.0.1`.
- `MCP_METRICS_PORT` - Sidecar port. Defaults to `3004`.
- `MCP_METRICS_DATA_DIR` - Directory for the default SQLite database.
  Defaults to `data`.
- `MCP_METRICS_DB_PATH` - Explicit SQLite database path. When set, this takes
  precedence over `MCP_METRICS_DATA_DIR`.

## Tool Arguments

`google_search` accepts `query`, `advance_search`, `mob_search`, `html`,
`domain`, `country`, `location`, `language`, `safe`, `nfpr`, `filter`, and
`results`, and `page`.

`webpage_scrape` accepts `url`, `dynamic`, and `format`. When `format` is
omitted, the tool returns HTML. Supported `format` values are `markdown`,
`summary`, `links`, and `images`.

## Usage

Install the package:

```bash
python3 -m pip install scrapingdog-mcp-server
```

Add the server to your MCP client configuration:

```json
{
    "mcpServers": {
        "scrapingdog": {
            "command": "python3",
            "args": ["-m", "scrapingdog_mcp_server"],
            "env": {
                "SCRAPINGDOG_API_KEY": "<Your Scrapingdog API key>"
            }
        }
    }
}
```

## Developing Locally

Install the package in editable mode:

```bash
python3 -m pip install -e ".[dev]"
```

Run the test suite:

```bash
pytest -q
```

## Debugging

Use the MCP inspector after installing dependencies:

```bash
SCRAPINGDOG_API_KEY=<the key> npx @modelcontextprotocol/inspector python3 -m scrapingdog_mcp_server
```

## License

scrapingdog-mcp-server is licensed under the MIT License.

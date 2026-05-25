# Scrapingdog MCP Server

A Model Context Protocol server that provides Google Search and webpage
scraping through [Scrapingdog](https://www.scrapingdog.com/).

## Available Tools

- `google_search` - Search Google web results through Scrapingdog.
- `webpage_scrape` - Scrape a webpage URL through Scrapingdog.

## Configuration

Set `SCRAPINGDOG_API_KEY` to your Scrapingdog API key.

Any tool parameter can be forced with a `SCRAPINGDOG_FORCE_` environment
variable. Forced values take precedence over values passed by the MCP client.
Parameter names are converted to upper snake case, so `advance_search` becomes
`SCRAPINGDOG_FORCE_ADVANCE_SEARCH`, `formats` becomes
`SCRAPINGDOG_FORCE_FORMATS`, and `country` becomes
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

`webpage_scrape` accepts `url`, `dynamic`, and `formats`. When `formats` is
omitted, Scrapingdog returns HTML. Supported `formats` values are `markdown`,
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

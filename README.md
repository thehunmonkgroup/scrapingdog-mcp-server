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

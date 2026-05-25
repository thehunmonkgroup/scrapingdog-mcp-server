"""Command-line entrypoint for the Scrapingdog MCP server."""

from __future__ import annotations

import argparse
import logging

from . import server


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    :return: Parsed command-line arguments.
    :rtype: argparse.Namespace
    """

    parser = argparse.ArgumentParser(description="Run the Scrapingdog MCP server.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def configure_logging(debug: bool) -> None:
    """Configure stderr logging.

    :param debug: Whether to enable debug logging.
    :type debug: bool
    :return: None.
    :rtype: None
    """

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )


def main() -> int:
    """Run the Scrapingdog MCP server.

    :return: Process exit code.
    :rtype: int
    """

    args = parse_args()
    configure_logging(args.debug)
    server.main()
    return 0


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())

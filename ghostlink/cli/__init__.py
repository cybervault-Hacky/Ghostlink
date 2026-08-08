"""Command-line interface: argument parsing, diagnostics, and entrypoint."""

from __future__ import annotations

from ghostlink.cli.arguments import CLIOptions, build_parser, parse_args

__all__ = ["CLIOptions", "build_parser", "parse_args"]

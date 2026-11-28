"""Importers: auto-ingest tool definitions from where they already live.

Users never hand-write tool schemas one by one (onboarding.md): catalogs are
imported from a machine-readable source and then flow through the SAME intake
guardrail as any manual registration — automation never lowers the quality bar.

- ``mcp_importer``     — connect to any MCP server, pull its self-described tools
"""

from toolspotter.importers.mcp_importer import import_from_mcp

__all__ = ["import_from_mcp"]

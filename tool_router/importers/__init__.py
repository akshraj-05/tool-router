"""Importers: auto-ingest tool definitions from where they already live.

Users never hand-write tool schemas one by one (onboarding.md): catalogs are
imported from machine-readable sources and then flow through the SAME intake
guardrail as any manual registration — automation never lowers the quality bar.

- ``mcp_importer``     — connect to any MCP server, pull its self-described tools
- ``openapi_importer`` — parse an OpenAPI/Swagger spec, one endpoint per tool
"""

from tool_router.importers.mcp_importer import import_from_mcp
from tool_router.importers.openapi_importer import import_from_openapi

__all__ = ["import_from_mcp", "import_from_openapi"]

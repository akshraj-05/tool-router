"""MCP server — "router as a tool" (integration-plugability.md, Pattern A).

Exposes the routing engine to ANY MCP-compatible agent as callable tools. Both are
read-only: an agent can ask which tool to use but cannot change the registry. The
catalog is assembled once, at startup (a file and/or a scan of other MCP servers).

- ``find_tools(query, k?)``   — the meta-tool: returns the minimal set of
  relevant tools (original schemas), the ordered call plan, and clarify info.
  The agent calls it mid-reasoning instead of carrying all 60+ schemas.
- ``registry_status()``       — tool count + derived-map stats.

Run:  tool-router mcp --catalog catalog.json
      tool-router mcp --import-mcp "python my_server.py"
Host config (e.g. Claude Desktop):
    {"mcpServers": {"tool-router": {"command": "tool-router",
                                    "args": ["mcp", "--catalog", "/path/catalog.json"]}}}

Requires the `mcp` extra: pip install .[mcp]
"""

from __future__ import annotations

import json
from typing import Any, Optional

from tool_router.config import Settings
from tool_router.engine.pipeline import RoutingEngine
from tool_router.log import get_logger
from tool_router.state import SavedState, StateStore

logger = get_logger("mcp")


def create_mcp_server(
    catalog: dict[str, Any],
    store: Optional[StateStore] = None,
    previous: Optional[SavedState] = None,
):
    """Build the FastMCP server over one RoutingEngine instance for ``catalog``."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The MCP SDK is missing or incompatible. tool-router supports mcp>=1.0,<2 — "
            "in the project folder run: pip install -e \".[mcp]\" (mcp 2.x renamed FastMCP and is not supported yet)."
        ) from exc

    server = FastMCP(
        "tool-router",
        instructions=(
            "Intelligent tool router. Call find_tools with the user's request to get "
            "the small set of relevant tools (with schemas) and a suggested call order, "
            "instead of loading an entire tool catalog."
        ),
    )
    engine = RoutingEngine(catalog, settings=Settings.from_env(), store=store, previous=previous)

    @server.tool()
    def find_tools(query: str, k: int = 8) -> str:
        """Route a natural-language request to the most relevant registered tools.

        Returns JSON: selected tool schemas (originals), the ordered call plan
        (ids may repeat), and clarify info when the request is too ambiguous
        or out of scope. `k` caps how many tools may be returned.
        """
        result = engine.route(query)
        selected = result.selected_tools[: max(1, k)]
        return json.dumps({
            "selected_tools": [engine.raw_tool(t) for t in selected],
            "plan": result.plan,
            "clarify": result.clarify,
            "clarify_question": result.clarify_question,
            "tokens_saved_pct": result.tokens_saved_pct,
        })

    @server.tool()
    def registry_status() -> str:
        """Current registry stats: tool count, clusters, dup groups, version families."""
        return json.dumps({
            "tool_count": engine.tool_count,
            "clusters": len(engine.maps.clusters),
            "near_duplicate_groups": len(engine.maps.dup_groups),
            "version_families": len(engine.maps.version_families),
        })

    return server


def run_mcp(
    catalog: dict[str, Any],
    store: Optional[StateStore] = None,
    previous: Optional[SavedState] = None,
) -> None:
    """Entry point used by `tool-router mcp` (stdio transport)."""
    create_mcp_server(catalog, store=store, previous=previous).run()  # stdio transport (MCP default)

"""MCP server — "router as a tool" (integration-plugability.md, Pattern A).

Exposes the routing engine to ANY MCP-compatible agent as callable tools:

- ``find_tools(query, k?)``   — the meta-tool: returns the minimal set of
  relevant tools (original schemas), the ordered call plan, and clarify info.
  The agent calls it mid-reasoning instead of carrying all 60+ schemas.
- ``register_tools(tools)``   — onboard/extend the registry (strict guardrail).
- ``registry_status()``       — tool count + derived-map stats.

Run:  tool-router mcp --catalog catalog.json
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

logger = get_logger("mcp")


def create_mcp_server(catalog: Optional[dict[str, Any]] = None):
    """Build the FastMCP server over one RoutingEngine instance."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The mcp extra is not installed — run: pip install .[mcp]"
        ) from exc

    server = FastMCP(
        "tool-router",
        instructions=(
            "Intelligent tool router. Call find_tools with the user's request to get "
            "the small set of relevant tools (with schemas) and a suggested call order, "
            "instead of loading an entire tool catalog."
        ),
    )
    state: dict[str, Optional[RoutingEngine]] = {"engine": None}
    if catalog is not None:
        state["engine"] = RoutingEngine(catalog, settings=Settings.from_env())

    def _engine() -> RoutingEngine:
        if state["engine"] is None:
            raise ValueError("No tool registry loaded — call register_tools first.")
        return state["engine"]

    @server.tool()
    def find_tools(query: str, k: int = 8) -> str:
        """Route a natural-language request to the most relevant registered tools.

        Returns JSON: selected tool schemas (originals), the ordered call plan
        (ids may repeat), and clarify info when the request is too ambiguous
        or out of scope. `k` caps how many tools may be returned.
        """
        engine = _engine()
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
    def register_tools(tools: list[dict]) -> str:
        """Register tools into the routing registry (validated by the intake guardrail).

        Creates the registry if none exists, otherwise adds to it. Returns
        JSON with accepted/rejected ids and rejection reasons.
        """
        if state["engine"] is None:
            state["engine"] = RoutingEngine({"tools": tools}, settings=Settings.from_env())
            return json.dumps({"status": "registered", "tool_count": state["engine"].tool_count})
        outcome = state["engine"].apply_mutation({"add": tools})
        return json.dumps(outcome)

    @server.tool()
    def registry_status() -> str:
        """Current registry stats: tool count, clusters, dup groups, version families."""
        engine = state["engine"]
        if engine is None:
            return json.dumps({"registry_loaded": False, "tool_count": 0})
        return json.dumps({
            "registry_loaded": True,
            "tool_count": engine.tool_count,
            "clusters": len(engine.maps.clusters),
            "near_duplicate_groups": len(engine.maps.dup_groups),
            "version_families": len(engine.maps.version_families),
        })

    return server


def run_mcp(catalog_path: Optional[str] = None) -> None:
    """Entry point used by `tool-router mcp` (stdio transport)."""
    catalog = None
    if catalog_path:
        with open(catalog_path, encoding="utf-8") as fh:
            catalog = json.load(fh)
        logger.info("mcp server pre-loading registry from %s", catalog_path)
    create_mcp_server(catalog).run()  # stdio transport (MCP default)

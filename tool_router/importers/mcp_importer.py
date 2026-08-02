"""MCP importer — pull tools from any MCP server (onboarding.md, Level 1).

MCP tools are self-describing (`name`, `description`, `inputSchema`), so a
whole catalog onboards from one connection: connect over stdio, `list_tools`,
convert each to our registry schema. Requires the `mcp` extra.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from tool_router.importers.common import params_from_json_schema, sanitize_name
from tool_router.log import get_logger

logger = get_logger("importers.mcp")


def _convert(tool: Any, prefix: str) -> dict[str, Any]:
    name = sanitize_name(tool.name)
    return {
        "id": f"{prefix}.{name}",
        "name": name,
        "cluster": prefix,
        "description": (tool.description or "").strip(),
        "tags": [prefix, "mcp-import"],
        "parameters": params_from_json_schema(getattr(tool, "inputSchema", None)),
        "returns": {"type": "object"},
    }


async def import_from_mcp_async(
    command: str,
    args: Optional[list[str]] = None,
    prefix: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Connect to an MCP server (stdio) and return its tools in registry schema."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise RuntimeError(
            "The mcp extra is not installed — run: pip install .[mcp]"
        ) from exc

    prefix = sanitize_name(prefix or command.rsplit("/", 1)[-1])
    params = StdioServerParameters(command=command, args=args or [], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = [_convert(t, prefix) for t in listed.tools]
    logger.info("imported %d tools from MCP server %r", len(tools), command)
    return tools


def import_from_mcp(
    command: str,
    args: Optional[list[str]] = None,
    prefix: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Synchronous wrapper around :func:`import_from_mcp_async`."""
    return asyncio.run(import_from_mcp_async(command, args, prefix, env))

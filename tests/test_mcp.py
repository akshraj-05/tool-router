"""MCP server tools (skipped when the optional `mcp` extra is not installed)."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp.server.fastmcp")

from toolspotter.mcp_server import create_mcp_server  # noqa: E402


def call(server, name: str, arguments: dict) -> dict:
    result = asyncio.run(server.call_tool(name, arguments))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_only_read_only_tools_are_exposed(catalog):
    """An agent must not be able to change the tool registry over MCP."""
    server = create_mcp_server(catalog)
    names = sorted(t.name for t in asyncio.run(server.list_tools()))
    assert names == ["find_tools", "registry_status"]


def test_registry_status_reports_the_loaded_catalog(catalog):
    server = create_mcp_server(catalog)
    assert call(server, "registry_status", {})["tool_count"] == len(catalog["tools"])


def test_find_tools_returns_schemas_and_a_plan(catalog):
    server = create_mcp_server(catalog)
    out = call(server, "find_tools", {"query": "Check the health status of the production server"})
    assert out["plan"] == ["ops.get_server_status"]
    assert out["selected_tools"][0]["id"] == "ops.get_server_status"


def test_find_tools_respects_k(catalog):
    server = create_mcp_server(catalog)
    out = call(server, "find_tools", {"query": "Send an email to Dana, Omar, and Lin", "k": 1})
    assert len(out["selected_tools"]) == 1

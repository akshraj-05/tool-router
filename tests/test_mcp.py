"""MCP server tools (skipped when the optional `mcp` extra is not installed)."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp.server.fastmcp")

from tool_router.mcp_server import create_mcp_server  # noqa: E402


def call(server, name: str, arguments: dict) -> dict:
    result = asyncio.run(server.call_tool(name, arguments))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_status_before_and_after_registering(catalog):
    server = create_mcp_server(None)
    assert call(server, "registry_status", {})["registry_loaded"] is False
    out = call(server, "register_tools", {"tools": catalog["tools"]})
    assert out["status"] == "registered"
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


def test_register_tools_extends_an_existing_registry(catalog):
    server = create_mcp_server(catalog)
    new = {"id": "weather.get_current_weather", "name": "get_current_weather",
           "cluster": "weather", "side_effects": "read",
           "description": "Get the current weather conditions for a named city",
           "parameters": [{"name": "city", "type": "string"}], "returns": {"type": "object"}}
    out = call(server, "register_tools", {"tools": [new]})
    assert out["added"] == [new["id"]]

"""Example requests, and a Postman collection built from the API's own description.

The collection is made from the running app's OpenAPI document, so a new endpoint
appears in it without further work. ``EXAMPLES`` supplies the request bodies, here
and in the API docs; a test sends every one of them.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
EXAMPLE_ID = "general.get_weather"

_TOOL = {
    "id": EXAMPLE_ID,
    "name": "get_weather",
    "description": "Get the current weather conditions for a named city",
    "parameters": [
        {"name": "city", "type": "string", "required": True, "description": "City name"},
    ],
}
_SECOND_TOOL = {
    "name": "get_forecast",
    "description": "Get the weather forecast for the coming days in a city",
    "parameters": [{"name": "city", "type": "string", "required": True}],
}

EXAMPLES: dict[tuple[str, str], Any] = {
    ("post", "/route"): {"query": "What is the weather in Pune?"},
    ("post", "/registry"): {"tools": [_TOOL]},
    ("post", "/registry/preview"): {"tools": [_TOOL], "mode": "merge"},
    ("post", "/registry/apply"): {"tools": [_TOOL], "mode": "merge"},
    ("post", "/registry/tools"): _TOOL,
    ("put", "/registry/tools/{tool_id}"): {
        **_TOOL, "description": "Get today's weather conditions for a named city"},
    ("patch", "/registry/mutate"): {"add": [_SECOND_TOOL], "remove": []},
    ("post", "/mcp/scan"): {"url": "https://example.com/mcp", "token": "your-token"},
    ("post", "/setup"): {"llm_provider": "none", "embedding_provider": "local"},
}

# Folder name by the first part of the path; the order here is the order shown.
_FOLDERS = {"route": "Routing", "registry": "Registry", "history": "History", "mcp": "MCP server"}
_METHOD_ORDER = ("post", "get", "put", "patch", "delete")


def add_examples(openapi: dict[str, Any]) -> dict[str, Any]:
    """Put the example bodies into an OpenAPI document (changed in place)."""
    for (method, path), body in EXAMPLES.items():
        operation = openapi.get("paths", {}).get(path, {}).get(method)
        if operation and "requestBody" in operation:
            operation["requestBody"]["content"]["application/json"]["example"] = body
    return openapi


def collection(openapi: dict[str, Any], base_url: str) -> dict[str, Any]:
    """A Postman v2.1 collection with one request per API operation."""
    folders: dict[str, list[dict[str, Any]]] = {name: [] for name in _FOLDERS.values()}
    folders["Service"] = []
    # Create a tool before reading, changing and deleting it when run top to bottom.
    operations = sorted(
        ((method, path, operation)
         for path, methods in openapi.get("paths", {}).items()
         for method, operation in methods.items() if method in _METHOD_ORDER),
        key=lambda item: (item[0] == "delete", item[1] != "/registry/tools",
                          item[1], _METHOD_ORDER.index(item[0])),
    )
    for method, path, operation in operations:
        parts = [part for part in path.split("/") if part]
        url_parts = [":" + p[1:-1] if p.startswith("{") else p for p in parts]
        url: dict[str, Any] = {
            "raw": "{{baseUrl}}/" + "/".join(url_parts), "host": ["{{baseUrl}}"],
            "path": url_parts,
        }
        variables = [{"key": p[1:], "value": "{{toolId}}"} for p in url_parts if p.startswith(":")]
        if variables:
            url["variable"] = variables
        request: dict[str, Any] = {"method": method.upper(), "header": [], "url": url}
        if operation.get("description"):
            request["description"] = operation["description"]
        body = EXAMPLES.get((method, path))
        if body is not None:
            request["header"] = [{"key": "Content-Type", "value": "application/json"}]
            request["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2),
                               "options": {"raw": {"language": "json"}}}
        folder = _FOLDERS.get(parts[0] if parts else "", "Service")
        folders[folder].append({"name": f"{method.upper()} {path}", "request": request})
    return {
        "info": {"name": "ToolSpotter", "schema": SCHEMA,
                 "description": openapi.get("info", {}).get("description", "")},
        "variable": [{"key": "baseUrl", "value": base_url.rstrip("/")},
                     {"key": "toolId", "value": EXAMPLE_ID}],
        "item": [{"name": name, "item": items} for name, items in folders.items() if items],
    }

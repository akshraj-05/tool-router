"""Shared conversion helpers for importers.

Importers emit tools in OUR registry schema; anything the source can't
express is derived conservatively (never fabricated):
- id       = "<prefix>.<sanitized_name>" (matches the guardrail id pattern)
- cluster  = caller-provided prefix / source tag (a real grouping, not a guess)
- params   = mapped from the source's JSON-Schema-style parameters

Imported tools still pass the strict guardrail at registration — an import is
a *source*, not a bypass.
"""

from __future__ import annotations

import re
from typing import Any

_SANITIZE = re.compile(r"[^a-z0-9_]+")
_JSON_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def mcp_safe_name(tool_id: str) -> str:
    """Map a registry id to an MCP/LLM-function-safe name (integrations.md).

    Function-calling names must match ``^[a-zA-Z0-9_-]+$`` (dots are not
    allowed) and stay short — ``comm.send_email`` -> ``comm_send_email``.
    Keep the inverse mapping when exposing catalog tools over MCP so results
    can be translated back to exact registry ids.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", tool_id)[:64]


def sanitize_name(name: str) -> str:
    """Make an identifier guardrail-compatible: lowercase snake_case."""
    cleaned = _SANITIZE.sub("_", name.strip().lower()).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"tool_{cleaned}" if cleaned else "tool"
    return cleaned


def map_param_type(json_type: Any) -> str:
    """Map a JSON-Schema type to our allowed parameter types."""
    if isinstance(json_type, list):          # nullable unions like ["string","null"]
        json_type = next((t for t in json_type if t != "null"), "string")
    return json_type if json_type in _JSON_TYPES else "string"


def params_from_json_schema(schema: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Convert a JSON-Schema object (properties/required) to our param list."""
    if not isinstance(schema, dict):
        return []
    required = set(schema.get("required") or [])
    params: list[dict[str, Any]] = []
    for pname, pdef in (schema.get("properties") or {}).items():
        pdef = pdef if isinstance(pdef, dict) else {}
        params.append({
            "name": pname,
            "type": map_param_type(pdef.get("type", "string")),
            "required": pname in required,
            "description": str(pdef.get("description") or ""),
        })
    return params

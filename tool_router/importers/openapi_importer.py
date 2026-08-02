"""OpenAPI/Swagger importer — one endpoint = one tool (onboarding.md, Level 1).

Reads an OpenAPI 3.x (or Swagger 2.0) spec dict and converts every operation
into our registry schema:
- name        = operationId (sanitized) or "<method>_<path>"
- description = summary + description
- cluster     = first tag (sanitized) or the caller-provided prefix
- parameters  = path/query/header params + JSON requestBody properties
"""

from __future__ import annotations

from typing import Any, Optional

from tool_router.importers.common import map_param_type, params_from_json_schema, sanitize_name
from tool_router.log import get_logger

logger = get_logger("importers.openapi")

_HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
# HTTP semantics -> our side-effect taxonomy (conservative mapping).
_SIDE_EFFECTS = {
    "get": "read", "head": "read", "options": "read",
    "post": "write", "put": "write", "patch": "write",
    "delete": "destructive",
}


def _operation_params(op: dict[str, Any]) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []
    for p in op.get("parameters") or []:
        if not isinstance(p, dict) or "name" not in p:
            continue
        schema = p.get("schema") or {}
        params.append({
            "name": sanitize_name(p["name"]),
            "type": map_param_type(schema.get("type", p.get("type", "string"))),
            "required": bool(p.get("required")),
            "description": p.get("description", ""),
        })
    body = op.get("requestBody") or {}
    json_content = (body.get("content") or {}).get("application/json") or {}
    params.extend(params_from_json_schema(json_content.get("schema")))
    return params


def import_from_openapi(
    spec: dict[str, Any],
    prefix: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Convert an OpenAPI/Swagger spec dict into registry-schema tools."""
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise ValueError("Not an OpenAPI spec: missing 'paths' object.")
    default_cluster = sanitize_name(
        prefix or (spec.get("info") or {}).get("title") or "api"
    )

    tools: list[dict[str, Any]] = []
    for path, operations in spec["paths"].items():
        if not isinstance(operations, dict):
            continue
        for method in _HTTP_METHODS:
            op = operations.get(method)
            if not isinstance(op, dict):
                continue
            name = sanitize_name(op.get("operationId") or f"{method}_{path}")
            cluster = sanitize_name(op["tags"][0]) if op.get("tags") else default_cluster
            description = " ".join(
                s.strip() for s in (op.get("summary", ""), op.get("description", "")) if s
            ).strip()
            tools.append({
                "id": f"{cluster}.{name}",
                "name": name,
                "cluster": cluster,
                "description": description,
                "tags": [cluster, "openapi-import", method],
                "side_effects": _SIDE_EFFECTS.get(method, "write"),
                "parameters": _operation_params(op),
                "returns": {"type": "object"},
            })
    logger.info("imported %d tools from OpenAPI spec %r", len(tools), default_cluster)
    return tools

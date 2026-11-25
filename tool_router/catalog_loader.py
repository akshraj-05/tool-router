"""Assemble the startup catalog for ``serve`` and ``mcp``.

Sources, combined in this order:
1. a catalog JSON file (``--catalog``), kept as written;
2. one or more MCP servers scanned live (``--import-mcp "<command>"``).

Scanned tools have had no human review, so each one passes the STRICT intake
guardrail; a tool that fails is left out and reported. When an id is already
taken, the earlier source wins.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Optional

from tool_router.domain.errors import CatalogError
from tool_router.engine.guardrail import validate_submission
from tool_router.importers.mcp_importer import DEFAULT_TIMEOUT, MCPImportError, import_from_mcp


def split_valid(
    tools: list[dict[str, Any]], known_ids: Optional[set[str]] = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (tools that pass the strict guardrail, one message per rejected tool)."""
    known = set(known_ids or ())
    accepted: list[dict[str, Any]] = []
    rejected: list[str] = []
    for tool in tools:
        verdict = validate_submission(tool, known_ids=known)
        if verdict.accepted:
            accepted.append(tool)
            known.add(tool["id"])
        else:
            rejected.append(
                f"{tool.get('id')}  [{verdict.primary_reason}] {verdict.issues[0].message}"
            )
    return accepted, rejected


def build_catalog(
    catalog_path: Optional[str] = None,
    mcp_commands: Optional[list[str]] = None,
    env_names: Optional[list[str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Return ``(catalog or None, report lines)``.

    ``None`` means no source was given. Raises ``MCPImportError`` if a server
    cannot be scanned, and ``CatalogError`` if the sources yield no tools: a
    server that silently starts with a partial or empty list is harder to debug
    than one that refuses to start.
    """
    if not catalog_path and not mcp_commands:
        return None, []

    catalog: dict[str, Any] = {"tools": []}
    report: list[str] = []
    if catalog_path:
        with open(catalog_path, encoding="utf-8") as fh:
            catalog = json.load(fh)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
            raise CatalogError("Catalog must be a dict with a 'tools' list.")
        report.append(f"loaded {len(catalog['tools'])} tools from {catalog_path}")

    for line in mcp_commands or []:
        parts = shlex.split(line)
        if not parts:
            raise MCPImportError("--import-mcp needs a command, e.g. \"python my_server.py\"")
        found = import_from_mcp(parts[0], parts[1:], env_names=env_names, timeout=timeout)
        known = {t.get("id") for t in catalog["tools"]}
        accepted, rejected = split_valid(found, known_ids=known)
        catalog["tools"] = [*catalog["tools"], *accepted]
        report.append(f"scanned {line!r}: {len(accepted)} of {len(found)} tools accepted")
        report.extend(f"  skipped {message}" for message in rejected)

    if not catalog["tools"]:
        raise CatalogError("No usable tools: " + "; ".join(report))
    return catalog, report

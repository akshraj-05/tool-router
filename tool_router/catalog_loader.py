"""Assemble the startup catalog for ``serve`` and ``mcp``.

Sources, combined in this order:
1. a catalog JSON file (``--catalog``), kept as written;
2. one or more MCP servers scanned live (``--import-mcp``): a local server given
   as a command, or a remote one given as a URL.

Scanned tools have had no human review, so each one passes the STRICT intake
guardrail; a tool that fails is left out and reported. When an id is already
taken, the earlier source wins.
"""

from __future__ import annotations

import shlex
from typing import Any, Optional

from tool_router.domain.errors import CatalogError
from tool_router.domain.naming import prepare_tool
from tool_router.domain.versioning import NONE, change_level
from tool_router.engine.guardrail import validate_submission
from tool_router.importers.mcp_importer import (
    DEFAULT_TIMEOUT,
    import_from_mcp,
    import_from_mcp_url,
)
from tool_router.importers.mcp_remote import (
    MCPImportError,
    build_headers,
    check_url,
    is_url,
    redact_url,
)
from tool_router.limits import read_catalog_file
from tool_router.state import SavedState


def split_valid(
    tools: list[dict[str, Any]], known_ids: Optional[set[str]] = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (tools that pass the strict guardrail, one message per rejected tool)."""
    known = set(known_ids or ())
    accepted: list[dict[str, Any]] = []
    rejected: list[str] = []
    for submitted in tools:
        tool = prepare_tool(submitted)
        verdict = validate_submission(tool, known_ids=known)
        if verdict.accepted:
            accepted.append(tool)
            known.add(tool["id"])
        else:
            rejected.append(
                f"{tool.get('id')}  [{verdict.primary_reason}] {verdict.issues[0].message}"
            )
    return accepted, rejected


def _check_sources(
    sources: list[str], env_names: Optional[list[str]], header_envs: Optional[list[str]],
) -> dict[str, str]:
    """Validate how the options fit the sources, before connecting to anything.

    Returns the authentication headers for the remote server (empty if none).
    """
    remote = [s for s in sources if is_url(s)]
    for url in remote:
        check_url(url)
    if env_names and len(remote) == len(sources):
        raise MCPImportError(
            "--mcp-env passes variables to local servers; for a URL use --mcp-header-env"
        )
    if not header_envs:
        return {}
    if not remote:
        raise MCPImportError("--mcp-header-env needs a remote server (a URL)")
    if len(remote) > 1:
        raise MCPImportError(
            "--mcp-header-env works with one remote server only, so that one server's "
            "secret is never sent to another"
        )
    return build_headers(header_envs)


def build_catalog(
    catalog_path: Optional[str] = None,
    mcp_sources: Optional[list[str]] = None,
    env_names: Optional[list[str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    header_envs: Optional[list[str]] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Return ``(catalog or None, report lines)``.

    ``None`` means no source was given. Raises ``MCPImportError`` if a server
    cannot be scanned, and ``CatalogError`` if the sources yield no tools: a
    server that silently starts with a partial or empty list is harder to debug
    than one that refuses to start.
    """
    headers = _check_sources(mcp_sources or [], env_names, header_envs)
    if not catalog_path and not mcp_sources:
        return None, []

    catalog: dict[str, Any] = {"tools": []}
    report: list[str] = []
    if catalog_path:
        catalog = read_catalog_file(catalog_path)
        report.append(f"loaded {len(catalog['tools'])} tools from {catalog_path}")

    for source in mcp_sources or []:
        if is_url(source):
            shown = redact_url(source)
            found = import_from_mcp_url(source, headers=headers, timeout=timeout)
        else:
            shown = source
            parts = shlex.split(source)
            if not parts:
                raise MCPImportError("--import-mcp needs a command or a URL")
            found = import_from_mcp(parts[0], parts[1:], env_names=env_names, timeout=timeout)
        prepared = (prepare_tool(t) for t in catalog["tools"])
        known = {t["id"] for t in prepared
                 if isinstance(t, dict) and isinstance(t.get("id"), str)}
        accepted, rejected = split_valid(found, known_ids=known)
        catalog["tools"] = [*catalog["tools"], *accepted]
        report.append(f"scanned {shown!r}: {len(accepted)} of {len(found)} tools accepted")
        report.extend(f"  skipped {message}" for message in rejected)

    if not catalog["tools"]:
        raise CatalogError("No usable tools: " + "; ".join(report))
    return catalog, report


def merge_with_saved(
    saved: Optional[SavedState], incoming: Optional[dict[str, Any]], prune: bool = False
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Upsert the startup sources into the saved registry, matching tools by id.

    New ids are added; an existing id takes the incoming definition (a changed one
    gets a new version when the engine saves it); saved tools absent from the
    input are kept unless ``prune``. An incoming tool that omits ``side_effects``
    keeps the saved value. Validation happens afterwards, in the engine, so an
    invalid input stops startup and leaves the saved file untouched.
    """
    if saved is None:
        return incoming, []
    if incoming is None:
        catalog = {"tools": saved.tools, "near_duplicate_groups": saved.near_duplicate_groups}
        return catalog, [f"loaded {len(saved.tools)} saved tools"]

    old = {t["id"]: t for t in saved.tools}
    merged: dict[str, Any] = dict(old)
    problems: list[Any] = []                 # kept so the guardrail reports them
    seen: set[str] = set()
    added = updated = unchanged = 0
    for submitted in incoming["tools"]:
        tool = prepare_tool(submitted)
        tool_id = tool.get("id") if isinstance(tool, dict) else None
        if not isinstance(tool_id, str) or tool_id in seen:
            problems.append(tool)
            continue
        seen.add(tool_id)
        before = old.get(tool_id)
        if before is None:
            added += 1
        else:
            if not submitted.get("side_effects"):
                tool["side_effects"] = before.get("side_effects")
            try:
                same = change_level(before, tool) == NONE
            except (KeyError, TypeError, AttributeError):   # malformed: the guardrail will say how
                same = False
            unchanged, updated = unchanged + same, updated + (not same)
        merged[tool_id] = tool

    missing = [i for i in old if i not in seen]
    if prune:
        for tool_id in missing:
            del merged[tool_id]
    tools = [*merged.values(), *problems]

    groups = incoming.get("near_duplicate_groups", [])
    if isinstance(groups, list):
        kept = [[i for i in g if i in merged] for g in saved.near_duplicate_groups]
        groups = [g for g in kept if len(g) > 1 and g not in groups] + groups
    catalog = {**incoming, "tools": tools, "near_duplicate_groups": groups}
    fate = "removed" if prune else "kept"
    report = [f"saved catalog: {added} new, {updated} changed, {unchanged} unchanged, "
              f"{len(missing)} not in the input ({fate})"]
    return catalog, report


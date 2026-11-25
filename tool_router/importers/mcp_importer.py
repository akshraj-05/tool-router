"""MCP importer — pull tools from an MCP server (onboarding.md, Level 1).

MCP tools are self-describing (`name`, `description`, `inputSchema`), so a
whole catalog onboards from one connection: start the server over stdio, read
every page of `list_tools`, and convert each tool to our registry schema.
Requires the `mcp` extra.

What the conversion guarantees
- **The original tool name is kept** in ``source.name``, so a caller can invoke the
  real tool on its server. ``name``/``id`` are normalized to our snake_case
  convention. The full input schema is not copied: the server remains its source.
- **Side effects are labelled conservatively.** A tool is ``destructive`` when
  the server says so (``destructiveHint``) OR its name contains a destructive
  verb; hints come from a third party, so the stricter answer wins.
- **Only server-provided text is used.** An over-long description is shortened to
  the registry limit (the full text stays in ``source.description``).

Importing only *lists* tools. It never calls them.

Security: starting an MCP server runs a local command. Only ever take that
command from the operator (CLI), never from a network request.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Optional

from tool_router.domain.errors import ToolRouterError
from tool_router.domain.naming import infer_side_effects
from tool_router.engine.guardrail import DESCRIPTION_MAX_CHARS
from tool_router.importers.common import params_from_json_schema, sanitize_name
from tool_router.log import get_logger

logger = get_logger("importers.mcp")

DEFAULT_TIMEOUT = 30.0
_MAX_PAGES = 1000          # stops a server that keeps returning a "next page"
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


class MCPImportError(ToolRouterError):
    """The MCP server could not be started or its tools could not be listed."""


def snake_case(name: str) -> str:
    """``deleteUserAccount`` / ``delete-user.account`` -> ``delete_user_account``."""
    return sanitize_name(_CAMEL_BOUNDARY.sub("_", name))


def cluster_name(text: str) -> str:
    """A cluster / id prefix. The registry id pattern allows only letters and ``_`` there."""
    cleaned = re.sub(r"[^a-z]+", "_", _CAMEL_BOUNDARY.sub("_", text).lower()).strip("_")
    return cleaned or "mcp"


def _side_effects(name: str, annotations: Any) -> str:
    destructive_hint = getattr(annotations, "destructiveHint", None)
    read_hint = getattr(annotations, "readOnlyHint", None)
    inferred = infer_side_effects(name)
    if destructive_hint is True or inferred == "destructive":
        return "destructive"
    if read_hint is True:
        return "read"
    if read_hint is None and destructive_hint is None:
        return inferred              # no hints at all: go by the name
    return "write"


def _shorten(text: str, limit: int = DESCRIPTION_MAX_CHARS) -> str:
    text = " ".join(text.split())          # docstrings arrive with newlines and indents
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _convert(tool: Any, prefix: str, used_names: Optional[set[str]] = None) -> dict[str, Any]:
    """One MCP tool -> our registry schema. ``used_names`` de-duplicates names in a server."""
    used_names = used_names if used_names is not None else set()
    base = name = snake_case(tool.name)
    counter = 2
    while name in used_names:              # e.g. "getUser" and "get_user" on one server
        name = f"{base}_{counter}"
        counter += 1
    used_names.add(name)

    description = (tool.description or "").strip()
    input_schema = getattr(tool, "inputSchema", None)
    return {
        "id": f"{prefix}.{name}",
        "name": name,
        "cluster": prefix,
        "description": _shorten(description),
        "side_effects": _side_effects(name, getattr(tool, "annotations", None)),
        "tags": [prefix, "mcp-import"],
        "parameters": params_from_json_schema(input_schema),
        "source": {
            "type": "mcp",
            "name": tool.name,
            "description": description,
        },
    }


async def _list_all_tools(session: Any) -> list[Any]:
    """Every tool on the server, following ``nextCursor`` across pages."""
    from mcp.types import PaginatedRequestParams

    tools: list[Any] = []
    cursor: Optional[str] = None
    seen: set[str] = set()
    for _ in range(_MAX_PAGES):
        if cursor is None:
            page = await session.list_tools()
        else:
            try:
                page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
            except TypeError:              # older 1.x clients take the cursor directly
                page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools
        if cursor in seen:
            raise MCPImportError("the server repeated a page cursor; stopping")
        seen.add(cursor)
    raise MCPImportError(f"the server returned more than {_MAX_PAGES} pages of tools")


def _forwarded_env(names: Optional[list[str]]) -> dict[str, str]:
    """The named variables from our environment (values are never typed on the command line)."""
    missing = [n for n in names or [] if n not in os.environ]
    if missing:
        raise MCPImportError(f"environment variable(s) not set: {', '.join(missing)}")
    return {n: os.environ[n] for n in names or []}


async def import_from_mcp_async(
    command: str,
    args: Optional[list[str]] = None,
    prefix: Optional[str] = None,
    env_names: Optional[list[str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Connect to an MCP server (stdio) and return its tools in registry schema.

    The server receives a minimal default environment plus the variables named in
    ``env_names``. ``prefix`` defaults to the name the server reports for itself.
    """
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client
    except ImportError as exc:
        raise MCPImportError(
            'the MCP SDK is missing or incompatible — run: pip install -e ".[mcp]"'
        ) from exc

    env = {**get_default_environment(), **_forwarded_env(env_names)}
    params = StdioServerParameters(command=command, args=args or [], env=env)

    async def fetch() -> tuple[str, list[Any]]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                started = await session.initialize()
                server_name = getattr(getattr(started, "serverInfo", None), "name", "") or ""
                return server_name, await _list_all_tools(session)

    shown = " ".join([command, *(args or [])])
    try:
        server_name, listed = await asyncio.wait_for(fetch(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MCPImportError(f"no tool list from {shown!r} within {timeout:g} s") from exc
    except MCPImportError:
        raise
    except Exception as exc:       # the SDK raises many types, often wrapped in groups
        inner: BaseException = exc
        while getattr(inner, "exceptions", None):
            inner = inner.exceptions[0]
        if isinstance(inner, MCPImportError):
            raise inner from exc
        raise MCPImportError(
            f"could not list tools from {shown!r}: {type(inner).__name__}: {inner}"
        ) from exc

    cluster = cluster_name(prefix or server_name or os.path.basename(command))
    used: set[str] = set()
    tools = [_convert(t, cluster, used) for t in listed]
    logger.info("listed %d tools from MCP server %r", len(tools), shown)
    return tools


def import_from_mcp(
    command: str,
    args: Optional[list[str]] = None,
    prefix: Optional[str] = None,
    env_names: Optional[list[str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Synchronous wrapper around :func:`import_from_mcp_async`."""
    return asyncio.run(import_from_mcp_async(command, args, prefix, env_names, timeout))

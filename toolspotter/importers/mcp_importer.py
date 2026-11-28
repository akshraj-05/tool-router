"""MCP importer — pull tools from an MCP server (onboarding.md, Level 1).

MCP tools are self-describing (`name`, `description`, `inputSchema`), so a
whole catalog onboards from one connection: connect, read every page of
`list_tools`, and convert each tool to our registry schema. Two connections are
supported: a LOCAL server started as a command (stdio) and a REMOTE server
reached by URL (Streamable HTTP; safety rules in ``mcp_remote``).
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
import contextlib
import logging
import os
import re
from typing import Any, Awaitable, Callable, Iterator, Optional
from urllib.parse import urlsplit

from toolspotter.domain.naming import infer_side_effects
from toolspotter.engine.guardrail import DESCRIPTION_MAX_CHARS
from toolspotter.importers.common import params_from_json_schema, sanitize_name
from toolspotter.importers.mcp_remote import (
    MCPImportError,
    check_url,
    origin_guard,
    redact_url,
    scrub,
)
from toolspotter.log import get_logger

logger = get_logger("importers.mcp")

DEFAULT_TIMEOUT = 30.0
_MAX_PAGES = 1000          # stops a server that keeps returning a "next page"
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


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


_SDK_MISSING = 'the MCP SDK is missing or incompatible — run: pip install -e ".[mcp]"'


async def _collect(read: Any, write: Any) -> tuple[str, list[Any]]:
    """Open a session over an established transport; return (server name, all tools)."""
    from mcp import ClientSession

    async with ClientSession(read, write) as session:
        started = await session.initialize()
        server_name = getattr(getattr(started, "serverInfo", None), "name", "") or ""
        return server_name, await _list_all_tools(session)


async def _run(
    fetch: Callable[[], Awaitable[tuple[str, list[Any]]]],
    shown: str,
    timeout: float,
    explain: Callable[[BaseException], str],
) -> tuple[str, list[Any]]:
    """Run one scan under the time limit and turn any failure into ``MCPImportError``."""
    try:
        return await asyncio.wait_for(fetch(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MCPImportError(explain(exc) or f"no tool list from {shown!r} within {timeout:g} s") from None
    except MCPImportError:
        raise
    except Exception as exc:       # the SDK raises many types, often wrapped in groups
        inner: BaseException = exc
        while getattr(inner, "exceptions", None):
            inner = inner.exceptions[0]
        if isinstance(inner, MCPImportError):
            raise inner from None
        raise MCPImportError(
            explain(inner)
            or f"could not list tools from {shown!r}: {type(inner).__name__}: {inner}"
        ) from None


def _to_registry(listed: list[Any], cluster_source: str, shown: str) -> list[dict[str, Any]]:
    cluster = cluster_name(cluster_source)
    used: set[str] = set()
    tools = [_convert(t, cluster, used) for t in listed]
    logger.info("listed %d tools from MCP server %r", len(tools), shown)
    return tools


async def import_from_mcp_async(
    command: str,
    args: Optional[list[str]] = None,
    prefix: Optional[str] = None,
    env_names: Optional[list[str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Start a local MCP server (stdio) and return its tools in registry schema.

    The server receives a minimal default environment plus the variables named in
    ``env_names``. ``prefix`` defaults to the name the server reports for itself.
    """
    try:
        from mcp import StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client
    except ImportError as exc:
        raise MCPImportError(_SDK_MISSING) from exc

    env = {**get_default_environment(), **_forwarded_env(env_names)}
    params = StdioServerParameters(command=command, args=args or [], env=env)

    async def fetch() -> tuple[str, list[Any]]:
        async with stdio_client(params) as (read, write):
            return await _collect(read, write)

    shown = " ".join([command, *(args or [])])
    server_name, listed = await _run(fetch, shown, timeout, lambda _exc: "")
    return _to_registry(listed, prefix or server_name or os.path.basename(command), shown)


@contextlib.contextmanager
def _quiet_http_logs() -> Iterator[None]:
    """The HTTP and MCP libraries log full URLs on errors; we report errors ourselves."""
    loggers = [logging.getLogger(n) for n in ("httpx", "httpcore", "mcp")]
    previous = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        for lg, level in zip(loggers, previous):
            lg.setLevel(level)


async def import_from_mcp_url_async(
    url: str,
    headers: Optional[dict[str, str]] = None,
    prefix: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Connect to a remote MCP server (Streamable HTTP) and return its tools.

    ``headers`` are sent with every request (see ``mcp_remote.build_headers``).
    ``prefix`` defaults to the server's reported name, then the URL's host name.
    """
    url = check_url(url)
    headers = dict(headers or {})
    try:
        import httpx
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise MCPImportError(_SDK_MISSING) from exc

    shown = redact_url(url)
    refused: list[int] = []              # HTTP error / redirect statuses seen

    async def note_status(response: Any) -> None:
        if response.status_code >= 300:
            refused.append(response.status_code)

    async def fetch() -> tuple[str, list[Any]]:
        async with httpx.AsyncClient(
            headers=headers, follow_redirects=False, timeout=timeout,
            # The SDK applies its own redirect rule; the request hook enforces ours too.
            event_hooks={"request": [origin_guard(url)], "response": [note_status]},
        ) as client:
            async with streamable_http_client(url, http_client=client) as (read, write, _):
                return await _collect(read, write)

    def explain(exc: BaseException) -> str:
        status = refused[-1] if refused else None     # the answer that ended the attempt
        if status in (401, 403):
            hint = "check the header value (a bearer token is sent as 'Bearer <token>')" if headers else "it needs --mcp-header-env (import-mcp: --header-env)"
            return f"{shown} rejected the request (HTTP {status}): authentication failed — {hint}"
        if status is not None and 300 <= status < 400:
            return (f"{shown} answered with a redirect (HTTP {status}) that was not followed "
                    "(redirects to another server are refused); use the final URL")
        if status is not None:
            return f"{shown} answered HTTP {status}; is this an MCP (Streamable HTTP) endpoint?"
        if isinstance(exc, asyncio.TimeoutError):
            return ""
        return scrub(f"could not list tools from {shown!r}: {type(exc).__name__}: {exc}", url, headers)

    with _quiet_http_logs():
        server_name, listed = await _run(fetch, shown, timeout, explain)
    return _to_registry(listed, prefix or server_name or (urlsplit(url).hostname or ""), shown)


def import_from_mcp_url(
    url: str,
    headers: Optional[dict[str, str]] = None,
    prefix: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Synchronous wrapper around :func:`import_from_mcp_url_async`."""
    return asyncio.run(import_from_mcp_url_async(url, headers, prefix, timeout))


def import_from_mcp(
    command: str,
    args: Optional[list[str]] = None,
    prefix: Optional[str] = None,
    env_names: Optional[list[str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Synchronous wrapper around :func:`import_from_mcp_async`."""
    return asyncio.run(import_from_mcp_async(command, args, prefix, env_names, timeout))

"""The website: its files and the endpoints made for it.

The website is four static files in ``ui/`` that call the same REST API as any
other client. Routing is not affected by anything here.
"""

# No "from __future__ import annotations" here: FastAPI is imported inside
# add_website (it is optional), so its types must be read where they are defined.

import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr

from tool_router.catalog_changes import Change, plan_change
from tool_router.catalog_loader import split_valid
from tool_router.domain.naming import SIDE_EFFECTS
from tool_router.engine.pipeline import RoutingEngine
from tool_router.importers.mcp_importer import import_from_mcp_url_async
from tool_router.importers.mcp_remote import MCPImportError, auth_header, redact_url
from tool_router.netutil import host_name
from tool_router.postman import add_examples, collection

UI_FOLDER = Path(__file__).resolve().parent / "ui"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# The pages load only their own files, cannot be framed and run no inline script.
_PAGE_HEADERS = [
    (b"content-security-policy",
     b"default-src 'self'; img-src 'self' data:; base-uri 'none'; "
     b"form-action 'none'; frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-cache"),
]


class UploadRequest(BaseModel):
    tools: list[Any] = Field(..., min_length=1, description="Tool definitions")
    mode: str = Field("merge", description="'merge' adds and updates; 'replace' replaces all")
    near_duplicate_groups: Any = None


class ScanRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000, description="https:// URL of the server")
    token: Optional[SecretStr] = Field(None, description="Used for this scan only; never saved")
    header: str = Field("Authorization", description="The header that carries the token")
    prefix: Optional[str] = Field(None, max_length=60, description="Group name for the tools")


class OriginCheck:
    """ASGI middleware: refuse changes requested by another website.

    A browser sends ``Origin`` with every request that can change something. If it
    names a site other than this server (or an allowed host name), the request is
    refused. Programs such as curl and Postman send no ``Origin`` and are unaffected.
    """

    def __init__(self, app, allowed: frozenset[str]) -> None:
        self.app, self.allowed = app, allowed

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = {k: v.decode("latin-1") for k, v in scope["headers"]}
        origin = headers.get(b"origin")
        if origin is None or self._ours(origin, headers.get(b"host", "")):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"detail": "Requests from another website are not allowed."}).encode()
        await send({"type": "http.response.start", "status": 403, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    def _ours(self, origin: str, host: str) -> bool:
        try:
            netloc = urlsplit(origin).netloc.lower()
        except ValueError:
            return False
        return bool(netloc) and (netloc == host.lower() or host_name(netloc) in self.allowed)


class PageHeaders:
    """ASGI middleware: add the security headers to the website's own files."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/ui"):
            await self.app(scope, receive, send)
            return

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *_PAGE_HEADERS]}
            await send(message)

        await self.app(scope, receive, with_headers)


def summarise(engine: Optional[RoutingEngine]) -> dict[str, Any]:
    """Registry facts for the dashboard."""
    tools = engine.list_tools() if engine else []
    effects = Counter(t.get("side_effects") for t in tools)
    return {
        "loaded": engine is not None,
        "tool_count": len(tools),
        "groups": dict(Counter(t.get("cluster") for t in tools).most_common()),
        "side_effects": {name: effects.get(name, 0) for name in SIDE_EFFECTS},
        "duplicate_groups": [list(g) for g in engine.maps.dup_groups] if engine else [],
        "old_tools": sum(bool(t.get("deprecated") or t.get("replaced_by")) for t in tools),
        "planner": None if engine is None else (
            "local" if engine.planner.__class__.__name__ == "LocalPlanner" else "llm"),
    }


def add_website(
    app,
    current_engine: Callable[[], Optional[RoutingEngine]],
    current_state: Callable[[], Any],
    apply_catalog: Callable[[Callable[[], tuple[dict[str, Any], Any]]], Any],
    run: Callable[[Callable[[], Any]], Any],
    saves: bool,
    mcp_scan: bool,
) -> None:
    """Add the website and its endpoints to ``app``.

    ``current_state`` returns the registry being served (tools and groups) or None.
    ``apply_catalog(prepare)`` replaces it, calling ``prepare`` under the registry
    lock. ``run`` calls a function and turns router errors into HTTP errors.
    """
    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles

    scanning = {"busy": False}

    def change_for(request: UploadRequest) -> Change:
        state = current_state()
        return plan_change(
            state.tools if state else [], state.near_duplicate_groups if state else [],
            request.tools, request.mode, request.near_duplicate_groups)

    @app.get("/registry/summary")
    def registry_summary() -> dict[str, Any]:
        """Counts for the dashboard, and what this server allows."""
        return {**summarise(current_engine()), "saved": saves, "mcp_scan": mcp_scan}

    @app.post("/registry/preview")
    def preview_upload(req: UploadRequest) -> dict[str, Any]:
        """What ``/registry/apply`` would do with this upload. Changes nothing."""
        return run(lambda: change_for(req).report())

    @app.post("/registry/apply")
    def apply_upload(req: UploadRequest) -> dict[str, Any]:
        """Add the valid tools of an upload to the registry, or replace it with them."""
        done: dict[str, Any] = {}

        def prepare() -> tuple[dict[str, Any], Any]:
            state = current_state()
            change = change_for(req)
            if not (change.added or change.changed or change.unchanged):
                raise HTTPException(status_code=422, detail={
                    "reason": "NOTHING_TO_SAVE", "rejected": change.rejected})
            done.update(change.report())
            catalog = {"tools": change.tools,
                       "near_duplicate_groups": change.near_duplicate_groups}
            return catalog, state

        apply_catalog(prepare)
        return {"status": "saved", **done}

    @app.get("/registry/export")
    def export_catalog():
        """The current tools as a catalog file."""
        state = current_state()
        if state is None:
            raise HTTPException(status_code=409, detail="No tool registry loaded yet.")
        return JSONResponse(
            {"tools": state.tools, "near_duplicate_groups": state.near_duplicate_groups},
            headers={"content-disposition": 'attachment; filename="catalog.json"'})

    @app.post("/mcp/scan")
    async def scan_mcp_server(req: ScanRequest) -> dict[str, Any]:
        """List the tools of a remote MCP server. Nothing is added to the registry."""
        if not mcp_scan:
            raise HTTPException(status_code=403, detail=(
                "Scanning from the website is off because this server accepts other "
                "machines. Start it with --allow-mcp-scan to turn it on."))
        if scanning["busy"]:
            raise HTTPException(status_code=429, detail="Another scan is still running.")
        scanning["busy"] = True
        try:
            token = req.token.get_secret_value().strip() if req.token else ""
            headers = auth_header(req.header, token) if token else {}
            found = await import_from_mcp_url_async(req.url, headers=headers, prefix=req.prefix)
            tools, rejected = split_valid(found)
        except MCPImportError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            scanning["busy"] = False
        return {"server": redact_url(req.url), "found": len(found),
                "tools": tools, "rejected": rejected}

    # ------------------------------------- API description and Postman

    describe = app.openapi

    def openapi() -> dict[str, Any]:
        return add_examples(describe())

    app.openapi = openapi

    @app.get("/postman.json", include_in_schema=False)
    def postman(request: Request):
        return JSONResponse(
            collection(app.openapi(), str(request.base_url)),
            headers={"content-disposition":
                     'attachment; filename="tool-router.postman_collection.json"'})

    # --------------------------------------------------------------- pages

    @app.get("/", include_in_schema=False)
    def home(request: Request):
        return RedirectResponse(request.scope.get("root_path", "").rstrip("/") + "/ui/")

    app.mount("/ui", StaticFiles(directory=UI_FOLDER, html=True), name="ui")

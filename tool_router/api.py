"""REST API — the routing-as-a-service surface (see docs/design/architecture.md).

JSON endpoints (no auth — a documented, deliberate omission for this scope):
- POST  /registry          register/replace the tool catalog (onboard)
- GET    /registry/tools         list every tool
- GET    /registry/tools/{id}    read one tool
- POST   /registry/tools         create one tool (201)
- PUT    /registry/tools/{id}    replace one tool; its id cannot change
- DELETE /registry/tools/{id}    delete one tool
- POST  /route             route one query -> selected tools + ordered plan + trace
- PATCH /registry/mutate   apply a PARTIAL catalog update: {"add": [...], "remove": [...]}
                           (PATCH, not PUT/POST: this is a partial update of the registry
                           resource, not a full replace and not a bare action)
- POST  /setup             save config (provider/model/key) — JSON, same as the others
- GET   /health            liveness + engine facts (always open, for probes)

Non-JSON exception:
- GET   /setup             a small server-rendered HTML form. This is a deliberate,
                           documented exception to the "everything is JSON" rule above:
                           it exists purely as a human-facing convenience so a browser can
                           open http://host:port/setup directly (no separate frontend, no
                           file editing, no interactive terminal) and POST to the same
                           /setup JSON endpoint. It is NOT part of the programmatic API
                           contract and no client should depend on its HTML shape.

One registry per service instance (single-tenant; multi-tenancy is a
documented future step). The app can boot pre-loaded via `tool-router serve
--catalog file.json` or be fed at runtime through POST /registry.

Requires the `api` extra: pip install .[api]
"""

from __future__ import annotations

import json
import threading
from html import escape as _escape
from typing import Any, Optional

from pydantic import BaseModel, Field

from tool_router import __version__
from tool_router.config import Settings
from tool_router.domain.errors import (
    CatalogError,
    DuplicateToolError,
    InvalidCatalogError,
    InvalidToolError,
    StateError,
    ToolInUseError,
    ToolNotFoundError,
)
from tool_router.domain.versioning import inherit_side_effects
from tool_router.engine.pipeline import RoutingEngine
from tool_router.limits import max_input_bytes
from tool_router.log import get_logger
from tool_router.netutil import host_name, is_loopback
from tool_router.state import SavedState, StateStore

logger = get_logger("api")


class RegistryRequest(BaseModel):
    # `Any`, so the guardrail sees every entry and can report all problems together.
    tools: list[Any] = Field(..., min_length=1, description="Tool definitions")
    # Optional catalog-level metadata (near_duplicate_groups etc.) is honored.
    model_config = {"extra": "allow"}


class RouteRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=10_000)
    context: Optional[dict[str, Any]] = Field(
        default=None,
        description="Accepted for forward compatibility; routing does not use it yet.",
    )


class MutateRequest(BaseModel):
    add: list[Any] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class _BodyLimit:
    """ASGI middleware: refuse request bodies over the limit with 413.

    Counts the bytes actually received, so a missing or false Content-Length
    cannot get a large body through.
    """

    def __init__(self, app, limit: int) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return                                  # client went away
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                body = json.dumps({"detail": f"Request body exceeds {self.limit} bytes."}).encode()
                await send({"type": "http.response.start", "status": 413, "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, replay, send)


class _HostCheck:
    """ASGI middleware: answer only requests addressed to this machine.

    A web page can point its own domain name at 127.0.0.1 ("DNS rebinding") and
    then reach a local server from the visitor's browser. Such requests carry the
    page's domain in ``Host``, so they are refused here. Loopback names are always
    accepted, plus any names the operator allowed.
    """

    def __init__(self, app, allowed: frozenset[str]) -> None:
        self.app, self.allowed = app, allowed

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        values = [v for k, v in scope["headers"] if k == b"host"]
        name = host_name(values[0].decode("latin-1")) if len(values) == 1 else ""
        if name and (is_loopback(name) or name in self.allowed):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"detail": (
            "Host header not allowed. This server answers requests for localhost; "
            "start it with --allow-host NAME to accept another name."
        )}).encode()
        await send({"type": "http.response.start", "status": 400, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def create_app(
    catalog: Optional[dict[str, Any]] = None,
    store: Optional[StateStore] = None,
    previous: Optional[SavedState] = None,
    allowed_hosts: Optional[list[str]] = None,
):
    """App factory. Import of FastAPI is deferred so the core package works
    without the `api` extra installed."""
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "FastAPI is not installed — install the service extra: pip install .[api]"
        ) from exc

    app = FastAPI(
        title="tool-router",
        version=__version__,
        description="Intelligent tool routing: large catalog in, minimal ordered tool plan out.",
    )
    app.add_middleware(_BodyLimit, limit=max_input_bytes())
    if allowed_hosts is not None:           # None = no Host check (see the CLI)
        names = frozenset(host_name(h) for h in allowed_hosts) - {""}
        app.add_middleware(_HostCheck, allowed=names)      # added last = runs first
    state: dict[str, Optional[RoutingEngine]] = {"engine": None}

    if catalog is not None:
        state["engine"] = RoutingEngine(
            catalog, settings=Settings.from_env(), store=store, previous=previous)
    # One change at a time, so replacing the registry cannot race a tool edit
    # (both save to the same folder). Routing never takes this lock.
    registry_lock = threading.Lock()

    def _engine_or_400() -> RoutingEngine:
        engine = state["engine"]
        if engine is None:
            raise HTTPException(
                status_code=409,
                detail="No tool registry loaded yet — POST /registry first.",
            )
        return engine

    @app.post("/registry")
    def register(req: RegistryRequest) -> dict[str, Any]:
        def replace() -> RoutingEngine:
            current = state["engine"]
            before = current.snapshot() if current else previous
            catalog = req.model_dump()
            if before:
                catalog["tools"] = inherit_side_effects(catalog["tools"], before.tools)
            engine = RoutingEngine(
                catalog, settings=Settings.from_env(), store=store, previous=before,
            )
            state["engine"] = engine
            return engine

        engine = _change(replace)
        return {
            "status": "indexed",
            "tool_count": engine.tool_count,
            "clusters": len(engine.maps.clusters),
            "near_duplicate_groups": len(engine.maps.dup_groups),
            "version_families": len(engine.maps.version_families),
        }

    @app.post("/route")
    def route(req: RouteRequest) -> dict[str, Any]:
        engine = _engine_or_400()
        result = engine.route(req.query, context=req.context)
        payload = result.to_response_dict()
        payload["selected_tool_schemas"] = [
            engine.raw_tool(tid) for tid in result.selected_tools
        ]
        payload["tokens_saved_pct"] = result.tokens_saved_pct
        payload["trace"] = result.trace
        return payload

    @app.patch("/registry/mutate")
    def mutate(req: MutateRequest) -> dict[str, Any]:
        _engine_or_400()
        return _change(lambda: state["engine"].apply_mutation({"add": req.add, "remove": req.remove}))

    def _change(action):
        """``_apply`` for operations that change the registry: one at a time."""
        def locked():
            with registry_lock:
                return action()
        return _apply(locked)

    def _apply(action):
        """Run a registry operation, translating its errors into HTTP responses."""
        try:
            return action()
        except StateError as exc:
            raise HTTPException(
                status_code=500, detail=f"Not saved, so nothing changed: {exc}"
            ) from exc
        except ToolNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DuplicateToolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ToolInUseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidCatalogError as exc:      # before CatalogError, its base class
            raise HTTPException(
                status_code=422, detail={"reason": "INVALID_CATALOG", "rejected": exc.rejected}
            ) from exc
        except InvalidToolError as exc:
            raise HTTPException(
                status_code=422, detail={"reason": exc.reason, "issues": exc.issues}
            ) from exc
        except CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/registry/tools")
    def list_tools() -> dict[str, Any]:
        tools = _engine_or_400().list_tools()
        return {"tool_count": len(tools), "tools": tools}

    @app.get("/registry/tools/{tool_id}")
    def read_tool(tool_id: str) -> dict[str, Any]:
        engine = _engine_or_400()
        return _apply(lambda: engine.get_tool(tool_id))

    @app.post("/registry/tools", status_code=201)
    def create_tool(tool: dict[str, Any]) -> dict[str, Any]:
        _engine_or_400()
        return _change(lambda: state["engine"].create_tool(tool))

    @app.put("/registry/tools/{tool_id}")
    def update_tool(tool_id: str, tool: dict[str, Any]) -> dict[str, Any]:
        _engine_or_400()
        return _change(lambda: state["engine"].update_tool(tool_id, tool))

    @app.delete("/registry/tools/{tool_id}")
    def delete_tool(tool_id: str) -> dict[str, Any]:
        _engine_or_400()
        _change(lambda: state["engine"].delete_tool(tool_id))
        return {"deleted": tool_id, "tool_count": state["engine"].tool_count}

    def _secret_slot(provider: str) -> Optional[str]:
        """Secret name for a provider, or None if that provider takes no key.

        Follows the same f"{provider}_key" convention store.py's secret names
        already use (gemini_key) — generic by construction, so a
        newly added provider works without touching this file.
        """
        return f"{provider}_key" if provider and provider != "none" else None

    @app.get("/setup")
    def setup_page():
        """Browser Settings page — a human-facing convenience, not a JSON API
        endpoint (see the module docstring). One `docker run`, open /setup,
        paste the key, pick a provider and any model name, Save — no file
        editing, no interactive terminal. Key is stored server-side
        (keyring/0600 file), shown only as present/absent, never echoed.
        Provider and model are free text — nothing here is hardcoded to a
        specific provider or model; whatever is currently stored is shown.
        """
        from fastapi.responses import HTMLResponse
        from tool_router.store import load_secret, load_stored_config

        stored = load_stored_config()
        provider = str(stored.get("llm_provider", "none"))
        secret_name = _secret_slot(provider)
        key_state = "present" if secret_name and load_secret(secret_name) else "absent"
        # Stored values are user-supplied text: escape before putting them in HTML.
        e = lambda value: _escape(str(value), quote=True)  # noqa: E731
        html = f"""<!doctype html><title>tool-router setup</title>
<style>body{{font-family:sans-serif;max-width:34rem;margin:3rem auto}}label{{display:block;margin:.8rem 0 .2rem}}input{{width:100%;padding:.4rem}}button{{margin-top:1rem;padding:.5rem 1.2rem}}</style>
<h2>tool-router setup</h2>
<p>Key for provider "{e(provider)}": <b>{key_state}</b> (stored server-side, never displayed)</p>
<label>LLM provider ("none" for fully offline, or any provider name tool_router supports)</label>
<input id=p value="{e(provider)}">
<label>LLM model (any model name your provider supports)</label>
<input id=m placeholder="e.g. gemini-3.5-flash-lite" value="{e(stored.get("llm_model", ""))}">
<label>Embedding provider</label>
<input id=e value="{e(stored.get("embedding_provider", "local"))}">
<label>API key (leave blank to keep current; not used for provider "none")</label>
<input id=k type=password autocomplete=off>
<button onclick="save()">Save</button><span id=s></span>
<script>async function save(){{
 const body={{llm_provider:p.value,llm_model:m.value,embedding_provider:e.value}};
 if(k.value) body.api_key=k.value;
 const r=await fetch('/setup',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify(body)}});
 s.textContent=r.ok?' saved — restart or re-register to apply':' error';k.value='';}}
</script>"""
        return HTMLResponse(html)

    @app.post("/setup")
    def setup_save(body: dict[str, Any]) -> dict[str, Any]:
        from tool_router.store import load_stored_config, save_secret, save_stored_config

        key = body.pop("api_key", None)
        allowed = {k: str(v) for k, v in body.items()
                   if k in {"llm_provider", "llm_model", "embedding_provider"}}
        if allowed:
            save_stored_config(allowed)
        stored_where = None
        if key:
            provider = allowed.get("llm_provider") or load_stored_config().get("llm_provider", "none")
            secret_name = _secret_slot(provider)
            if secret_name is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Provider '{provider}' takes no API key.",
                )
            stored_where = save_secret(secret_name, key)
        return {"status": "saved", "config": allowed,
                "key_stored": stored_where or "unchanged"}

    @app.get("/health")
    def health() -> dict[str, Any]:
        engine = state["engine"]
        return {
            "status": "ok",
            "version": __version__,
            "registry_loaded": engine is not None,
            "tool_count": engine.tool_count if engine else 0,
            "planner": type(engine.planner).__name__ if engine else None,
        }

    return app

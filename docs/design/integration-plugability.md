# Integration & Plugability — How the Router Plugs Into an Agent

> How a real AI agent talks to our router. This is the *integration surface* only — it does
> NOT change the core engine or the pipeline (see `architecture.md`).

## Two patterns (same router brain, different socket)

### Pattern A — Router as a tool (MCP meta-tool)
The agent is given ONE special tool, `find_tools(query)`. Mid-thinking, the agent calls it;
our router returns the top-K real tools; the agent then uses those.

- ✅ Plug-and-play with any MCP-compatible agent — register one tool, no framework changes.
- ✅ Matches the standard agent/tool-loop pattern MCP hosts already expect.
- ✅ Agent can re-ask to widen (natural fallback loop).
- ❌ Costs one extra LLM round-trip (call tool → get result → call again).

### Pattern B — Router as an API gateway (pre-filter middleware)
The agent framework calls `POST /route` BEFORE the LLM and injects only the K returned tools.
The LLM never "sees" the router — it just gets a slim, pre-filtered tool list.

- ✅ Maximum token savings (no extra round-trip).
- ✅ Deterministic, transparent, fully in our control.
- ✅ Works as a standalone routing API gateway in front of any agent framework.
- ❌ The agent framework must call our API (a little more integration glue).

## DECISION

**Build order: ENGINE → REST API (primary demo surface) → MCP (showcase). We do BOTH; REST first.**

Why REST first:
- **REST is low-risk and fast** (FastAPI + curl/Swagger/tiny UI in ~half a day) and it *shows
  the token savings on screen* — the clearest way to demonstrate a working system. No dependency
  on a third-party MCP host behaving on camera.
- **MCP is the plug-and-play cherry** — a thin wrapper added once the engine is solid, so any
  MCP-based agent can adopt the router with zero framework changes.

## Important: this is independent of the core engine

We ALWAYS implement the core `route()` engine first. REST and MCP are thin layers on top of
it, so routing behaves the same through either one.

## Build implication (in order)

1. **Core engine** — mandatory, transport-agnostic. Build first.
2. **REST API (primary, now):** FastAPI gateway — `POST /registry` (onboard/index tools),
   `POST /route` (query → top-K tools + plan), `GET /health`. Reliable demo spine; shows token
   savings live.
3. **MCP adapter (showcase):** thin MCP server exposing `find_tools(query, k)` and `registry_status`
   (both read-only) over the same engine — for the plug-and-play story.
- **Demo:** lead with the REST call (query in → slim correct tool set + tokens saved); then
  show the same engine plugged into an MCP agent as the "it just drops in anywhere" moment.

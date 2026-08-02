# Tool Router — Intelligent Tool Router for Large Tool Catalogs

## Project Overview
**Routing-as-a-service for AI agents.** An agent with 60+ tools can't carry every schema in its
prompt — token cost explodes and selection accuracy collapses among look-alike tools. This router
sits between the query and the catalog: register your tools once (bulk JSON, or auto-import from
any **MCP server** / **OpenAPI spec** — all validated by a strict intake guardrail), then every
query returns the **minimal, ordered, high-precision tool plan** the agent should see.

**Measured on an internal 29-case evaluation suite** (64-tool catalog; methodology and full
breakdown in [docs/eval_report.md](docs/eval_report.md)):

| Configuration | Accuracy | Token savings |
|---|---|---|
| Naive keyword/cluster baseline | 7/29 (24%) | 94% |
| This router — local-only (zero LLM, offline) | 24/29 (83%) | 98% |
| **This router — hybrid (LLM planner + HyDE + scope-arbiter)** | **26/29 (90%)** | **98%** |

**Note**: the 90% hybrid number was measured with a **free-tier Gemini API key** on
**`gemini-3.1-flash-lite`** (the lightweight, free-tier-friendly model) — a paid tier or a larger
model in the same family would plausibly push accuracy higher still; see
[docs/eval_report.md](docs/eval_report.md) for the full breakdown.

Plus: registry guardrail **11/11** fixtures · catalog-churn scenarios **8/8** (add / remove /
replace / version / scale 64→104). These historical numbers were measured against a larger
internal catalog/test suite that isn't bundled with this repo (see below for what *is* included
and runnable out of the box).

**How it works:** fused local retrieval (TF-IDF/dense embeddings + BM25 + capability signals +
side-effect/version/PII penalties, all combined into one score) → version/duplicate collapse +
confidence gate (dynamic-K, clarify/refuse) → **one** LLM call for final ordering/repeat-counts/
version reasoning. On low-confidence queries it adds **conditional HyDE** (hypothetical-doc
retrieval) for recall, with the LLM as an explicit **scope arbiter** so out-of-scope queries
still return nothing. A deterministic local planner sits beneath it all, so the engine **never
crashes and runs fully offline** if no key is present. One engine, **three entry points**: a plain
Python class (for programmatic/batch use), a REST API (any language), and an MCP tool
`find_tools` (any MCP-based agent).

## Live URL
Not deployed — runs locally in one command (see below).

## Technical Design / Architecture Decision Record
- **[docs/design/architecture.md](docs/design/architecture.md)** — master architecture + diagram
  (rendered image: [docs/design/architechure_diagram.png](docs/design/architechure_diagram.png))
- [docs/design/engine-design.md](docs/design/engine-design.md) — engine internals & cost model
- [docs/eval_report.md](docs/eval_report.md) — measured iteration trajectory (24%→90%)

## Tech Stack
Python 3.11+ · pydantic · platformdirs · FastAPI/uvicorn (REST) · MCP SDK · provider-agnostic
LLM/embedding layer — **local TF-IDF is the default (no LLM, fully offline)**, **Gemini is
live-verified**, an **AWS Bedrock provider exists in code but is not live-tested** · Docker.
Lean core — hosted providers are optional extras; the engine runs with zero external
dependencies out of the box. Storage is a plain JSON file cache (no vector DB — see
`docs/design/storage.md` for why that's the right call at this scale).

## Setup & Run Instructions

```bash
# 1. install (venv recommended)
python3 -m venv .venv && .venv/bin/pip install -e ".[api,mcp]"

# 2. (optional) configure an LLM — engine runs fully offline without it
.venv/bin/tool-router init          # wizard: provider, key (hidden), model
# or: export GEMINI_API_KEY=... TOOL_ROUTER_LLM_PROVIDER=gemini \
#            TOOL_ROUTER_LLM_MODEL=gemini-flash-lite-latest

# 3. run the REST service (docs at /docs, config page at /setup)
.venv/bin/tool-router serve --catalog path/to/tools_catalog.json
curl -X POST localhost:8080/route -H 'content-type: application/json' \
     -d '{"query": "Pull last quarter revenue and email the chart to finance"}'

# or as an MCP server (find_tools / register_tools):
.venv/bin/tool-router mcp --catalog path/to/tools_catalog.json

# or Docker:
docker build -t tool-router . && docker run -p 8080:8080 tool-router

# auto-import a catalog instead of hand-writing one:
.venv/bin/tool-router import-mcp --command <mcp-server-cmd> -o tools.json
.venv/bin/tool-router import-openapi openapi.json -o tools.json
```

**Real vs. mocked:** the router, guardrail, churn engine, REST/MCP surfaces, importers and
persistence are fully real and tested. The router selects tools; it never executes them, so any
catalog (mock or real) works the same way — bring your own tool definitions.

## Additional Links / Resources
- `.env.example` — full configuration reference (no secrets in this repo)

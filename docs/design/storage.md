# Storage — Design Decisions (as implemented)

Goals: **survive restarts, easy to inspect, no external database, no secrets in a project.**

## What is stored

| Data | Where | Notes |
|---|---|---|
| Registered tools and their history | `.tool-router/catalog.json` | `state.py` |
| Tool vectors | `.tool-router/embeddings.json` | content hash + model stamp (`store.py::JsonCache`) |
| LLM enrichment (off by default) | `.tool-router/enrichment.json` | same cache format |
| Settings and API key | per-user config dir / OS keyring | never in the project |

BM25 statistics and derived maps (clusters, near-duplicate groups, version families) are
recalculated from the catalog when the index is built. Queries and results are not stored.

## The state folder

`serve` and `mcp` use `./.tool-router` (change with `--state-dir`, disable with `--no-persist`).
Code that builds a `RoutingEngine` without a `StateStore` keeps everything in memory.

```
.tool-router/
  .gitignore         "*"
  catalog.json       {"format_version": 1, "saved_at": "...", "near_duplicate_groups": [...],
                      "tools": [...], "history": {"<id>": [older definitions]}}
  embeddings.json    {"model_stamp": "...", "entries": {content_hash: vector}}
```

- **Atomic writes:** each file is written to a temporary file (mode 0600), flushed to disk and
  renamed over the old one.
- **Disk first:** a change is saved before the new index is published. If saving fails, memory
  and disk both keep the previous state.
- **Damaged state:** an unreadable `catalog.json` stops startup and is left untouched. A damaged
  cache file is discarded and recalculated.
- **One router per folder:** there is no cross-process lock.

## Startup: upsert into the saved registry

`--catalog` and `--import-mcp` are merged into the saved catalog by tool id
(`catalog_loader.merge_with_saved`): new ids are added, existing ids take the incoming
definition (an omitted `side_effects` keeps the saved value), and saved tools missing from the
input are kept unless `--prune`. The merged catalog passes the same strict guardrail; if it
fails, startup stops and the saved file is unchanged. `POST /registry` replaces the registry.

## Versions and history (`domain/versioning.py`)

A changed definition becomes the current entry with a higher version; the previous one moves to
`history` (10 kept per id, never routed). Major: the call name changed, or a parameter was
removed, renamed, retyped or made required. Minor: an optional parameter added, a parameter made
optional, or a parameter description changed. Patch: anything else. A higher caller-supplied
version is used instead. Deleted tools move to history, and re-adding one continues its numbering.

## Vectors

- Cosine similarity runs in-process; there is no vector database.
- A vector is reused only if both the tool-text hash and the model stamp match. The local TF-IDF
  stamp includes a corpus hash, so any catalog change recalculates its vectors (cheap). Hosted
  providers re-embed only changed tools.
- Vectors of removed tools are dropped. `serve --rebuild` / `TOOL_ROUTER_REBUILD=1` starts fresh.
- JSON vectors are large for dense models; this suits hundreds of tools better than thousands.

## Docker

Mount the state folder to keep it between containers (`/app/.tool-router`, writable by the
container user). Saved settings need a mount of the config dir.

## Configuration & Setup (key + model) — no file editing

Users configure the provider, key, and model without hand-editing a file.

### What's configurable
- **Generation LLM provider** — `none` (default, fully offline) | `gemini` (live-verified) — via `TOOL_ROUTER_LLM_PROVIDER`.
- **Generation LLM model** (`TOOL_ROUTER_LLM_MODEL`) and **embedding provider/model**
  (`TOOL_ROUTER_EMBEDDING_PROVIDER`/`_MODEL`) — changing the embedding provider invalidates and
  rebuilds the cache via the model-stamp check above.
- No key is required for the default (local, offline) configuration.

### pip → terminal wizard
```bash
tool-router init                          # interactive: provider, key (hidden), model
tool-router config set llm_model gemini-2.5-flash
tool-router config show                   # key shown as present/absent, never the value
tool-router serve --catalog catalog.json
```

### REST → `/setup` page
```bash
tool-router serve --port 8080
# GET  /setup  -> a minimal HTML form (provider / model / key)
# POST /setup  -> saves non-secret config + (optionally) the key
```
Implemented as a simple form served by the same FastAPI app (`tool_router/api.py`) — no
external config UI. In Docker, persisting saved config/secrets across container restarts
requires mounting a volume at the resolved config directory (see the override caveat above).

### Where config is stored & key safety
- Non-secret settings: `config.json` in the per-user config dir (`platformdirs`), never in a project.
- Secret (the API key): **OS keyring** if available (`keyring` package), else a `0600`
  permission fallback file (`secrets.json`) in the same config dir. Key input is never echoed
  or logged — logs only report `key=present`/`key=absent`.

### Precedence
```
environment variable  >  stored config (init / config set / /setup)  >  built-in default
```
There is currently no separate "CLI flag overrides everything" layer beyond what `serve`'s own
arguments (`--catalog`, `--rebuild`, etc.) provide; env vars are the primary override mechanism
for automation.

### Confirm at startup (no mystery, no leak)
Logged once per engine build (`RoutingEngine._log_summary`), e.g.:
```
tool-router ready · tools=64 · embedding=local-tfidf · planner=LocalPlanner · llm=none · key=absent · saved to=.tool-router
```

## In one line

A `.tool-router` folder holds the registry, its history and the vector cache as atomically
written JSON; settings and keys stay in the user's config dir.

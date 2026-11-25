# Storage — Design Decisions (as implemented)

How the router persists tools + embeddings. Goals: **easy to use, pluggable, OS-agnostic,
no external DB, safe across multiple projects.**

## What we store

Only two small things (whole thing is well under 1 MB at our scale):
1. **The registry** — the tool definitions themselves, held in memory (`RoutingEngine._raw_tools`
   / `_raw_by_id`); returned to clients unchanged (`raw_tool()`).
2. **Embeddings** — the "meaning numbers" per tool (expensive to compute → cached to disk).

Derived maps (clusters, near-dup groups, version families, capability tokens) are recomputed in
milliseconds from the registry at index-build time, not separately persisted.

## The store: an embedded JSON file (no server, no vector DB)

- **What we actually ship** (`tool_router/store.py`): a small persisted key→value cache
  (`JsonCache`) — per-tool embedding vectors written to a plain JSON file
  (`embeddings.json`), keyed by a content hash of the tool's text and stamped with the
  embedding provider's identity (model id, or a corpus hash for the local TF-IDF provider).
  A second cache (`enrichment.json`) holds one-time LLM-enrichment results the same way.
  Similarity search itself runs **in-process** (plain Python cosine in `engine/text.py`,
  BM25 in `engine/bm25.py`) over these vectors — there is no vector-database server or
  embedded vector-DB library involved.
- No external server, no Postgres, no vector-DB dependency, nothing to install or run — it's
  pure Python and ships as part of the package.
- **Why not a vector DB (e.g. Chroma):** at the catalog sizes this router targets (order of
  10²–10³ tools; verified up to 104 in the scale-stress scenario), brute-force cosine over all
  vectors is sub-millisecond — a vector DB's approximate-nearest-neighbor index buys nothing at
  this scale. It would also only replace the *dense-cosine* slice of the fused score (dense +
  BM25 + capability + penalties, see `engine-design.md`), not the whole ranking — so we'd still
  compute the other signals in Python and recombine. It remains a reasonable option if the
  catalog grows far larger, kept behind the same `EmbeddingProvider`/store boundary so it could
  be added later without touching the engine.

## Where the data lives (OS-agnostic)

Never hardcode a path like `/opt` (Linux-only). Instead:

- **pip / local:** `platformdirs.user_data_dir("tool_router", "tool_router")` → correct per-OS
  location (Linux `~/.local/share/...`, macOS `~/Library/Application Support/...`, Windows
  `%LOCALAPPDATA%\...`). Overridable via `TOOL_ROUTER_DATA_DIR`.
- **Docker:** always Linux inside the container, so the same `platformdirs` call resolves
  consistently; mount a volume at whatever `TOOL_ROUTER_DATA_DIR` you set (or the default
  resolved path) so it survives container restarts.

Config/secrets use a **separate** platformdirs location, `user_config_dir(...)` — currently
**not** overridable by an env var (only the data dir has `TOOL_ROUTER_DATA_DIR`); persisting it
across container restarts means mounting a volume at that resolved path.

## Folder layout (as implemented)

```
<platformdirs data dir>/
  projects/
    <project-id>/
      embeddings.json     # {"model_stamp": "...", "entries": {content_hash: vector}}
      enrichment.json     # {"model_stamp": "llm-enrich-v1", "entries": {content_hash: {...}}}

<platformdirs config dir>/
  config.json             # non-secret settings written by `tool-router init` / `config set` / /setup
  secrets.json            # 0600-permission fallback when the OS keyring is unavailable
```

There is no separate `registry.json`/`meta.json` on disk — the registry lives in the running
process's memory for the lifetime of the service; each cache file's own `model_stamp` field
plays the role a separate `meta.json` would.

## Multi-project isolation — automatic

Each project gets its own `<project-id>` folder → indexes never mix. The id is derived
**automatically** via a marker file in the current working directory:

```
your-project/
  .tool_router        # { "project_id": "..." }  auto-created on first run
```

Resolution order (`store.py::resolve_project_id`):
```
marker file exists?  -> read project_id            (stable even if the folder is renamed/moved)
else writable?       -> generate id, write marker
else                 -> hash(cwd absolute path)     (fallback; not rename-stable)
```

- Two projects (different working directories) → two markers → two isolated stores.
- Same project (same working directory) → same id → reuses its cache (no re-embedding cost).
- **Add `.tool_router` to `.gitignore`** so the id isn't committed (else clones would share it).
- Caches are **per-project, not shared across projects** — two projects that happen to register
  an identical tool each pay their own one-time embedding cost.

## Conflict handling (self-healing)

1. **Cross-project clobber** → solved by per-project `<project-id>` namespacing above.
2. **Stale index (tool changed but old embedding remains)** → each embedding is keyed by a
   **content hash** of the tool's text. On (re)build: same hash → reuse; changed → re-embed.
   Removed tools simply aren't looked up again — the cache file can accumulate unused entries
   over time (harmless; no active pruning is implemented).
3. **Embedding-model mismatch** → the cache's `model_stamp` records the provider identity; if
   the configured provider/model differs, the whole cache is invalidated and rebuilt (vectors
   from two different models are never mixed).
4. **Escape hatch** → `TOOL_ROUTER_REBUILD=1` or `tool-router serve --rebuild` wipes and
   re-embeds from scratch.

## Cost note

The content-hash cache means restarts cost nothing (vectors reload from disk) and a registry
mutation re-embeds only the tools that actually changed — this is what keeps LLM-embedding
providers (e.g. Gemini) affordable across repeated runs.

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
- Non-secret settings: `config.json` in the platformdirs config dir.
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
tool-router ready · tools=64 · embedding=local-tfidf · planner=LocalPlanner · llm=none · key=absent · data=/home/.../tool_router
```

## In one line

An embedded, dependency-free JSON file cache (no server, no vector DB) for embeddings +
enrichment, located per-OS via `platformdirs`, namespaced per project via an auto marker file,
self-healing on content hash + model stamp; config (key + model) set via `tool-router init` or
a `/setup` form — never by hand-editing a file — with environment variables as the primary
override for automation.

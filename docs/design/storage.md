# Storage

Goals: survive restarts, need no database server, keep secrets out of projects.

## What is stored

| Data | Where |
|---|---|
| Registered tools and their history | `.tool-router/catalog.json` |
| Vectors and near-duplicate pairs | `.tool-router/vectors.db` (SQLite, part of standard Python) |
| Settings | `config.json` in the per-user config folder |
| API key | OS keyring, or a `0600` file in the config folder |

Search text, tokens, the keyword index and planner data are rebuilt from the catalog at start.
Queries and results are not stored.

## The state folder

`serve` and `mcp` use `./.tool-router` (`--state-dir` to change it, `--no-persist` to disable).
A `RoutingEngine` built without a `StateStore` keeps everything in memory. The folder contains a
`.gitignore` of `*`. There is no locking between processes: use one router per folder.

## `catalog.json` — your data

```json
{"format_version": 1, "saved_at": "…", "near_duplicate_groups": [],
 "tools": [ … ], "history": {"<id>": [ … ]}}
```

- Written to a temporary file (mode `0600`), flushed, then renamed over the old one.
- Saved **before** the new index goes live. If saving fails, nothing changes.
- A damaged file stops startup and is left untouched.
- It is also a valid `--catalog` file. Back up this file.

## `vectors.db` — a cache

| Table | Columns | Contents |
|---|---|---|
| `vectors` | `hash`, `vector` | Key: a hash of the tool's search text. Local vectors are JSON text; Gemini vectors are 64-bit floats in a BLOB |
| `meta` | `name`, `value` | `model_stamp`, and `duplicate_pairs` with its fingerprint |

- Everything in it can be recomputed, so it can be deleted at any time.
- It holds **one** method's vectors. Changing `TOOL_ROUTER_EMBEDDING_PROVIDER` replaces them.
- While Gemini is unavailable (an outage or a missing key) nothing is written, so its saved
  vectors survive.
- Each save is one transaction and writes only what changed.
- Unusable rows are recomputed, a damaged file is replaced, and a locked or unwritable file is
  left alone with a warning. None of these stops the router.
- `serve --rebuild` or `TOOL_ROUTER_REBUILD=1` recomputes everything.

An `embeddings.json` from an earlier version is ignored and can be deleted.

## Startup merge

`--catalog` and `--import-mcp` are merged into the saved catalog by tool `id`: new tools are
added and existing ones take the incoming definition (an omitted `side_effects` keeps the saved
value). Saved tools missing from the input are kept unless `--prune`. `POST /registry` replaces
the registry.

## Versions

A changed definition becomes current with a higher version; the old one moves to `history`
(10 per id, never routed).

| Change | Version |
|---|---|
| Call name changed; parameter removed, renamed, retyped or made required | `x` |
| Optional parameter added, parameter made optional, parameter description changed | `y` |
| Anything else | `z` |

A higher version supplied by the caller is used instead. A deleted tool that is added again
continues its numbering.

## Configuration

```bash
tool-router init                              # provider, key, model
tool-router config set llm_model gemini-2.5-flash
tool-router config show                       # the key is shown only as present or absent
```

The REST service also offers `/setup`. Precedence: environment variable, then stored config,
then default.

## Docker

Mount `/app/.tool-router` to keep the registry between containers.

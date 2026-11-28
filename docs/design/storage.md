# Storage

Goals: survive restarts, need no database server, keep secrets out of projects.

## What is stored

| Data | Where |
|---|---|
| Registered tools, their history and declared duplicate groups | `.tool-router/router.db` |
| Vectors and near-duplicate pairs | `.tool-router/router.db` |
| Recent requests | `.tool-router/router.db` |
| Settings | `config.json` in the per-user config folder |
| API key | OS keyring, or a `0600` file in the config folder |

Search text, tokens, the keyword index and planner data are rebuilt from the tools at start and
held in memory. Queries are answered from that memory, never from the database.

## The state folder

`serve` and `mcp` use `./.tool-router` (`--state-dir` to change it, `--no-persist` to disable).
A `RoutingEngine` built without a `StateStore` keeps everything in memory. The folder contains a
`.gitignore` of `*`. Use one router per folder: two would overwrite each other's changes.

## `router.db`

One SQLite file (part of standard Python), readable by its owner only.

| Table | Columns | If it is lost |
|---|---|---|
| `tools` | `position`, `id`, `definition` (JSON text) | The registry is gone |
| `tool_history` | `position`, `tool_id`, `definition` | Version history is gone |
| `duplicate_groups` | `position`, `tool_ids` | Declared groups must be declared again |
| `vectors` | `hash`, `vector` | Computed again |
| `cache` | `name`, `value` | Computed again |
| `requests` | `id`, `at`, `query`, `result`, `plan`, `ms`, `tokens_saved_pct` | History is gone |

`position` keeps the order of the tools, which can decide a tie between equal scores. The format
number is stored in SQLite's `user_version`.

Because the registry is in this file, **the router never deletes or replaces it**.

## Registry

- Tools, history and groups are written in one transaction: all of it, or nothing. A process
  killed while saving leaves the previous version.
- Saved **before** the new index goes live. If saving fails, nothing changes.
- A damaged, empty or unreadable file stops startup and is left as it is. Move it aside to start
  fresh.
- `tool-router export -o tools.json` writes the saved tools as a `--catalog` file. Use it to read
  them or to keep a copy.

## Vectors and cached results

- `vectors`: the key is a hash of the tool's search text. Local vectors are JSON text; Gemini
  vectors are 64-bit floats in a BLOB.
- `cache`: `model_stamp`, and `duplicate_pairs` with its fingerprint.
- The tables hold **one** method's vectors. Changing `TOOL_ROUTER_EMBEDDING_PROVIDER` replaces
  them.
- While Gemini is unavailable (an outage or a missing key) nothing is written, so its saved
  vectors survive.
- Each save is one transaction and writes only what changed.
- Unusable rows are computed again; a failed write is logged and the router carries on.
- `serve --rebuild` or `TOOL_ROUTER_REBUILD=1` recomputes everything.

## Request history

Each request answered by `POST /route` becomes one row. The MCP server records nothing.

| Column | Contents |
|---|---|
| `at` | UTC time |
| `query` | The first 500 characters, **as plain text** |
| `result` | `plan`, `question` or `none` |
| `plan` | Tool ids in call order, as JSON text |
| `ms` | Routing time |
| `tokens_saved_pct` | Estimate; empty unless tools were returned |

- Only the newest 1,000 rows are kept.
- Rows are written by a background thread, which collects requests for half a second and saves
  them together. Answering never waits for the disk. A server that is stopped normally writes
  what is left; a killed one can lose that last half second.
- A history problem is logged and never fails a request.
- `GET /history` returns totals and recent requests. `DELETE /history` removes them and
  overwrites their text in the file.
- With `--no-persist` there is no history.

## Startup merge

`--catalog` and `--import-mcp` are merged into the saved registry by tool `id`: new tools are
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
tool-router config set llm_model gemini-3.5-flash-lite
tool-router config show                       # the key is shown only as present or absent
```

The REST service also offers `/setup`. Precedence: environment variable, then stored config,
then default.

## Docker

Mount `/app/.tool-router` to keep the registry between containers.

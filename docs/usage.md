# Using ToolSpotter

Details that go beyond the [README](../README.md). Internals are in [design/](design/architecture.md).

## Website

`toolspotter serve` also serves a website at http://127.0.0.1:8080/ui/.

| Section | What you can do |
|---|---|
| Dashboard | See tool counts by group and side effect, duplicate groups, and recent requests with routing time and estimated tokens saved |
| Try it | Type a request and see the chosen tools, their scores and what was dropped |
| Add tools | Upload a catalog file or scan a remote MCP server, preview the result, then save |
| Tools | Search, filter, create, edit and delete tools |
| API | Open the API documentation, or download a Postman collection and the current catalog |

It is plain HTML, CSS and JavaScript, served by the same process, with no build step: a dark theme with a side menu on wide screens and a top menu on phones. Animations stop when your system asks for reduced motion. The website loads nothing from the internet. The API documentation at `/docs` is FastAPI's built-in page, which loads its viewer from a public CDN and so needs an internet connection.

- **Upload** adds and updates by default; "Replace all" removes tools that are not in the upload. Invalid tools are listed and skipped.
- **MCP scan** accepts URLs only; to load a local server, use `--import-mcp`. A token typed on the page is used for that scan and never saved. Scanning is off when the server accepts other machines, unless started with `--allow-mcp-scan`.
- **Other websites** cannot make changes: requests that carry another site's `Origin` are refused.
- There is still **no login**. Anyone who can reach the server can use the website.

## Load tools straight from an MCP server

Point the router at an MCP server and it scans the server's tools at startup:

```bash
toolspotter serve --host 127.0.0.1 --import-mcp "python my_server.py"
toolspotter mcp --import-mcp "python my_server.py"
```

- Quote the whole command. Repeat `--import-mcp` for several servers, and add `--catalog file.json` to combine them with a file.
- The server gets only a minimal environment. Pass a variable it needs by name: `--mcp-env GITHUB_TOKEN`.
- The scan gives up after 30 seconds (`--mcp-timeout`). If it fails, or finds no usable tools, the router does not start.
- Tools that fail the intake checks are skipped and listed, for example for a description shorter than 6 words or more than 12 parameters.
- Each tool keeps its original name in `source.name`; use that name to call it on its server.
- A tool is marked destructive if the server says so or its name contains a word such as `delete` or `restart`. Other destructive tools may not be recognised.
- Restart the router to pick up changes on a server.

`--import-mcp` runs the command or contacts the address you give it, so only use servers you trust. It is available on the command line only, not through the REST API.

### Remote servers

Give a URL instead of a command (Streamable HTTP):

Authentication is by HTTP header. `--mcp-header-env HEADER=NAME` sends `HEADER` with the value of the environment variable `NAME`:

```bash
read -rs TOKEN                                  # paste the token; it stays out of shell history
export SHOP_AUTH="Bearer $TOKEN"; unset TOKEN
toolspotter serve --host 127.0.0.1 \
    --import-mcp "https://shop.example.com/mcp" --mcp-header-env Authorization=SHOP_AUTH
```

| The server expects | Variable holds | Option |
|---|---|---|
| Nothing | — | none |
| Bearer token | `Bearer <token>` | `--mcp-header-env Authorization=NAME` |
| API key or another header | the key | `--mcp-header-env X-API-Key=NAME` |
| Basic | `Basic <encoded>` | `--mcp-header-env Authorization=NAME` |
| Several headers | one variable each | repeat the option |

- Values are read from environment variables and are not printed or stored with the tools.
- `https` is required; plain `http` is accepted for `localhost` only.
- Redirects are followed only within the same server. A redirect to another server is refused, so your token is never sent elsewhere.
- A user name or password inside the URL is refused.
- `--mcp-header-env` works with **one** remote server at a time, so one server's secret is never sent to another.
- **Not supported:** OAuth (including browser sign-in), client certificates and the older SSE transport.
- If any server cannot be scanned, the router does not start.

To review the tools before loading them, write them to a file instead:

```bash
toolspotter import-mcp --command python --args my_server.py -o tools.json
# remote: toolspotter import-mcp --url https://shop.example.com/mcp --header-env Authorization=SHOP_AUTH -o tools.json
toolspotter serve --catalog tools.json
```

## Tool format

```json
{
  "name": "send_email",
  "description": "Send an email message to a recipient with a subject and body",
  "parameters": [{"name": "to", "type": "string", "required": true}],
  "side_effects": "write",
  "cluster": "comm",
  "id": "comm.send_email"
}
```

`name`, `description` and `parameters` are required. `cluster` defaults to `general`, and `id` is built as `cluster.name` if you leave it out. `side_effects` is `read`, `write` or `destructive`; any other value is refused. If you leave it out it is guessed from the name (`delete_…`, `restart_…` and similar are destructive; `get_…`, `list_…` and similar are read; everything else is write). The guess misses names such as `close_account`, so set it for any tool that changes or deletes something. An update that omits it keeps the current value. Optional: `tags`, `version`, `deprecated`, `replaced_by`.

**Versions.** To update a tool, replace it with `PUT` and change `version`, which is information only. If the old and new versions must both stay callable, register both and mark the old one `"deprecated": true` with `"replaced_by": "<new id>"`. The router then returns the new one unless the request asks for the legacy or old one. Names are never used to guess versions.

A catalog file is `{"tools": [ ... ]}`.

## Validation

A catalog file, an API upload, single-tool requests and MCP scans all apply the same rules.

| Rule | Limit |
|---|---|
| Required | `name`, `description`, `parameters` |
| Unknown fields and wrong data types | Refused |
| `name` | snake_case, up to 128 characters |
| `description` | 6 words to 500 characters |
| `parameters` | Up to 12 objects, each with a unique `name`; `type` is `string`, `number`, `integer`, `boolean`, `array` or `object` |
| `replaced_by` | Must name an existing tool, with no cycle; that tool cannot be deleted while referenced |
| Catalog file or request body | Up to 5 MB (`TOOLSPOTTER_MAX_BYTES`) |
| Tools in a registry | Up to 5,000 (`TOOLSPOTTER_MAX_TOOLS`) |

If a catalog has invalid tools, **nothing is loaded** and every problem is listed (over REST: status 422 with a `rejected` list). Check a file beforehand with `toolspotter validate tools.json`, which accepts a list of tools or a `{"tools": [...]}` object. An MCP scan skips invalid tools and lists them.

## Manage tools over REST

| Request | Action |
|---|---|
| `POST /registry` | Load or replace the whole catalog |
| `GET /registry/tools` | List all tools |
| `GET /registry/tools/{id}` | Read one tool |
| `POST /registry/tools` | Create one tool |
| `PUT /registry/tools/{id}` | Replace one tool (its id cannot change) |
| `DELETE /registry/tools/{id}` | Delete one tool |
| `PATCH /registry/mutate` | Add and remove several tools at once |
| `GET /history` | Totals and the most recent requests (`?limit=20`) |
| `DELETE /history` | Delete the saved requests |
| `GET /registry/summary` | Counts by group and side effect |
| `POST /registry/preview` | Show what an upload would change, without saving |
| `POST /registry/apply` | Add the valid tools of an upload (`"mode": "merge"` or `"replace"`) |
| `GET /registry/export` | Download the current tools as a catalog file |
| `POST /mcp/scan` | List the tools of a remote MCP server |
| `GET /postman.json` | Download a Postman collection |

A rejected create or update changes nothing. Changes are saved to the `.toolspotter` folder (see below).

## Network access

**The REST API has no authentication**, so it is local by default.

| Setting | Who can use the API |
|---|---|
| default (`--host 127.0.0.1`) | Programs on this machine only |
| `--host 0.0.0.0` | Anyone who can connect: they can read, change and delete tools and use `/setup`. The CLI prints a warning. |

- A local server answers only requests whose `Host` header is `localhost` or a loopback address, which blocks web pages that point their own domain at your machine. Behind a proxy or a custom host name, add `--allow-host NAME`.
- "This machine" includes every program and user account on it. Under WSL, Windows programs can usually reach it too.
- Before exposing it to a network you do not trust, put authentication and TLS in front of it.

For Docker:

```bash
docker build -t toolspotter .
docker run -p 127.0.0.1:8080:8080 toolspotter
```

Inside the container the service must listen on `0.0.0.0` (the image does this), so publish the port on `127.0.0.1` as above to keep it local. To also check the `Host` header there:

```bash
docker run -p 127.0.0.1:8080:8080 toolspotter serve --host 0.0.0.0 --port 8080 --allow-host localhost
```

To keep registered tools when the container is replaced, mount a folder. **Create it first:** if it does not exist, Docker creates it as root and the router stops with `Permission denied`.

```bash
mkdir -p .toolspotter
docker run -p 127.0.0.1:8080:8080 \
  -v "$PWD/.toolspotter:/app/.toolspotter" \
  -v "$PWD/tests/data/catalog.json:/app/catalog.json:ro" \
  toolspotter serve --host 0.0.0.0 --port 8080 --allow-host localhost --catalog /app/catalog.json
```

- The container runs as user 1000. If your user id is different, add `--user "$(id -u):$(id -g)"`. Use it only together with the mounted folder.
- To use Gemini, add `-e GEMINI_API_KEY -e TOOLSPOTTER_LLM_PROVIDER=gemini`. The key is passed at run time and is not stored in the image.

For the MCP server, keep standard input open with `-i`. An MCP host can start it with:

```bash
docker run -i --rm -v "/path/to/catalog.json:/app/catalog.json:ro" toolspotter mcp --catalog /app/catalog.json --no-persist
```

A local MCP server given to `--import-mcp` runs inside the container, so its code and dependencies must be there.

## Saved registry (`.toolspotter/`)

`serve` and `mcp` keep everything they save in one SQLite file, in a `.toolspotter` folder in the directory you start them from, and load it again on the next start.

```
.toolspotter/
├── .gitignore         ignores everything in the folder
└── router.db          tools and their history, saved vectors, recent requests
```

```bash
toolspotter serve --catalog tests/data/catalog.json   # first start: loads and saves
toolspotter serve                                      # later: loads the saved tools
toolspotter export -o tools.json                       # saved tools as a catalog file
```

- **Upsert.** With `--catalog` or `--import-mcp`, tools are matched by `id`: new ones are added and existing ones take the incoming definition. Saved tools missing from the input are **kept**; add `--prune` to remove them. The incoming definition wins over an earlier API edit.
- **Versions.** A changed definition becomes the current entry with a higher version, and the old one moves to `history` (the last 10 per tool). History is never routed.

| Change | Version |
|---|---|
| New tool | `1.0.0` |
| Description, side effects, tags and other details | third number |
| Optional parameter added, parameter made optional, parameter description | second number |
| Name changed; parameter removed, renamed, retyped or made required | first number |

  A higher `version` that you supply is used instead.
- **Vectors.** Saved vectors are computed again whenever they cannot be used. One method's vectors are held at a time, and changing `TOOLSPOTTER_EMBEDDING_PROVIDER` replaces them. While Gemini is unavailable nothing is written, so its saved vectors survive an outage.
- **Faster restarts.** Near-duplicate tools are found once and reused until a tool, a threshold or the vector method changes. `serve --rebuild` recomputes everything.
- **Safety.** A change is saved in one transaction before it takes effect; if saving fails, nothing changes. The router never deletes `router.db`: a damaged file stops startup and is left as it is. Settings and API keys are never written here, and the file is readable only by you.
- **Request history.** The last 1,000 requests to `POST /route` are saved with the first 500 characters of each query, **as plain text**. `DELETE /history` removes them. Saving happens in the background; `--no-persist` turns it off. MCP requests are not recorded.
- **Options.** `--state-dir PATH` uses another folder (useful when an MCP host starts the router from an unexpected directory). `--no-persist` keeps everything in memory.
- **Limit.** Run one router per folder; two would overwrite each other's changes.
- **Docker.** Mount the folder to keep it, and create it first: see [Network access](#network-access).

## When Gemini is unavailable

If Gemini embeddings fail, the router uses local embeddings for that query (or for that index, if the failure happens while indexing) and records `embedding_fallback` in the trace for query failures. Rate limits are retried first, so a request can wait over a minute before falling back.

The local planner is deterministic and works offline. LLM-assisted features depend on provider availability, model behaviour, credentials and rate limits; failures are designed to fall back to the local path.

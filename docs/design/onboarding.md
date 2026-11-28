# Onboarding tools

```
tool → guardrail (validate or reject) → version → index → save
```

## Ways to add tools

| Source | How |
|---|---|
| Catalog JSON file | `serve` / `mcp --catalog FILE`, or `POST /registry` |
| One tool at a time | `POST`, `PUT`, `DELETE` on `/registry/tools` |
| Several changes at once | `PATCH /registry/mutate` with `add` and `remove` |
| Local MCP server | `--import-mcp "<command>"` |
| Remote MCP server | `--import-mcp https://…` (Streamable HTTP) |
| MCP server to a file, for review | `toolspotter import-mcp --command …` or `--url …` |

## Tool format

Required: `name`, `description`, `parameters`. Optional: `id` (built as `cluster.name`),
`cluster` (`general`), `side_effects` (guessed from the name), `tags`, `version`,
`deprecated`, `replaced_by`, `source` (set by MCP import). A parameter has `name`, and
optionally `type`, `required`, `description`. The older fields `returns`, `aliases` and
`replaces` are accepted and ignored.

`side_effects` is `read`, `write` or `destructive`. The guess is weak: a destructive word in the
name means destructive, a read verb first means read, anything else write. **Declare it** for
tools that change or delete data.

## Guardrail rules

| Group | Rule | Reason code |
|---|---|---|
| Shape | A JSON object | `MALFORMED_JSON_STRUCTURE` |
| | Required fields present | `MISSING_REQUIRED_FIELD` |
| | No unknown fields | `UNKNOWN_FIELD` |
| | Correct field types | `INVALID_FIELD_TYPE` |
| Names | `id` is `cluster.name` in lowercase, up to 128 characters | `BAD_ID_FORMAT` |
| | `name` is lowercase snake_case, up to 128 characters | `BAD_NAME_FORMAT` |
| Description | At least 6 words | `DESCRIPTION_TOO_VAGUE` |
| | At most 500 characters | `DESCRIPTION_TOO_LONG` |
| Parameters | At most 12 | `TOO_MANY_PARAMETERS` |
| | Objects with unique names and no unknown fields | `INVALID_PARAMETER` |
| | Type is string, number, integer, boolean, array or object | `INVALID_PARAM_TYPE` |
| Values | `version` is `x.y.z` | `BAD_VERSION_FORMAT` |
| | `side_effects` is one of the three values | `INVALID_SIDE_EFFECTS` |
| References | `id` is unique | `DUPLICATE_ID` |
| | `replaced_by` exists | `DANGLING_REPLACED_BY` |
| | No `replaced_by` cycle | `CIRCULAR_VERSION_REF` |

Also: a tool that another points to cannot be removed, `near_duplicate_groups` must name
existing tools, a registry cannot be empty, input is limited to 5 MB
(`TOOLSPOTTER_MAX_BYTES`) and 5,000 tools (`TOOLSPOTTER_MAX_TOOLS`).

## How strict each path is

| Path | An invalid tool |
|---|---|
| Catalog file, `POST /registry` | Every problem is listed; nothing is loaded |
| Create or update one tool | Rejected; the registry is unchanged |
| Mutate | That tool is rejected; others apply. A failed replacement keeps the old tool |
| MCP scan | The tool is skipped and listed |

## MCP import

Tools are only **listed**, never called. All pages are read. The original tool name is kept in
`source.name`, which is the name to call on that server. A tool is destructive if the server
says so or its name contains a destructive word. Long descriptions are shortened.

| Option | Use |
|---|---|
| `--mcp-env NAME` | Pass a variable to a local server |
| `--mcp-header-env HEADER=NAME` | Send a header to one remote server, value from a variable |
| `--mcp-timeout SECONDS` | Give up after this long (default 30) |

Starting a local server runs a command, so it is accepted only from the command line, never
over the network. OAuth is not supported. One server that cannot be scanned stops startup.

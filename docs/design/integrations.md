# Using the router from an agent

The router receives the user's request as text and returns tools. Your agent calls them.

## REST: filter before the model call

Ask the router first, then give your model only the returned tools.

```bash
curl -s -X POST http://127.0.0.1:8080/route \
  -H 'content-type: application/json' \
  -d '{"query": "what was the revenue last quarter"}'
```

| Response field | Meaning |
|---|---|
| `selected_tools` | Ids of the chosen tools |
| `selected_tool_schemas` | Their registered definitions |
| `plan` | Call order; an id may repeat |
| `clarify`, `clarify_question` | Ask the user instead of acting |
| `tokens_saved_pct` | Estimate compared with sending the whole catalog |
| `trace` | Why these tools were chosen |

An empty `selected_tools` with `clarify: false` means no registered tool applies. The `context`
field in the request is accepted but not used yet.

```python
import requests

def tools_for(query):
    result = requests.post("http://127.0.0.1:8080/route", json={"query": query}, timeout=30)
    result.raise_for_status()
    return result.json()
```

Convert each definition to your model provider's tool format.

## MCP: the router as a tool

```json
{"mcpServers": {"tool-router": {
  "command": "tool-router",
  "args": ["mcp", "--catalog", "/path/catalog.json"]}}}
```

| Tool | Returns |
|---|---|
| `find_tools(query, k=8)` | Definitions of up to `k` tools, the plan and any clarification |
| `registry_status()` | Counts of tools, clusters, duplicate groups and version families |

Both are read-only. `find_tools` returns definitions as information; the router does not expose
or run the tools themselves. The agent must be able to call them, for example through their own
MCP server, using `source.name` for imported tools.

## Tool ids

Ids are preserved exactly as registered. Many function-calling APIs do not allow dots in names;
`importers/common.py::mcp_safe_name` shows a safe mapping, but the router does not apply it.

## Network

By default the REST service answers only this machine. It has no authentication: see
[architecture](architecture.md#6-security-posture).

# How Agentic Systems Integrate With the Router

Two integration modes over the **same engine**: the **REST API (pre-filter)** — our primary,
works with any framework/language — and **MCP (router-as-a-tool)** — the plug-and-play
showcase for MCP agents.

Mental model: our system takes the **user's natural-language query** and returns the **relevant
tools (+ their schemas + suggested order)**. We select tools; the client's LLM executes them.
There are two LLMs — the client's agent LLM (does the work) and our internal planner
(hidden plumbing). We never receive anything "from the LLM"; we read the query text.

---

## A. API (REST pre-filter) — PRIMARY

**Idea:** before the agent's LLM turn, call `/route` with the user query, get back the K tool
schemas, bind **only those** to the LLM. The agent LLM never sees the other tools → max token
savings. Works with any framework because it's just an HTTP call before the LLM step.

### Generic (any language / framework)
```
POST /route { "query": "<user request>" }
   → { "selected_tools": [ {id, description, parameters}, ... ],
       "plan": [...] }
# feed `selected_tools` into your LLM's tools/functions param
```

### LangChain
```python
import requests
from langchain_google_genai import ChatGoogleGenerativeAI

def tools_for(query):
    res = requests.post("http://localhost:8080/route", json={"query": query}).json()
    return [to_lc_tool(t) for t in res["selected_tools"]]   # convert schema -> LC tool

query = "email last quarter's revenue chart to finance"
llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash")
llm_with_tools = llm.bind_tools(tools_for(query))          # only K bound, not 64
response = llm_with_tools.invoke(query)
```

### LangGraph (a router node before the agent node)
```python
def router_node(state):                              # NEW node
    res = requests.post(".../route", json={"query": state["query"]}).json()
    return {"tools": res["selected_tools"]}

def agent_node(state):
    llm_with_tools = llm.bind_tools([to_lc_tool(t) for t in state["tools"]])
    return {"messages": [llm_with_tools.invoke(state["query"])]}

g.add_edge(START, "router")
g.add_edge("router", "agent")
g.add_conditional_edges("agent", needs_tool, {"yes": "tools", "no": END})
g.add_edge("tools", "agent")
```

The agent LLM never sees the other tools; a node calls our API to decide which tools to bind
for that turn.

---

## B. MCP — "router as a tool" (showcase)

**Idea:** we run as an MCP server exposing a `find_tools(query)` tool. Any MCP-compatible agent
binds that one tool; the LLM calls it mid-reasoning and gets the relevant tools back.

### The MCP server (us)
```
tool: find_tools(query: str, k?: int) -> [ {name, description, inputSchema}, ... ]
tool: registry_status()               -> tool count and registry stats
```
Both are read-only. The catalog is assembled at startup, from `--catalog` and/or a scan
of other MCP servers with `--import-mcp "<command or URL>"`; an agent cannot change it over MCP.
Scanned tools keep their original name in `source.name`, which is the name to use when
calling the tool on its own server.

### Any MCP host (e.g. Claude Desktop / mcp.json)
```json
{ "mcpServers": {
    "tool-router": { "command": "tool-router", "args": ["mcp", "--catalog", "/path/catalog.json"] }
} }
```
The agent now has `find_tools`; it calls it whenever it needs a capability.

### LangGraph / LangChain via MCP adapter
```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

client = MultiServerMCPClient({
    "tool-router": {"command": "tool-router", "args": ["mcp", "--catalog", "/path/catalog.json"], "transport": "stdio"}
})
tools = await client.get_tools()          # includes find_tools
agent = create_react_agent(llm, tools)    # LLM can now call find_tools during its loop
```

### Honest nuance about MCP
`find_tools` returns a list of relevant tool schemas; *how the agent then calls them* depends
on the host:
- **(i) Guidance mode** — `find_tools` returns schemas as context; the real tools are also
  served by the same MCP server, so the agent knows which to call. Simple, but all tools are
  technically exposed.
- **(ii) Dynamic mode** — the agent dynamically adds the returned tools to its callable set.
  Cleaner (true filtering), but not every host supports mid-session tool registration.

This is exactly why the REST pre-filter (A) is our primary — it delivers token savings
deterministically and works everywhere. MCP (B) is the plug-and-play / discovery showcase; we
implement whichever mode the demo host supports best.

---

## Tool IDs / names

- We **preserve the tool `id`** from the registry exactly (e.g. `fin.get_revenue_report`) — a
  caller matches `selected_tools`/`plan` against specific ids, so we never invent or alter them.
- If a tool lacks an id, we build it as `cluster.name` from the declared values
  (`cluster` defaults to `general`). A repeated id in a catalog is refused.
- **MCP/LLM function names** must match `^[a-zA-Z0-9_-]+$` and be <= ~64 chars (dots often not
  allowed). A sanitizer for this exists (`importers/common.py::mcp_safe_name`,
  `comm.send_email` -> `comm_send_email`) but is not currently invoked in the live path — our
  MCP server exposes only meta-tools (`find_tools`, `registry_status`), which
  have no dots. It would be needed if catalog tools were ever exposed directly over MCP by name.

---

## Summary

| Framework | API (pre-filter) | MCP (router-as-tool) |
|---|---|---|
| **LangChain** | call `/route`, `bind_tools(k)` | MCP adapter -> `find_tools` |
| **LangGraph** | router node -> `bind_tools(k)` | MCP adapter -> ReAct agent |
| **Any / other language** | HTTP `/route` before LLM | any MCP client via stdio/HTTP |

**In one line:** API mode = your app calls `/route` and binds only the returned tools to the
LLM (works everywhere, max savings); MCP mode = the LLM itself calls our `find_tools` tool
during reasoning (plug-and-play for MCP agents).

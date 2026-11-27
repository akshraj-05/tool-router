# Why two interfaces

Both are thin layers over the same engine, so routing behaves the same through either.

| | REST | MCP |
|---|---|---|
| Who calls | Your application, before the model | The model, during its tool loop |
| Extra model round trip | No | Yes |
| Works with | Any language | Any MCP host |
| Manage tools | Yes | No |

REST is the primary interface: it filters tools deterministically and works everywhere. MCP lets
an MCP host use the router without extra code.

Registration is kept off MCP so that an agent cannot change which tools exist, and scanning a
local MCP server runs a command, which must come from the operator.

Code examples are in [integrations](integrations.md).

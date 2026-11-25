"""An MCP server that returns its tool list one tool per page."""

import asyncio
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

PAGES = 3
LOOP = "--loop" in sys.argv      # misbehave: keep pointing at the same page
server = Server("paged")


@server.list_tools()
async def list_tools(request: types.ListToolsRequest) -> types.ListToolsResult:
    cursor = request.params.cursor if request.params else None
    index = int(cursor or 0)
    tool = types.Tool(
        name=f"fetch_item_{index}",
        description=f"Fetch inventory item number {index} from the warehouse system",
        inputSchema={"type": "object", "properties": {}},
    )
    following = str(index + 1) if index + 1 < PAGES else None
    return types.ListToolsResult(tools=[tool], nextCursor="1" if LOOP else following)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


asyncio.run(main())

"""A Streamable HTTP MCP server for the importer tests.

    python http_shop.py PORT [--require 'Header: value' ...] [--redirect URL] [--hang]

The path ``/old`` always redirects to ``/mcp`` on the same server.

``--require`` makes the server answer 401 unless every given header matches.
"""

import asyncio
import sys

import uvicorn
from mcp.server.fastmcp import FastMCP

port = int(sys.argv[1])
options = sys.argv[2:]
required = {}
for i, value in enumerate(options):
    if value == "--require":
        name, _, expected = options[i + 1].partition(": ")
        required[name.lower().encode()] = expected.encode()
redirect = options[options.index("--redirect") + 1] if "--redirect" in options else None

server = FastMCP("Remote Shop")


@server.tool()
def getOrderStatus(orderId: str) -> str:
    """Get the shipping status of a customer order by its order id"""
    return f"order {orderId} has shipped"


@server.tool()
def deleteOrder(orderId: str) -> str:
    """Delete a customer order and all of its stored history"""
    return "deleted"


inner = server.streamable_http_app()


async def app(scope, receive, send):
    if scope["type"] == "http":
        if "--hang" in options:
            await asyncio.sleep(60)
        if redirect:
            await send({"type": "http.response.start", "status": 307,
                        "headers": [(b"location", redirect.encode())]})
            await send({"type": "http.response.body", "body": b""})
            return
        if scope["path"] == "/old":
            await send({"type": "http.response.start", "status": 307,
                        "headers": [(b"location", b"/mcp")]})
            await send({"type": "http.response.body", "body": b""})
            return
        headers = dict(scope["headers"])
        if any(headers.get(name) != expected for name, expected in required.items()):
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
    await inner(scope, receive, send)


uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical")

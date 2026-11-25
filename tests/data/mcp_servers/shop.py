"""A small MCP server used by the importer tests."""

import os
from enum import Enum

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("Shop Server 2")


class Priority(str, Enum):
    low = "low"
    high = "high"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def getOrderStatus(orderId: str) -> str:
    """Get the shipping status of a customer order by its order id"""
    return f"order {orderId} has shipped"


@server.tool(annotations=ToolAnnotations(destructiveHint=True))
def closeAccount(accountId: str) -> str:
    """Close a customer account and erase everything stored for it"""
    return "closed"


@server.tool()
def delete_draft(draftId: str) -> str:
    """Remove a saved draft order that was never submitted"""
    return "deleted"


@server.tool()
def create_ticket(title: str, priority: Priority) -> str:
    """Open a new support ticket with a title and a priority level"""
    return "ticket"


@server.tool()
def ping() -> str:
    """Ping"""
    return "pong"


@server.tool()
def get_long_report() -> str:
    return "report"


@server.tool(description="Show the value of the probe token from the environment " + "word " * 150)
def show_token() -> str:
    return os.environ.get("SHOP_TOKEN", "no token")


server.run()

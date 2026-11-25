"""Scanning real MCP servers (subprocesses over stdio) and loading the result."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.fastmcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import get_default_environment, stdio_client  # noqa: E402

from tool_router.catalog_loader import build_catalog  # noqa: E402
from tool_router.cli import main  # noqa: E402
from tool_router.domain.errors import CatalogError  # noqa: E402
from tool_router.importers.mcp_importer import (  # noqa: E402
    MCPImportError,
    cluster_name,
    import_from_mcp,
    snake_case,
)

SERVERS = Path(__file__).parent / "data" / "mcp_servers"
CATALOG = Path(__file__).parent / "data" / "catalog.json"
SHOP = str(SERVERS / "shop.py")
PAGED = str(SERVERS / "paged.py")


def command(*parts: str) -> str:
    return shlex.join([sys.executable, *parts])


@pytest.fixture(scope="module")
def shop() -> dict:
    tools = import_from_mcp(sys.executable, [SHOP])
    return {t["source"]["name"]: t for t in tools}


# ------------------------------------------------------------ conversion


def test_names_are_normalized_and_the_original_is_kept(shop):
    tool = shop["getOrderStatus"]
    assert tool["name"] == "get_order_status"
    assert tool["id"] == "shop_server.get_order_status"      # digits are not allowed in a cluster
    assert tool["source"]["name"] == "getOrderStatus"


def test_destructive_hint_is_carried_over(shop):
    assert shop["closeAccount"]["side_effects"] == "destructive"


def test_destructive_verb_is_detected_without_a_hint(shop):
    assert shop["delete_draft"]["side_effects"] == "destructive"


def test_read_hint_and_unlabelled_write(shop):
    assert shop["getOrderStatus"]["side_effects"] == "read"
    assert shop["create_ticket"]["side_effects"] == "write"


def test_input_schema_is_not_copied_but_parameters_are(shop):
    tool = shop["create_ticket"]
    assert "input_schema" not in tool["source"]
    assert [p["name"] for p in tool["parameters"]] == ["title", "priority"]


def test_long_description_is_shortened_but_kept_in_full(shop):
    tool = shop["show_token"]
    assert len(tool["description"]) <= 500 and tool["description"].endswith("…")
    assert len(tool["source"]["description"]) > 500


def test_name_helpers():
    assert snake_case("getHTTPStatus") == "get_http_status"
    assert snake_case("delete-user.account") == "delete_user_account"
    assert cluster_name("My Server 3!") == "my_server"
    assert cluster_name("123") == "mcp"


# ------------------------------------------------------------ connection


def test_every_page_is_read():
    tools = import_from_mcp(sys.executable, [PAGED])
    assert [t["name"] for t in tools] == ["fetch_item_0", "fetch_item_1", "fetch_item_2"]


def test_a_repeating_page_cursor_is_stopped():
    with pytest.raises(MCPImportError, match="repeated a page cursor"):
        import_from_mcp(sys.executable, [PAGED, "--loop"])


def test_hanging_server_times_out():
    started = time.monotonic()
    with pytest.raises(MCPImportError, match="within 2 s"):
        import_from_mcp(sys.executable, ["-c", "import time; time.sleep(60)"], timeout=2)
    assert time.monotonic() - started < 20


def test_server_that_exits_reports_a_readable_error():
    with pytest.raises(MCPImportError, match="could not list tools"):
        import_from_mcp(sys.executable, ["-c", "raise SystemExit(3)"], timeout=10)


def test_missing_program_reports_a_readable_error():
    with pytest.raises(MCPImportError, match="could not list tools"):
        import_from_mcp("definitely-not-a-real-program", timeout=10)


def test_only_named_environment_variables_are_forwarded(monkeypatch):
    """The scanned server must not inherit secrets it was not given."""
    monkeypatch.setenv("SHOP_TOKEN", "secret-123")
    code = (
        "import os\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "s = FastMCP('env')\n"
        "token = os.environ.get('SHOP_TOKEN', 'absent')\n"
        "s.tool(name='get_token', description='Report the token this server received ' + token)(lambda: token)\n"
        "s.run()\n"
    )
    without = import_from_mcp(sys.executable, ["-c", code])
    with_it = import_from_mcp(sys.executable, ["-c", code], env_names=["SHOP_TOKEN"])
    assert without[0]["description"].endswith("absent")
    assert with_it[0]["description"].endswith("secret-123")


def test_unset_environment_variable_is_an_error(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(MCPImportError, match="NOT_SET_ANYWHERE"):
        import_from_mcp(sys.executable, [SHOP], env_names=["NOT_SET_ANYWHERE"])


# --------------------------------------------------------- catalog loader


def test_scan_loads_only_valid_tools_and_reports_the_rest():
    catalog, report = build_catalog(mcp_sources=[command(SHOP)])
    names = {t["name"] for t in catalog["tools"]}
    assert "get_order_status" in names and "ping" not in names
    assert any("ping" in line and "DESCRIPTION_TOO_VAGUE" in line for line in report)


def test_file_and_scan_are_combined():
    catalog, _ = build_catalog(str(CATALOG), [command(SHOP)])
    ids = {t["id"] for t in catalog["tools"]}
    assert {"ops.get_server_status", "shop_server.get_order_status"} <= ids


def test_duplicate_server_does_not_duplicate_tools():
    once, _ = build_catalog(mcp_sources=[command(SHOP)])
    twice, report = build_catalog(mcp_sources=[command(SHOP), command(SHOP)])
    assert len(twice["tools"]) == len(once["tools"])
    assert any("DUPLICATE_ID" in line for line in report)


def test_no_usable_tools_is_an_error():
    code = ("from mcp.server.fastmcp import FastMCP\n"
            "s = FastMCP('empty')\ns.run()\n")
    with pytest.raises(CatalogError, match="No usable tools"):
        build_catalog(mcp_sources=[command("-c", code)])


def test_no_source_returns_nothing():
    assert build_catalog() == (None, [])


# -------------------------------------------------------------------- CLI


def test_import_command_writes_only_valid_tools(tmp_path, capsys):
    out = tmp_path / "tools.json"
    assert main(["import-mcp", "--command", sys.executable, "--args", SHOP, "-o", str(out)]) == 0
    written = {t["name"] for t in json.loads(out.read_text())["tools"]}
    assert "close_account" in written and "ping" not in written
    assert "IMPORT REJECT" in capsys.readouterr().err


def test_import_command_stdout_is_clean_json(capsys):
    assert main(["import-mcp", "--command", sys.executable, "--args", SHOP]) == 0
    assert json.loads(capsys.readouterr().out)["tools"]


def test_serve_exits_with_an_error_if_the_scan_fails(capsys):
    assert main(["serve", "--import-mcp", "definitely-not-a-real-program"]) == 1
    assert "could not list tools" in capsys.readouterr().err


def test_end_to_end_mcp_scan_route_then_call_the_real_tool(sandbox):
    """agent -> router (which scanned the shop at startup) -> back to the shop."""

    async def run() -> str:
        env = {**get_default_environment(), **{
            k: os.environ[k] for k in ("XDG_CONFIG_HOME", "HOME")}}
        router = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tool_router.cli", "mcp", "--import-mcp", command(SHOP)], env=env,
            cwd=str(Path.cwd()))
        async with stdio_client(router) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                reply = await session.call_tool(
                    "find_tools", {"query": "What is the shipping status of order 42?"})
                chosen = json.loads(reply.content[0].text)["selected_tools"][0]
        shop_server = StdioServerParameters(command=sys.executable, args=[SHOP])
        async with stdio_client(shop_server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(chosen["source"]["name"], {"orderId": "42"})
                return result.content[0].text

    assert asyncio.run(asyncio.wait_for(run(), timeout=60)) == "order 42 has shipped"


def test_end_to_end_rest_server_scans_at_startup(sandbox):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "tool_router.cli", "serve", "--host", "127.0.0.1",
         "--port", str(port), "--import-mcp", command(SHOP)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        body = json.dumps({"query": "What is the shipping status of order 42?"}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/route", data=body,
            headers={"content-type": "application/json"})
        deadline = time.monotonic() + 40
        while True:
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    routed = json.loads(response.read())
                break
            except OSError:
                assert server.poll() is None and time.monotonic() < deadline, "server did not start"
                time.sleep(0.3)
        assert routed["selected_tools"] == ["shop_server.get_order_status"]
        assert routed["selected_tool_schemas"][0]["source"]["name"] == "getOrderStatus"
    finally:
        server.terminate()
        server.wait(timeout=10)

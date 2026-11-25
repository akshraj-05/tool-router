"""Scanning remote MCP servers over Streamable HTTP (real servers on loopback)."""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.fastmcp")
pytest.importorskip("uvicorn")

from tool_router.catalog_loader import build_catalog  # noqa: E402
from tool_router.cli import main  # noqa: E402
from tool_router.importers.mcp_importer import import_from_mcp_url  # noqa: E402
from tool_router.importers.mcp_remote import (  # noqa: E402
    MCPImportError,
    build_headers,
    check_url,
    is_url,
    origin_guard,
    redact_url,
    same_server,
)

SERVER = str(Path(__file__).parent / "data" / "mcp_servers" / "http_shop.py")
LOCAL_SERVER = str(Path(__file__).parent / "data" / "mcp_servers" / "shop.py")
TOKEN = "tok-SECRET-12345"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def remote(*options: str):
    """Run the test server; yield its URL."""
    port = free_port()
    process = subprocess.Popen([sys.executable, SERVER, str(port), *options],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert process.poll() is None and time.monotonic() < deadline, "server did not start"
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.kill()               # a hanging request would delay a graceful stop
        process.wait(timeout=10)


@pytest.fixture(scope="module")
def open_server():
    with remote() as url:
        yield url


@pytest.fixture(scope="module")
def token_server():
    with remote("--require", f"Authorization: Bearer {TOKEN}") as url:
        yield url


# ---------------------------------------------------------------- success


def test_open_server_is_scanned_and_converted(open_server):
    tools = {t["name"]: t for t in import_from_mcp_url(open_server)}
    assert tools["get_order_status"]["id"] == "remote_shop.get_order_status"
    assert tools["get_order_status"]["source"]["name"] == "getOrderStatus"
    assert tools["delete_order"]["side_effects"] == "destructive"


def test_bearer_token_in_the_authorization_header(token_server, monkeypatch):
    monkeypatch.setenv("SHOP_AUTH", f"Bearer {TOKEN}")
    assert len(import_from_mcp_url(token_server, headers=build_headers(["Authorization=SHOP_AUTH"]))) == 2


def test_several_custom_headers(monkeypatch):
    monkeypatch.setenv("KEY", "key-abc")
    monkeypatch.setenv("ACCOUNT", "acct-9")
    with remote("--require", "X-API-Key: key-abc", "--require", "X-Account: acct-9") as url:
        headers = build_headers(["X-API-Key=KEY", "X-Account=ACCOUNT"])
        assert len(import_from_mcp_url(url, headers=headers)) == 2


def test_basic_authentication_as_a_header(monkeypatch):
    monkeypatch.setenv("BASIC", "Basic dXNlcjpwYXNz")
    with remote("--require", "Authorization: Basic dXNlcjpwYXNz") as url:
        assert import_from_mcp_url(url, headers=build_headers(["Authorization=BASIC"]))


def test_catalog_loader_combines_local_and_remote(open_server):
    import shlex

    local = shlex.join([sys.executable, LOCAL_SERVER])
    catalog, report = build_catalog(mcp_sources=[local, open_server])
    ids = {t["id"] for t in catalog["tools"]}
    assert {"shop_server.get_order_status", "remote_shop.get_order_status"} <= ids
    assert any(open_server in line for line in report)


def test_import_command_with_url(token_server, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHOP_AUTH", f"Bearer {TOKEN}")
    out = tmp_path / "tools.json"
    assert main(["import-mcp", "--url", token_server, "--header-env", "Authorization=SHOP_AUTH", "-o", str(out)]) == 0
    assert len(json.loads(out.read_text())["tools"]) == 2
    assert TOKEN not in out.read_text() + capsys.readouterr().err


def test_rest_server_loads_a_remote_catalog(token_server, monkeypatch, capsys):
    import uvicorn

    from tool_router import api

    seen = {}
    real = api.create_app
    monkeypatch.setenv("SHOP_AUTH", f"Bearer {TOKEN}")
    monkeypatch.setattr(api, "create_app", lambda catalog: seen.setdefault("catalog", catalog) and real(catalog))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    code = main(["serve", "--import-mcp", token_server, "--mcp-header-env", "Authorization=SHOP_AUTH"])
    output = capsys.readouterr()
    assert code == 0 and "2 of 2 tools accepted" in output.err
    assert TOKEN not in output.out + output.err
    assert TOKEN not in json.dumps(seen["catalog"])       # never stored with the tools


# ---------------------------------------------------------------- failures


def test_wrong_token_is_an_authentication_error_without_the_secret(token_server, monkeypatch, capfd):
    monkeypatch.setenv("SHOP_AUTH", "Bearer wrong-SECRET-999")
    with pytest.raises(MCPImportError, match="authentication failed") as caught:
        import_from_mcp_url(token_server, headers=build_headers(["Authorization=SHOP_AUTH"]), timeout=10)
    shown = str(caught.value) + "".join(capfd.readouterr())
    assert "wrong-SECRET-999" not in shown


def test_missing_token_says_which_option_to_use(token_server):
    with pytest.raises(MCPImportError, match="--mcp-header-env"):
        import_from_mcp_url(token_server, timeout=10)


def test_query_string_secret_is_never_printed(token_server, capfd):
    with pytest.raises(MCPImportError) as caught:
        import_from_mcp_url(token_server + "?api_key=QUERY-SECRET-1", timeout=10)
    shown = str(caught.value) + "".join(capfd.readouterr())
    assert "QUERY-SECRET-1" not in shown


def test_redirect_to_another_server_is_refused_and_credentials_are_not_forwarded(monkeypatch):
    received: list[bytes] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(8)

    def accept():
        with contextlib.suppress(OSError):
            connection, _ = listener.accept()
            received.append(connection.recv(4096))
            connection.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    monkeypatch.setenv("SHOP_AUTH", f"Bearer {TOKEN}")
    target = f"http://127.0.0.1:{listener.getsockname()[1]}/steal"
    try:
        with remote("--redirect", target) as url:
            with pytest.raises(MCPImportError, match="redirect"):
                import_from_mcp_url(url, headers=build_headers(["Authorization=SHOP_AUTH"]), timeout=5)
    finally:
        thread.join(timeout=10)
        listener.close()
    assert received == []                      # the second server was never contacted


def test_redirect_within_the_same_server_is_followed(token_server, monkeypatch):
    monkeypatch.setenv("SHOP_AUTH", f"Bearer {TOKEN}")
    moved = token_server.replace("/mcp", "/old")
    assert len(import_from_mcp_url(moved, headers=build_headers(["Authorization=SHOP_AUTH"]), timeout=10)) == 2


@pytest.mark.parametrize("endpoint, target, allowed", [
    ("https://a.example/mcp", "https://a.example/other", True),
    ("https://a.example/mcp", "https://A.example:443/x", True),
    ("http://a.example/mcp", "https://a.example/mcp", True),          # upgrade
    ("https://a.example/mcp", "http://a.example/mcp", False),         # downgrade
    ("https://a.example/mcp", "https://b.example/mcp", False),
    ("https://a.example/mcp", "https://a.example.evil.test/mcp", False),
    ("https://a.example/mcp", "https://a.example:8443/mcp", False),
    ("http://127.0.0.1:3000/mcp", "http://127.0.0.1:3001/mcp", False),
])
def test_same_server_rule(endpoint, target, allowed):
    assert same_server(endpoint, target) is allowed


def test_the_routers_own_guard_blocks_another_server_without_help_from_the_sdk():
    import asyncio

    import httpx

    sent: list[str] = []

    def respond(request):
        sent.append(str(request.url))
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            event_hooks={"request": [origin_guard("https://a.example/mcp")]},
        ) as client:
            await client.post("https://a.example/mcp")
            await client.post("https://b.example/steal")

    with pytest.raises(MCPImportError, match="blocked"):
        asyncio.run(run())
    assert sent == ["https://a.example/mcp"]


def test_silent_server_times_out():
    with remote("--hang") as url:
        started = time.monotonic()
        with pytest.raises(MCPImportError):
            import_from_mcp_url(url, timeout=2)
        assert time.monotonic() - started < 20


def test_unreachable_server_has_a_readable_error():
    with pytest.raises(MCPImportError, match="could not list tools|within"):
        import_from_mcp_url(f"http://127.0.0.1:{free_port()}/mcp", timeout=5)


def test_wrong_path_reports_the_status(open_server):
    with pytest.raises(MCPImportError, match="HTTP 404"):
        import_from_mcp_url(open_server.replace("/mcp", "/nope"), timeout=10)


def test_a_failing_server_stops_startup(capsys):
    assert main(["serve", "--import-mcp", f"http://127.0.0.1:{free_port()}/mcp",
                 "--mcp-timeout", "5"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err


@pytest.mark.parametrize("argv", [
    ["serve", "--mcp-token-env", "X"],
    ["mcp", "--mcp-token-env", "X"],
    ["import-mcp", "--url", "https://a.example/mcp", "--token-env", "X"],
])
def test_the_token_shortcut_no_longer_exists(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err


# -------------------------------------------------------------- the rules


@pytest.mark.parametrize("url", [
    "https://example.com/mcp", "http://localhost:3000/mcp",
    "http://127.0.0.1/mcp", "http://[::1]:8000/mcp",
])
def test_acceptable_urls(url):
    assert check_url(url) == url


@pytest.mark.parametrize("url, message", [
    ("http://example.com/mcp", "plain http"),
    ("http://127.0.0.1.example.com/mcp", "plain http"),
    ("ftp://example.com/mcp", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("https:///mcp", "no host"),
    ("https://user:pw@example.com/mcp", "user name or password"),
    ("https://example.com:99999/mcp", "not a valid URL"),
])
def test_refused_urls(url, message):
    with pytest.raises(MCPImportError, match=message):
        check_url(url)


def test_url_detection_and_redaction():
    assert is_url("https://a/mcp") and not is_url("python my_server.py")
    assert not is_url("node server.js --endpoint=https://x")
    assert redact_url("https://u:p@host:8/mcp?key=s#f") == "https://host:8/mcp?…"


@pytest.mark.parametrize("headers, message", [
    (["X-Key=UNSET_VARIABLE"], "not set or empty"),
    (["X-Key"], "Header-Name=ENV_VARIABLE"),
    (["X-Key=sk-live-abc123"], "Header-Name=ENV_VARIABLE"),
    (["Bad Header=GOOD"], "Header-Name=ENV_VARIABLE"),
    (["Content-Type=GOOD"], "managed by the protocol"),
    (["Authorization=GOOD", "authorization=GOOD"], "more than once"),
    (["X-Key=MULTILINE"], "cannot carry"),
])
def test_bad_header_options(headers, message, monkeypatch):
    monkeypatch.setenv("GOOD", "value")
    monkeypatch.setenv("MULTILINE", "a\r\nInjected: 1")
    monkeypatch.delenv("UNSET_VARIABLE", raising=False)
    with pytest.raises(MCPImportError, match=message) as caught:
        build_headers(headers)
    assert "sk-live-abc123" not in str(caught.value)


@pytest.mark.parametrize("options, message", [
    ({"mcp_sources": ["https://a.example/mcp", "https://b.example/mcp"], "header_envs": ["X-K=T"]}, "one remote server"),
    ({"mcp_sources": ["python x.py"], "header_envs": ["X-K=T"]}, "needs a remote server"),
    ({"header_envs": ["X-K=T"]}, "needs a remote server"),
    ({"mcp_sources": ["https://a.example/mcp"], "env_names": ["T"]}, "local servers"),
    ({"mcp_sources": ["python x.py", "http://a.example/mcp"]}, "plain http"),
])
def test_options_are_checked_before_connecting(options, message, monkeypatch):
    monkeypatch.setenv("T", "value")
    with pytest.raises(MCPImportError, match=message):
        build_catalog(**options)


@pytest.mark.parametrize("argv", [
    ["import-mcp", "--url", "https://a.example/mcp", "--env", "X"],
    ["import-mcp", "--command", "python", "--header-env", "X-K=X"],
])
def test_import_command_rejects_mixed_options(argv, capsys):
    assert main(argv) == 1
    assert "--url" in capsys.readouterr().err

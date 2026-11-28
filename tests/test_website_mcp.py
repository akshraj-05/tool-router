"""Scanning an MCP server from the website (real servers on loopback)."""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("mcp.server.fastmcp")
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from test_mcp_remote import TOKEN, free_port, remote  # noqa: E402

from tool_router.api import create_app  # noqa: E402
from tool_router.importers.mcp_remote import MCPImportError, auth_header  # noqa: E402


def make(**options) -> TestClient:
    return TestClient(create_app(**{"mcp_scan": True, **options}), base_url="http://127.0.0.1:8080")


@pytest.fixture(scope="module")
def open_server():
    with remote() as url:
        yield url


@pytest.fixture(scope="module")
def token_server():
    with remote("--require", f"Authorization: Bearer {TOKEN}") as url:
        yield url


def test_scan_then_add(open_server):
    client = make()
    found = client.post("/mcp/scan", json={"url": open_server, "prefix": "shop"}).json()

    assert found["server"] == open_server and found["tools"]
    assert found["found"] == len(found["tools"]) + len(found["rejected"])
    assert all(t["id"].startswith("shop.") for t in found["tools"])
    assert client.get("/health").json()["tool_count"] == 0          # scanning adds nothing

    done = client.post("/registry/apply", json={"tools": found["tools"]}).json()
    assert done["rejected"] == [] and done["tool_count_after"] == len(found["tools"])


@pytest.mark.parametrize("token", [TOKEN, f"Bearer {TOKEN}", f"  {TOKEN}  "])
def test_the_token_is_sent(token_server, token):
    reply = make().post("/mcp/scan", json={"url": token_server, "token": token})
    assert reply.status_code == 200 and reply.json()["tools"]


@pytest.mark.parametrize("token", [None, "", "wrong-SECRET-999"])
def test_a_refused_scan_explains_without_the_secret(token_server, token, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    reply = make().post("/mcp/scan", json={"url": token_server, "token": token})

    assert reply.status_code == 502 and "401" in reply.json()["detail"]
    for text in (reply.text, caplog.text, *capfd.readouterr()):
        assert "wrong-SECRET" not in text


def test_the_token_is_not_logged_or_saved(token_server, caplog, capfd, tmp_path):
    from tool_router.state import StateStore
    caplog.set_level(logging.DEBUG)
    client = make(store=StateStore(tmp_path / "state"))
    found = client.post("/mcp/scan", json={"url": token_server, "token": TOKEN}).json()
    client.post("/registry/apply", json={"tools": found["tools"]})

    for text in (caplog.text, *capfd.readouterr()):
        assert TOKEN not in text
    for path in (tmp_path / "state").iterdir():
        assert TOKEN.encode() not in path.read_bytes()


def test_a_secret_in_the_url_is_not_shown(open_server):
    reply = make().post("/mcp/scan", json={"url": f"{open_server}?key=URL-SECRET-1"})
    assert "URL-SECRET" not in reply.text


@pytest.mark.parametrize("body, message", [
    ({"url": "python my_server.py"}, "scheme"),
    ({"url": "file:///etc/passwd"}, "scheme"),
    ({"url": "http://example.com/mcp"}, "https"),
    ({"url": "https://user:pw@example.com/mcp"}, "user name or password"),
    ({"url": "http://127.0.0.1:1/mcp", "token": "a\r\nb"}, "token"),
    ({"url": "http://127.0.0.1:1/mcp", "token": "abc", "header": "Host"}, "managed by"),
    ({"url": "http://127.0.0.1:1/mcp", "token": "abc", "header": "bad header"}, "header name"),
])
def test_what_cannot_be_scanned(body, message):
    reply = make().post("/mcp/scan", json=body)
    assert reply.status_code == 502 and message in reply.json()["detail"]


def test_an_unreachable_server_and_the_next_scan(open_server):
    client = make()
    reply = client.post("/mcp/scan", json={"url": f"http://127.0.0.1:{free_port()}/mcp"})
    assert reply.status_code == 502
    assert client.post("/mcp/scan", json={"url": open_server}).status_code == 200


def test_scanning_can_be_off(open_server):
    reply = make(mcp_scan=False).post("/mcp/scan", json={"url": open_server})
    assert reply.status_code == 403 and "--allow-mcp-scan" in reply.json()["detail"]


@pytest.mark.parametrize("name, token, sent", [
    ("Authorization", "abc", "Bearer abc"),
    ("authorization", "Basic abc==", "Basic abc=="),
    ("X-Api-Key", "abc", "abc"),
])
def test_how_a_token_becomes_a_header(name, token, sent):
    assert auth_header(name, token) == {name: sent}


def test_header_errors_never_repeat_the_token():
    with pytest.raises(MCPImportError) as error:
        auth_header("Authorization", "SECRETé")
    assert "SECRET" not in str(error.value)

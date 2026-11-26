"""Local by default: the bind address, the warning and the Host header check."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
from starlette.testclient import TestClient  # noqa: E402

from tool_router.api import create_app  # noqa: E402
from tool_router.cli import build_parser, main  # noqa: E402
from tool_router.netutil import host_name, is_loopback  # noqa: E402

CATALOG = Path(__file__).parent / "data" / "catalog.json"
WARNING = "reachable from other machines"
QUERY = {"query": "Check the health status of the production server"}


@pytest.fixture
def serve(monkeypatch, capsys):
    """Run `serve` without opening a port; return (bound host, app, stderr)."""
    def run(*args):
        seen: dict = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
        assert main(["serve", "--no-persist", *args]) == 0
        return seen["host"], seen["app"], capsys.readouterr().err
    return run


def status(app, host: str, path: str = "/health") -> int:
    return TestClient(app, base_url="http://localhost").get(path, headers={"host": host}).status_code


def test_default_bind_is_this_machine_only(serve):
    assert build_parser().parse_args(["serve"]).host == "127.0.0.1"
    host, _, err = serve()
    assert host == "127.0.0.1" and WARNING not in err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_no_warning_for_local_addresses(serve, host):
    assert WARNING not in serve("--host", host)[2]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
def test_warning_for_network_addresses(serve, host):
    bound, _, err = serve("--host", host)
    assert bound == host and WARNING in err and "NO authentication" in err


@pytest.mark.parametrize("header", [
    "localhost", "localhost:8080", "LOCALHOST:8080", "localhost.", "127.0.0.1",
    "127.0.0.1:9", "[::1]", "[::1]:8080",
])
def test_local_server_accepts_local_names(serve, header):
    assert status(serve()[1], header) == 200


@pytest.mark.parametrize("header", [
    "evil.example", "evil.example:8080", "127.0.0.1.evil.example", "localhost.evil.example",
    "localhost@evil.example", "192.168.1.20", "0.0.0.0", "[::1]evil.example",
    "localhost:80:evil", "localhost:abc", "",
])
def test_local_server_refuses_other_names(serve, header):
    assert status(serve()[1], header) == 400


def test_refusal_covers_every_endpoint_and_changes_nothing(serve):
    _, app, _ = serve("--catalog", str(CATALOG))
    client = TestClient(app, base_url="http://localhost")
    evil = {"host": "evil.example"}
    before = client.get("/registry/tools").json()
    for method, path in [("GET", "/registry/tools"), ("DELETE", "/registry/tools/hr.get_employee"),
                         ("POST", "/route"), ("POST", "/setup"), ("GET", "/docs"),
                         ("GET", "/openapi.json"), ("GET", "/no-such-path")]:
        response = client.request(method, path, headers=evil, json={})
        assert response.status_code == 400 and "--allow-host" in response.json()["detail"], path
    assert client.get("/registry/tools").json() == before
    assert client.post("/route", json=QUERY).status_code == 200


def test_an_allowed_name_is_accepted_alongside_localhost(serve):
    _, app, _ = serve("--allow-host", "Router.Internal", "--allow-host", "other.test:99")
    for header in ("router.internal", "router.internal:443", "other.test", "localhost"):
        assert status(app, header) == 200
    assert status(app, "evil.example") == 400


def test_network_bind_does_not_check_the_host_unless_names_are_listed(serve):
    assert status(serve("--host", "0.0.0.0")[1], "any.name.example") == 200
    _, app, _ = serve("--host", "0.0.0.0", "--allow-host", "router.internal")
    assert status(app, "router.internal") == 200 and status(app, "any.name.example") == 400


def test_app_factory_without_a_list_checks_nothing(catalog):
    assert status(create_app(catalog), "any.name.example") == 200


@pytest.mark.parametrize("value, expected", [
    ("127.0.0.1", True), ("127.8.8.8", True), ("::1", True), ("localhost", True),
    ("::ffff:127.0.0.1", True), ("0.0.0.0", False), ("::", False), ("10.0.0.5", False), ("", False),
])
def test_loopback_detection(value, expected):
    assert is_loopback(value) is expected


def test_host_name_parsing():
    assert host_name(" Example.COM:80 ") == "example.com" and host_name("[::1]:1") == "::1"
    assert host_name("[::1") == "" and host_name("a:b") == ""

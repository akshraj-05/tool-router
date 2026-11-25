"""REST API: lifecycle and the /setup endpoint."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient  # noqa: E402

from tool_router.api import create_app  # noqa: E402
from tool_router.store import load_secret, save_stored_config  # noqa: E402

QUERY = {"query": "Check the health status of the production server"}
LOOPBACK = ("127.0.0.1", 50000)


@pytest.fixture
def client(catalog):
    return TestClient(create_app(catalog), client=LOOPBACK)


# --------------------------------------------------------------- lifecycle


def test_route_before_any_registry_is_a_conflict():
    client = TestClient(create_app(None), client=LOOPBACK)
    assert client.post("/route", json=QUERY).status_code == 409


def test_register_then_route(catalog):
    client = TestClient(create_app(None), client=LOOPBACK)
    body = client.post("/registry", json=catalog).json()
    assert body["status"] == "indexed" and body["tool_count"] == len(catalog["tools"])
    routed = client.post("/route", json=QUERY).json()
    assert routed["selected_tools"] == ["ops.get_server_status"]
    assert routed["selected_tool_schemas"][0]["id"] == "ops.get_server_status"
    assert routed["trace"]["decision"] == "proceed"


def test_bad_catalogs_are_rejected(client):
    assert client.post("/registry", json={"tools": []}).status_code == 422
    assert client.post("/route", json={"query": ""}).status_code == 422


def test_mutation_endpoint(client):
    tool = {
        "id": "weather.get_current_weather", "name": "get_current_weather", "cluster": "weather",
        "description": "Get the current weather conditions for a named city",
        "side_effects": "read", "parameters": [{"name": "city", "type": "string"}],
        "returns": {"type": "object"},
    }
    out = client.patch("/registry/mutate", json={"add": [tool], "remove": ["hr.get_employee"]}).json()
    assert out["added"] == [tool["id"]] and out["removed"] == ["hr.get_employee"]
    routed = client.post("/route", json={"query": "What is the current weather in Berlin?"}).json()
    assert tool["id"] in routed["selected_tools"]


def test_health_reports_state(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["registry_loaded"] is True
    assert body["planner"] == "LocalPlanner"


# ------------------------------------------------------------------- setup


def test_setup_saves_a_key_for_the_stored_provider(client):
    """Regression: posting only a key used to raise NameError."""
    save_stored_config({"llm_provider": "gemini"})
    body = client.post("/setup", json={"api_key": "k-123"}).json()
    assert body["status"] == "saved" and body["key_stored"] in ("keyring", "file(0600)")
    assert load_secret("gemini_key") == "k-123"


def test_setup_saves_provider_model_and_key_together(client):
    body = client.post("/setup", json={
        "llm_provider": "gemini", "llm_model": "gemini-x", "api_key": "k-9"}).json()
    assert body["config"] == {"llm_provider": "gemini", "llm_model": "gemini-x"}
    assert load_secret("gemini_key") == "k-9"


def test_setup_rejects_a_key_for_a_keyless_provider(client):
    assert client.post("/setup", json={"llm_provider": "none", "api_key": "k"}).status_code == 400


def test_setup_ignores_unknown_fields(client):
    out = client.post("/setup", json={"llm_provider": "none", "evil": "x"}).json()
    assert out["config"] == {"llm_provider": "none"}


def test_setup_page_never_echoes_the_key_and_escapes_stored_values(client):
    client.post("/setup", json={"llm_provider": "gemini", "api_key": "topsecret",
                                "llm_model": '"><script>alert(1)</script>'})
    page = client.get("/setup").text
    assert "topsecret" not in page
    assert '"><script>alert(1)' not in page
    assert "&quot;&gt;&lt;script&gt;alert(1)" in page

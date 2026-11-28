"""Optional identity fields, and create / read / update / delete."""

from __future__ import annotations

import copy

import pytest

from toolspotter.config import Settings
from toolspotter.domain.errors import (
    CatalogError,
    DuplicateToolError,
    InvalidToolError,
    ToolNotFoundError,
)
from toolspotter.domain.naming import build_id, prepare_tool
from toolspotter.engine.pipeline import RoutingEngine

# no id, no cluster: both are optional
MINIMAL = {
    "name": "get_current_weather",
    "description": "Get the current weather conditions for a named city",
    "parameters": [{"name": "city", "type": "string"}],
}
WEATHER_ID = "general.get_current_weather"
EMPLOYEE = "hr.get_employee"


# ------------------------------------------------------------ id and cluster


def test_missing_id_and_cluster_are_filled_in():
    tool = prepare_tool(MINIMAL)
    assert tool["cluster"] == "general" and tool["id"] == WEATHER_ID
    assert "id" not in MINIMAL                     # the caller's dict is not modified


def test_provided_values_are_never_changed():
    tool = prepare_tool({**MINIMAL, "id": "mine.custom_id", "cluster": "weather"})
    assert (tool["id"], tool["cluster"]) == ("mine.custom_id", "weather")


def test_id_is_built_from_the_declared_cluster():
    assert prepare_tool({**MINIMAL, "cluster": "Weather APIs 2"})["id"] == "weather_apis.get_current_weather"
    assert build_id("123", "x") == "general.x"


def test_catalog_without_ids_or_clusters_loads_and_routes():
    engine = RoutingEngine({"tools": [MINIMAL]}, settings=Settings())
    assert engine.route("What is the weather in Berlin city?").selected_tools == [WEATHER_ID]


def test_two_tools_that_would_share_an_id_are_refused():
    with pytest.raises(CatalogError, match="DUPLICATE_ID"):
        RoutingEngine({"tools": [MINIMAL, dict(MINIMAL)]}, settings=Settings())


def test_bad_name_is_reported_as_a_name_problem(engine):
    with pytest.raises(InvalidToolError) as caught:
        engine.create_tool({**MINIMAL, "name": "Get Weather"})
    assert caught.value.reason == "BAD_NAME_FORMAT"


def test_removed_fields_are_ignored_not_rejected(engine):
    stored = engine.create_tool({**MINIMAL, "returns": {"type": "object"}, "aliases": ["x"]})
    assert stored["id"] == WEATHER_ID


# ---------------------------------------------------------------------- CRUD


def test_create_read_update_delete(engine):
    count = engine.tool_count
    assert engine.create_tool(MINIMAL)["id"] == WEATHER_ID
    assert engine.get_tool(WEATHER_ID)["name"] == "get_current_weather"
    assert len(engine.list_tools()) == count + 1

    changed = {**MINIMAL, "description": "Report today's temperature and rainfall for a city"}
    assert engine.update_tool(WEATHER_ID, changed)["id"] == WEATHER_ID
    assert "rainfall" in engine.get_tool(WEATHER_ID)["description"]
    assert engine.tool_count == count + 1

    engine.delete_tool(WEATHER_ID)
    assert engine.tool_count == count
    with pytest.raises(ToolNotFoundError):
        engine.get_tool(WEATHER_ID)


def test_create_refuses_an_existing_id(engine):
    engine.create_tool(MINIMAL)
    with pytest.raises(DuplicateToolError):
        engine.create_tool(MINIMAL)


def test_rejected_update_keeps_the_old_tool(engine):
    before = copy.deepcopy(engine.get_tool(EMPLOYEE))
    with pytest.raises(InvalidToolError):
        engine.update_tool(EMPLOYEE, {**before, "description": "too short"})
    assert engine.get_tool(EMPLOYEE) == before


def test_update_keeps_the_cluster_when_omitted_and_cannot_rename(engine):
    body = {k: v for k, v in engine.get_tool(EMPLOYEE).items() if k not in ("id", "cluster")}
    assert engine.update_tool(EMPLOYEE, body)["cluster"] == "hr"
    with pytest.raises(CatalogError, match="does not match"):
        engine.update_tool(EMPLOYEE, {**body, "id": "hr.someone_else"})


def test_unknown_ids_raise_not_found(engine):
    for action in (lambda: engine.update_tool("no.such", MINIMAL), lambda: engine.delete_tool("no.such")):
        with pytest.raises(ToolNotFoundError):
            action()


def test_last_tool_cannot_be_deleted():
    engine = RoutingEngine({"tools": [MINIMAL]}, settings=Settings())
    with pytest.raises(CatalogError, match="last tool"):
        engine.delete_tool(WEATHER_ID)
    assert engine.tool_count == 1


# ------------------------------------------------------- replace via mutation


def test_failed_replacement_in_a_mutation_keeps_the_old_tool(engine):
    """Regression: a rejected replacement used to delete the tool outright."""
    before = engine.get_tool(EMPLOYEE)
    bad = {**before, "description": "too short"}
    out = engine.apply_mutation({"remove": [EMPLOYEE], "add": [bad]})
    assert out["kept_previous"] == [EMPLOYEE] and out["removed"] == [] and out["added"] == []
    assert engine.get_tool(EMPLOYEE) == before


def test_successful_replacement_in_a_mutation(engine):
    new = {**engine.get_tool(EMPLOYEE), "description": "Look up one employee profile using their id"}
    out = engine.apply_mutation({"remove": [EMPLOYEE], "add": [new]})
    assert out["removed"] == [EMPLOYEE] and out["added"] == [EMPLOYEE] and out["kept_previous"] == []
    assert "profile" in engine.get_tool(EMPLOYEE)["description"]


# ----------------------------------------------------------------------- REST


@pytest.fixture
def client(catalog):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from toolspotter.api import create_app

    return TestClient(create_app(catalog))


def test_rest_crud_round_trip(client):
    total = client.get("/registry/tools").json()["tool_count"]
    created = client.post("/registry/tools", json=MINIMAL)
    assert created.status_code == 201 and created.json()["id"] == WEATHER_ID
    assert client.get(f"/registry/tools/{WEATHER_ID}").json()["cluster"] == "general"

    changed = {**MINIMAL, "description": "Report today's temperature and rainfall for a city"}
    assert client.put(f"/registry/tools/{WEATHER_ID}", json=changed).status_code == 200
    routed = client.post("/route", json={"query": "temperature and rainfall for a city"}).json()
    assert WEATHER_ID in routed["selected_tools"]

    deleted = client.delete(f"/registry/tools/{WEATHER_ID}")
    assert deleted.json() == {"deleted": WEATHER_ID, "tool_count": total}
    assert client.get(f"/registry/tools/{WEATHER_ID}").status_code == 404


def test_rest_error_codes(client):
    assert client.post("/registry/tools", json=client.get(f"/registry/tools/{EMPLOYEE}").json()).status_code == 409
    bad = client.post("/registry/tools", json={**MINIMAL, "description": "short"})
    assert bad.status_code == 422 and bad.json()["detail"]["reason"] == "DESCRIPTION_TOO_VAGUE"
    assert client.put("/registry/tools/no.such", json=MINIMAL).status_code == 404
    assert client.delete("/registry/tools/no.such").status_code == 404
    assert client.put(f"/registry/tools/{EMPLOYEE}", json={**MINIMAL, "id": "a.b"}).status_code == 400


def test_rest_crud_needs_a_registry():
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from toolspotter.api import create_app

    empty = TestClient(create_app(None))
    assert empty.get("/registry/tools").status_code == 409
    assert empty.post("/registry/tools", json=MINIMAL).status_code == 409

"""The same strict rules apply to a catalog file, a catalog upload and single tools."""

from __future__ import annotations

import json

import pytest

from toolspotter.cli import main
from toolspotter.config import Settings
from toolspotter.domain.errors import CatalogError, InvalidCatalogError, ToolInUseError
from toolspotter.engine.guardrail import validate_submission
from toolspotter.engine.pipeline import RoutingEngine

GOOD = {
    "name": "get_weather",
    "description": "Get the current weather conditions for a named city",
    "parameters": [{"name": "city"}],
}


def tool(**changes) -> dict:
    return {**GOOD, **changes}


def without(field: str) -> dict:
    return {k: v for k, v in GOOD.items() if k != field}


# (submitted tool, expected reason code) — every entry is invalid
BAD = {
    "not an object": ("oops", "MALFORMED_JSON_STRUCTURE"),
    "no name": (without("name"), "MISSING_REQUIRED_FIELD"),
    "no description": (without("description"), "MISSING_REQUIRED_FIELD"),
    "no parameters": (without("parameters"), "MISSING_REQUIRED_FIELD"),
    "misspelled field": (tool(side_effect="destructive"), "UNKNOWN_FIELD"),
    "name is a number": (tool(name=5), "INVALID_FIELD_TYPE"),
    "description is a number": (tool(description=5), "INVALID_FIELD_TYPE"),
    "id is a number": (tool(id=5), "INVALID_FIELD_TYPE"),
    "cluster is a list": (tool(cluster=["a"]), "INVALID_FIELD_TYPE"),
    "side_effects is a list": (tool(side_effects=["read"]), "INVALID_FIELD_TYPE"),
    "deprecated is text": (tool(deprecated="yes"), "INVALID_FIELD_TYPE"),
    "tags is text": (tool(tags="weather"), "INVALID_FIELD_TYPE"),
    "tag is a number": (tool(tags=[1]), "INVALID_FIELD_TYPE"),
    "version is a number": (tool(version=2), "INVALID_FIELD_TYPE"),
    "parameters is text": (tool(parameters="city"), "INVALID_FIELD_TYPE"),
    "parameter is text": (tool(parameters=["city"]), "INVALID_PARAMETER"),
    "parameter without name": (tool(parameters=[{"type": "string"}]), "INVALID_PARAMETER"),
    "repeated parameter": (tool(parameters=[{"name": "a"}, {"name": "a"}]), "INVALID_PARAMETER"),
    "parameter typo": (tool(parameters=[{"name": "a", "requred": True}]), "INVALID_PARAMETER"),
    "required is text": (tool(parameters=[{"name": "a", "required": "yes"}]), "INVALID_PARAMETER"),
    "bad parameter type": (tool(parameters=[{"name": "a", "type": "blob"}]), "INVALID_PARAM_TYPE"),
    "too many parameters": (tool(parameters=[{"name": f"p{i}"} for i in range(13)]), "TOO_MANY_PARAMETERS"),
    "bad name": (tool(name="Get Weather"), "BAD_NAME_FORMAT"),
    "name too long": (tool(name="a" * 129), "BAD_ID_FORMAT"),
    "bad id": (tool(id="Bad Id"), "BAD_ID_FORMAT"),
    "short description": (tool(description="weather"), "DESCRIPTION_TOO_VAGUE"),
    "long description": (tool(description="word " * 200), "DESCRIPTION_TOO_LONG"),
    "bad version": (tool(version="v2"), "BAD_VERSION_FORMAT"),
    "bad side_effects": (tool(side_effects="Delete"), "INVALID_SIDE_EFFECTS"),
    "successor missing": (tool(replaced_by="a.none"), "DANGLING_REPLACED_BY"),
    "points at itself": (tool(id="a.x", replaced_by="a.x"), "CIRCULAR_VERSION_REF"),
}


def build(*tools, **meta) -> RoutingEngine:
    return RoutingEngine({"tools": list(tools), **meta}, settings=Settings())


@pytest.mark.parametrize("case", BAD, ids=list(BAD))
def test_catalog_load_refuses_it(case):
    submitted, code = BAD[case]
    with pytest.raises(InvalidCatalogError) as caught:
        build(submitted)
    assert caught.value.rejected[0]["reason"] == code


@pytest.mark.parametrize("case", BAD, ids=list(BAD))
def test_single_tool_create_refuses_it(case, engine):
    submitted, code = BAD[case]
    before = engine.tool_count
    out = engine.apply_mutation({"add": [submitted]})
    assert out["added"] == [] and out["rejected"][0]["reason"] == code
    assert engine.tool_count == before


def test_every_invalid_tool_is_listed_at_once():
    with pytest.raises(InvalidCatalogError) as caught:
        build(GOOD, tool(name="two", description="x"), tool(name="Three"))
    assert [r["index"] for r in caught.value.rejected] == [1, 2]


def test_legacy_fields_are_still_accepted():
    assert build(tool(returns={}, aliases=[], replaces="a.b")).tool_count == 1


def test_successor_may_appear_later_in_the_catalog_or_batch(engine):
    old = tool(name="old_report", replaced_by="general.new_report")
    new = tool(name="new_report")
    assert build(old, new).tool_count == 2
    out = engine.apply_mutation({"add": [old, new]})
    assert sorted(out["added"]) == ["general.new_report", "general.old_report"]


def test_two_new_tools_pointing_at_each_other_are_refused():
    with pytest.raises(InvalidCatalogError) as caught:
        build(tool(name="a", replaced_by="general.b"), tool(name="b", replaced_by="general.a"))
    assert {r["reason"] for r in caught.value.rejected} == {"CIRCULAR_VERSION_REF"}


def test_a_referenced_successor_cannot_be_deleted():
    engine = build(tool(name="old", replaced_by="general.new"), tool(name="new"))
    with pytest.raises(ToolInUseError):
        engine.delete_tool("general.new")
    out = engine.apply_mutation({"remove": ["general.new"]})
    assert out["removed"] == [] and out["rejected"][0]["reason"] == "REFERENCED_BY_OTHER_TOOL"
    assert engine.tool_count == 2
    out = engine.apply_mutation({"remove": ["general.new", "general.old"], "add": [tool(name="third")]})
    assert out["removed"] == ["general.new", "general.old"]


@pytest.mark.parametrize("groups", ["a", ["a"], [[1]], [["general.nope"]]])
def test_near_duplicate_groups_are_validated(groups):
    with pytest.raises(CatalogError, match="near_duplicate_groups"):
        build(GOOD, near_duplicate_groups=groups)


def test_tool_count_limit(engine):
    small = Settings(max_tools=2)
    with pytest.raises(CatalogError, match="limit of 2"):
        RoutingEngine({"tools": [tool(name=f"t{i}") for i in range(3)]}, settings=small)
    full = RoutingEngine({"tools": [tool(name="t1"), tool(name="t2")]}, settings=small)
    with pytest.raises(CatalogError, match="limit of 2"):
        full.create_tool(tool(name="t3"))
    with pytest.raises(CatalogError, match="limit of 2"):
        full.apply_mutation({"add": [tool(name="t3")]})
    assert full.tool_count == 2


def test_self_check_of_the_sample_catalog(catalog):
    assert all(validate_submission(t, known_ids=set(), reference_ids={x["id"] for x in catalog["tools"]}).accepted
               for t in catalog["tools"])


# ------------------------------------------------------------- catalog file


def serve(path, capsys, monkeypatch) -> tuple[int, str]:
    uvicorn = pytest.importorskip("uvicorn")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    code = main(["serve", "--catalog", str(path)])
    return code, capsys.readouterr().err


@pytest.mark.parametrize("content, message", [
    (b"{not json", "not valid UTF-8 JSON"),
    (b"\xff\xfe\x00", "not valid UTF-8 JSON"),
    (b"[]", "'tools' list"),
    (b'{"tools": "x"}', "'tools' list"),
    (b'{"tools": []}', "no usable tools"),
    (b'{"tools": ["oops"]}', "MALFORMED_JSON_STRUCTURE"),
    (b'{"tools": [{"name": "x"}]}', "MISSING_REQUIRED_FIELD"),
])
def test_bad_catalog_file_stops_startup_with_a_message(tmp_path, capsys, monkeypatch, content, message):
    path = tmp_path / "c.json"
    path.write_bytes(content)
    code, err = serve(path, capsys, monkeypatch)
    assert code == 1 and message.lower() in err.lower() and "Traceback" not in err


def test_missing_file_and_oversized_file(tmp_path, capsys, monkeypatch):
    assert serve(tmp_path / "nope.json", capsys, monkeypatch)[0] == 1
    path = tmp_path / "big.json"
    path.write_text(json.dumps({"tools": [GOOD]}))
    monkeypatch.setenv("TOOLSPOTTER_MAX_BYTES", "20")
    code, err = serve(path, capsys, monkeypatch)
    assert code == 1 and "larger than 20 bytes" in err


def test_valid_file_with_byte_order_mark_loads(tmp_path, capsys, monkeypatch):
    path = tmp_path / "ok.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"tools": [GOOD]}).encode())
    assert serve(path, capsys, monkeypatch)[0] == 0


# --------------------------------------------------------------------- REST


@pytest.fixture
def client(catalog):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from toolspotter.api import create_app

    return TestClient(create_app(catalog))


@pytest.mark.parametrize("case", BAD, ids=list(BAD))
def test_upload_and_create_agree(case, client):
    submitted, code = BAD[case]
    upload = client.post("/registry", json={"tools": [submitted]})
    create = client.post("/registry/tools", json=submitted)
    assert upload.status_code == 422 and create.status_code == 422
    assert upload.json()["detail"]["rejected"][0]["reason"] == code
    if isinstance(submitted, dict):          # the single-tool endpoint only takes an object
        assert create.json()["detail"]["reason"] == code


def test_a_refused_upload_keeps_the_current_registry(client):
    before = client.get("/registry/tools").json()
    assert client.post("/registry", json={"tools": [tool(name="Bad Name")]}).status_code == 422
    assert client.get("/registry/tools").json() == before


def test_json_inside_a_string_is_not_accepted_as_a_tool(client):
    text = json.dumps(tool(name="sneaky_tool"))
    upload = client.post("/registry", json={"tools": [text]})
    mutate = client.patch("/registry/mutate", json={"add": [text]})
    assert upload.status_code == 422 and mutate.json()["added"] == []


def test_upload_lists_every_problem_together(client):
    response = client.post("/registry", json={"tools": [GOOD, "oops", tool(name="Bad Name")]})
    assert [r["index"] for r in response.json()["detail"]["rejected"]] == [1, 2]


def test_update_is_validated_too(client):
    response = client.put("/registry/tools/hr.get_employee", json=tool(colour="red"))
    assert response.status_code == 422 and response.json()["detail"]["reason"] == "UNKNOWN_FIELD"


def test_oversized_request_is_refused(catalog, monkeypatch):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from toolspotter.api import create_app

    monkeypatch.setenv("TOOLSPOTTER_MAX_BYTES", "500")
    small = TestClient(create_app(catalog))
    big = {"tools": [tool(description="word " * 300)]}
    assert small.post("/registry", json=big).status_code == 413
    streamed = small.post("/registry", content=(b"x" * 200 for _ in range(5)),
                          headers={"content-type": "application/json"})
    assert streamed.status_code == 413                      # no Content-Length header
    assert small.post("/route", json={"query": "check server status"}).status_code == 200

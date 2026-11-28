"""The website's files and the endpoints made for it."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tool_router.api import create_app  # noqa: E402
from tool_router.catalog_changes import plan_change  # noqa: E402
from tool_router.cli import main  # noqa: E402
from tool_router.domain.errors import CatalogError  # noqa: E402
from tool_router.postman import EXAMPLES, SCHEMA  # noqa: E402
from tool_router.state import StateStore  # noqa: E402
from tool_router.web import UI_FOLDER  # noqa: E402

BASE = "http://127.0.0.1:8080"
WEATHER = {"name": "get_current_weather",
           "description": "Get the current weather conditions for a named city",
           "parameters": [{"name": "city"}]}
EMPLOYEE = "hr.get_employee"


def tool(name="send_report", **changes) -> dict:
    return {"id": f"general.{name}", "name": name, "cluster": "general", "side_effects": "read",
            "description": "Send the weekly report to a chosen recipient", "parameters": [],
            **changes}


def make(catalog=None, **options) -> TestClient:
    return TestClient(create_app(catalog, **options), base_url=BASE)


@pytest.fixture
def client(catalog):
    return make(catalog, store=StateStore(Path("state")))


# ------------------------------------------------------------------- pages


def test_the_home_page_leads_to_the_website(client):
    reply = client.get("/", follow_redirects=False)
    assert reply.status_code in (302, 307) and reply.headers["location"] == "/ui/"
    page = client.get("/ui/")
    assert page.status_code == 200 and "Tool Router" in page.text


def test_pages_have_security_headers(client):
    for path in ("/ui/", "/ui/app.js", "/ui/effects.js", "/ui/style.css"):
        headers = client.get(path).headers
        assert "default-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"


def test_every_file_a_page_uses_exists_and_nothing_comes_from_outside(client):
    page = client.get("/ui/").text
    for link in re.findall(r'(?:src|href)="([^"#:]+\.(?:js|css))"', page):
        assert client.get(f"/ui/{link}").status_code == 200
    for name in ("index.html", "app.js", "effects.js", "style.css"):
        text = (UI_FOLDER / name).read_text()
        assert not re.search(r'(?:src|href)="https?://', text)
        assert "innerHTML" not in text and "<script>" not in text


def test_every_element_the_script_looks_for_is_on_the_page():
    page = (UI_FOLDER / "index.html").read_text()
    script = (UI_FOLDER / "app.js").read_text()
    effects = (UI_FOLDER / "effects.js").read_text()
    wanted = set(re.findall(r'\$\("([a-z-]+)"\)', script))
    wanted |= set(re.findall(r'getElementById\("([a-z-]+)"\)', effects))
    assert wanted - set(re.findall(r'id="([a-z-]+)"', page)) == set()


def test_the_api_docs_are_served_with_the_examples(client):
    page = client.get("/docs")                       # FastAPI's built-in page
    assert page.status_code == 200 and "/openapi.json" in page.text
    assert 'href="../docs"' in client.get("/ui/").text
    body = client.get("/openapi.json").json()["paths"]["/route"]["post"]["requestBody"]
    assert body["content"]["application/json"]["example"] == EXAMPLES[("post", "/route")]


def test_the_icon_is_served(client):
    assert 'href="favicon.svg"' in client.get("/ui/").text
    icon = client.get("/ui/favicon.svg")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml")


# ----------------------------------------------------------------- summary


def test_summary_of_a_registry(client, catalog):
    summary = client.get("/registry/summary").json()

    assert summary["loaded"] and summary["tool_count"] == len(catalog["tools"])
    assert sum(summary["groups"].values()) == sum(summary["side_effects"].values()) == 13
    assert len(summary["duplicate_groups"]) == 2 and summary["old_tools"] == 1
    assert summary["planner"] == "local" and summary["saved"] and not summary["mcp_scan"]


def test_summary_of_an_empty_server():
    summary = make().get("/registry/summary").json()
    assert summary == {
        "loaded": False, "tool_count": 0, "groups": {}, "duplicate_groups": [], "old_tools": 0,
        "side_effects": {"read": 0, "write": 0, "destructive": 0}, "planner": None,
        "saved": False, "mcp_scan": False,
    }


# ------------------------------------------------------------ upload logic


def test_merge_adds_updates_and_keeps():
    current = [tool("a_tool"), tool("b_tool")]
    upload = [tool("a_tool", description="Send the monthly report to a chosen recipient"),
              tool("c_tool")]

    change = plan_change(current, [], upload, "merge")

    assert (change.added, change.changed, change.unchanged, change.removed) == (
        ["general.c_tool"], ["general.a_tool"], [], [])
    assert [t["name"] for t in change.tools] == ["a_tool", "b_tool", "c_tool"]
    assert change.tools[0]["description"].startswith("Send the monthly")


def test_replace_removes_what_the_upload_lacks():
    change = plan_change([tool("a_tool"), tool("b_tool")], [], [tool("a_tool")], "replace")
    assert change.removed == ["general.b_tool"] and change.unchanged == ["general.a_tool"]
    assert [t["name"] for t in change.tools] == ["a_tool"]


@pytest.mark.parametrize("mode, left", [("merge", ["a_tool", "b_tool"]), ("replace", ["b_tool"])])
def test_a_rejected_update(mode, left):
    current = [tool("a_tool"), tool("b_tool")]
    change = plan_change(current, [], [tool("a_tool", description="short"), tool("b_tool")], mode)

    assert [r["id"] for r in change.rejected] == ["general.a_tool"]
    assert [t["name"] for t in change.tools] == left
    assert change.tools[0]["description"] == tool()["description"]
    assert change.removed == ([] if mode == "merge" else ["general.a_tool"])


def test_a_tool_pointing_at_a_rejected_one_is_rejected_too():
    upload = [tool("old_tool", replaced_by="general.new_tool"),
              tool("new_tool", description="short"), tool("fine_tool")]

    change = plan_change([], [], upload, "merge")

    assert change.added == ["general.fine_tool"]
    assert [(r["index"], r["reason"]) for r in change.rejected] == [
        (0, "DANGLING_REPLACED_BY"), (1, "DESCRIPTION_TOO_VAGUE")]


def test_a_cycle_with_a_saved_tool_rejects_only_the_upload():
    current = [tool("a_tool", replaced_by="general.b_tool"), tool("b_tool")]
    change = plan_change(current, [], [tool("b_tool", replaced_by="general.a_tool")], "merge")
    assert [r["reason"] for r in change.rejected] == ["CIRCULAR_VERSION_REF"]
    assert change.tools == current


def test_repeated_ids_and_things_that_are_not_tools():
    change = plan_change([], [], [tool(), tool(), "text", None, 5], "merge")
    assert change.added == ["general.send_report"]
    assert [r["index"] for r in change.rejected] == [1, 2, 3, 4]


def test_side_effects_are_kept_when_an_upload_omits_them():
    again = tool("close_account")
    del again["side_effects"]
    change = plan_change([tool("close_account", side_effects="destructive")], [], [again])
    assert change.tools[0]["side_effects"] == "destructive" and change.unchanged


def test_duplicate_groups():
    a, b, c = "general.a_tool", "general.b_tool", "general.c_tool"
    current = [tool("a_tool"), tool("b_tool")]
    merged = plan_change(current, [[a, b]], [tool("c_tool")], "merge", [[a, c], [c, "x.y"]])
    assert merged.near_duplicate_groups == [[a, b], [a, c]]
    replaced = plan_change(current, [[a, b]], [tool("a_tool")], "replace", [[a, b]])
    assert replaced.near_duplicate_groups == []


@pytest.mark.parametrize("mode, groups", [("overwrite", None), ("merge", "x"), ("merge", [[1]])])
def test_a_bad_mode_or_group_list(mode, groups):
    with pytest.raises(CatalogError):
        plan_change([], [], [tool()], mode, groups)


def test_planning_changes_nothing():
    current, upload = [tool("a_tool")], [{"name": "get_thing", "description": "x"}]
    before = copy.deepcopy((current, upload))
    plan_change(current, [], upload, "replace")
    assert (current, upload) == before


# -------------------------------------------------------------- upload API


def test_preview_changes_nothing(client):
    before = StateStore(Path("state")).load()
    report = client.post("/registry/preview", json={"tools": [WEATHER], "mode": "replace"}).json()

    assert report["added"] == ["general.get_current_weather"] and len(report["removed"]) == 13
    assert report["tool_count_after"] == 1
    assert StateStore(Path("state")).load() == before
    assert client.get("/health").json()["tool_count"] == 13


def test_apply_does_what_the_preview_said(client):
    updated = {**client.get(f"/registry/tools/{EMPLOYEE}").json(),
               "description": "Look up one employee profile using their id"}
    del updated["version"]
    body = {"tools": [WEATHER, updated, tool("bad_tool", description="short")], "mode": "merge"}
    report = client.post("/registry/preview", json=body).json()

    done = client.post("/registry/apply", json=body).json()

    assert done == {"status": "saved", **report}
    assert done["changed"] == [EMPLOYEE] and len(done["rejected"]) == 1
    saved = StateStore(Path("state")).load()
    assert len(saved.tools) == 14 == client.get("/health").json()["tool_count"]
    assert client.get(f"/registry/tools/{EMPLOYEE}").json()["version"] == "1.0.1"
    assert saved.history[EMPLOYEE][0]["version"] == "1.0.0"
    assert len(saved.near_duplicate_groups) == 1


def test_apply_to_an_empty_server():
    client = make()
    assert client.post("/registry/apply", json={"tools": [WEATHER]}).status_code == 200
    answer = client.post("/route", json={"query": "current weather in a city"}).json()
    assert answer["selected_tools"] == ["general.get_current_weather"]


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_an_upload_with_nothing_valid_changes_nothing(client, mode):
    reply = client.post("/registry/apply",
                        json={"tools": [tool(description="short")], "mode": mode})
    assert reply.status_code == 422 and reply.json()["detail"]["reason"] == "NOTHING_TO_SAVE"
    assert client.get("/health").json()["tool_count"] == 13


@pytest.mark.parametrize("body, status", [
    ({"tools": []}, 422), ({}, 422), ({"tools": [WEATHER], "mode": "other"}, 400),
    ({"tools": [WEATHER], "near_duplicate_groups": 5}, 400),
])
def test_bad_upload_requests(client, body, status):
    for path in ("/registry/preview", "/registry/apply"):
        assert client.post(path, json=body).status_code == status


def test_the_tool_limit_still_applies(client, monkeypatch):
    monkeypatch.setenv("TOOL_ROUTER_MAX_TOOLS", "13")
    reply = client.post("/registry/apply", json={"tools": [WEATHER]})
    assert reply.status_code == 400 and "exceeds the limit" in reply.json()["detail"]


def test_a_failed_save_is_reported(client, monkeypatch):
    from tool_router.domain.errors import StateError
    monkeypatch.setattr(StateStore, "save",
                        lambda self, state: (_ for _ in ()).throw(StateError("full")))
    reply = client.post("/registry/apply", json={"tools": [WEATHER]})
    assert reply.status_code == 500 and "nothing changed" in reply.json()["detail"]
    assert client.get("/health").json()["tool_count"] == 13


def test_export_can_be_uploaded_again(client):
    reply = client.get("/registry/export")
    assert 'filename="catalog.json"' in reply.headers["content-disposition"]
    exported = reply.json()
    other = make()
    done = other.post("/registry/apply", json={**exported, "mode": "replace"}).json()
    assert done["tool_count_after"] == 13 and done["rejected"] == []
    assert other.get("/registry/export").json() == exported


def test_export_without_tools():
    assert make().get("/registry/export").status_code == 409


# --------------------------------------------------------- other websites


@pytest.mark.parametrize("method, path", [
    ("post", "/route"), ("post", "/registry"), ("post", "/registry/apply"),
    ("put", f"/registry/tools/{EMPLOYEE}"), ("delete", f"/registry/tools/{EMPLOYEE}"),
    ("patch", "/registry/mutate"), ("delete", "/history"), ("post", "/setup"),
    ("post", "/mcp/scan"),
])
@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:9999"])
def test_changes_from_another_website_are_refused(client, method, path, origin):
    reply = client.request(method.upper(), path, json={}, headers={"origin": origin})
    assert reply.status_code == 403 and "another website" in reply.json()["detail"]


@pytest.mark.parametrize("headers", [{}, {"origin": BASE}])
def test_programs_and_the_website_itself_are_allowed(client, headers):
    assert client.post("/route", json={"query": "health"}, headers=headers).status_code == 200


def test_an_allowed_host_name_may_send_changes(catalog):
    client = TestClient(create_app(catalog, allowed_hosts=["router.local"]), base_url=BASE)
    reply = client.post("/route", json={"query": "health"},
                        headers={"origin": "https://router.local"})
    assert reply.status_code == 200


def test_reading_is_not_blocked(client):
    assert client.get("/health", headers={"origin": "https://evil.example"}).status_code == 200


# ----------------------------------------------------------------- Postman


def operations(client):
    return {(method, path) for path, methods in client.get("/openapi.json").json()["paths"].items()
            for method in methods}


def test_the_collection_has_every_endpoint(client):
    reply = client.get("/postman.json")
    data = reply.json()
    assert "postman_collection" in reply.headers["content-disposition"]
    assert data["info"]["schema"] == SCHEMA
    assert {v["key"]: v["value"] for v in data["variable"]}["baseUrl"] == BASE
    found = set()
    for folder in data["item"]:
        for item in folder["item"]:
            method, path = item["name"].split(" ", 1)
            found.add((method.lower(), path))
            assert item["request"]["url"]["raw"].startswith("{{baseUrl}}/")
    assert found == operations(client)


def test_every_example_belongs_to_an_endpoint_with_a_body(client):
    paths = client.get("/openapi.json").json()["paths"]
    needing_a_body = {(m, p) for p, methods in paths.items() for m, op in methods.items()
                      if "requestBody" in op}
    assert set(EXAMPLES) == needing_a_body
    for (method, path), body in EXAMPLES.items():
        content = paths[path][method]["requestBody"]["content"]["application/json"]
        assert content["example"] == body


def test_running_the_collection_from_top_to_bottom_works(catalog):
    client = make(catalog, store=StateStore(Path("state")))
    data = client.get("/postman.json").json()
    variables = {v["key"]: v["value"] for v in data["variable"]}
    items = [item for folder in data["item"] for item in folder["item"]]
    # Registry requests in collection order; replacing everything would hide the rest.
    results = {}
    for item in items:
        request = item["request"]
        path = "/".join(variables["toolId"] if p.startswith(":") else p
                        for p in request["url"]["path"])
        if item["name"] in ("POST /registry", "POST /mcp/scan"):
            continue
        body = request.get("body", {}).get("raw")
        reply = client.request(request["method"], f"/{path}", content=body,
                               headers={"content-type": "application/json"} if body else {})
        results[item["name"]] = reply.status_code
    assert {name: code for name, code in results.items() if code >= 400} == {}


def test_the_replace_example_works(client):
    assert client.post("/registry", json=EXAMPLES[("post", "/registry")]).status_code == 200


# ----------------------------------------------------------------- CLI


@pytest.mark.parametrize("args, scan", [
    ([], True), (["--host", "0.0.0.0"], False),
    (["--host", "0.0.0.0", "--allow-mcp-scan"], True),
])
def test_scanning_is_on_only_for_a_local_server_or_when_asked(args, scan, monkeypatch, capsys):
    uvicorn = pytest.importorskip("uvicorn")
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append(app))
    assert main(["serve", "--no-persist", *args]) == 0
    assert "Website: http://127.0.0.1:8080/ui/" in capsys.readouterr().err
    client = TestClient(started[0], base_url="http://localhost")
    assert client.get("/registry/summary").json()["mcp_scan"] is scan

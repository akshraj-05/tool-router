"""The .tool-router folder: saving, restarting, upserting and versioning."""

from __future__ import annotations

import copy
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from tool_router import state as state_module
from tool_router.catalog_loader import merge_with_saved
from tool_router.cli import main
from tool_router.config import Settings
from tool_router.domain.errors import InvalidToolError, StateError
from tool_router.domain.versioning import apply_versions, bump, change_level
from tool_router.engine.pipeline import RoutingEngine
from tool_router.state import SavedState, StateStore

STATE = Path(".tool-router")
EMPLOYEE = "hr.get_employee"


def tool(name="send_report", **changes) -> dict:
    base = {
        "id": f"general.{name}", "name": name, "cluster": "general", "side_effects": "write",
        "description": "Send the weekly report to a chosen recipient",
        "parameters": [{"name": "to", "type": "string", "required": True, "description": "Address"}],
    }
    return {**base, **changes}


def params(*extra, **first):
    return [{"name": "to", "type": "string", "required": True, "description": "Address", **first}, *extra]


def saved() -> dict:
    return json.loads((STATE / "catalog.json").read_text())


def by_id(data: dict) -> dict:
    return {t["id"]: t for t in data["tools"]}


def build(catalog, store=None, previous=None) -> RoutingEngine:
    return RoutingEngine(catalog, settings=Settings(), store=store, previous=previous)


# ----------------------------------------------------------- version rules


@pytest.mark.parametrize("changes, version", [
    ({}, "1.0.0"),
    ({"description": "Send the monthly report to a chosen recipient"}, "1.0.1"),
    ({"side_effects": "destructive"}, "1.0.1"),
    ({"tags": ["mail"]}, "1.0.1"),
    ({"parameters": params({"name": "cc", "type": "string"})}, "1.1.0"),
    ({"parameters": params(description="Recipient address")}, "1.1.0"),
    ({"parameters": params(required=False)}, "1.1.0"),
    ({"parameters": params({"name": "cc", "required": True})}, "2.0.0"),
    ({"parameters": params(type="array")}, "2.0.0"),
    ({"parameters": []}, "2.0.0"),
    ({"parameters": [{"name": "recipient", "required": True}]}, "2.0.0"),
    ({"name": "mail_report"}, "2.0.0"),
    # the biggest change wins
    ({"description": "Send the monthly report to a chosen recipient", "parameters": params(type="array")}, "2.0.0"),
])
def test_version_follows_the_size_of_the_change(changes, version):
    old = tool(version="1.0.0")
    new = {**tool(), **changes}
    tools, history = apply_versions([old], [new], {})
    assert tools[0]["version"] == version
    assert history == ({} if version == "1.0.0" else {old["id"]: [old]})


def test_new_tool_starts_at_one_and_inputs_are_not_modified():
    new = tool()
    original = copy.deepcopy(new)
    tools, _ = apply_versions([], [new], {})
    assert tools[0]["version"] == "1.0.0" and new == original


def test_parameter_order_and_spelled_out_defaults_are_not_a_change():
    a = tool(parameters=[{"name": "x"}, {"name": "y"}])
    b = tool(parameters=[{"name": "y", "type": "string", "required": False, "description": ""}, {"name": "x"}])
    assert change_level(a, b) == 0


@pytest.mark.parametrize("declared, expected", [("3.0.0", "3.0.0"), ("1.2.3", "2.0.1"), ("2.0.0", "2.0.1")])
def test_a_supplied_version_counts_only_if_higher(declared, expected):
    old = tool(version="2.0.0")
    new = tool(version=declared, description="Send the monthly report to a chosen recipient")
    assert apply_versions([old], [new], {})[0][0]["version"] == expected


def test_higher_version_alone_is_recorded_and_ten_compares_above_nine():
    tools, history = apply_versions([tool(version="1.9.0")], [tool(version="1.10.0")], {})
    assert tools[0]["version"] == "1.10.0" and history


def test_delete_keeps_history_and_re_adding_continues_numbering():
    old = tool(version="1.4.0")
    tools, history = apply_versions([old], [], {})
    assert tools == [] and history[old["id"]] == [old]
    same, history_same = apply_versions([], [tool()], history)
    assert same[0]["version"] == "1.4.0" and history_same == {}
    changed, history_changed = apply_versions([], [tool(parameters=[])], history)
    assert changed[0]["version"] == "2.0.0" and history_changed[old["id"]] == [old]


def test_history_is_limited_to_ten_per_tool():
    current, history = [tool(version="1.0.0")], {}
    for i in range(15):
        current, history = apply_versions(
            current, [tool(description=f"Send report number {i} to a chosen recipient")], history)
    kept = history["general.send_report"]
    assert len(kept) == 10 and current[0]["version"] == "1.0.15" and kept[-1]["version"] == "1.0.14"


def test_bump():
    assert [bump("1.2.3", n) for n in (1, 2, 3)] == ["1.2.4", "1.3.0", "2.0.0"]


# ------------------------------------------------------------------- store


def test_round_trip_permissions_and_gitignore():
    store = StateStore(STATE)
    store.save(SavedState([tool()], {"a.b": [tool()]}, []))
    assert store.load() == SavedState([tool()], {"a.b": [tool()]}, [])
    assert (STATE / ".gitignore").read_text() == "*\n"
    assert stat.S_IMODE((STATE / "catalog.json").stat().st_mode) == 0o600


def test_nothing_saved_yet():
    assert StateStore(STATE).load() is None


def test_an_interrupted_write_keeps_the_old_file(monkeypatch):
    store = StateStore(STATE)
    store.save(SavedState([tool()]))
    before = (STATE / "catalog.json").read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(state_module.os, "replace", fail)
    with pytest.raises(StateError, match="could not save"):
        store.save(SavedState([tool("other")]))
    assert (STATE / "catalog.json").read_bytes() == before
    assert sorted(p.name for p in STATE.iterdir()) == [".gitignore", "catalog.json"]


@pytest.mark.parametrize("content", [
    "", "{not json", "[]", '{"tools": []}', '{"format_version": 2, "tools": [{"id": "a.b"}]}',
    '{"format_version": 1, "tools": []}', '{"format_version": 1, "tools": ["x"]}',
    '{"format_version": 1, "tools": [{"id": "a.b"}], "history": []}',
])
def test_damaged_file_is_reported_and_left_alone(content):
    STATE.mkdir()
    (STATE / "catalog.json").write_text(content)
    with pytest.raises(StateError, match="cannot be read"):
        StateStore(STATE).load()
    assert (STATE / "catalog.json").read_text() == content


def test_unusable_folder():
    Path("blocker").write_text("a file, not a folder")
    with pytest.raises(StateError, match="--state-dir"):
        StateStore("blocker").prepare()


# ------------------------------------------------------------------ engine


def test_registry_and_vectors_are_saved(catalog):
    build(catalog, StateStore(STATE))
    data = saved()
    assert data["format_version"] == 1 and len(data["tools"]) == 13
    vectors = json.loads((STATE / "embeddings.json").read_text())
    assert len(vectors["entries"]) == 13


def test_every_change_is_saved_and_survives_a_restart(catalog):
    store = StateStore(STATE)
    engine = build(catalog, store)
    engine.create_tool(tool())
    updated = engine.update_tool(EMPLOYEE, {**engine.get_tool(EMPLOYEE), "description": "Look up one employee profile using their id"})
    assert updated["version"] == "1.0.1"
    engine.delete_tool("comm.send_email")
    again = build({"tools": store.load().tools}, store, store.load())
    assert {t["id"]: t for t in again.list_tools()} == {t["id"]: t for t in engine.list_tools()}
    assert again.get_tool(EMPLOYEE)["version"] == "1.0.1"
    history = saved()["history"]
    assert history[EMPLOYEE][0]["version"] == "1.0.0" and "comm.send_email" in history


def test_old_definitions_are_never_routed(catalog):
    engine = build(catalog, StateStore(STATE))
    engine.update_tool(EMPLOYEE, {**engine.get_tool(EMPLOYEE), "description": "Look up one employee profile using their id"})
    assert [t["id"] for t in engine.list_tools()].count(EMPLOYEE) == 1
    assert engine.tool_count == 13


def test_replacement_through_mutate_is_versioned(catalog):
    engine = build(catalog, StateStore(STATE))
    new = {**engine.get_tool(EMPLOYEE), "parameters": []}
    engine.apply_mutation({"remove": [EMPLOYEE], "add": [new]})
    assert engine.get_tool(EMPLOYEE)["version"] == "2.0.0"


@pytest.mark.parametrize("change", [
    lambda e: e.create_tool(tool()),
    lambda e: e.update_tool(EMPLOYEE, {**e.get_tool(EMPLOYEE), "parameters": []}),
    lambda e: e.delete_tool(EMPLOYEE),
    lambda e: e.apply_mutation({"add": [tool()]}),
])
def test_a_failed_save_changes_nothing(change, catalog, monkeypatch):
    store = StateStore(STATE)
    engine = build(catalog, store)
    before_memory, before_disk = engine.snapshot(), (STATE / "catalog.json").read_bytes()

    def fail(state):
        raise StateError("disk full")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(StateError):
        change(engine)
    assert engine.snapshot() == before_memory
    assert (STATE / "catalog.json").read_bytes() == before_disk


def test_a_rejected_change_is_not_saved(catalog):
    engine = build(catalog, StateStore(STATE))
    before = (STATE / "catalog.json").read_bytes()
    with pytest.raises(InvalidToolError):
        engine.create_tool(tool(description="short"))
    assert (STATE / "catalog.json").read_bytes() == before


def test_vectors_of_removed_tools_are_dropped(catalog):
    engine = build(catalog, StateStore(STATE))
    engine.delete_tool("comm.send_email")
    assert len(json.loads((STATE / "embeddings.json").read_text())["entries"]) == 12


def test_deleting_a_grouped_tool_still_allows_a_restart(catalog):
    store = StateStore(STATE)
    engine = build(catalog, store)
    engine.delete_tool("fin.get_revenue_report")
    state = store.load()
    assert state.near_duplicate_groups == []
    build({"tools": state.tools, "near_duplicate_groups": state.near_duplicate_groups}, store, state)


def test_without_a_store_nothing_is_written(catalog):
    engine = build(catalog)
    engine.create_tool(tool())
    assert list(Path.cwd().iterdir()) == []
    assert "version" not in engine.get_tool(EMPLOYEE)


def test_no_secret_is_written_to_the_folder(catalog, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-123")
    build(catalog, StateStore(STATE))
    for path in STATE.iterdir():
        assert "SECRET-KEY-123" not in path.read_text()


# ------------------------------------------------------------------ upsert


def test_merge_adds_updates_keeps_and_prunes():
    old = [tool("a_tool", version="1.0.0"), tool("b_tool", version="1.0.0")]
    incoming = {"tools": [tool("a_tool", description="Send the monthly report to a chosen recipient"), tool("c_tool")]}
    merged, report = merge_with_saved(SavedState(old), incoming)
    assert [t["name"] for t in merged["tools"]] == ["a_tool", "b_tool", "c_tool"]
    assert "1 new, 1 changed, 0 unchanged, 1 not in the input (kept)" in report[0]
    pruned, _ = merge_with_saved(SavedState(old), incoming, prune=True)
    assert [t["name"] for t in pruned["tools"]] == ["a_tool", "c_tool"]


def test_merge_keeps_declared_side_effects_when_the_input_omits_them():
    old = [tool("close_account", side_effects="destructive", version="1.0.0")]
    incoming = tool("close_account")
    del incoming["side_effects"]
    merged, report = merge_with_saved(SavedState(old), {"tools": [incoming]})
    assert merged["tools"][0]["side_effects"] == "destructive" and "1 unchanged" in report[0]


# --------------------------------------------------------------------- CLI


@pytest.fixture
def serve(monkeypatch, capsys):
    """Run `tool-router serve ...` without opening a port; return (code, app, stderr)."""
    uvicorn = pytest.importorskip("uvicorn")

    def run(*args):
        started: list = []
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append(app))
        code = main(["serve", *args])
        return code, (started[0] if started else None), capsys.readouterr().err

    return run


def write(path: str, *tools) -> str:
    Path(path).write_text(json.dumps({"tools": list(tools)}))
    return path


def test_start_then_restart_without_a_source(serve):
    assert serve("--catalog", write("c.json", tool()))[0] == 0
    code, app, err = serve()
    assert code == 0 and "loaded 1 saved tools" in err
    from starlette.testclient import TestClient
    assert TestClient(app, base_url="http://localhost").get("/registry/tools").json()["tool_count"] == 1


def test_changed_input_makes_a_new_version_and_keeps_history(serve):
    serve("--catalog", write("c.json", tool("a_tool"), tool("b_tool")))
    changed = tool("a_tool", description="Send the monthly report to a chosen recipient")
    _, _, err = serve("--catalog", write("c.json", changed))
    assert "0 new, 1 changed, 0 unchanged, 1 not in the input (kept)" in err
    data = saved()
    assert by_id(data)["general.a_tool"]["version"] == "1.0.1"
    assert by_id(data)["general.b_tool"]["version"] == "1.0.0"
    assert data["history"]["general.a_tool"][0]["description"] == tool()["description"]


def test_restarting_with_the_same_input_changes_no_version(serve):
    source = write("c.json", tool())
    for _ in range(3):
        serve("--catalog", source)
    assert by_id(saved())["general.send_report"]["version"] == "1.0.0" and saved()["history"] == {}


def test_prune(serve):
    serve("--catalog", write("c.json", tool("a_tool"), tool("b_tool")))
    serve("--catalog", write("c.json", tool("a_tool")), "--prune")
    assert list(by_id(saved())) == ["general.a_tool"] and "general.b_tool" in saved()["history"]


def test_prune_needs_a_source(serve):
    code, _, err = serve("--prune")
    assert code == 1 and "--prune needs" in err


def test_invalid_input_stops_startup_and_keeps_the_saved_file(serve):
    serve("--catalog", write("c.json", tool()))
    before = (STATE / "catalog.json").read_bytes()
    code, _, err = serve("--catalog", write("bad.json", tool(description="short")))
    assert code == 1 and "DESCRIPTION_TOO_VAGUE" in err
    assert (STATE / "catalog.json").read_bytes() == before


def test_damaged_saved_file_stops_startup(serve):
    STATE.mkdir()
    (STATE / "catalog.json").write_text("{broken")
    code, _, err = serve("--catalog", write("c.json", tool()))
    assert code == 1 and "cannot be read" in err and "Traceback" not in err
    assert (STATE / "catalog.json").read_text() == "{broken"


def test_no_persist_and_state_dir(serve):
    source = write("c.json", tool())
    serve("--catalog", source, "--no-persist")
    assert sorted(p.name for p in Path.cwd().iterdir()) == ["c.json"]
    serve("--catalog", source, "--state-dir", "elsewhere/state")
    assert Path("elsewhere/state/catalog.json").exists() and not STATE.exists()


def test_the_saved_file_is_also_a_normal_catalog_file(serve):
    serve("--catalog", write("c.json", tool()))
    assert main(["validate", str(STATE / "catalog.json")]) == 0
    assert serve("--catalog", str(STATE / "catalog.json"), "--state-dir", "copy")[0] == 0


def test_no_old_marker_file_is_created(serve):
    serve("--catalog", write("c.json", tool()))
    assert not Path(".tool_router").exists()


# --------------------------------------------------------------------- API


def test_api_upload_is_versioned_and_save_errors_are_reported(serve, monkeypatch):
    from starlette.testclient import TestClient

    _, app, _ = serve("--catalog", write("c.json", tool("a_tool")))
    client = TestClient(app, base_url="http://localhost")
    changed = tool("a_tool", description="Send the monthly report to a chosen recipient")
    assert client.post("/registry", json={"tools": [changed]}).status_code == 200
    assert by_id(saved())["general.a_tool"]["version"] == "1.0.1"

    monkeypatch.setattr(StateStore, "save", lambda self, state: (_ for _ in ()).throw(StateError("full")))
    response = client.post("/registry/tools", json=tool("b_tool"))
    assert response.status_code == 500 and "nothing changed" in response.json()["detail"]
    assert client.get("/registry/tools").json()["tool_count"] == 1


def test_re_upload_without_side_effects_keeps_the_saved_value(serve):
    from starlette.testclient import TestClient

    _, app, _ = serve("--catalog", write("c.json", tool("close_account", side_effects="destructive")))
    again = tool("close_account")
    del again["side_effects"]
    assert TestClient(app, base_url="http://localhost").post("/registry", json={"tools": [again]}).status_code == 200
    stored = by_id(saved())["general.close_account"]
    assert stored["side_effects"] == "destructive" and stored["version"] == "1.0.0"


def test_duplicate_ids_in_the_input_are_still_refused(serve):
    serve("--catalog", write("c.json", tool()))
    code, _, err = serve("--catalog", write("two.json", tool(), tool()))
    assert code == 1 and "DUPLICATE_ID" in err


def test_api_starting_empty_saves_the_first_upload(serve):
    from starlette.testclient import TestClient

    _, app, _ = serve()
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/registry/tools").status_code == 409
    client.post("/registry", json={"tools": [tool()]})
    assert list(by_id(saved())) == ["general.send_report"]


# ------------------------------------------------------- real process, killed


def test_changes_survive_killing_the_real_server():
    pytest.importorskip("uvicorn")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    source = write("c.json", tool("a_tool"))

    def start(*args):
        process = subprocess.Popen(
            [sys.executable, "-m", "tool_router.cli", "serve", "--host", "127.0.0.1",
             "--port", str(port), *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=os.getcwd())
        deadline = time.monotonic() + 40
        while True:
            try:
                urllib.request.urlopen(base + "/health", timeout=2).read()
                return process
            except OSError:
                assert process.poll() is None and time.monotonic() < deadline, "server did not start"
                time.sleep(0.2)

    server = start("--catalog", source)
    try:
        request = urllib.request.Request(
            base + "/registry/tools", data=json.dumps(tool("b_tool")).encode(),
            headers={"content-type": "application/json"})
        urllib.request.urlopen(request, timeout=10).read()
    finally:
        server.kill()                       # no chance to save anything on the way out
        server.wait(10)

    server = start()
    try:
        with urllib.request.urlopen(base + "/registry/tools", timeout=10) as response:
            tools = json.loads(response.read())["tools"]
        assert sorted(t["name"] for t in tools) == ["a_tool", "b_tool"]
    finally:
        server.kill()
        server.wait(10)

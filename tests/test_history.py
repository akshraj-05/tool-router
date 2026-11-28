"""Request history: what is saved, the limits, and that routing never depends on it."""

from __future__ import annotations

import contextlib
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from tool_router import history as history_module
from tool_router.domain.errors import StateError
from tool_router.domain.models import RouteResult
from tool_router.history import MAX_QUERY_CHARS, MAX_ROWS, RequestHistory
from tool_router.state import StateStore

DATABASE = Path("state/router.db")
PLAN = RouteResult(selected_tools=["a.b"], plan=["a.b", "a.b"], tokens_saved_pct=0.9)
QUESTION = RouteResult(clarify=True, clarify_question="Which one?")
NOTHING = RouteResult()
HEALTH = "Check the health status of the production server"


@pytest.fixture
def history():
    made = RequestHistory(DATABASE)
    yield made
    made.close()


def rows(statement: str = "SELECT query FROM requests ORDER BY id") -> list:
    with contextlib.closing(sqlite3.connect(DATABASE)) as db:
        return db.execute(statement).fetchall()


# ------------------------------------------------------------------- saving


def test_one_request_becomes_one_row(history):
    history.record("send it", PLAN, 3.14159)

    (saved,) = history.recent()

    assert saved.pop("at").endswith("Z")
    assert saved == {"id": 1, "query": "send it", "result": "plan", "plan": ["a.b", "a.b"],
                     "ms": 3.142, "tokens_saved_pct": 0.9}


@pytest.mark.parametrize("result, kind", [(PLAN, "plan"), (QUESTION, "question"), (NOTHING, "none")])
def test_the_kind_of_answer(history, result, kind):
    history.record("q", result, 1.0)
    assert history.recent()[0]["result"] == kind


def test_the_newest_come_first_and_can_be_limited(history):
    for n in range(5):
        history.record(f"q{n}", NOTHING, 1.0)
    assert [r["query"] for r in history.recent(3)] == ["q4", "q3", "q2"]
    assert history.recent(0) == []


def test_long_and_unusual_text(history):
    history.record("é" * 2000, NOTHING, 1.0)
    history.record("broken \ud800 text '); DROP TABLE requests; --", NOTHING, 1.0)

    newest, oldest = history.recent()

    assert oldest["query"] == "é" * MAX_QUERY_CHARS
    assert newest["query"] == "broken ? text '); DROP TABLE requests; --"


def test_only_the_newest_rows_are_kept(history, monkeypatch):
    monkeypatch.setattr(history_module, "MAX_ROWS", 10)
    for n in range(35):
        history.record(f"q{n}", NOTHING, 1.0)
        if n % 7 == 0:
            history.flush()                     # several separate writes

    history.flush()

    assert [q for (q,) in rows()] == [f"q{n}" for n in range(25, 35)]


def test_the_default_limit_is_a_thousand(history):
    assert MAX_ROWS == 1000
    for n in range(MAX_ROWS + 50):
        history.record(f"q{n}", NOTHING, 1.0)
        if n % 200 == 0:
            history.flush()
    history.flush()
    assert rows("SELECT COUNT(*), MIN(id), MAX(id) FROM requests") == [(1000, 51, 1050)]


def test_history_survives_a_restart(history):
    history.record("before", PLAN, 1.0)
    history.close()

    assert [r["query"] for r in RequestHistory(DATABASE).recent()] == ["before"]


def test_closing_writes_what_is_queued_and_stops_recording(history):
    for n in range(200):
        history.record(f"q{n}", NOTHING, 1.0)
    history.close()
    history.record("too late", NOTHING, 1.0)

    assert len(rows()) == 200
    assert not any(t.name == "request-history" and t is history._writer and t.is_alive()
                   for t in threading.enumerate())


def test_requests_are_written_together(history, monkeypatch):
    batches = []
    insert = history._insert
    monkeypatch.setattr(history, "_insert", lambda found: (batches.append(len(found)), insert(found)))
    for n in range(20):
        history.record(f"q{n}", NOTHING, 1.0)
        time.sleep(0.002)
    history.flush()

    assert sum(batches) == 20 and len(batches) <= 2


def test_a_request_is_written_without_anyone_asking(history):
    history.record("q", NOTHING, 1.0)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (DATABASE.exists() and DATABASE.stat().st_size):
        time.sleep(0.05)
    while time.monotonic() < deadline and rows() != [("q",)]:
        time.sleep(0.05)

    assert rows() == [("q",)]


def test_reading_and_closing_do_not_wait_for_the_collecting_pause(history, monkeypatch):
    monkeypatch.setattr(history_module, "_COLLECT_SECONDS", 30)
    started = time.perf_counter()
    history.record("a", NOTHING, 1.0)
    assert len(history.recent()) == 1
    history.record("b", NOTHING, 1.0)
    history.close()

    assert time.perf_counter() - started < 5 and len(rows()) == 2


def test_many_threads_lose_nothing(history):
    def work(worker):
        for n in range(100):
            history.record(f"{worker}-{n}", PLAN, 1.0)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    history.flush()

    assert len({q for (q,) in rows()}) == 800


# ------------------------------------------------------------------ totals


def test_summary(history):
    history.record("a", RouteResult(plan=["x"], tokens_saved_pct=0.9), 2.0)
    history.record("b", RouteResult(plan=["y"], tokens_saved_pct=0.8), 4.0)
    history.record("c", QUESTION, 6.0)
    history.record("d", NOTHING, 8.0)

    assert history.summary() == {
        "requests": 4, "kept_at_most": 1000, "average_ms": 5.0,
        "average_tokens_saved_pct": 0.85,            # questions and empty answers not counted
        "results": {"plan": 2, "question": 1, "none": 1},
    }


def test_empty_summary_creates_no_file(history):
    assert history.summary() == {
        "requests": 0, "kept_at_most": 1000, "average_ms": None,
        "average_tokens_saved_pct": None, "results": {"plan": 0, "question": 0, "none": 0},
    }
    assert history.recent() == [] and history.clear() == 0 and not DATABASE.exists()


# ------------------------------------------------------------------- clearing


def test_clear_removes_the_text_from_the_file_and_nothing_else(history, catalog):
    from tool_router.engine.pipeline import RoutingEngine
    from tool_router.config import Settings

    RoutingEngine(catalog, settings=Settings(), store=StateStore(Path("state")))
    for n in range(30):
        history.record(f"PRIVATE-TEXT-{n}", PLAN, 1.0)
    history.flush()
    assert b"PRIVATE-TEXT" in DATABASE.read_bytes()

    assert history.clear() == 30

    assert b"PRIVATE-TEXT" not in DATABASE.read_bytes()
    assert history.recent() == []
    assert len(StateStore(Path("state")).load().tools) == len(catalog["tools"])


def test_trimmed_text_is_removed_from_the_file(history, monkeypatch):
    monkeypatch.setattr(history_module, "MAX_ROWS", 1)
    history.record("OLD-PRIVATE-TEXT", NOTHING, 1.0)
    history.flush()
    history.record("new", NOTHING, 1.0)
    history.flush()
    assert b"OLD-PRIVATE-TEXT" not in DATABASE.read_bytes()


# ----------------------------------------------------------------- problems


def test_recording_does_not_wait_for_the_database(history):
    history.record("first", NOTHING, 1.0)
    history.flush()
    with contextlib.closing(sqlite3.connect(DATABASE)) as other:
        other.execute("BEGIN EXCLUSIVE")
        started = time.perf_counter()
        for n in range(50):
            history.record(f"q{n}", NOTHING, 1.0)
        took = time.perf_counter() - started
        other.rollback()

    assert took < 0.5
    history.flush()
    assert len(rows()) == 51


def test_a_damaged_file_never_breaks_recording_and_is_left_alone(history):
    DATABASE.parent.mkdir()
    DATABASE.write_bytes(b"not a database")

    history.record("q", PLAN, 1.0)
    history.flush()
    for read in (history.recent, history.summary, history.clear):
        with pytest.raises(StateError):
            read()

    assert DATABASE.read_bytes() == b"not a database"
    assert history._writer.is_alive()


def test_a_full_queue_drops_the_request_without_blocking(monkeypatch):
    monkeypatch.setattr(history_module, "_QUEUE_SIZE", 2)
    busy, release = threading.Event(), threading.Event()
    history = RequestHistory(DATABASE)

    def slow(rows):
        busy.set()
        release.wait(10)

    monkeypatch.setattr(history, "_insert", slow)
    history.record("taken", NOTHING, 1.0)
    assert busy.wait(5)
    started = time.perf_counter()
    for n in range(20):
        history.record(f"q{n}", NOTHING, 1.0)

    assert time.perf_counter() - started < 0.5 and history._queue.qsize() == 2
    release.set()
    history.close()


def test_without_a_file_nothing_is_saved():
    history = RequestHistory(None)
    history.record("q", PLAN, 1.0)
    assert not history.enabled and history.recent() == [] and history.clear() == 0
    assert history.summary()["requests"] == 0
    assert history._writer is None and list(Path.cwd().iterdir()) == []


# ---------------------------------------------------------------------- API


@pytest.fixture
def client(catalog):
    from tool_router.api import create_app
    testclient = pytest.importorskip("fastapi.testclient")
    app = create_app(catalog, store=StateStore(Path("state")))
    with testclient.TestClient(app) as made:
        yield made


def test_routed_requests_appear_in_history(client):
    answer = client.post("/route", json={"query": HEALTH}).json()
    client.post("/route", json={"query": "zzzz qqqq"})

    data = client.get("/history").json()

    assert data["enabled"] is True and data["summary"]["requests"] == 2
    assert data["requests"][1]["query"] == HEALTH
    assert data["requests"][1]["plan"] == answer["plan"]
    assert data["requests"][1]["tokens_saved_pct"] == answer["tokens_saved_pct"]
    assert data["requests"][0]["result"] == "none"
    assert [r["query"] for r in client.get("/history?limit=1").json()["requests"]] == ["zzzz qqqq"]


def test_history_can_be_cleared(client):
    client.post("/route", json={"query": HEALTH})
    assert client.delete("/history").json() == {"cleared": 1}
    assert client.get("/history").json()["summary"]["requests"] == 0


@pytest.mark.parametrize("limit", ["-1", "1001", "many"])
def test_a_bad_limit_is_refused(client, limit):
    assert client.get(f"/history?limit={limit}").status_code == 422


def test_requests_that_were_not_routed_are_not_saved(client):
    assert client.post("/route", json={"query": ""}).status_code == 422
    assert client.get("/history").json()["summary"]["requests"] == 0


def test_routing_works_when_history_cannot_be_saved(client):
    with contextlib.closing(sqlite3.connect(DATABASE)) as db:
        with db:
            db.execute("DROP TABLE requests")

    assert client.post("/route", json={"query": HEALTH}).json()["selected_tools"]
    assert client.get("/history").status_code == 500


def test_shutdown_waits_for_requests_still_being_written(catalog, monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    from tool_router.api import create_app
    insert = RequestHistory._insert

    def slow(self, rows):
        time.sleep(0.3)                             # still writing when the server stops
        insert(self, rows)

    monkeypatch.setattr(RequestHistory, "_insert", slow)
    with testclient.TestClient(create_app(catalog, store=StateStore(Path("state")))) as client:
        for _ in range(5):
            client.post("/route", json={"query": HEALTH})

    assert rows("SELECT COUNT(*) FROM requests") == [(5,)]


def test_history_is_off_without_a_store(catalog):
    testclient = pytest.importorskip("fastapi.testclient")
    from tool_router.api import create_app
    with testclient.TestClient(create_app(catalog)) as client:
        client.post("/route", json={"query": HEALTH})
        data = client.get("/history").json()
        assert data["enabled"] is False and data["requests"] == []
        assert client.delete("/history").json() == {"cleared": 0}
    assert list(Path.cwd().iterdir()) == []


def test_a_running_server_saves_history_and_keeps_it_after_a_restart():
    pytest.importorskip("uvicorn")
    catalog = Path(__file__).resolve().parent / "data" / "catalog.json"
    root = Path(__file__).resolve().parent.parent
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def call(path, body=None):
        request = urllib.request.Request(
            base + path, data=None if body is None else json.dumps(body).encode(),
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as reply:
            return json.load(reply)

    @contextlib.contextmanager
    def server(*args):
        code = (f"import sys; sys.path.insert(0, {str(root)!r}); "
                "from tool_router.cli import main; sys.exit(main(sys.argv[1:]))")
        process = subprocess.Popen(
            [sys.executable, "-c", code, "serve", "--port", str(port), *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(200):
                with contextlib.suppress(OSError):
                    call("/health")
                    break
                time.sleep(0.05)
            yield
        finally:
            process.terminate()
            process.wait(timeout=20)

    with server("--catalog", str(catalog)):
        for n in range(20):
            call("/route", {"query": f"{HEALTH} {n}"})
    with server():                                   # no catalog: everything from router.db
        data = call("/history?limit=50")
        assert call("/health")["tool_count"] == 13

    assert data["summary"]["requests"] == 20
    assert [r["query"] for r in data["requests"]][-1] == f"{HEALTH} 0"

"""``router.db`` under interruption and concurrent use."""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tool_router.database import open_database
from tool_router.state import SavedState, StateStore

TABLES = {"tools", "tool_history", "duplicate_groups", "vectors", "cache", "requests"}


def registry(label: str, count: int = 400) -> SavedState:
    tools = [{"id": f"g.tool_{i}", "description": f"{label} " + "x" * 200} for i in range(count)]
    return SavedState(tools, {"g.tool_0": [{"id": "g.tool_0", "description": label}]},
                      [["g.tool_0", "g.tool_1"]])


def labels(state: SavedState) -> set[str]:
    """Every version marker in a loaded registry; one marker means nothing was mixed."""
    return {t["description"].split()[0] for t in state.tools} | {
        state.history["g.tool_0"][0]["description"]}


def test_a_new_file_has_every_table():
    with open_database(Path("new/router.db")) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == TABLES


def test_a_failed_transaction_changes_nothing():
    path = Path("router.db")
    with pytest.raises(RuntimeError):
        with open_database(path) as db:
            db.execute("INSERT INTO cache (name, value) VALUES ('a', 'b')")
            raise RuntimeError("stop")
    with contextlib.closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM cache").fetchone() == (0,)


def test_loading_while_saving_never_mixes_two_versions():
    store = StateStore(Path("state"))
    store.save(registry("v0"))
    seen, errors = [], []
    done = threading.Event()

    def read():
        while not done.is_set():
            try:
                seen.append(labels(store.load()))
            except Exception as exc:                          # reported below
                errors.append(exc)

    readers = [threading.Thread(target=read) for _ in range(3)]
    for thread in readers:
        thread.start()
    try:
        for n in range(1, 25):
            store.save(registry(f"v{n}"))
    finally:
        done.set()
        for thread in readers:
            thread.join()

    assert not errors and seen
    assert all(len(found) == 1 for found in seen)


KILLED_WRITER = """
import sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from test_database import registry
from tool_router.state import StateStore
store = StateStore("state")
n = 0
while True:
    n += 1
    store.save(registry(f"v{n}", 2000))
    print(n, flush=True)
"""


def test_a_process_killed_while_saving_leaves_one_whole_version():
    here = Path(__file__).resolve().parent
    store = StateStore(Path("state"))
    store.save(registry("v0", 2000))
    for delay in (0.0, 0.003, 0.007, 0.012, 0.02, 0.031):
        writer = subprocess.Popen(
            [sys.executable, "-c", KILLED_WRITER, str(here), str(here.parent)],
            stdout=subprocess.PIPE, text=True)
        try:
            assert writer.stdout.readline().strip() == "1"      # one save has finished
            time.sleep(delay)
        finally:
            writer.kill()                                       # no chance to clean up
            writer.wait()
            writer.stdout.close()

        state = store.load()

        assert len(state.tools) == 2000
        assert len(labels(state)) == 1

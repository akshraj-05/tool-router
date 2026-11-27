"""A damaged or unusable ``vectors.db`` must never stop the router."""

from __future__ import annotations

import contextlib
import sqlite3
import stat
from pathlib import Path

import pytest

from tool_router.config import Settings
from tool_router.engine.pipeline import RoutingEngine
from tool_router.state import StateStore

DATABASE = Path("state/vectors.db")


def _engine(catalog, **settings):
    return RoutingEngine(catalog, settings=Settings(**settings), store=StateStore(Path("state")))


def _vectors(engine):
    return [entry.embedding for entry in engine._index.entries]


def _sql(statement: str, *values):
    with contextlib.closing(sqlite3.connect(DATABASE)) as db:
        with db:
            return db.execute(statement, values).fetchall()


def test_saved_vectors_are_exactly_the_computed_ones(catalog):
    computed = _vectors(_engine(catalog))

    assert _vectors(_engine(catalog)) == computed
    assert len(_sql("SELECT hash FROM vectors")) == len(catalog["tools"])


def test_the_database_is_readable_by_its_owner_only(catalog):
    _engine(catalog)

    assert stat.S_IMODE(DATABASE.stat().st_mode) == 0o600


@pytest.mark.parametrize("content", [b"", b"not a database", b'{"entries": {}}', b"\xff" * 4096])
def test_a_damaged_file_is_replaced(catalog, content):
    expected = _vectors(_engine(catalog))
    DATABASE.write_bytes(content)

    assert _vectors(_engine(catalog)) == expected
    assert len(_sql("SELECT hash FROM vectors")) == len(catalog["tools"])


@pytest.mark.parametrize("statements", [
    ["DROP TABLE vectors"],
    ["DROP TABLE meta"],
    ["DROP TABLE vectors", "CREATE TABLE vectors (other TEXT)"],
    ["DROP TABLE meta", "CREATE TABLE meta (other TEXT)"],
])
def test_unexpected_tables_are_repaired(catalog, statements):
    expected = _vectors(_engine(catalog))
    for statement in statements:
        _sql(statement)

    assert _vectors(_engine(catalog)) == expected
    assert _vectors(_engine(catalog)) == expected
    assert len(_sql("SELECT hash FROM vectors")) == len(catalog["tools"])


@pytest.mark.parametrize("value", [
    "junk", "[1.0, 2.0]", '{"word": "heavy"}', '{"word": null}', '{"word": true}',
    '{"word": NaN}', '{"word": Infinity}', b"abc", b"\x00" * 16, 5, 2.5,
])
def test_unusable_saved_vectors_are_computed_again(catalog, value):
    expected = _vectors(_engine(catalog))
    _sql("UPDATE vectors SET vector = ?", value)

    assert _vectors(_engine(catalog)) == expected
    assert ("junk",) not in _sql("SELECT vector FROM vectors")
    assert _vectors(_engine(catalog)) == expected


@pytest.mark.parametrize("value", ["junk", "[]", '{"fingerprint": "old", "value": []}'])
def test_unusable_saved_duplicate_pairs_are_computed_again(catalog, value):
    expected = _engine(catalog).maps.dup_groups
    _sql("UPDATE meta SET value = ? WHERE name = 'duplicate_pairs'", value)

    assert _engine(catalog).maps.dup_groups == expected


def test_a_file_that_cannot_be_written_does_not_stop_the_router(catalog):
    DATABASE.mkdir(parents=True)                     # a folder where the file belongs

    engine = _engine(catalog)

    assert engine.route("Check the health status of the production server").selected_tools


def test_a_locked_database_is_left_alone(catalog, monkeypatch):
    _engine(catalog)
    before = DATABASE.read_bytes()
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda path, timeout=5: real(path, timeout=0))
    with contextlib.closing(real(DATABASE)) as other:
        other.execute("BEGIN EXCLUSIVE")
        engine = _engine(catalog, rebuild=True)

    assert engine.tool_count == len(catalog["tools"])
    assert DATABASE.read_bytes() == before

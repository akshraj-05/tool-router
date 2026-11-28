"""Unusable saved vectors never stop the router; the file itself is never deleted.

Vectors share ``router.db`` with the registry, so a damaged file stops startup.
"""

from __future__ import annotations

import contextlib
import sqlite3
import stat
from pathlib import Path

import pytest

from tool_router.config import Settings
from tool_router.domain.errors import StateError
from tool_router.engine.pipeline import RoutingEngine
from tool_router.state import StateStore

DATABASE = Path("state/router.db")


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


@pytest.mark.parametrize("content", [b"not a database", b'{"entries": {}}', b"\xff" * 4096])
def test_a_damaged_file_is_never_replaced(catalog, content):
    _engine(catalog)
    DATABASE.write_bytes(content)

    with pytest.raises(StateError):
        _engine(catalog)

    assert DATABASE.read_bytes() == content


@pytest.mark.parametrize("statements", [
    ["DROP TABLE vectors"],
    ["DROP TABLE cache"],
    ["DROP TABLE vectors", "CREATE TABLE vectors (other TEXT)"],
    ["DROP TABLE cache", "CREATE TABLE cache (other TEXT)"],
])
def test_unusable_vector_tables_do_not_stop_the_router(catalog, statements):
    expected = _vectors(_engine(catalog))
    for statement in statements:
        _sql(statement)

    assert _vectors(_engine(catalog)) == expected
    assert _vectors(_engine(catalog)) == expected
    assert StateStore(Path("state")).load().tools          # the registry is still there


def test_vectors_that_cannot_be_saved_do_not_stop_the_router(catalog):
    _engine(catalog)
    _sql("CREATE TRIGGER full BEFORE INSERT ON vectors BEGIN SELECT RAISE(ABORT, 'full'); END")

    engine = _engine(catalog, rebuild=True)

    assert engine.route("Check the health status of the production server").selected_tools
    assert StateStore(Path("state")).load().tools


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
    _sql("UPDATE cache SET value = ? WHERE name = 'duplicate_pairs'", value)

    assert _engine(catalog).maps.dup_groups == expected


def test_a_folder_where_the_file_belongs_stops_startup(catalog):
    DATABASE.mkdir(parents=True)

    with pytest.raises(StateError):
        _engine(catalog)


def test_a_locked_database_is_left_alone(catalog, monkeypatch):
    _engine(catalog)
    before = DATABASE.read_bytes()
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda path, timeout=5: real(path, timeout=0))
    with contextlib.closing(real(DATABASE)) as other:
        other.execute("BEGIN EXCLUSIVE")
        with pytest.raises(StateError, match="could not save"):
            _engine(catalog, rebuild=True)

    assert DATABASE.read_bytes() == before

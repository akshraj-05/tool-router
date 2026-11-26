"""A damaged or unusable vector cache must never stop the router."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tool_router.config import Settings
from tool_router.engine.pipeline import RoutingEngine
from tool_router.state import StateStore


def _engine(catalog):
    return RoutingEngine(catalog, settings=Settings(), store=StateStore(Path("state")))


def _vectors(engine):
    return [entry.embedding for entry in engine._index.entries]


def _damage_every_entry(path: Path, value) -> None:
    payload = json.loads(path.read_text())
    payload["entries"] = {key: value for key in payload["entries"]}
    path.write_text(json.dumps(payload))


@pytest.mark.parametrize("content", [
    b"not json", b"", b"\xff\xfe\x00", b"[1]", b'"text"', b"null",
    b'{"entries": []}', b'{"model_stamp": "x"}',
])
def test_an_unreadable_embedding_cache_is_rebuilt(catalog, content):
    expected = _vectors(_engine(catalog))
    path = Path("state/embeddings.json")
    path.write_bytes(content)

    assert _vectors(_engine(catalog)) == expected
    assert len(json.loads(path.read_text())["entries"]) == len(catalog["tools"])


@pytest.mark.parametrize("entries", ["[]", '"text"', "5"])
def test_entries_of_the_wrong_type_are_ignored(catalog, entries):
    expected = _vectors(_engine(catalog))
    path = Path("state/embeddings.json")
    stamp = json.loads(path.read_text())["model_stamp"]
    path.write_text(f'{{"model_stamp": "{stamp}", "entries": {entries}}}')

    assert _vectors(_engine(catalog)) == expected


@pytest.mark.parametrize("value", [
    "junk", 5, None, [1.0, 2.0], {"word": "heavy"}, {"word": None},
    {"word": True}, {"word": float("nan")}, {"word": float("inf")},
])
def test_unusable_cached_vectors_are_computed_again(catalog, value):
    expected = _vectors(_engine(catalog))
    path = Path("state/embeddings.json")
    _damage_every_entry(path, value)

    assert _vectors(_engine(catalog)) == expected
    assert list(json.loads(path.read_text())["entries"].values())[0] != value

"""A cache or an optional LLM reply must never stop the router."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tool_router.config import Settings
from tool_router.engine.pipeline import RoutingEngine
from tool_router.state import StateStore

ENRICH = replace(Settings(), llm_enrich=True, llm_provider="gemini")
GOOD = '{"description": "Shows totals", "tags": ["income"], "example_queries": ["what did we earn"]}'


def _engine(catalog, **kwargs):
    kwargs.setdefault("settings", Settings())
    return RoutingEngine(catalog, store=StateStore(Path("state")), **kwargs)


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


@pytest.mark.parametrize("reply", [
    '{"tags": [1, 2]}', '{"tags": 5}', '{"tags": "income"}',
    '{"example_queries": [null]}', '{"description": ["a"]}',
])
def test_an_unusable_llm_reply_is_skipped_and_not_cached(catalog, fake_chat, reply):
    plain = _engine(catalog)._index.entries[0].search_text

    engine = _engine(catalog, settings=ENRICH, chat=fake_chat(reply))

    assert engine._index.entries[0].search_text == plain
    path = Path("state/enrichment.json")
    assert not path.exists() or json.loads(path.read_text())["entries"] == {}


def test_one_unusable_reply_does_not_stop_the_other_tools(catalog, fake_chat):
    engine = _engine(catalog, settings=ENRICH, chat=fake_chat('{"tags": 5}', GOOD))
    first, second = engine._index.entries[:2]

    assert "income" not in first.search_text
    assert "income" in second.search_text


def test_unusable_cached_enrichment_is_asked_for_again(catalog, fake_chat):
    _engine(catalog, settings=ENRICH, chat=fake_chat(GOOD))
    path = Path("state/enrichment.json")
    _damage_every_entry(path, {"tags": [1]})

    engine = _engine(catalog, settings=ENRICH, chat=fake_chat(GOOD))

    assert "income" in engine._index.entries[0].search_text
    assert list(json.loads(path.read_text())["entries"].values())[0] == json.loads(GOOD)

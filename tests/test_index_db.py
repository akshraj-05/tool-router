"""What ``vectors.db`` keeps: across outages, setting changes and restarts."""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from tool_router.config import ConfidenceThresholds, Settings
from tool_router.domain.errors import ProviderError
from tool_router.engine import derive, pipeline
from tool_router.engine.pipeline import RoutingEngine
from tool_router.providers.local_tfidf import LocalTfidfEmbedding
from tool_router.state import StateStore

DATABASE = Path("state/vectors.db")
NEW = {"name": "get_current_weather",
       "description": "Get the current weather conditions for a named city",
       "parameters": [{"name": "city"}]}
LOCAL, HOSTED = Settings(), Settings(embedding_provider="gemini")


class Hosted:
    """A hosted-style dense provider that counts its calls and can be switched off."""

    stamp = "hosted:1"

    def __init__(self) -> None:
        self.down = False
        self.embedded = 0

    def fit_documents(self, texts):
        return None

    def _vector(self, text):
        if self.down:
            raise ProviderError("simulated outage")
        return [1.0, len(text) / 7, 0.1 + 0.2]

    def embed_documents(self, texts):
        vectors = [self._vector(t) for t in texts]
        self.embedded += len(texts)
        return vectors

    def embed_query(self, text):
        return self._vector(text)


@pytest.fixture
def hosted(monkeypatch):
    provider = Hosted()
    real = pipeline.make_embedding_provider
    monkeypatch.setattr(pipeline, "make_embedding_provider",
                        lambda name: provider if name == "gemini" else real(name))
    return provider


@pytest.fixture
def pair_runs(monkeypatch):
    """How many times the slow duplicate comparison actually ran."""
    runs = []

    def counted(*args):
        runs.append(1)
        return derive.find_duplicate_pairs(*args)

    monkeypatch.setattr(pipeline, "find_duplicate_pairs", counted)
    return runs


def build(catalog, settings=LOCAL, previous=False):
    store = StateStore(Path("state"))
    if previous:
        state = store.load()
        catalog = {"tools": state.tools, "near_duplicate_groups": state.near_duplicate_groups}
        return RoutingEngine(catalog, settings=settings, store=store, previous=state)
    return RoutingEngine(catalog, settings=settings, store=store)


def contents():
    with contextlib.closing(sqlite3.connect(DATABASE)) as db:
        return (sorted(db.execute("SELECT hash, vector FROM vectors").fetchall()),
                dict(db.execute("SELECT name, value FROM meta").fetchall()))


def test_hosted_vectors_are_reused_after_a_restart(catalog, hosted):
    first = build(catalog, HOSTED)
    hosted.embedded = 0

    again = build(catalog, HOSTED, previous=True)

    assert hosted.embedded == 0
    assert [e.embedding for e in again._index.entries] == [e.embedding for e in first._index.entries]


def test_only_a_new_tool_is_sent_to_the_provider(catalog, hosted):
    engine = build(catalog, HOSTED)
    hosted.embedded = 0

    engine.create_tool(NEW)

    assert hosted.embedded == 1
    assert len(contents()[0]) == len(catalog["tools"]) + 1


def test_an_outage_during_a_change_keeps_the_saved_vectors(catalog, hosted):
    engine = build(catalog, HOSTED)
    before = contents()

    hosted.down = True
    engine.create_tool(NEW)
    assert contents() == before

    hosted.down, hosted.embedded = False, 0
    engine.delete_tool("comm.send_email")
    assert hosted.embedded == 1                      # only the tool added during the outage
    assert contents()[1]["model_stamp"] == "hosted:1"


def test_an_outage_at_startup_keeps_the_saved_vectors(catalog, hosted):
    build(catalog, HOSTED)
    before = contents()
    hosted.down = True

    build({"tools": [*catalog["tools"], NEW]}, HOSTED)

    assert contents() == before


def test_a_missing_key_keeps_the_saved_vectors(catalog, hosted, monkeypatch):
    build(catalog, HOSTED)
    before = contents()
    # What the factory does without an API key: it hands back the local provider.
    monkeypatch.setattr(pipeline, "make_embedding_provider", lambda name: LocalTfidfEmbedding())

    build(catalog, HOSTED)

    assert contents() == before


def test_changing_the_setting_replaces_the_vectors(catalog, hosted):
    build(catalog, HOSTED)
    build(catalog, LOCAL, previous=True)
    assert contents()[1]["model_stamp"].startswith("local-tfidf:")
    assert all(isinstance(vector, str) for _, vector in contents()[0])

    hosted.embedded = 0
    build(catalog, HOSTED, previous=True)
    assert hosted.embedded == len(catalog["tools"])
    assert all(isinstance(vector, bytes) for _, vector in contents()[0])


def test_duplicate_pairs_are_reused_after_a_restart(catalog, pair_runs):
    first = build(catalog)

    again = build(catalog, previous=True)

    assert len(pair_runs) == 1
    assert again.maps.dup_groups == first.maps.dup_groups


@pytest.mark.parametrize("change", [
    {"description": "Fetch the server health report for one named machine"},
    {"side_effects": "destructive"},
    {"cluster": "other"},
    {"parameters": [{"name": "host", "type": "string", "required": True, "description": "Host"}]},
])
def test_duplicate_pairs_are_recomputed_when_a_tool_changes(catalog, pair_runs, change):
    build(catalog)
    catalog["tools"][-1].update(change)

    build(catalog)

    assert len(pair_runs) == 2


def test_duplicate_pairs_are_recomputed_when_a_threshold_changes(catalog, pair_runs):
    build(catalog)

    build(catalog, replace(LOCAL, confidence=ConfidenceThresholds(dup_similarity=0.9)))

    assert len(pair_runs) == 2


def test_duplicate_pairs_are_recomputed_when_the_rules_change(catalog, pair_runs, monkeypatch):
    build(catalog)
    monkeypatch.setattr(derive, "DUPLICATE_RULES_VERSION", derive.DUPLICATE_RULES_VERSION + 1)

    build(catalog)

    assert len(pair_runs) == 2


def test_rebuild_recomputes_everything(catalog, hosted, pair_runs):
    build(catalog, HOSTED)
    hosted.embedded = 0

    build(catalog, replace(HOSTED, rebuild=True))

    assert hosted.embedded == len(catalog["tools"]) and len(pair_runs) == 2


def test_nothing_is_written_without_a_store(catalog):
    RoutingEngine(catalog, settings=LOCAL)

    assert not list(Path.cwd().rglob("vectors.db"))

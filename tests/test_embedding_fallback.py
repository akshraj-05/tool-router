"""A failing hosted embedding provider must never crash routing or the index."""

from __future__ import annotations

import pytest

from tool_router.config import Settings
from tool_router.domain.errors import ProviderError
from tool_router.engine import pipeline
from tool_router.engine.pipeline import RoutingEngine
from tool_router.providers import gemini

QUERY = "Check the health status of the production server"
NEW = {"name": "get_current_weather",
       "description": "Get the current weather conditions for a named city",
       "parameters": [{"name": "city"}]}


class FlakyEmbedding:
    """A hosted-style dense provider that can be switched off."""

    stamp = "flaky:1"

    def __init__(self) -> None:
        self.down = False
        self.query_calls = 0

    def fit_documents(self, texts):
        return None

    def _vector(self, text):
        if self.down:
            raise ProviderError("simulated outage")
        return [1.0, float(len(text) % 5)]

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return self._vector(text)


@pytest.fixture
def flaky(monkeypatch):
    provider = FlakyEmbedding()
    real = pipeline.make_embedding_provider
    monkeypatch.setattr(pipeline, "make_embedding_provider",
                        lambda name: provider if name == "gemini" else real(name))
    return provider


def build(catalog) -> RoutingEngine:
    return RoutingEngine(catalog, settings=Settings(embedding_provider="gemini"))


def test_outage_during_a_query_falls_back_to_local(catalog, flaky):
    engine = build(catalog)
    assert "embedding_fallback" not in engine.route(QUERY).trace
    flaky.down = True
    result = engine.route(QUERY)
    assert result.selected_tools == ["ops.get_server_status"]
    assert result.trace["embedding_fallback"] is True


def test_the_provider_is_used_again_after_it_recovers(catalog, flaky):
    engine = build(catalog)
    flaky.down = True
    engine.route(QUERY)
    flaky.down = False
    assert "embedding_fallback" not in engine.route(QUERY).trace


def test_a_broken_provider_is_tried_only_once_per_request(catalog, flaky):
    engine = build(catalog)
    flaky.down, flaky.query_calls = True, 0
    result = engine.route("Generate a chart from the data, then send an email with the subject and body")
    assert result.plan == ["docs.generate_chart", "comm.send_email"]    # multi-step: several scorings
    assert flaky.query_calls == 1


def test_outage_at_startup_builds_a_local_index(catalog, flaky):
    flaky.down = True
    engine = build(catalog)
    assert engine.route(QUERY).selected_tools == ["ops.get_server_status"]


def test_outage_during_a_tool_change_still_applies_it(catalog, flaky):
    engine = build(catalog)
    flaky.down = True
    engine.create_tool(NEW)
    result = engine.route("What is the current weather in Berlin?")
    assert "general.get_current_weather" in result.selected_tools


def test_local_embeddings_need_no_fallback(engine):
    assert engine._index.fallback is None


@pytest.mark.parametrize("reply", [{}, {"embedding": None}, {"embedding": {"values": []}},
                                   {"embedding": {"values": ["a"]}}])
def test_malformed_gemini_reply_is_a_provider_error(reply, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(gemini, "_post", lambda url, payload: reply)
    with pytest.raises(ProviderError):
        gemini.GeminiEmbedding().embed_query("hello")


def test_real_gemini_class_outage_is_survived(catalog, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    state = {"down": False}

    def post(url, payload):
        if state["down"]:
            raise ProviderError("HTTP 503")
        return {"embedding": {"values": [1.0, 0.5]}}

    monkeypatch.setattr(gemini, "_post", post)
    engine = build(catalog)
    state["down"] = True
    assert engine.route(QUERY).selected_tools == ["ops.get_server_status"]

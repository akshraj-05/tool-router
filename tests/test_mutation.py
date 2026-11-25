"""Catalog churn: add, remove, reject, and consistency while routing continues."""

from __future__ import annotations

import threading

import pytest

NEW_TOOL = {
    "id": "weather.get_current_weather", "name": "get_current_weather", "cluster": "weather",
    "description": "Get the current weather conditions for a named city",
    "side_effects": "read",
    "parameters": [{"name": "city", "type": "string", "required": True}],
    "returns": {"type": "object"},
}


def test_added_tool_is_routable_immediately(engine):
    before = engine.tool_count
    outcome = engine.apply_mutation({"add": [NEW_TOOL]})
    assert outcome["added"] == [NEW_TOOL["id"]]
    assert engine.tool_count == before + 1
    result = engine.route("What is the current weather in Berlin?")
    assert NEW_TOOL["id"] in result.selected_tools


def test_removed_tool_is_never_returned(engine):
    engine.apply_mutation({"remove": ["ops.get_server_status"]})
    result = engine.route("Check the health status of the production server")
    assert "ops.get_server_status" not in result.selected_tools


def test_unknown_removals_are_reported_not_errors(engine):
    outcome = engine.apply_mutation({"remove": ["nope.nothing"]})
    assert outcome["unknown_remove_ids"] == ["nope.nothing"]


def test_invalid_submissions_are_rejected_and_not_indexed(engine):
    bad = {**NEW_TOOL, "id": "Weather-Now", "description": "short"}
    before = engine.tool_count
    outcome = engine.apply_mutation({"add": [bad]})
    assert outcome["added"] == [] and len(outcome["rejected"]) == 1
    assert engine.tool_count == before


def test_duplicate_id_is_rejected(engine):
    outcome = engine.apply_mutation({"add": [NEW_TOOL]})
    again = engine.apply_mutation({"add": [NEW_TOOL]})
    assert outcome["added"] and again["rejected"][0]["reason"] == "DUPLICATE_ID"


def test_malformed_delta_raises(engine):
    from tool_router.domain.errors import CatalogError

    with pytest.raises(CatalogError):
        engine.apply_mutation("not a dict")
    with pytest.raises(CatalogError):
        engine.apply_mutation({"add": "nope"})


def test_new_state_is_invisible_until_the_rebuild_finishes(engine, monkeypatch):
    """Readers must see the complete old index or the complete new one, never a mix.

    The rebuild is paused part-way through. While paused, the engine must still
    answer from the old index; once released, it must switch to the new one.
    """
    from tool_router.engine import pipeline

    started, release = threading.Event(), threading.Event()
    real_derive = pipeline.derive_maps

    def slow_derive(*args, **kwargs):
        started.set()
        assert release.wait(timeout=10)
        return real_derive(*args, **kwargs)

    monkeypatch.setattr(pipeline, "derive_maps", slow_derive)
    old_count = engine.tool_count
    worker = threading.Thread(target=engine.apply_mutation, args=({"add": [NEW_TOOL]},))
    worker.start()
    try:
        assert started.wait(timeout=10)
        assert engine.tool_count == old_count                       # still the old index
        during = engine.route("Check the health status of the production server")
        assert during.selected_tools == ["ops.get_server_status"]   # and it still answers
    finally:
        release.set()
        worker.join(timeout=10)
    assert engine.tool_count == old_count + 1


def test_concurrent_routing_and_mutation_never_error(engine):
    errors: list[Exception] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                engine.route("Check the health status of the production server")
            except Exception as exc:          # pragma: no cover - the failure we look for
                errors.append(exc)
                return

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    try:
        for _ in range(6):
            engine.apply_mutation({"add": [NEW_TOOL]})
            engine.apply_mutation({"remove": [NEW_TOOL["id"]]})
    finally:
        stop.set()
        for t in readers:
            t.join(timeout=10)
    assert not errors, errors[:1]

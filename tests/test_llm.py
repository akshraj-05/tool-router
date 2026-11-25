"""LLM-assisted paths, driven by a scripted fake chat (no network, no keys)."""

from __future__ import annotations

import json

import pytest

from tool_router.config import ConfidenceThresholds, Settings
from tool_router.domain.errors import ProviderError
from tool_router.engine.pipeline import RoutingEngine
from tool_router.providers.base import extract_json_object

QUERY = "Check the health status of the production server"
VAGUE = "is everything okay over there right now"


def plan(*tool_ids, **flags) -> str:
    body = {"plan": [{"tool_id": t} for t in tool_ids], "clarify": False,
            "clarify_question": None, "insufficient": False,
            "needed_capability": None, "out_of_scope": False}
    body.update(flags)
    return json.dumps(body)


def make(catalog, chat, **settings) -> RoutingEngine:
    return RoutingEngine(catalog, settings=Settings(**settings), chat=chat)


# ---------------------------------------------------------------- planner


def test_llm_plan_is_used_when_valid(catalog, fake_chat):
    chat = fake_chat(plan("ops.get_server_status"))
    result = make(catalog, chat).route(QUERY)
    assert result.plan == ["ops.get_server_status"]
    assert len(chat.calls) == 1


def test_llm_cannot_invent_tools(catalog, fake_chat):
    chat = fake_chat(plan("made.up_tool", "ops.get_server_status"))
    result = make(catalog, chat).route(QUERY)
    assert result.selected_tools == ["ops.get_server_status"]


def test_llm_failure_falls_back_to_local_planner(catalog, fake_chat):
    chat = fake_chat(ProviderError("rate limited"))
    result = make(catalog, chat).route(QUERY)
    assert result.selected_tools == ["ops.get_server_status"]


def test_unparseable_reply_falls_back_to_local_planner(catalog, fake_chat):
    chat = fake_chat("Sure! I think you want the server tool.")
    result = make(catalog, chat).route(QUERY)
    assert result.selected_tools == ["ops.get_server_status"]


def test_empty_plan_without_a_reason_falls_back(catalog, fake_chat):
    chat = fake_chat(plan())
    result = make(catalog, chat).route(QUERY)
    assert result.selected_tools == ["ops.get_server_status"]


def test_out_of_scope_verdict_is_honoured(catalog, fake_chat):
    chat = fake_chat(plan(out_of_scope=True))
    result = make(catalog, chat).route(QUERY)
    assert result.selected_tools == [] and not result.clarify
    assert result.trace["out_of_scope"] is True


def test_widening_is_bounded(catalog, fake_chat):
    chat = fake_chat(plan(insufficient=True, needed_capability="email"))
    make(catalog, chat).route(QUERY)
    assert len(chat.calls) <= 2          # one plan + at most one widened re-plan


# ------------------------------------------------------------ arbitration


def test_confident_local_match_overrides_an_llm_clarify(catalog, fake_chat):
    """A weak model that over-refuses must not block a high-confidence query."""
    chat = fake_chat(plan(clarify=True, clarify_question="Which server?"))
    result = make(catalog, chat).route(QUERY)
    top = result.trace["top_candidates"][0]["fused"]
    assert top >= 0.45                              # the premise of this test
    assert not result.clarify
    assert result.selected_tools == ["ops.get_server_status"]


def test_arbitration_threshold_is_configurable(catalog, fake_chat):
    chat = fake_chat(plan(clarify=True, clarify_question="Which server?"))
    strict = ConfidenceThresholds(arbitration_min_fused=2.0)     # never override
    result = make(catalog, chat, confidence=strict).route(QUERY)
    assert result.clarify and result.clarify_question == "Which server?"


# ------------------------------------------------------------------- HyDE


def test_vague_query_is_empty_offline(engine):
    assert engine.route(VAGUE).selected_tools == []


def test_hyde_rescues_a_vague_in_scope_query(catalog, fake_chat):
    hypothetical = "A tool that checks the health and uptime status of a production server."
    chat = fake_chat(hypothetical, plan("ops.get_server_status"))
    result = make(catalog, chat).route(VAGUE)
    assert result.trace["hyde"]["fired"] is True
    assert result.selected_tools == ["ops.get_server_status"]


def test_hyde_failure_never_crashes_routing(catalog, fake_chat):
    chat = fake_chat(ProviderError("down"))
    result = make(catalog, chat).route(VAGUE)
    assert result.selected_tools == []
    assert result.trace["hyde"]["fired"] is False


def test_hyde_does_not_fire_when_already_confident(catalog, fake_chat):
    chat = fake_chat(plan("ops.get_server_status"))
    result = make(catalog, chat).route(QUERY)
    assert result.trace["hyde"] == {"fired": False}


def test_hyde_can_fire_below_the_out_of_scope_floor(catalog, fake_chat):
    """Documented behaviour: HyDE has no lower bound tied to the floor."""
    chat = fake_chat("A tool that checks server health.", plan(out_of_scope=True))
    result = make(catalog, chat).route(VAGUE)
    assert result.trace["hyde"]["fired"] is True


# ------------------------------------------------------------ JSON parsing


def test_extract_json_object_handles_fences_and_trailing_prose():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('{"a": 1} and then some prose') == {"a": 1}
    assert extract_json_object('{"a": {"b": "}"}} {"c": 2}') == {"a": {"b": "}"}}


def test_extract_json_object_rejects_garbage():
    for bad in ("no json here", '{"a": 1', "{not json}"):
        with pytest.raises(ProviderError):
            extract_json_object(bad)

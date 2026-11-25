"""Offline routing behaviour (no LLM): retrieval, gate, dedup, planner."""

from __future__ import annotations


def test_single_tool_is_selected(engine):
    result = engine.route("Check the health status of the production server")
    assert result.selected_tools == ["ops.get_server_status"]
    assert not result.clarify


def test_out_of_scope_query_returns_nothing(engine):
    result = engine.route("What is the capital of France?")
    assert result.selected_tools == [] and result.plan == []
    assert not result.clarify
    assert result.trace["decision"] == "empty"


def test_empty_query_asks_for_clarification(engine):
    result = engine.route("   ")
    assert result.clarify and result.clarify_question


def test_sweeping_destructive_request_asks_before_acting(engine):
    result = engine.route("Delete all the stored log files permanently")
    assert result.clarify
    assert result.selected_tools == []


def test_destructive_request_without_target_asks_which_one(engine):
    result = engine.route("Restart the server")
    assert result.clarify


def test_destructive_request_with_target_is_routed(engine):
    result = engine.route("Restart the server named Atlas")
    assert "ops.restart_server" in result.selected_tools
    assert not result.clarify


def test_current_version_preferred_over_deprecated(engine):
    result = engine.route("Create an invoice for the customer Acme")
    assert "docs.create_invoice_v2" in result.selected_tools
    assert "docs.create_invoice" not in result.selected_tools


def test_explicit_legacy_signal_selects_the_old_version(engine):
    result = engine.route("Create an invoice for the customer Acme using the legacy format")
    assert "docs.create_invoice" in result.selected_tools
    assert "docs.create_invoice_v2" not in result.selected_tools


def test_never_returns_two_versions_of_the_same_tool(engine):
    for query in ("Create an invoice for Acme", "create a legacy invoice for Acme"):
        selected = set(engine.route(query).selected_tools)
        assert not {"docs.create_invoice", "docs.create_invoice_v2"} <= selected


def test_near_duplicates_collapse_to_one_tool(engine):
    result = engine.route("What was our total revenue last quarter?")
    fin = [t for t in result.selected_tools if t.startswith("fin.")]
    assert len(fin) == 1


def test_enumeration_repeats_the_tool_once_per_recipient(engine):
    result = engine.route("Send an email to Dana, Omar, and Lin")
    assert result.plan.count("comm.send_email") == 3
    assert result.selected_tools.count("comm.send_email") == 1   # selection is unique


def test_multi_step_request_is_ordered(engine):
    result = engine.route("Generate a chart from the data, then send an email with the subject and body")
    plan = result.plan
    assert "docs.generate_chart" in plan and "comm.send_email" in plan
    assert plan.index("docs.generate_chart") < plan.index("comm.send_email")


def test_result_reports_token_savings_and_trace(engine):
    result = engine.route("Check the health status of the production server")
    assert 0 < result.tokens_saved_pct < 1
    assert result.trace["top_candidates"]
    assert result.trace["decision"] == "proceed"


def test_routing_ignores_catalog_order(catalog):
    """Positional bias: shuffling the catalog must not change the answer."""
    from tool_router.config import Settings
    from tool_router.engine.pipeline import RoutingEngine

    query = "Check the health status of the production server"
    forward = RoutingEngine(catalog, settings=Settings()).route(query).selected_tools
    catalog["tools"].reverse()
    backward = RoutingEngine(catalog, settings=Settings()).route(query).selected_tools
    assert forward == backward


def test_original_schema_is_returned_untouched(engine, catalog):
    original = next(t for t in catalog["tools"] if t["id"] == "ops.get_server_status")
    assert engine.raw_tool("ops.get_server_status") == original


def test_catalog_without_tools_list_is_rejected():
    import pytest
    from tool_router.domain.errors import CatalogError
    from tool_router.engine.pipeline import RoutingEngine

    with pytest.raises(CatalogError):
        RoutingEngine({"tools": "nope"})

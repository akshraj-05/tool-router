"""Version families come only from explicit `replaced_by` links, never from names."""

from __future__ import annotations

from tool_router.config import Settings
from tool_router.engine.pipeline import RoutingEngine


def tool(tool_id: str, name: str, **extra) -> dict:
    return {"id": tool_id, "name": name, "cluster": "docs", "side_effects": "write",
            "description": "Create an invoice for a customer in the billing system",
            "parameters": [{"name": "customer", "type": "string"}], **extra}


def build(*tools) -> RoutingEngine:
    return RoutingEngine({"tools": list(tools)}, settings=Settings())


def test_replaced_by_links_the_pair_and_prefers_the_new_one():
    engine = build(tool("docs.a", "create_invoice", deprecated=True, replaced_by="docs.b"),
                   tool("docs.b", "create_invoice"))
    assert engine.maps.version_families == {"docs.b": ["docs.a", "docs.b"]}
    assert engine.route("Create an invoice for a customer").selected_tools == ["docs.b"]
    assert engine.route("Create a legacy invoice for a customer").selected_tools == ["docs.a"]


def test_a_chain_of_replacements_is_one_family():
    engine = build(tool("docs.a", "invoice_one", replaced_by="docs.b"),
                   tool("docs.b", "invoice_two", replaced_by="docs.c"),
                   tool("docs.c", "invoice_three"))
    assert engine.maps.version_families == {"docs.c": ["docs.a", "docs.b", "docs.c"]}


def test_a_version_suffix_in_the_name_links_nothing():
    engine = build(tool("docs.create_invoice", "create_invoice"),
                   tool("docs.create_invoice_v2", "create_invoice_v2"))
    assert engine.maps.version_families == {}


def test_sharing_a_name_links_nothing():
    engine = build(tool("docs.a", "create_invoice"), tool("docs.b", "create_invoice"))
    assert engine.maps.version_families == {}


def test_the_version_field_links_nothing():
    engine = build(tool("docs.a", "make_invoice", version="1.0.0"),
                   tool("docs.b", "issue_invoice", version="2.0.0"))
    assert engine.maps.version_families == {}


def test_a_removed_replaces_field_is_ignored():
    engine = build(tool("docs.a", "make_invoice"),
                   tool("docs.b", "issue_invoice", replaces="docs.a"))
    assert engine.maps.version_families == {}


def test_names_are_returned_exactly_as_registered():
    engine = build(tool("docs.x", "create_invoice_v2"))
    assert engine.get_tool("docs.x")["name"] == "create_invoice_v2"

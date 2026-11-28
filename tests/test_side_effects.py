"""side_effects: inferred when omitted, validated when given, kept across updates."""

from __future__ import annotations

import pytest

from toolspotter.config import Settings
from toolspotter.domain.errors import CatalogError, InvalidToolError
from toolspotter.domain.naming import infer_side_effects, prepare_tool
from toolspotter.engine.pipeline import RoutingEngine


def tool(name: str, **extra) -> dict:
    return {"name": name, "description": f"A sample tool used to test the {name} behaviour",
            "parameters": [], **extra}


@pytest.mark.parametrize("name, expected", [
    ("delete_user", "destructive"),
    ("server_restart", "destructive"),        # destructive verb anywhere in the name
    ("get_cancel_policy", "destructive"),     # cautious: the stricter answer wins
    ("deleteUser", "destructive"),
    ("get_invoice", "read"),
    ("list_users", "read"),
    ("send_email", "write"),
    ("close_account", "write"),               # known limit: no listed verb
    ("order_search", "write"),                # a read verb counts only as the first word
    ("getting_started", "write"),
])
def test_inference_from_the_name(name, expected):
    assert infer_side_effects(name) == expected


def test_missing_value_is_filled_in():
    assert prepare_tool(tool("delete_user"))["side_effects"] == "destructive"
    assert prepare_tool(tool("delete_user", side_effects=""))["side_effects"] == "destructive"


def test_a_declared_value_is_never_overridden():
    assert prepare_tool(tool("delete_draft", side_effects="write"))["side_effects"] == "write"


def test_an_inferred_destructive_tool_triggers_the_safety_question():
    engine = RoutingEngine({"tools": [tool("restart_server")]}, settings=Settings())
    assert engine.route("Restart the server").clarify


@pytest.mark.parametrize("value", ["Destructive", "delete", "READ", "none"])
def test_misspelled_value_refuses_the_catalog(value):
    with pytest.raises(CatalogError, match="INVALID_SIDE_EFFECTS"):
        RoutingEngine({"tools": [tool("delete_user", side_effects=value)]}, settings=Settings())


def test_misspelled_value_is_rejected_on_create(engine):
    with pytest.raises(InvalidToolError) as caught:
        engine.create_tool(tool("archive_records", side_effects="Write"))
    assert caught.value.reason == "INVALID_SIDE_EFFECTS"


def test_update_without_the_field_keeps_the_declared_value(engine):
    engine.create_tool(tool("close_account", side_effects="destructive"))
    updated = engine.update_tool("general.close_account", tool("close_account"))
    assert updated["side_effects"] == "destructive"


def test_replacement_in_a_mutation_keeps_the_declared_value(engine):
    engine.create_tool(tool("close_account", side_effects="destructive"))
    engine.apply_mutation({"remove": ["general.close_account"], "add": [tool("close_account")]})
    assert engine.get_tool("general.close_account")["side_effects"] == "destructive"

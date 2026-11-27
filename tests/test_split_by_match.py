"""The local planner also splits where each side of "and" matches a different tool."""

from __future__ import annotations

import pytest

from tool_router.config import Settings
from tool_router.engine.pipeline import RoutingEngine


def _tool(name, description, effect="read", cluster="general"):
    return {"id": f"{cluster}.{name}", "name": name, "cluster": cluster, "side_effects": effect,
            "description": description, "parameters": []}


@pytest.fixture
def small():
    return RoutingEngine({"tools": [
        _tool("get_sales_report", "Fetch the quarterly sales report for the company"),
        _tool("send_message", "Deliver an email message to a chosen colleague", "write"),
        _tool("wipe_backups", "Erase old backup archives from storage permanently", "destructive"),
    ]}, settings=Settings())


def test_a_second_task_without_a_tool_verb_is_found(small):
    result = small.route("get the quarterly sales report and email a message to my colleague")

    assert result.plan == ["general.get_sales_report", "general.send_message"]


def test_a_list_of_names_is_not_split(small):
    planner = small.planner
    score = lambda text: small._score_and_collapse(text)[0]  # noqa: E731

    assert planner._split_where_tools_differ("email a message to Dana, Omar and Lin", score) == [
        "email a message to Dana, Omar and Lin"
    ]


def test_two_halves_of_one_task_are_not_split(small):
    score = lambda text: small._score_and_collapse(text)[0]  # noqa: E731

    text = "fetch the sales report and the quarterly company report"
    assert small.planner._split_where_tools_differ(text, score) == [text]


def test_a_destructive_side_is_not_split_off(small):
    score = lambda text: small._score_and_collapse(text)[0]  # noqa: E731

    text = "get the quarterly sales report and old backup archives from storage"
    assert small.planner._split_where_tools_differ(text, score) == [text]

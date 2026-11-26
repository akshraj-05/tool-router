"""Automatic near-duplicate detection, and the similarity helper it relies on."""

from __future__ import annotations

import pytest

from tool_router.config import ConfidenceThresholds
from tool_router.engine.derive import derive_maps
from tool_router.engine.ingest import ingest_tools
from tool_router.engine.text import cosine, norm

# Different sizes, so cosine() swaps its arguments in one of the two orders.
SPARSE = ({"a": 1.0, "b": 2.0, "c": 3.0}, {"b": 5.0})
DENSE = ([1.0, 2.0, 3.0], [0.0, 5.0, 1.0])


@pytest.mark.parametrize("pair", [SPARSE, SPARSE[::-1], DENSE])
def test_precomputed_lengths_give_the_same_similarity(pair):
    a, b = pair
    assert cosine(a, b, norm(a), norm(b)) == cosine(a, b)


@pytest.mark.parametrize("empty", [None, {}, []])
def test_an_empty_vector_has_no_length(empty):
    assert norm(empty) == 0.0


def _entry_tools(*parameter_names):
    tools = [
        {
            "id": f"crm.tool_{i}", "name": f"tool_{i}", "cluster": "crm", "side_effects": "read",
            "description": "Look up the customer record in the system",
            "parameters": [{"name": n, "type": "string", "description": ""} for n in names],
        }
        for i, names in enumerate(parameter_names)
    ]
    entries = ingest_tools(tools)
    for entry in entries:
        entry.embedding = [1.0, 0.0]          # identical text as far as vectors go
    return entries


def _groups(entries):
    return derive_maps(entries, {}, ConfidenceThresholds()).dup_groups


def test_similar_tools_sharing_parameters_are_grouped():
    assert _groups(_entry_tools(["email"], ["email"])) == [["crm.tool_0", "crm.tool_1"]]


@pytest.mark.parametrize("names", [(["email"], ["phone"]), ([], [])])
def test_similar_tools_without_shared_parameters_are_not_grouped(names):
    assert _groups(_entry_tools(*names)) == []


def test_shared_parameters_are_not_enough_when_vectors_differ():
    entries = _entry_tools(["email"], ["email"])
    entries[1].embedding = [0.0, 1.0]
    assert _groups(entries) == []

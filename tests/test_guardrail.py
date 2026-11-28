"""Registry intake guardrail: each rule rejects with its own stable reason code."""

from __future__ import annotations

import copy
import json

import pytest

from toolspotter.engine.guardrail import validate_submission

VALID = {
    "id": "demo.fetch_widget", "name": "fetch_widget", "cluster": "demo",
    "description": "Fetch a single widget record by its identifier",
    "side_effects": "read",
    "parameters": [{"name": "widget_id", "type": "string", "required": True}],
}


def _with(**changes):
    tool = copy.deepcopy(VALID)
    tool.update(changes)
    return tool


def test_valid_submission_is_accepted():
    result = validate_submission(VALID, known_ids=set())
    assert result.accepted and not result.issues


@pytest.mark.parametrize("submission, code", [
    (_with(id="Demo-Widget"), "BAD_ID_FORMAT"),
    (_with(name="Fetch Widget"), "BAD_NAME_FORMAT"),
    (_with(description="Too short"), "DESCRIPTION_TOO_VAGUE"),
    (_with(description="word " * 200), "DESCRIPTION_TOO_LONG"),
    (_with(parameters=[{"name": f"p{i}", "type": "string"} for i in range(13)]), "TOO_MANY_PARAMETERS"),
    (_with(parameters=[{"name": "x", "type": "blob"}]), "INVALID_PARAM_TYPE"),
    (_with(version="v2"), "BAD_VERSION_FORMAT"),
    (_with(deprecated=True, replaced_by="demo.nothing_here"), "DANGLING_REPLACED_BY"),
])
def test_each_rule_reports_its_reason_code(submission, code):
    result = validate_submission(submission, known_ids=set())
    assert not result.accepted
    assert code in [issue.code for issue in result.issues]


def test_missing_required_field_is_reported():
    tool = copy.deepcopy(VALID)
    del tool["parameters"]
    result = validate_submission(tool, known_ids=set())
    assert result.primary_reason == "MISSING_REQUIRED_FIELD"


def test_duplicate_id_is_rejected():
    result = validate_submission(VALID, known_ids={"demo.fetch_widget"})
    assert result.primary_reason == "DUPLICATE_ID"


@pytest.mark.parametrize("submission", ["{not json", json.dumps(VALID), [1, 2], 5, None])
def test_only_a_json_object_is_a_tool(submission):
    result = validate_submission(submission, known_ids=set())
    assert result.primary_reason == "MALFORMED_JSON_STRUCTURE"


def test_circular_replacement_chain_is_detected():
    a = _with(id="demo.tool_a", name="tool_a", replaced_by="demo.tool_b")
    b = _with(id="demo.tool_b", name="tool_b", replaced_by="demo.tool_a")
    result = validate_submission(a, known_ids={"demo.tool_b"},
                                 known_tools={"demo.tool_b": b})
    assert "CIRCULAR_VERSION_REF" in [i.code for i in result.issues]


def test_primary_reason_is_highest_precedence_failure():
    tool = _with(id="BAD", description="short")
    result = validate_submission(tool, known_ids=set())
    assert result.primary_reason == "BAD_ID_FORMAT"      # id check precedes description
    assert len(result.issues) >= 2                        # but every failure is collected

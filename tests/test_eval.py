"""The evaluation data stays well formed, and the scoring does what it says.

No accuracy threshold is asserted here: run ``python eval/run.py`` for scores.
"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from tool_router.domain.models import RouteResult
from tool_router.engine.guardrail import validate_catalog

_spec = importlib.util.spec_from_file_location(
    "eval_run", Path(__file__).parent.parent / "eval" / "run.py"
)
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)

CATALOG, REQUESTS = run.load()


def test_every_tool_passes_the_guardrail():
    assert [r.issues for r in validate_catalog(CATALOG["tools"]) if not r.accepted] == []


def test_request_ids_and_texts_are_unique():
    for field in ("id", "query"):
        assert [v for v, n in Counter(r[field] for r in REQUESTS).items() if n > 1] == []


def test_every_request_is_well_formed():
    known = {t["id"] for t in CATALOG["tools"]}
    for request in REQUESTS:
        assert set(request) == {"id", "type", "split", "query", "expect"}, request
        assert request["split"] in run.SPLITS, request
        expect = request["expect"]
        if "tools" in expect:
            assert expect["tools"] and set(expect["tools"]) <= known, request
        else:
            assert expect["outcome"] in ("clarify", "none"), request


def test_both_halves_cover_every_type_about_equally():
    counts = {s: Counter(r["type"] for r in REQUESTS if r["split"] == s) for s in run.SPLITS}
    assert set(counts["dev"]) == set(counts["final"])
    for kind, number in counts["dev"].items():
        assert abs(number - counts["final"][kind]) <= 2, kind


@pytest.mark.parametrize("expect, result, ok", [
    ({"tools": ["a", "b"]}, RouteResult(plan=["a", "b"]), True),
    ({"tools": ["a", "b"]}, RouteResult(plan=["b", "a"]), False),
    ({"tools": ["a"]}, RouteResult(plan=["a", "b"]), False),
    ({"tools": ["a", "a"]}, RouteResult(plan=["a"]), False),
    ({"tools": ["a"]}, RouteResult(clarify=True), False),
    ({"outcome": "clarify"}, RouteResult(clarify=True), True),
    ({"outcome": "clarify"}, RouteResult(), False),
    ({"outcome": "none"}, RouteResult(), True),
    ({"outcome": "none"}, RouteResult(clarify=True), False),
    ({"outcome": "none"}, RouteResult(plan=["a"]), False),
])
def test_scoring(expect, result, ok):
    assert run.passed(expect, result) is ok


def test_final_failures_are_not_listed(capsys):
    run.main(["--split", "final"])
    output = capsys.readouterr().out
    assert "TOTAL" in output and "failures" not in output

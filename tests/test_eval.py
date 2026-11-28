"""The evaluation data stays well formed, and the scoring does what it says.

No accuracy threshold is asserted here: run ``python eval/run.py`` for scores.
"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from toolspotter.domain.errors import ProviderError
from toolspotter.domain.models import RouteResult
from toolspotter.engine.guardrail import validate_catalog
from toolspotter.providers import gemini

_spec = importlib.util.spec_from_file_location(
    "eval_run", Path(__file__).parent.parent / "eval" / "run.py"
)
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)


@pytest.fixture(params=sorted(run.SETS))
def data(request):
    return run.load(request.param)


def test_every_tool_passes_the_guardrail(data):
    catalog, _ = data
    assert [r.issues for r in validate_catalog(catalog["tools"]) if not r.accepted] == []


def test_request_ids_and_texts_are_unique(data):
    _, requests = data
    for field in ("id", "query"):
        assert [v for v, n in Counter(r[field] for r in requests).items() if n > 1] == []


def test_every_request_is_well_formed(data):
    catalog, requests = data
    known = {t["id"] for t in catalog["tools"]}
    for request in requests:
        assert set(request) == {"id", "type", "split", "query", "expect"}, request
        assert request["split"] in run.SPLITS, request
        expect = request["expect"]
        if "tools" in expect:
            assert expect["tools"] and set(expect["tools"]) <= known, request
        else:
            assert expect["outcome"] in ("clarify", "none"), request


def test_both_halves_cover_every_type_about_equally(data):
    _, requests = data
    counts = {s: Counter(r["type"] for r in requests if r["split"] == s) for s in run.SPLITS}
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


@pytest.mark.parametrize("options", [[], ["--set", "playwright"], ["--set", "custom"], ["--baseline"]])
def test_final_failures_are_not_listed(options, capsys):
    run.main(["--split", "final", *options])
    output = capsys.readouterr().out
    assert "TOTAL" in output and "failures" not in output


def test_the_baseline_picks_one_tool_or_none():
    baseline = run.KeywordBaseline({"tools": [
        {"id": "a.send_email", "name": "send_email", "description": "Send an email", "parameters": []},
        {"id": "a.get_report", "name": "get_report", "description": "Fetch a report", "parameters": []},
    ]})

    assert baseline.route("email the team").plan == ["a.send_email"]
    assert baseline.route("zzz").plan == []


def _gemini(monkeypatch, reply):
    """Real Gemini classes with a scripted network layer and settings from the environment."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("TOOLSPOTTER_LLM_PROVIDER", "gemini")
    monkeypatch.setattr(gemini, "_post", reply)


def test_gemini_calls_are_counted(monkeypatch, capsys):
    plan = '{"plan": [], "out_of_scope": true}'
    _gemini(monkeypatch, lambda url, payload: {"candidates": [{"content": {"parts": [{"text": plan}]}}]})

    run.main(["--split", "final", "--from-env"])

    output = capsys.readouterr().out
    assert "failed: 0" in output and "NOT a clean" not in output


def test_a_run_with_unusable_replies_says_plans_were_local(monkeypatch, capsys):
    _gemini(monkeypatch, lambda url, payload: {"candidates": [{"content": {"parts": [{"text": "{"}]}}]})

    run.main(["--split", "final", "--from-env"])

    output = capsys.readouterr().out
    local, total = output.split("plans made locally instead: ")[1].split()[0:3:2]
    assert local == total != "0"


def test_a_run_stops_when_calls_keep_failing(monkeypatch):
    def down(url, payload):
        raise ProviderError("HTTP 429")

    _gemini(monkeypatch, down)

    with pytest.raises(SystemExit, match="failed in a row"):
        run.main(["--split", "final", "--from-env"])


def test_a_run_stops_if_a_requested_provider_is_missing(monkeypatch):
    monkeypatch.setenv("TOOLSPOTTER_EMBEDDING_PROVIDER", "gemini")      # and no key

    with pytest.raises(SystemExit, match="local ones are in use"):
        run.main(["--split", "final", "--from-env"])


def test_the_watch_restores_the_original_methods():
    original = gemini.GeminiChat.complete

    with run.GeminiWatch():
        assert gemini.GeminiChat.complete is not original

    assert gemini.GeminiChat.complete is original


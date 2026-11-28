"""Score the router on the test requests.

    python eval/run.py                  # GitHub MCP tools: tuning half ("dev"), with failures
    python eval/run.py --split final    # final-score half: totals only
    python eval/run.py --set playwright # or: github (default), custom
    python eval/run.py --baseline       # plain keyword search instead of the router
    python eval/run.py --from-env       # use TOOLSPOTTER_* settings (e.g. Gemini)
    python eval/run.py --from-env --cache .eval-cache --delay 4   # reuse vectors, go slowly

A request passes only if the answer is exactly right:

- expected tools: the plan is those tools, in that order (repeats included);
- expected "clarify": the router asks a question;
- expected "none": the router returns no tool and asks nothing.

The failures of the final half are never listed, so that it cannot be tuned
against. By default the settings are the built-in ones (local, no LLM) and the
environment is ignored, so everyone gets the same result.

The baseline picks the single best BM25 match over each tool's name, description
and parameter text. It cannot ask a question or plan several calls, so compare it
with the router on the "one expected tool" line.

With Gemini the router quietly falls back to local code when a call fails, so every
call is counted. A score with failed calls is marked as not a clean Gemini score,
and the run stops if calls keep failing.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from toolspotter.config import Settings  # noqa: E402
from toolspotter.domain.models import RouteResult  # noqa: E402
from toolspotter.engine.bm25 import BM25Index  # noqa: E402
from toolspotter.engine.llm_planner import LLMPlanner  # noqa: E402
from toolspotter.engine.pipeline import RoutingEngine  # noqa: E402
from toolspotter.providers.gemini import GeminiChat, GeminiEmbedding  # noqa: E402
from toolspotter.providers.local_tfidf import LocalTfidfEmbedding  # noqa: E402
from toolspotter.state import StateStore  # noqa: E402

SPLITS = ("dev", "final")
# GitHub and Playwright MCP tool definitions (see SOURCES.md) and a custom catalog.
SETS = {name: HERE / name for name in ("github", "playwright", "custom")}
GIVE_UP_AFTER = 5           # failed Gemini calls in a row


def load(name: str = "github") -> tuple[dict, list[dict]]:
    catalog = json.loads((SETS[name] / "catalog.json").read_text(encoding="utf-8"))
    requests = json.loads((SETS[name] / "requests.json").read_text(encoding="utf-8"))
    return catalog, requests


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class KeywordBaseline:
    """The simplest sensible router: the best BM25 match, or nothing if no word matches."""

    def __init__(self, catalog: dict) -> None:
        self._ids = [t["id"] for t in catalog["tools"]]
        self._index = BM25Index([
            words(" ".join([t["name"], t["description"]] + [
                f"{p['name']} {p.get('description', '')}" for p in t["parameters"]
            ]))
            for t in catalog["tools"]
        ])
        self.tool_count = len(self._ids)

    def route(self, query: str) -> RouteResult:
        score, tool_id = max(zip(self._index.scores(words(query)), self._ids))
        chosen = [tool_id] if score > 0 else []
        return RouteResult(selected_tools=chosen, plan=chosen)


class GeminiWatch:
    """Counts Gemini calls and failures while active, optionally pausing before each."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = self.failed = self.plans = self.local_plans = 0
        self._in_a_row = 0
        self._originals: list[tuple[type, str, object]] = []

    def __enter__(self) -> "GeminiWatch":
        self._wrap(GeminiChat, "complete", self._call)
        self._wrap(GeminiEmbedding, "_embed_one", self._call)
        self._wrap(LLMPlanner, "plan", self._plan)
        return self

    def __exit__(self, *exc_info) -> None:
        for owner, name, original in self._originals:
            setattr(owner, name, original)

    def _wrap(self, owner: type, name: str, around) -> None:
        original = getattr(owner, name)
        self._originals.append((owner, name, original))
        setattr(owner, name, lambda *args, **kwargs: around(original, *args, **kwargs))

    def _call(self, original, *args, **kwargs):
        if self.delay:
            time.sleep(self.delay)
        self.calls += 1
        try:
            reply = original(*args, **kwargs)
        except Exception as exc:
            self.failed += 1
            self._in_a_row += 1
            print(f"  Gemini call failed: {exc}"[:200], file=sys.stderr)
            if self._in_a_row >= GIVE_UP_AFTER:
                raise SystemExit(f"stopped: {GIVE_UP_AFTER} Gemini calls failed in a row "
                                 "(rate limit or quota?). No score was produced.")
            raise
        self._in_a_row = 0
        return reply

    def _plan(self, original, *args, **kwargs):
        outcome = original(*args, **kwargs)
        self.plans += 1
        if not any(step.get("planner") == "llm" for step in outcome.segments):
            self.local_plans += 1          # failed call, or a reply that could not be used
        return outcome

    def summary(self) -> str:
        text = f"Gemini calls: {self.calls}, failed: {self.failed}"
        if self.plans:
            text += f" · plans made locally instead: {self.local_plans} of {self.plans}"
        if self.failed:
            text += "\nWARNING: calls failed, so this is NOT a clean Gemini score."
        return text


def check_active(engine: RoutingEngine, settings: Settings) -> None:
    """Stop if a provider that was asked for is not the one in use."""
    local = isinstance(engine._index.provider, LocalTfidfEmbedding)
    if settings.embedding_provider == "gemini" and local:
        raise SystemExit("stopped: Gemini vectors were requested but local ones are in use "
                         "(check the key, quota and model).")
    if settings.llm_provider == "gemini" and engine._chat is None:
        raise SystemExit("stopped: the Gemini planner was requested but is not available "
                         "(check the key).")


def passed(expect: dict, result) -> bool:
    if "tools" in expect:
        return not result.clarify and result.plan == expect["tools"]
    if expect["outcome"] == "clarify":
        return result.clarify
    return not result.clarify and not result.plan


def describe(result) -> str:
    if result.clarify:
        return "asked a question"
    return " -> ".join(result.plan) or "no tool"


def interval(right: int, total: int) -> tuple[float, float]:
    """95% Wilson score interval: how far a score on this few requests can be trusted."""
    z = 1.96
    p = right / total
    scale = 1 + z * z / total
    middle = (p + z * z / (2 * total)) / scale
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return max(0.0, middle - margin), min(1.0, middle + margin)


def evaluate(engine: RoutingEngine | KeywordBaseline, requests: list[dict]) -> list[dict]:
    rows = []
    for request in requests:
        started = time.perf_counter()
        result = engine.route(request["query"])
        expected = request["expect"].get("tools")
        rows.append({
            "request": request,
            "ok": passed(request["expect"], result),
            "found": None if expected is None else set(expected) <= set(result.selected_tools),
            "got": describe(result),
            "ms": (time.perf_counter() - started) * 1000,
        })
    return rows


def report(rows: list[dict], split: str, show_failures: bool) -> None:
    print(f"\n{split} requests: {len(rows)}\n")
    print(f"{'type':<12}{'passed':>10}{'score':>8}")
    for kind in dict.fromkeys(row["request"]["type"] for row in rows):
        group = [row for row in rows if row["request"]["type"] == kind]
        right = sum(row["ok"] for row in group)
        print(f"{kind:<12}{f'{right}/{len(group)}':>10}{right / len(group):>8.0%}")
    right = sum(row["ok"] for row in rows)
    low, high = interval(right, len(rows))
    print(f"{'TOTAL':<12}{f'{right}/{len(rows)}':>10}{right / len(rows):>8.0%}"
          f"   (95% interval {low:.0%} to {high:.0%})")

    single = [row for row in rows if len(row["request"]["expect"].get("tools", [])) == 1]
    right = sum(row["ok"] for row in single)
    print(f"\none expected tool: {right}/{len(single)} ({right / len(single):.0%})")

    with_tools = [row for row in rows if row["found"] is not None]
    found = sum(row["found"] for row in with_tools)
    print(f"all needed tools returned: {found}/{len(with_tools)} "
          f"({found / len(with_tools):.0%}), ignoring order and extra tools")
    print(f"average time per request: {sum(row['ms'] for row in rows) / len(rows):.1f} ms")

    if show_failures:
        print("\nfailures")
        for row in rows:
            if row["ok"]:
                continue
            request = row["request"]
            wanted = " -> ".join(request["expect"].get("tools", [])) or request["expect"]["outcome"]
            print(f"  {request['id']}: {request['query']}")
            print(f"      expected {wanted}\n      got      {row['got']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Score the router on the test requests.")
    parser.add_argument("--split", choices=(*SPLITS, "all"), default="dev")
    parser.add_argument("--set", choices=tuple(SETS), default="github", dest="name",
                        help="which test set (default: github)")
    parser.add_argument("--baseline", action="store_true",
                        help="score plain keyword search instead of the router")
    parser.add_argument("--from-env", action="store_true",
                        help="take settings from the environment instead of the defaults")
    parser.add_argument("--cache", metavar="DIR",
                        help="save vectors in DIR so a later run does not request them again")
    parser.add_argument("--delay", type=float, default=0.0, metavar="SECONDS",
                        help="pause before each Gemini call, to stay within rate limits")
    args = parser.parse_args(argv)
    logging.disable(logging.WARNING)

    catalog, requests = load(args.name)
    with GeminiWatch(args.delay) as watch:
        if args.baseline:
            engine = KeywordBaseline(catalog)
            print(f"set: {args.name} · tools: {engine.tool_count} · keyword baseline")
        else:
            settings = Settings.from_env() if args.from_env else Settings()
            store = StateStore(Path(args.cache) / args.name) if args.cache else None
            engine = RoutingEngine(catalog, settings=settings, store=store)
            check_active(engine, settings)
            print(f"set: {args.name} · tools: {engine.tool_count} · "
                  f"vectors: {settings.embedding_provider} · LLM: {settings.llm_provider}"
                  + (f" ({settings.llm_model or 'default model'})"
                     if settings.llm_provider != "none" else ""))
        for split in SPLITS if args.split == "all" else (args.split,):
            rows = evaluate(engine, [r for r in requests if r["split"] == split])
            report(rows, split, show_failures=split == "dev")
        if watch.calls:
            print("\n" + watch.summary())


if __name__ == "__main__":
    main()

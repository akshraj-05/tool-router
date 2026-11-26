"""Score the router on the test requests.

    python eval/run.py                  # tuning half ("dev"), with the failures
    python eval/run.py --split final    # final-score half: totals only
    python eval/run.py --from-env       # use TOOL_ROUTER_* settings (e.g. Gemini)

A request passes only if the answer is exactly right:

- expected tools: the plan is those tools, in that order (repeats included);
- expected "clarify": the router asks a question;
- expected "none": the router returns no tool and asks nothing.

The failures of the final half are never listed, so that it cannot be tuned
against. By default the settings are the built-in ones (local, no LLM) and the
environment is ignored, so everyone gets the same result.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from tool_router.config import Settings  # noqa: E402
from tool_router.engine.pipeline import RoutingEngine  # noqa: E402

SPLITS = ("dev", "final")


def load() -> tuple[dict, list[dict]]:
    catalog = json.loads((HERE / "catalog.json").read_text(encoding="utf-8"))
    requests = json.loads((HERE / "requests.json").read_text(encoding="utf-8"))
    return catalog, requests


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


def evaluate(engine: RoutingEngine, requests: list[dict]) -> list[dict]:
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

    with_tools = [row for row in rows if row["found"] is not None]
    found = sum(row["found"] for row in with_tools)
    print(f"\nall needed tools returned: {found}/{len(with_tools)} "
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
    parser.add_argument("--from-env", action="store_true",
                        help="take settings from the environment instead of the defaults")
    args = parser.parse_args(argv)
    logging.disable(logging.WARNING)

    catalog, requests = load()
    settings = Settings.from_env() if args.from_env else Settings()
    engine = RoutingEngine(catalog, settings=settings)
    print(f"tools: {engine.tool_count} · vectors: {settings.embedding_provider} · "
          f"LLM: {settings.llm_provider}")
    for split in SPLITS if args.split == "all" else (args.split,):
        rows = evaluate(engine, [r for r in requests if r["split"] == split])
        report(rows, split, show_failures=split == "dev")


if __name__ == "__main__":
    main()

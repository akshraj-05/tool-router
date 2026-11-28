"""Conditional HyDE — Hypothetical Document Embeddings, fired only on low confidence.

Rationale (evidence-driven, engine-design.md): blind semantic expansion HURTS precision on
look-alike tools (measured: dense embeddings 79% < TF-IDF 83%). So HyDE is NOT in the default retrieval path. It activates whenever the top
fused score is below hyde_max (see pipeline.py's trigger check) — deliberately with NO lower
bound tied to the floor, since a colloquial in-scope query can score ~0 and needs HyDE exactly
as much as a "weak but present" one does. In that range it can only help:

1. Generate a short hypothetical tool description for the query (one cheap LLM call).
2. Retrieve again using that hypothetical (it "looks like" a real tool doc → tighter match).
3. Merge with the original candidates by max score, re-collapse duplicates.

It DOES fire below the out-of-scope floor, on purpose: a colloquial in-scope query and a truly
out-of-scope query can both score ~0 lexically, so excluding below-floor scores would stop HyDE
from rescuing exactly the queries that need it. Safety is enforced afterwards instead: the floor
still applies to the merged (HyDE-boosted) score, and the LLM planner's explicit `out_of_scope`
verdict vetoes a hypothetical that only matched by accident. It never fires when
top >= hyde_max (already confident — don't disturb it). Requires a chat model; degrades to a
no-op if unavailable or if generation fails.
"""

from __future__ import annotations

from typing import Callable, Optional

from toolspotter.domain.errors import ProviderError
from toolspotter.domain.models import DerivedMaps
from toolspotter.engine.dedup import collapse
from toolspotter.engine.retrieval import Candidate, QuerySignals
from toolspotter.log import get_logger

logger = get_logger("hyde")

_SYSTEM = """\
Given a user request, write a SHORT description (1-2 sentences) of the single ideal API tool
that would fulfil it — as if it were that tool's documentation. Name the action and its key
inputs/outputs. Describe only what such a tool does; do not answer the request. Output only the
description, no preamble."""


def generate_hypothetical(query: str, chat) -> Optional[str]:
    """One cheap LLM call → a hypothetical tool doc for the query (or None on failure)."""
    try:
        text = chat.complete(_SYSTEM, query, max_tokens=160).strip()
        return text or None
    except ProviderError as exc:
        logger.warning("HyDE generation failed (%s) — skipping HyDE", exc)
        return None


def _merge_by_max(a: list[Candidate], b: list[Candidate]) -> list[Candidate]:
    """Union two candidate lists, keeping the higher-scoring instance per tool id."""
    best: dict[str, Candidate] = {}
    for cand in (*a, *b):
        cur = best.get(cand.tool_id)
        if cur is None or cand.fused > cur.fused:
            best[cand.tool_id] = cand
    return sorted(best.values(), key=lambda c: (-c.fused, c.tool_id))


def apply_hyde(
    query: str,
    candidates: list[Candidate],
    chat,
    score_and_collapse: Callable[[str], tuple[list[Candidate], dict]],
    maps: DerivedMaps,
    signals: QuerySignals,
) -> tuple[list[Candidate], dict]:
    """Run a HyDE retrieval pass and merge it into the candidates (or no-op)."""
    hypothetical = generate_hypothetical(query, chat)
    if not hypothetical:
        return candidates, {"fired": False, "reason": "no hypothetical generated"}

    hyde_candidates, _ = score_and_collapse(hypothetical)
    merged = _merge_by_max(candidates, hyde_candidates)
    merged, _ = collapse(merged, maps, signals)   # re-dedup after the union

    before_top = candidates[0].tool_id if candidates else None
    after_top = merged[0].tool_id if merged else None
    logger.info("HyDE fired: top %s -> %s", before_top, after_top)
    return merged, {
        "fired": True,
        "hypothetical": hypothetical[:180],
        "top_before": before_top,
        "top_after": after_top,
    }

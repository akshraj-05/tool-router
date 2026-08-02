"""Planners: turn a shortlist into an ordered call plan.

This module ships the **LocalPlanner** — a deterministic, zero-LLM planner that
is both the offline default and the permanent degradation fallback
(engine-design.md, "graceful degradation"). ``engine/llm_planner.py`` implements
the same interface as the primary when a generation LLM is configured; the
local planner stays behind it for LLM-down resilience.

LocalPlanner strategy (general mechanisms, all catalog-derived — nothing is
fitted to specific test queries):

1. **Segmentation** — multi-step queries are split into clause segments at
   sequence connectors ("then", ";") and at "and/,"-boundaries ONLY when the
   next word is a *tool verb* (the set of leading verbs of tool names in the
   registry — derived from the catalog, not hardcoded English).
2. **Per-segment retrieval** — each segment is scored independently through
   the same fused pipeline; its top tool anchors that step. Weak segments are
   merged back into their predecessor; adjacent duplicate steps collapse.
3. **Coverage extension (greedy set-cover)** — one clause can still need two
   tools ("export the sales pipeline to a spreadsheet" = fetch + export). A
   helper tool joins a step only when it covers *discriminative* query terms
   the picked tools don't: gain is weighted by corpus IDF and must exceed a
   scale-free threshold (multiples of the corpus mean IDF), so the rule
   self-calibrates to any catalog instead of using tuned magic counts.
4. **Data-flow ordering** — within a step: reads before writes, helper
   (prerequisite) writes before the anchor action, destructive tools are
   never auto-added as helpers.
5. **Repeat counts** — an enumeration ("Dana, Omar, and Lin") inside a
   segment repeats that segment's anchor tool once per item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from tool_router.engine.retrieval import Candidate, QuerySignals, analyze_query
from tool_router.engine.text import tokenize
from tool_router.log import get_logger

logger = get_logger("planner")

# Returns scored+collapsed candidates for an arbitrary piece of text.
SegmentScorer = Callable[[str], list[Candidate]]

_SEGMENT_FLOOR = 0.12        # a segment whose top tool scores below this is merged back
_HELPER_MIN_RATIO = 0.5      # helper must score at least this fraction of the anchor
_GAIN_IDF_UNITS = 1.5        # required coverage gain, in multiples of the corpus mean IDF
_MAX_STEP_TOOLS = 3          # per-step cap: anchor + at most 2 helpers
_CONNECTOR_SPLIT = re.compile(r"\s*(?:;|\bthen\b|\bafter that\b)\s*", re.IGNORECASE)


@dataclass
class PlanOutcome:
    plan: list[str] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)   # trace: text + tools + calls
    # Set by the LLM planner (the local planner leaves the defaults):
    clarify: bool = False                 # planner-level "ask instead of act"
    clarify_question: str | None = None
    insufficient: bool = False            # shortlist lacks a needed capability
    needed_capability: str | None = None  # what to widen for
    out_of_scope: bool = False            # LLM's explicit "no tool applies" verdict (scope arbiter)


class Planner(Protocol):
    def plan(
        self,
        query: str,
        signals: QuerySignals,
        shortlist: list[Candidate],
        score_segment: SegmentScorer,
    ) -> PlanOutcome: ...


class LocalPlanner:
    """Deterministic no-LLM planner: the offline default, and the fallback when an LLM is configured."""

    def __init__(
        self,
        tool_verbs: set[str],
        idf: Callable[[str], float],
        mean_idf: float,
    ) -> None:
        # tool_verbs: leading verbs of tool names in the registry ("get",
        # "send", "export", ...) — clause-boundary detection, catalog-derived.
        # idf/mean_idf: corpus rarity statistics powering coverage gain.
        self._tool_verbs = tool_verbs
        self._idf = idf
        self._mean_idf = mean_idf if mean_idf > 0 else 1.0

    # -- public --------------------------------------------------------------

    def plan(
        self,
        query: str,
        signals: QuerySignals,
        shortlist: list[Candidate],
        score_segment: SegmentScorer,
    ) -> PlanOutcome:
        segments = self._segment(query)
        outcome = PlanOutcome()

        if len(segments) == 1:
            self._plan_step(segments[0], shortlist, outcome)
            return outcome

        # Multi-step: score each segment independently; merge weak segments back.
        steps: list[tuple[str, list[Candidate]]] = []
        pending_text = ""
        for seg in segments:
            text = f"{pending_text} {seg}".strip()
            ranked = score_segment(text)
            if not ranked or ranked[0].fused < _SEGMENT_FLOOR:
                pending_text = text        # too weak alone — merge into the next segment
                continue
            pending_text = ""
            steps.append((text, ranked))
        if pending_text and steps:
            # Trailing weak fragment: re-score merged into the last step.
            text = f"{steps[-1][0]} {pending_text}".strip()
            ranked = score_segment(text)
            if ranked:
                steps[-1] = (text, ranked)

        # Collapse adjacent segments that resolved to the same anchor tool.
        merged: list[tuple[str, list[Candidate]]] = []
        for text, ranked in steps:
            if merged and merged[-1][1][0].tool_id == ranked[0].tool_id:
                merged[-1] = (f"{merged[-1][0]} {text}", ranked)
            else:
                merged.append((text, ranked))

        for text, ranked in merged:
            self._plan_step(text, ranked[:8], outcome)

        if not outcome.plan and shortlist:   # safety net: never a silent no-op
            outcome.plan = [shortlist[0].tool_id]
        return outcome

    # -- internals -------------------------------------------------------------

    def _plan_step(self, text: str, ranked: list[Candidate], outcome: PlanOutcome) -> None:
        """Resolve one step: anchor + coverage helpers, data-flow ordered."""
        signals = analyze_query(text)
        step_tools = self._coverage_extend(signals, ranked)
        count = self._repeat_count(signals) if len(step_tools) == 1 else 1
        anchor_id = ranked[0].tool_id
        for cand in step_tools:
            calls = count if cand.tool_id == anchor_id else 1
            outcome.plan.extend([cand.tool_id] * calls)
            outcome.segments.append(
                {"text": text, "tool": cand.tool_id,
                 "score": round(cand.fused, 4), "calls": calls,
                 "role": "anchor" if cand.tool_id == anchor_id else "helper"}
            )

    def _coverage_extend(
        self, signals: QuerySignals, shortlist: list[Candidate]
    ) -> list[Candidate]:
        """Greedy IDF-weighted set-cover: add helpers only for real new coverage."""
        anchor = shortlist[0]

        def cover_tokens(cand: Candidate) -> set[str]:
            """Tokens a tool 'owns': its name + declared capabilities/tags."""
            return set(tokenize(cand.entry.tool.name)) | cand.entry.capability_tokens

        def matched(cand: Candidate) -> set[str]:
            return signals.content & (set(cand.entry.tokens) | cand.entry.capability_tokens)

        picked = [anchor]
        covered = matched(anchor)
        for cand in shortlist[1:]:
            if len(picked) >= _MAX_STEP_TOOLS:
                break
            if cand.fused < max(_SEGMENT_FLOOR, _HELPER_MIN_RATIO * anchor.fused):
                continue
            if cand.entry.tool.side_effects == "destructive":
                continue  # never auto-add destructive tools as helpers
            gain_tokens = (signals.content & cover_tokens(cand)) - covered
            gain = sum(self._idf(t) for t in gain_tokens) / self._mean_idf
            if gain >= _GAIN_IDF_UNITS:
                picked.append(cand)
                covered |= matched(cand)

        # Data-flow ordering: reads first (best score first), then helper
        # writes (prerequisites), then the anchor if it writes.
        reads = sorted(
            (c for c in picked if c.entry.tool.side_effects == "read"),
            key=lambda c: -c.fused,
        )
        writes = [c for c in picked if c.entry.tool.side_effects != "read"]
        writes.sort(key=lambda c: (c.tool_id == anchor.tool_id, -c.fused))
        return reads + writes

    def _segment(self, query: str) -> list[str]:
        """Split a query into clause segments at sequence/verb boundaries."""
        parts: list[str] = []
        for chunk in _CONNECTOR_SPLIT.split(query):
            if chunk and chunk.strip():
                parts.extend(self._split_on_verb_boundaries(chunk.strip()))
        return [p for p in parts if p] or [query]

    def _split_on_verb_boundaries(self, text: str) -> list[str]:
        """Split on ', <verb>' / ' and <verb>' where <verb> starts a tool name."""
        tokens = re.split(r"(\s*,\s*and\s+|\s*,\s*|\s+and\s+)", text)
        segments: list[str] = []
        current = tokens[0] if tokens else ""
        i = 1
        while i < len(tokens):
            sep = tokens[i]
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            first_word = nxt.split()[0].lower().rstrip(".,!?") if nxt.split() else ""
            if first_word in self._tool_verbs:
                segments.append(current.strip())
                current = nxt
            else:
                current = f"{current}{sep}{nxt}"
            i += 2
        if current.strip():
            segments.append(current.strip())
        return segments

    @staticmethod
    def _repeat_count(signals: QuerySignals) -> int:
        """Enumerated entities in the segment => one call per entity."""
        return signals.enumeration_count if signals.enumeration_count >= 2 else 1

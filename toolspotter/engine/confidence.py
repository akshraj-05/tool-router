"""Dynamic-K / confidence gate (engine-design.md §5).

Decides, from local signals only (no LLM):
- ``empty``   — nothing in the catalog applies (out-of-scope query)
- ``clarify`` — acting now would mean guessing (underspecified/destructive)
- ``proceed`` — hand a confidence-sized shortlist to the planner

The clarify heuristics below are the documented no-LLM degradation rules;
the LLM planner (engine/llm_planner.py), when configured, adds a second,
semantic refuse/clarify judgment on top. Principles (from the problem
statement, not from specific test cases):
  * a destructive/irreversible action must not fire on an under-specified
    referent ("cancel the meeting" — which one?);
  * an instruction whose object is only a pronoun with no resolvable content
    is unanswerable ("send it to the team" — send WHAT, WHERE?).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from toolspotter.config import ConfidenceThresholds
from toolspotter.engine.retrieval import Candidate, QuerySignals


@dataclass
class Decision:
    kind: str                                   # "empty" | "clarify" | "proceed"
    shortlist: list[Candidate] = field(default_factory=list)
    clarify_question: str | None = None
    reason: str = ""


def decide(
    candidates: list[Candidate],
    signals: QuerySignals,
    thresholds: ConfidenceThresholds,
) -> Decision:
    """Gate the query: empty / clarify / proceed-with-shortlist."""
    if not candidates:
        return Decision(kind="empty", reason="no candidates")

    top = candidates[0]

    # 1. Out of scope: nothing scores meaningfully.
    if top.fused < thresholds.out_of_scope_floor:
        return Decision(
            kind="empty",
            reason=f"top fused score {top.fused:.3f} below floor "
                   f"{thresholds.out_of_scope_floor} — no tool applies",
        )

    # 2a. Sweeping destructive request ("delete ALL ... permanently"): a bulk
    # irreversible operation must be confirmed, never routed on first ask.
    if signals.destructive_intent and signals.sweeping:
        return Decision(
            kind="clarify",
            reason="sweeping destructive request requires confirmation",
            clarify_question=(
                "This would be a bulk, irreversible operation. Can you confirm the "
                "exact scope (which records/objects) before I route any action?"
            ),
        )

    # 2b. Destructive action on an under-specified referent.
    if (
        top.entry.tool.side_effects == "destructive"
        and signals.destructive_intent
        and not signals.has_identifier
    ):
        return Decision(
            kind="clarify",
            reason="destructive intent without a concrete referent",
            clarify_question=(
                "This looks like a destructive action, but I can't tell exactly "
                "which item it should apply to. Can you specify it (id or name)?"
            ),
        )

    # 2c. Ambiguous version signal: the query leans on historical continuity
    # ("the way we always have") while the top tool belongs to a version
    # family — old and new behave differently, so guessing is unsafe.
    if (
        top.entry.family_id is not None
        and signals.continuity
        and not signals.legacy_signal
    ):
        return Decision(
            kind="clarify",
            reason="ambiguous version reference on a versioned tool family",
            clarify_question=(
                "There are multiple versions of this capability (current and legacy). "
                "Which one should I use?"
            ),
        )

    # 3. Pronoun-only object with almost no content: nothing resolvable to act on.
    if (
        signals.deictic_reference
        and not signals.has_identifier
        and len(signals.content) <= 3
    ):
        return Decision(
            kind="clarify",
            reason="query object is an unresolved pronoun with no content",
            clarify_question=(
                "I need a bit more detail — what exactly should be sent/acted on, "
                "and to whom or where?"
            ),
        )

    # 4. Proceed: confidence-sized shortlist (dynamic-K).
    cutoff = top.fused * thresholds.shortlist_ratio
    shortlist = [c for c in candidates if c.fused >= cutoff][: thresholds.shortlist_max]
    return Decision(kind="proceed", shortlist=shortlist, reason="confident")

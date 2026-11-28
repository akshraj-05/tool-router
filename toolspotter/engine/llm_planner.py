"""LLM planner — the ONE generation-LLM call per query (engine-design.md §6).

Receives the query + the confidence-sized shortlist (~8 compact tool schemas)
and produces the precise final decision: selection among look-alikes, causal
ordering, repeat counts, version reasoning, refuse/clarify — the semantic work
deliberately kept OUT of local retrieval.

Safety properties:
- **Structured output**: the model must return strict JSON; replies are parsed
  and validated — tool ids not present in the shortlist are dropped (the
  planner can never invent a tool).
- **Insufficiency signal**: the model may flag ``insufficient`` with a
  ``needed_capability``; the pipeline then widens the shortlist and re-plans
  (bounded).
- **Total degradation**: ANY failure (no credentials, network, bad JSON) falls
  back to the deterministic LocalPlanner. The engine never crashes and never
  returns nothing because of the LLM.
"""

from __future__ import annotations

import json
from typing import Any

from toolspotter.domain.errors import ProviderError
from toolspotter.engine.planner import LocalPlanner, PlanOutcome, SegmentScorer
from toolspotter.engine.retrieval import Candidate, QuerySignals
from toolspotter.log import get_logger
from toolspotter.providers.base import ChatClient, extract_json_object

logger = get_logger("llm_planner")

_SYSTEM_PROMPT = """\
You are a tool-routing planner. Given a user request and a list of candidate tools,
decide which tools to call, in what order, and how many times.

Reply with STRICT JSON only (no prose, no markdown) in exactly this shape:
{"plan": [{"tool_id": "..."}], "clarify": false, "clarify_question": null,
 "insufficient": false, "needed_capability": null, "out_of_scope": false}

Rules:
- Use ONLY tool_ids from the provided candidates. Never invent a tool.
- Order the plan causally (fetch data before transforming it, transform before sending).
- If a step must run once per entity (e.g. one email per recipient), repeat its tool_id.
- Include a tool only if the request actually needs it; keep the plan minimal.
- Prefer current tool versions; use a deprecated version ONLY if the user explicitly
  asks for the legacy/old behaviour.
- If the request needs a capability none of the candidates has (e.g. an attachment,
  a specific field), set "insufficient": true and name it in "needed_capability".
- CRITICAL — scope check: the candidate tools were retrieved by similarity and may be
  irrelevant. If NONE of them genuinely serves the request (e.g. it is general knowledge,
  chit-chat, or a task no listed tool performs), set "out_of_scope": true and return an
  empty plan. Do not force-fit an unrelated tool.
- If the request is too ambiguous or destructive without a clear target, set
  "clarify": true with a short "clarify_question".

Your ENTIRE reply must be that single JSON object, starting with "{" — no preamble,
no markdown fences, no explanation, nothing after the closing "}".
"""


def _compact_tool(cand: Candidate) -> dict[str, Any]:
    """Minimal schema the planner needs — keeps the prompt (and cost) small."""
    tool = cand.entry.tool
    spec: dict[str, Any] = {
        "tool_id": tool.id,
        "description": tool.description,
        "parameters": [
            {"name": p.name, "type": p.type, "required": p.required}
            for p in tool.parameters
        ],
        "side_effects": tool.side_effects,
    }
    if tool.deprecated:
        spec["deprecated"] = True
    if tool.replaced_by:
        spec["replaced_by"] = tool.replaced_by
    return spec


class LLMPlanner:
    """Primary planner: one chat-LLM call; LocalPlanner as total fallback."""

    def __init__(self, chat: ChatClient, fallback: LocalPlanner) -> None:
        self._chat = chat
        self._fallback = fallback

    def plan(
        self,
        query: str,
        signals: QuerySignals,
        shortlist: list[Candidate],
        score_segment: SegmentScorer,
    ) -> PlanOutcome:
        try:
            outcome = self._plan_llm(query, shortlist)
        except ProviderError as exc:
            logger.warning("LLM planning failed (%s) — using local fallback planner", exc)
            return self._fallback.plan(query, signals, shortlist, score_segment)

        # An empty plan is authoritative ONLY when the LLM gives a reason
        # (out_of_scope / clarify / insufficient) — that is a deliberate
        # "no tool applies" verdict, the scope-arbiter role HyDE relies on.
        # An empty plan with NO reason is a model slip → let the deterministic
        # planner take over (never silently no-op).
        if (
            not outcome.plan
            and not outcome.clarify
            and not outcome.insufficient
            and not outcome.out_of_scope
        ):
            logger.warning("LLM returned an empty plan without a reason — using local fallback")
            return self._fallback.plan(query, signals, shortlist, score_segment)
        return outcome

    # -- internals -----------------------------------------------------------

    def _plan_llm(self, query: str, shortlist: list[Candidate]) -> PlanOutcome:
        candidates_json = json.dumps([_compact_tool(c) for c in shortlist], indent=1)
        user = f"User request:\n{query}\n\nCandidate tools:\n{candidates_json}"
        reply = self._chat.complete(_SYSTEM_PROMPT, user)
        parsed = extract_json_object(reply)

        allowed = {c.tool_id for c in shortlist}
        plan: list[str] = []
        invented: list[str] = []
        for step in parsed.get("plan", []) or []:
            tool_id = step.get("tool_id") if isinstance(step, dict) else step
            if isinstance(tool_id, str) and tool_id in allowed:
                plan.append(tool_id)
            elif tool_id:
                invented.append(str(tool_id))
        if invented:
            logger.warning("LLM proposed unknown tool ids (dropped): %s", invented)

        question = parsed.get("clarify_question")
        needed = parsed.get("needed_capability")
        outcome = PlanOutcome(
            plan=plan,
            clarify=bool(parsed.get("clarify")),
            clarify_question=question if isinstance(question, str) else None,
            insufficient=bool(parsed.get("insufficient")),
            needed_capability=needed if isinstance(needed, str) else None,
            out_of_scope=bool(parsed.get("out_of_scope")) and not plan,
        )
        outcome.segments.append({"planner": "llm", "tools": plan})
        return outcome

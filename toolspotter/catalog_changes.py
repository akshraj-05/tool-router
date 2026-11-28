"""Work out what an upload would do to the registry, before doing it.

Two modes:

- ``merge``: add new tools and update existing ones; the others stay.
- ``replace``: the upload becomes the whole registry.

A tool that fails validation is skipped and reported. In ``merge`` mode the saved
definition of that id then stays; in ``replace`` mode it is removed, and the
result says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from toolspotter.domain.errors import CatalogError
from toolspotter.domain.versioning import NONE, change_level, inherit_side_effects
from toolspotter.domain.naming import prepare_tool
from toolspotter.engine.guardrail import validate_catalog

MODES = ("merge", "replace")


@dataclass(frozen=True)
class Change:
    """What applying an upload would give."""

    tools: list[dict[str, Any]]                 # the registry afterwards
    near_duplicate_groups: list[list[str]]
    added: list[str]
    changed: list[str]
    unchanged: list[str]
    removed: list[str]
    rejected: list[dict[str, Any]]

    def report(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "rejected": self.rejected,
            "tool_count_after": len(self.tools),
        }


def _check_groups(groups: Any) -> list[list[str]]:
    if not (isinstance(groups, list) and all(
        isinstance(g, list) and all(isinstance(i, str) for i in g) for g in groups
    )):
        raise CatalogError("near_duplicate_groups must be a list of lists of tool ids.")
    return groups


def plan_change(
    current: list[dict[str, Any]],
    current_groups: list[list[str]],
    submitted: list[Any],
    mode: str = "merge",
    submitted_groups: Any = None,
) -> Change:
    """Compare an upload with the current registry. Changes nothing."""
    if mode not in MODES:
        raise CatalogError(f"mode must be one of {', '.join(MODES)}.")
    new_groups = _check_groups([] if submitted_groups is None else submitted_groups)
    saved = {t["id"]: t for t in current}

    candidates = [(i, prepare_tool(t))
                  for i, t in enumerate(inherit_side_effects(submitted, current))]
    rejected: list[dict[str, Any]] = []
    # Dropping an invalid tool can make another one invalid (its replaced_by now
    # points nowhere), so check again until everything left is accepted.
    while True:
        offered = {t["id"] for _, t in candidates
                   if isinstance(t, dict) and isinstance(t.get("id"), str)}
        base = [t for t in current if t["id"] not in offered] if mode == "merge" else []
        results = validate_catalog([*base, *(t for _, t in candidates)])[len(base):]
        failed = {index: result for (index, _), result in zip(candidates, results)
                  if not result.accepted}
        if not failed:
            break
        rejected.extend(
            {"index": index, "id": result.tool_id, "reason": result.primary_reason,
             "issues": [issue.message for issue in result.issues]}
            for index, result in failed.items()
        )
        candidates = [(i, t) for i, t in candidates if i not in failed]
    rejected.sort(key=lambda item: item["index"])

    accepted = {t["id"]: t for _, t in candidates}
    added = [i for i in accepted if i not in saved]
    changed = [i for i, t in accepted.items()
               if i in saved and change_level(saved[i], t) != NONE]
    unchanged = [i for i in accepted if i in saved and i not in changed]
    if mode == "merge":
        tools = [accepted.get(t["id"], t) for t in current] + [accepted[i] for i in added]
        removed: list[str] = []
        groups = [*current_groups, *(g for g in new_groups if g not in current_groups)]
    else:
        tools = list(accepted.values())
        removed = [i for i in saved if i not in accepted]
        groups = new_groups

    present = {t["id"] for t in tools}
    groups = [[i for i in group if i in present] for group in groups]
    return Change(tools, [g for g in groups if len(g) > 1],
                  added, changed, unchanged, removed, rejected)

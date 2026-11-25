"""Version numbers and history for saved tools.

When a saved tool's definition changes, the new definition becomes the current
entry with a higher ``version``, and the old definition is moved to history. The
router only ever searches current entries; history is a record.

Which part of ``x.y.z`` goes up (the biggest change wins):

- x (major): the name a caller uses changed, a parameter was removed, renamed or
  retyped, or a parameter became required (including a new required one);
- y (minor): an optional parameter was added, a parameter became optional, or a
  parameter's description changed;
- z (patch): anything else, such as the tool's description.

A ``version`` supplied by the caller is used only if it is higher than the saved
one.
"""

from __future__ import annotations

from typing import Any, Optional

from tool_router.domain.naming import prepare_tool

INITIAL_VERSION = "1.0.0"
HISTORY_LIMIT = 10          # old definitions kept per tool id

NONE, PATCH, MINOR, MAJOR = 0, 1, 2, 3
History = dict[str, list[dict[str, Any]]]


def _parameters(tool: dict[str, Any]) -> dict[str, tuple[str, bool, str]]:
    return {
        p["name"]: (p.get("type", "string"), bool(p.get("required", False)), p.get("description", ""))
        for p in tool.get("parameters") or []
    }


def _call_name(tool: dict[str, Any]) -> tuple[Any, Any]:
    return tool.get("name"), (tool.get("source") or {}).get("name")


def _other_fields(tool: dict[str, Any]) -> tuple[Any, ...]:
    return (
        tool.get("description", ""), tool.get("side_effects"), tool.get("cluster"),
        tuple(tool.get("tags") or ()), bool(tool.get("deprecated", False)), tool.get("replaced_by"),
    )


def change_level(old: dict[str, Any], new: dict[str, Any]) -> int:
    """How big the difference between two definitions of a tool is."""
    level = NONE
    if _call_name(old) != _call_name(new):
        level = MAJOR
    before, after = _parameters(old), _parameters(new)
    for name in before.keys() - after.keys():
        level = MAJOR
    for name in after.keys() - before.keys():
        level = max(level, MAJOR if after[name][1] else MINOR)
    for name in before.keys() & after.keys():
        (old_type, old_required, old_text), (new_type, new_required, new_text) = before[name], after[name]
        if old_type != new_type or (new_required and not old_required):
            level = MAJOR
        elif old_required != new_required or old_text != new_text:
            level = max(level, MINOR)
    if _other_fields(old) != _other_fields(new):
        level = max(level, PATCH)
    return level


def _number(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def bump(version: str, level: int) -> str:
    major, minor, patch = _number(version)
    if level >= MAJOR:
        return f"{major + 1}.0.0"
    if level == MINOR:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def inherit_side_effects(
    submitted: list[Any], previous: list[dict[str, Any]]
) -> list[Any]:
    """Keep a saved ``side_effects`` value where a re-upload omits the field.

    Without this, uploading a definition again without it would guess from the name
    and could quietly stop treating a destructive tool as destructive.
    """
    saved = {t["id"]: t.get("side_effects") for t in previous}
    result = []
    for tool in submitted:
        if isinstance(tool, dict) and not tool.get("side_effects"):
            prepared = prepare_tool(tool)
            tool_id = prepared.get("id")
            if isinstance(tool_id, str) and saved.get(tool_id):
                tool = {**prepared, "side_effects": saved[tool_id]}
        result.append(tool)
    return result


def apply_versions(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    history: Optional[History] = None,
) -> tuple[list[dict[str, Any]], History]:
    """Number the ``current`` tools against ``previous`` ones.

    Returns ``(current tools with versions, new history)``. Neither input is
    modified. Removed tools move to history, and a tool that is added again
    continues from its last version.
    """
    new_history: History = {k: list(v) for k, v in (history or {}).items()}
    before = {t["id"]: t for t in previous}
    numbered: list[dict[str, Any]] = []

    for tool in current:
        tool_id = tool["id"]
        declared = tool.get("version")
        old = before.get(tool_id)
        returning = old is None and bool(new_history.get(tool_id))
        base = new_history[tool_id][-1] if returning else old

        if base is None:                                   # never seen before
            numbered.append({**tool, "version": declared or INITIAL_VERSION})
            continue

        base_version = base.get("version") or INITIAL_VERSION
        higher = bool(declared) and _number(declared) > _number(base_version)
        level = change_level(base, tool)
        if level == NONE and not higher:                   # unchanged: keep the number
            if returning:
                new_history[tool_id].pop()                 # it is the current entry again
            numbered.append({**tool, "version": base_version})
            continue

        version = declared if higher else bump(base_version, max(level, PATCH))
        if not returning:
            new_history.setdefault(tool_id, []).append(old)
        numbered.append({**tool, "version": version})

    present = {t["id"] for t in numbered}
    for tool_id, old in before.items():
        if tool_id not in present:
            new_history.setdefault(tool_id, []).append(old)

    return numbered, {k: v[-HISTORY_LIMIT:] for k, v in new_history.items() if v}

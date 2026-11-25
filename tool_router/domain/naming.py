"""Defaults for the optional fields of a tool definition.

``cluster``, ``id`` and ``side_effects`` may be omitted by whoever registers a tool:

- ``cluster`` defaults to ``general``;
- ``id`` is built as ``<cluster>.<name>`` from the declared values;
- ``side_effects`` is inferred from the verbs in the name (see ``infer_side_effects``).

A value the caller did provide is never changed. The id is derived only from what
the caller declared (never from anything computed from the rest of the catalog),
so the same definition always gets the same id.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_CLUSTER = "general"

SIDE_EFFECTS = ("read", "write", "destructive")
# A destructive verb anywhere in a name wins; a read verb counts only as the first word.
DESTRUCTIVE_VERBS = frozenset(
    "delete remove cancel wipe purge drop restart terminate destroy erase kill shutdown reboot".split()
)
READ_VERBS = frozenset(
    "get list search fetch read find check show view describe query lookup count".split()
)


def infer_side_effects(name: str) -> str:
    """A cautious guess from a tool name: ``destructive``, ``read`` or ``write``.

    Anything not clearly a read is treated as a write. This is only a fallback:
    a name such as ``close_account`` is destructive but contains no listed verb,
    so tools that change or delete things should declare ``side_effects``.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    words = [w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w]
    if DESTRUCTIVE_VERBS.intersection(words):
        return "destructive"
    return "read" if words and words[0] in READ_VERBS else "write"


def build_id(cluster: str, name: str) -> str:
    """``("Finance Tools", "get_report")`` -> ``finance_tools.get_report``."""
    prefix = re.sub(r"[^a-z]+", "_", cluster.lower()).strip("_") or DEFAULT_CLUSTER
    suffix = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "tool"
    return f"{prefix}.{suffix}"


def prepare_tool(raw: Any) -> Any:
    """Return a copy of ``raw`` with ``cluster``, ``id`` and ``side_effects`` filled in if absent.

    Anything that is not a dict is returned unchanged for the guardrail to reject.
    """
    if not isinstance(raw, dict):
        return raw
    tool = dict(raw)
    cluster = tool.get("cluster")
    if cluster is None or (isinstance(cluster, str) and not cluster.strip()):
        tool["cluster"] = DEFAULT_CLUSTER
    name = tool.get("name")
    if tool.get("side_effects") in (None, "") and isinstance(name, str):
        tool["side_effects"] = infer_side_effects(name)
    if tool.get("id") in (None, "") and isinstance(name, str) and name.strip():
        tool.pop("id", None)
        tool = {"id": build_id(str(tool["cluster"]), name), **tool}
    return tool

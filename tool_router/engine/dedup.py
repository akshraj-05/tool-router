"""Deduplication + version-family collapse (engine-design.md §4).

Two collapses run after scoring, both pure local logic on pre-derived maps:

1. **Version families** — within a family (create_invoice v1/v2), keep exactly
   one member: the *current* one by default, the *deprecated* one when the
   query carries an explicit legacy signal. Selecting both versions at once is
   always wrong — only one should ever be returned.
2. **Near-duplicate groups** — within a group (get_user/fetch_profile/...),
   keep only the highest-scoring member. Because the parameter match is part of
   the fused score, the member matching the query's specifics wins the group
   (e.g. "by email" -> get_user_by_email).
"""

from __future__ import annotations

from tool_router.domain.models import DerivedMaps
from tool_router.engine.retrieval import Candidate, QuerySignals


def collapse(
    candidates: list[Candidate],
    maps: DerivedMaps,
    signals: QuerySignals,
) -> tuple[list[Candidate], dict[str, list[str]]]:
    """Drop redundant candidates; returns (kept, {reason: dropped_ids})."""
    dropped: dict[str, list[str]] = {"version_family": [], "near_duplicate": []}
    by_id = {c.tool_id: c for c in candidates}
    to_drop: set[str] = set()

    # --- version families: keep exactly one member per family -------------
    for members in maps.version_families.values():
        present = [by_id[m] for m in members if m in by_id and m not in to_drop]
        if len(present) < 2:
            continue
        prefer_deprecated = signals.legacy_signal
        preferred = [
            c for c in present
            if (not c.entry.is_current if prefer_deprecated else c.entry.is_current)
        ]
        pool = preferred or present  # fall back if no member matches the preference
        keep = max(pool, key=lambda c: c.fused)
        for c in present:
            if c.tool_id != keep.tool_id:
                to_drop.add(c.tool_id)
                dropped["version_family"].append(c.tool_id)

    # --- near-duplicate groups: keep the best-scoring member --------------
    for group in maps.dup_groups:
        present = [by_id[m] for m in group if m in by_id and m not in to_drop]
        if len(present) < 2:
            continue
        keep = max(present, key=lambda c: c.fused)
        for c in present:
            if c.tool_id != keep.tool_id:
                to_drop.add(c.tool_id)
                dropped["near_duplicate"].append(c.tool_id)

    kept = [c for c in candidates if c.tool_id not in to_drop]
    return kept, dropped

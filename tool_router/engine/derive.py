"""Auto-derivation of registry-level maps (engine-design.md §Auto-derivation).

Derives, per registry:
- clusters          — from each tool's `cluster` field (`general` if omitted)
- version families  — ONLY from explicit `replaced_by` links (names are never
                      interpreted); a tool that is `deprecated` or has
                      `replaced_by` is marked superseded
- near-dup groups   — explicit catalog `near_duplicate_groups` (optional
                      catalog-provided metadata a router is expected to use
                      when present) UNIONED with auto-detected groups (embedding cosine +
                      parameter-name overlap), so unseen catalogs without the
                      explicit metadata still get dedup. Union-find keeps
                      groups consistent.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from typing import Optional

from tool_router.config import ConfidenceThresholds
from tool_router.domain.models import DerivedMaps, IndexedTool
from tool_router.domain.naming import DEFAULT_CLUSTER
from tool_router.engine.text import cosine, jaccard, norm
from tool_router.log import get_logger

logger = get_logger("derive")

# Saved duplicate pairs are reused only while this number is unchanged: raise it
# whenever find_duplicate_pairs() could give a different answer for the same input.
DUPLICATE_RULES_VERSION = 1
Pairs = list[tuple[str, str]]


def duplicate_fingerprint(
    entries: list[IndexedTool], thresholds: ConfidenceThresholds, model_stamp: str
) -> str:
    """Identifies everything find_duplicate_pairs() depends on."""
    tools = sorted(
        (e.tool.id, e.tool.cluster, e.tool.side_effects,
         sorted(p.name for p in e.tool.parameters), e.search_text)
        for e in entries
    )
    facts = [DUPLICATE_RULES_VERSION, model_stamp, asdict(thresholds), tools]
    return hashlib.sha256(json.dumps(facts).encode("utf-8")).hexdigest()


def find_duplicate_pairs(
    entries: list[IndexedTool],
    thresholds: ConfidenceThresholds,
    unchanged: frozenset[str] = frozenset(),
    known: Optional[Pairs] = None,
) -> Pairs:
    """Pairs of tools that look like they do the same job, sorted by id.

    Two tools are compared only if they share a cluster and side effects and, unless
    the overlap threshold is zero, at least one parameter name. Those are the only
    pairs that can qualify, so most pairs are never looked at.

    For an incremental update, ``unchanged`` names tools whose cluster, side effects,
    parameters and vector are the same as when ``known`` was computed. Pairs between
    two such tools are taken from ``known``; only pairs involving a new or changed
    tool are compared again.
    """
    ordered = sorted(entries, key=lambda e: e.tool.id)
    present = {e.tool.id for e in ordered}
    pairs: Pairs = [
        (a, b) for a, b in known or []
        if a in unchanged and b in unchanged and a in present and b in present
    ]
    # Computed once per tool.
    param_names = [{p.name for p in e.tool.parameters} for e in ordered]
    lengths: dict[int, float] = {}

    def length(i: int) -> float:
        if i not in lengths:
            lengths[i] = norm(ordered[i].embedding)
        return lengths[i]

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, e in enumerate(ordered):
        # Near-dups live in the same capability cluster, and a read tool and a write
        # tool are never near-duplicates (similar params ≠ same action).
        groups[(e.tool.cluster, e.tool.side_effects)].append(i)

    share_required = thresholds.dup_param_overlap > 0
    for members in groups.values():
        having: dict[str, set[int]] = defaultdict(set)     # parameter name -> tools
        if share_required:
            for i in members:
                for name in param_names[i]:
                    having[name].add(i)
        for i in members:
            a = ordered[i]
            if a.tool.id in unchanged:       # its pairs are reused, or found from the other side
                continue
            candidates = (
                set().union(*(having[n] for n in param_names[i])) if share_required else set(members)
            )
            for j in candidates:
                b = ordered[j]
                # Visit each pair once; two changed tools are compared from the earlier one.
                if j == i or (j < i and b.tool.id not in unchanged):
                    continue
                if jaccard(param_names[i], param_names[j]) < thresholds.dup_param_overlap:
                    continue
                if cosine(a.embedding, b.embedding, length(i), length(j)) >= thresholds.dup_similarity:
                    first, second = (i, j) if i < j else (j, i)
                    pairs.append((ordered[first].tool.id, ordered[second].tool.id))
    return sorted(pairs)


def unchanged_for_duplicates(
    entries: list[IndexedTool], previous: list[IndexedTool]
) -> frozenset[str]:
    """Ids whose duplicate-relevant facts and vector are exactly as before."""
    before = {e.tool.id: e for e in previous}

    def facts(e: IndexedTool) -> tuple:
        return (e.tool.cluster, e.tool.side_effects, {p.name for p in e.tool.parameters})

    return frozenset(
        e.tool.id for e in entries
        if (old := before.get(e.tool.id)) is not None
        and facts(e) == facts(old) and e.embedding == old.embedding
    )


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def groups(self) -> list[list[str]]:
        by_root: dict[str, list[str]] = defaultdict(list)
        for x in self._parent:
            by_root[self.find(x)].append(x)
        return [sorted(members) for members in by_root.values() if len(members) > 1]


def derive_maps(
    entries: list[IndexedTool],
    catalog_meta: dict,
    thresholds: ConfidenceThresholds,
    duplicate_pairs: Optional[Pairs] = None,
) -> DerivedMaps:
    """Build clusters, version families, and near-duplicate groups; annotate entries.

    ``duplicate_pairs`` takes an earlier result of find_duplicate_pairs(); without
    it the pairs are computed here."""
    by_id = {e.tool.id: e for e in entries}

    # --- clusters --------------------------------------------------------
    # Every tool declares a cluster or was given the default one at intake.
    clusters: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        clusters[e.tool.cluster or DEFAULT_CLUSTER].append(e.tool.id)

    # --- version families --------------------------------------------------
    # A family is a set of tools connected by `replaced_by` (chains included).
    # Its key is the id of the member nothing supersedes, when there is one.
    links = _UnionFind()
    for e in entries:
        successor = e.tool.replaced_by
        if successor and successor in by_id:
            links.union(successor, e.tool.id)

    version_families: dict[str, list[str]] = {}
    for members in links.groups():
        current = [m for m in members if by_id[m].tool.replaced_by is None]
        version_families[current[0] if current else members[0]] = members

    family_of = {m: key for key, members in version_families.items() for m in members}
    for e in entries:
        e.family_id = family_of.get(e.tool.id)
        e.is_current = not e.tool.deprecated and e.tool.replaced_by is None

    # --- near-duplicate groups ---------------------------------------------
    uf = _UnionFind()
    # (a) explicit catalog metadata — trusted input, exploit it.
    explicit = catalog_meta.get("near_duplicate_groups") or []
    for group in explicit:
        ids = [tid for tid in group if tid in by_id]
        for a, b in zip(ids, ids[1:]):
            uf.union(a, b)
    # (b) auto-detection for catalogs without the metadata (generalization).
    if duplicate_pairs is None:
        duplicate_pairs = find_duplicate_pairs(entries, thresholds)
    for a, b in duplicate_pairs:
        uf.union(a, b)
    dup_groups = uf.groups()

    logger.info(
        "derived %d clusters, %d version families, %d near-dup groups",
        len(clusters), len(version_families), len(dup_groups),
    )
    return DerivedMaps(
        clusters=dict(clusters),
        dup_groups=dup_groups,
        version_families=version_families,
    )

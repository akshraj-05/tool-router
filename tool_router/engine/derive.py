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


def find_duplicate_pairs(entries: list[IndexedTool], thresholds: ConfidenceThresholds) -> Pairs:
    """Pairs of tools that look like they do the same job.

    This compares tools in pairs, so it is the slowest part of building an index.
    """
    pairs: Pairs = []
    ordered = sorted(entries, key=lambda e: e.tool.id)
    # Computed once per tool: this loop visits every pair.
    param_names = [{p.name for p in e.tool.parameters} for e in ordered]
    lengths = [norm(e.embedding) for e in ordered]
    for i, a in enumerate(ordered):
        for j in range(i + 1, len(ordered)):
            b = ordered[j]
            if a.tool.cluster != b.tool.cluster:
                continue  # near-dups live in the same capability cluster
            if a.tool.side_effects != b.tool.side_effects:
                continue  # a read tool and a write tool are never near-duplicates
                          # (similar params ≠ same action: get_object vs upload_to_s3)
            # Parameter names first: far cheaper than comparing vectors.
            if jaccard(param_names[i], param_names[j]) < thresholds.dup_param_overlap:
                continue
            similarity = cosine(a.embedding, b.embedding, lengths[i], lengths[j])
            if similarity >= thresholds.dup_similarity:
                pairs.append((a.tool.id, b.tool.id))
    return pairs


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

"""Auto-derivation of registry-level maps (engine-design.md §Auto-derivation).

Derives, per registry:
- clusters          — from each tool's `cluster` field
- version families  — from replaces/replaced_by links, `deprecated`, and
                      `_v<N>` name suffixes; marks each tool current/superseded
- near-dup groups   — explicit catalog `near_duplicate_groups` (optional
                      catalog-provided metadata a router is expected to use
                      when present) UNIONED with auto-detected groups (embedding cosine +
                      parameter-name overlap), so unseen catalogs without the
                      explicit metadata still get dedup. Union-find keeps
                      groups consistent.
"""

from __future__ import annotations

import re
from collections import defaultdict

from tool_router.config import ConfidenceThresholds
from tool_router.domain.models import DerivedMaps, IndexedTool
from tool_router.engine.text import cosine, jaccard
from tool_router.log import get_logger

logger = get_logger("derive")

_VSUFFIX = re.compile(r"^(?P<base>.+?)_v\d+$")


def _centroid(vectors: list) -> dict[str, float]:
    """Term-wise mean of sparse dict vectors (dense lists averaged likewise)."""
    if vectors and isinstance(vectors[0], list):
        n = len(vectors)
        return [sum(v[i] for v in vectors) / n for i in range(len(vectors[0]))]  # type: ignore
    acc: dict[str, float] = defaultdict(float)
    for vec in vectors:
        if isinstance(vec, dict):
            for term, val in vec.items():
                acc[term] += val
    n = max(1, len(vectors))
    return {term: val / n for term, val in acc.items()}


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
) -> DerivedMaps:
    """Build clusters, version families, and near-duplicate groups; annotate entries."""
    by_id = {e.tool.id: e for e in entries}

    # --- clusters --------------------------------------------------------
    # Tools that declare a cluster keep it. Tools WITHOUT one are assigned to
    # the nearest existing cluster by embedding-centroid similarity (so mixed
    # catalogs still get hierarchical structure); with no similar cluster they
    # stay "uncategorized".
    clusters: dict[str, list[str]] = defaultdict(list)
    unclustered: list[IndexedTool] = []
    for e in entries:
        if e.tool.cluster:
            clusters[e.tool.cluster].append(e.tool.id)
        else:
            unclustered.append(e)
    if unclustered and clusters:
        centroids = {
            name: _centroid([by_id[tid].embedding for tid in ids])
            for name, ids in clusters.items()
        }
        for e in unclustered:
            sims = sorted(
                ((cosine(e.embedding, c), name) for name, c in centroids.items()),
                reverse=True,
            )
            best_sim, best = sims[0]
            second_sim = sims[1][0] if len(sims) > 1 else 0.0
            # Relative decision (scale-free): assign only when the best cluster
            # CLEARLY beats the runner-up — absolute cosine magnitudes differ
            # wildly between sparse and dense embeddings, so a fixed floor
            # would be a tuned magic number. Tiny floor only to reject
            # zero-signal tools.
            confident = best_sim >= 0.05 and best_sim >= 1.3 * max(second_sim, 1e-9)
            target = best if confident else "uncategorized"
            clusters[target].append(e.tool.id)
            if target != "uncategorized":
                e.tool.cluster = target
            logger.info(
                "cluster-derived %s -> %s (best %.3f vs next %.3f)",
                e.tool.id, target, best_sim, second_sim,
            )
    elif unclustered:
        clusters["uncategorized"].extend(e.tool.id for e in unclustered)

    # --- version families --------------------------------------------------
    # Family key = the "base" name: explicit replaces/replaced_by links first,
    # then _v<N> suffix stripping. is_current=False for deprecated/superseded.
    family_of: dict[str, str] = {}

    def family_key(entry: IndexedTool) -> str:
        m = _VSUFFIX.match(entry.tool.name)
        base_name = m.group("base") if m else entry.tool.name
        return f"{entry.tool.cluster}.{base_name}"

    for e in entries:
        family_of[e.tool.id] = family_key(e)
    # Link families across explicit replaces/replaced_by edges.
    for e in entries:
        for ref in (e.tool.replaces, e.tool.replaced_by):
            if ref and ref in by_id:
                canonical = family_of[e.tool.id]
                family_of[ref] = canonical

    version_families: dict[str, list[str]] = defaultdict(list)
    for tool_id, fam in family_of.items():
        version_families[fam].append(tool_id)
    version_families = {
        fam: sorted(members) for fam, members in version_families.items() if len(members) > 1
    }

    for e in entries:
        fam = family_of[e.tool.id]
        e.family_id = fam if fam in version_families else None
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
    ordered = sorted(entries, key=lambda e: e.tool.id)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if a.tool.cluster != b.tool.cluster:
                continue  # near-dups live in the same capability cluster
            if a.tool.side_effects != b.tool.side_effects:
                continue  # a read tool and a write tool are never near-duplicates
                          # (similar params ≠ same action: get_object vs upload_to_s3)
            sim = cosine(a.embedding, b.embedding)
            if sim < thresholds.dup_similarity:
                continue
            params_a = {p.name for p in a.tool.parameters}
            params_b = {p.name for p in b.tool.parameters}
            if jaccard(params_a, params_b) >= thresholds.dup_param_overlap:
                uf.union(a.tool.id, b.tool.id)
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

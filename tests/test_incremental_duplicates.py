"""Duplicate detection looks only at pairs that can qualify, and reuses unchanged ones.

Both shortcuts must give exactly the answer of comparing every pair from scratch.
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from tool_router.config import ConfidenceThresholds, Settings
from tool_router.engine import derive, pipeline
from tool_router.engine.derive import find_duplicate_pairs, unchanged_for_duplicates
from tool_router.engine.ingest import ingest_tools
from tool_router.engine.pipeline import RoutingEngine
from tool_router.engine.text import cosine, jaccard
from tool_router.providers.local_tfidf import LocalTfidfEmbedding

PARAMS = ["id", "name", "query", "limit", "date", "user"]


def _tool(rng: random.Random, number: int, words: list[str]) -> dict:
    return {
        "id": f"c.t{number:03d}", "name": f"t{rng.randint(0, 5)}", "cluster": rng.choice("ab"),
        "side_effects": rng.choice(["read", "read", "write"]),
        "description": " ".join(rng.choices(words, k=rng.randint(6, 12))),
        "parameters": [{"name": n, "type": "string", "description": ""}
                       for n in rng.sample(PARAMS, rng.randint(0, 3))],
    }


def _entries(tools: list[dict], dense: bool):
    entries = ingest_tools(tools)
    if dense:       # a vector that depends only on the tool's own text, like a hosted model
        for entry in entries:
            own = random.Random(entry.search_text)
            entry.embedding = [own.choice([0.0, own.random()]) for _ in range(6)]
    else:
        provider = LocalTfidfEmbedding()
        texts = [e.search_text for e in entries]
        provider.fit_documents(texts)
        for entry, vector in zip(entries, provider.embed_documents(texts)):
            entry.embedding = vector
    return entries


def _every_pair(entries, thresholds):
    """The plain definition: compare every pair."""
    ordered = sorted(entries, key=lambda e: e.tool.id)
    return [
        (a.tool.id, b.tool.id)
        for i, a in enumerate(ordered) for b in ordered[i + 1:]
        if a.tool.cluster == b.tool.cluster and a.tool.side_effects == b.tool.side_effects
        and jaccard({p.name for p in a.tool.parameters},
                    {p.name for p in b.tool.parameters}) >= thresholds.dup_param_overlap
        and cosine(a.embedding, b.embedding) >= thresholds.dup_similarity
    ]


@pytest.mark.parametrize("dense", [True, False])
@pytest.mark.parametrize("overlap", [0.0, 0.34, 1.0])
def test_candidates_and_reuse_match_comparing_every_pair(dense, overlap):
    thresholds = ConfidenceThresholds(dup_similarity=0.3, dup_param_overlap=overlap)
    found = 0
    for seed in range(60):
        rng = random.Random(seed)
        words = [f"w{i}" for i in range(rng.randint(6, 25))]
        tools = [_tool(rng, i, words) for i in range(rng.randint(2, 30))]
        before = _entries(tools, dense)
        known = find_duplicate_pairs(before, thresholds)
        assert known == _every_pair(before, thresholds)

        changed = [t for t in tools if rng.random() > 0.15]                    # delete some
        changed = [_tool(rng, int(t["id"][3:]), words) if rng.random() < 0.2 else t
                   for t in changed]                                           # rewrite some
        changed += [_tool(rng, 100 + i, words) for i in range(rng.randint(0, 4))]  # add some
        if not changed:
            continue
        after = _entries(changed, dense)
        incremental = find_duplicate_pairs(
            after, thresholds, unchanged_for_duplicates(after, before), known)
        assert incremental == _every_pair(after, thresholds), seed
        found += len(incremental)
    assert found                                       # the comparison was not vacuous


def test_an_update_compares_only_the_changed_tool(catalog, monkeypatch):
    class Hosted:
        stamp = "hosted:1"
        def fit_documents(self, texts): pass
        def embed_documents(self, texts): return [[1.0, len(t) / 7] for t in texts]
        def embed_query(self, text): return [1.0, 1.0]

    monkeypatch.setattr(pipeline, "make_embedding_provider", lambda name: Hosted())
    engine = RoutingEngine(catalog, settings=replace(Settings(), embedding_provider="gemini"))
    compared = []
    monkeypatch.setattr(derive, "cosine", lambda a, b, *lengths: compared.append(1) or cosine(a, b))
    tool = engine.get_tool("comm.send_email")

    engine.update_tool(tool["id"], {**tool, "description": "Send one email message to a chosen person"})

    same_group = [t for t in engine.list_tools()
                  if (t["cluster"], t["side_effects"]) == (tool["cluster"], tool["side_effects"])]
    assert len(compared) <= len(same_group) - 1


def test_changes_give_the_same_groups_as_a_fresh_start(catalog):
    engine = RoutingEngine(catalog, settings=Settings())
    new = {**engine.get_tool("fin.get_revenue_report"), "id": "fin.get_revenue_summary",
           "name": "get_revenue_summary"}
    new.pop("version", None)

    engine.create_tool(new)
    engine.delete_tool("comm.send_email")

    state = engine.snapshot()                        # tools plus the declared groups
    fresh = RoutingEngine(
        {"tools": state.tools, "near_duplicate_groups": state.near_duplicate_groups},
        settings=Settings(),
    )
    assert sorted(engine.maps.dup_groups) == sorted(fresh.maps.dup_groups)

"""Hybrid retrieval: fused multi-signal scoring (engine-design.md §2–3).

fused(t) = w_dense * cosine(query_vec, tool_vec)      # semantic
         + w_bm25  * bm25(query, tool)                # lexical (exact terms)
         + w_param * param_match(query, tool)         # does it take what's mentioned?
         - side-effect penalty  (destructive tool, non-destructive query)
         - deprecated penalty   (old version, no legacy signal)

Scores are order-independent by construction (defeats positional bias).
Tool NAMES are diluted inside search_text rather than being a signal of
their own (defeats naming decoys / cross-cluster traps).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tool_router.config import RetrievalWeights
from tool_router.domain.models import IndexedTool, Vector
from tool_router.domain.naming import DESTRUCTIVE_VERBS
from tool_router.engine.bm25 import BM25Index
from tool_router.engine.text import STOPWORDS, cosine, tokenize

# Language-level signal lexicons (linguistic, not catalog- or testcase-specific).
LEGACY_TOKENS = frozenset("legacy deprecated v1 old previous".split())
DEICTIC_PRONOUNS = frozenset("it this that them these those one".split())
SWEEPING_TOKENS = frozenset("all every everything entire permanently".split())
CONTINUITY_TOKENS = frozenset("always usual usually normally typically".split())
# Personally-identifying / privileged data categories: tools scoped to these
# are over-privileged for queries that don't ask for them (least privilege).
# "address" is deliberately absent: it usually means an email, server or web address.
SENSITIVE_TOKENS = frozenset("ssn social salary dob birthdate pii compensation".split())

_ID_LIKE = re.compile(r"\b(?:[A-Za-z]+-\d+|\d{2,}|[A-Z]{2,}\d+)\b")   # U-1002, CUST-22, 500
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_PROPER_NOUN = re.compile(r"(?<!^)(?<![.!?]\s)\b[A-Z][a-z]+\b")        # mid-sentence Capitalized
_ENUM_TOKEN = r"(?!and\b|then\b|or\b)[A-Za-z0-9_$-]{1,30}"
_ENUMERATION = re.compile(
    rf"\b{_ENUM_TOKEN}(?:\s*,\s*{_ENUM_TOKEN})+(?:\s*,)?\s+and\s+{_ENUM_TOKEN}\b"
)


@dataclass
class QuerySignals:
    """Everything the engine derives from the raw query text."""

    text: str
    tokens: list[str] = field(default_factory=list)
    content: set[str] = field(default_factory=set)
    destructive_intent: bool = False
    legacy_signal: bool = False
    has_identifier: bool = False           # concrete referent: id/email/name/enumeration
    deictic_reference: bool = False        # "it", "them", ... standing in for an object
    enumeration_count: int = 0             # "A, B, and C" -> 3
    sweeping: bool = False                 # "all/every/permanently" — bulk scope
    continuity: bool = False               # "as we always do" — ambiguous version signal
    sensitive_request: bool = False        # explicitly asks for PII-class data


def analyze_query(query: str) -> QuerySignals:
    """Extract routing signals from the query (deterministic, no LLM)."""
    tokens = tokenize(query)
    token_set = set(tokens)
    enum_match = _ENUMERATION.search(query)
    enum_count = 0
    if enum_match:
        span = enum_match.group(0)
        head, _, tail = span.rpartition(" and ")
        items = [p.strip() for p in head.split(",") if p.strip()] + [tail.strip()]
        enum_count = len([i for i in items if i])
    has_identifier = bool(
        _ID_LIKE.search(query)
        or _EMAIL.search(query)
        or _PROPER_NOUN.search(query)
        or enum_count >= 2
    )
    return QuerySignals(
        text=query,
        tokens=tokens,
        content={t for t in tokens if t not in STOPWORDS},
        destructive_intent=bool(token_set & DESTRUCTIVE_VERBS),
        legacy_signal=bool(token_set & LEGACY_TOKENS),
        has_identifier=has_identifier,
        deictic_reference=bool(token_set & DEICTIC_PRONOUNS),
        enumeration_count=enum_count,
        sweeping=bool(token_set & SWEEPING_TOKENS),
        continuity=bool(token_set & CONTINUITY_TOKENS),
        sensitive_request=bool(token_set & SENSITIVE_TOKENS),
    )


@dataclass
class Candidate:
    """One scored tool with its per-signal breakdown (kept for the trace)."""

    entry: IndexedTool
    fused: float
    dense: float
    bm25: float
    param_match: float

    @property
    def tool_id(self) -> str:
        return self.entry.tool.id


def score_tools(
    entries: list[IndexedTool],
    bm25_index: BM25Index,
    query_vector: Vector,
    signals: QuerySignals,
    weights: RetrievalWeights,
) -> list[Candidate]:
    """Score every tool against the query; returns candidates sorted best-first.

    Scoring the full catalog is intentional at this scale (~64–100 tools it is
    microseconds and avoids recall loss from premature filtering); cluster
    pre-filtering is a documented scale feature for much larger catalogs.
    """
    bm25_scores = bm25_index.scores(sorted(signals.content))  # content tokens only
    candidates: list[Candidate] = []
    for i, entry in enumerate(entries):
        dense = cosine(query_vector, entry.embedding)
        lexical = bm25_scores[i]
        overlap = len(signals.content & entry.param_tag_words)
        param_match = min(1.0, overlap / 3.0)  # saturate: 3+ shared words is a strong match

        fused = weights.dense * dense + weights.bm25 * lexical + weights.param_match * param_match
        if entry.tool.side_effects == "destructive" and not signals.destructive_intent:
            fused -= weights.side_effect_penalty
        if not entry.is_current and not signals.legacy_signal:
            fused -= weights.deprecated_penalty
        # Least privilege: a tool exposing PII-class fields is over-privileged
        # for a query that never asks for such data (e.g. an SSN-scoped
        # employee record fetch when only an id lookup is needed).
        if not signals.sensitive_request and (entry.param_tag_words & SENSITIVE_TOKENS):
            fused -= 0.10

        candidates.append(
            Candidate(entry=entry, fused=fused, dense=dense, bm25=lexical, param_match=param_match)
        )
    # Deterministic ordering: score desc, then id asc for stable ties.
    candidates.sort(key=lambda c: (-c.fused, c.tool_id))
    return candidates

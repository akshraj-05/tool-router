"""Shared text utilities: tokenizer + stopwords + similarity helpers.

One tokenizer for everything (BM25, TF-IDF, capability matching) so all
signals live in the same token space. Deterministic, dependency-free.
"""

from __future__ import annotations

import math
import re
from typing import Iterable

from tool_router.domain.models import Vector

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_'-]*")

# Minimal English stopword list — just enough to keep function words from
# dominating lexical overlap. Deliberately small: aggressive lists can strip
# meaningful words in short tool descriptions.
STOPWORDS: frozenset[str] = frozenset(
    """a an and are as at be but by for from has have if in into is it its of on or
    that the their them then there these this to was we were what when which who will
    with you your me my our i so do does did done can could should would about
    up out off down
    """.split()
)

_AMPERSAND_ABBREV = re.compile(r"\b([a-z0-9]+)&([a-z0-9]+)\b")
_PURPOSE_CLAUSE = re.compile(r"\b(?:so that|so|in order to)\b", re.IGNORECASE)


_PARTICLES = frozenset({"up", "in", "out", "off", "on", "back", "down"})


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens; splits snake_case, keeps ids like 'u-1002'.

    Deterministic, domain-independent normalizations (standard IR practice):
    - light plural stemming — for words ending in 's' ALSO emit the singular
      ("emails" -> emails + email, "500s" -> 500s + 500);
    - verb-particle joining — for phrasal verbs ("look up", "sign in") ALSO
      emit the joined form ("lookup", "signin") so they match the snake_case
      naming convention of API tool names (``lookup_employee``).
    """
    tokens: list[str] = []

    def _emit(tok: str) -> None:
        tokens.append(tok)
        if len(tok) >= 4 and tok.endswith("s") and not tok.endswith("ss"):
            tokens.append(tok[:-1])

    lowered = text.lower()
    # Ampersand abbreviations ("p&l", "q&a"): emit collapsed variants
    # ("pnl"/"pl", "qna"/"qa") — a general orthographic convention.
    for m in _AMPERSAND_ABBREV.finditer(lowered):
        a, b = m.group(1), m.group(2)
        tokens.append(a + "n" + b)
        tokens.append(a + b)

    raw = _TOKEN_RE.findall(lowered)
    for i, tok in enumerate(raw):
        # split snake_case identifiers into their words AND keep the original
        if "_" in tok:
            for part in tok.split("_"):
                if part:
                    _emit(part)
        _emit(tok)
        if i + 1 < len(raw) and raw[i + 1] in _PARTICLES and tok.isalpha():
            tokens.append(tok + raw[i + 1])
    return tokens


def strip_purpose_clause(text: str) -> str:
    """Drop trailing purpose clauses ("... so people can find it later").

    Purpose subclauses express WHY, not WHAT — the command is the main clause,
    and letting purpose vocabulary drive tool choice routes to the wrong
    action (e.g. a search tool instead of the requested upload). The clause is
    only stripped when a meaningful main clause remains.
    """
    m = _PURPOSE_CLAUSE.search(text)
    if not m:
        return text
    head = text[: m.start()].strip(" ,;—-")
    return head if len(content_tokens(head)) >= 3 else text


def content_tokens(text: str) -> list[str]:
    """Tokens minus stopwords — the 'meaningful' words of a text."""
    return [t for t in tokenize(text) if t not in STOPWORDS]


def norm(v: Vector | None) -> float:
    """Length of a vector (0.0 for a missing or empty one)."""
    if v is None:
        return 0.0
    values = v.values() if isinstance(v, dict) else v
    return math.sqrt(sum(x * x for x in values))


def cosine(
    a: Vector | None,
    b: Vector | None,
    norm_a: float | None = None,
    norm_b: float | None = None,
) -> float:
    """Cosine similarity for sparse dict vectors or dense list vectors.

    Pass ``norm_a`` / ``norm_b`` (from :func:`norm`) when comparing the same
    vector many times, so its length is not computed again for every pair.
    """
    if a is None or b is None:
        return 0.0
    if isinstance(a, dict) and isinstance(b, dict):
        if len(b) < len(a):  # iterate over the smaller map
            a, b, norm_a, norm_b = b, a, norm_b, norm_a
        dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        dot = sum(x * y for x, y in zip(a, b))
    else:
        return 0.0
    na = norm(a) if norm_a is None else norm_a
    nb = norm(b) if norm_b is None else norm_b
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard overlap of two token collections."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

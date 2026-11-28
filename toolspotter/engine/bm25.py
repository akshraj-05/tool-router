"""Okapi BM25 (pure Python, dependency-free).

The sparse/lexical half of hybrid retrieval: catches exact terms — "excel",
"csv", service names, ids — that embeddings can blur. ~64-100 docs, so a
simple inverted-index-free implementation is amply fast (microseconds).
"""

from __future__ import annotations

import math
from collections import Counter

K1 = 1.5
B = 0.75


class BM25Index:
    """BM25 over pre-tokenized documents; scores normalized to 0..1 per query."""

    def __init__(self, tokenized_docs: list[list[str]]) -> None:
        self._docs = [Counter(toks) for toks in tokenized_docs]
        self._doc_lens = [sum(c.values()) for c in self._docs]
        self._avg_len = (sum(self._doc_lens) / len(self._doc_lens)) if self._docs else 0.0
        n = len(self._docs)
        df: Counter[str] = Counter()
        for counts in self._docs:
            df.update(counts.keys())
        # BM25+ style floor at 0 keeps very common terms from going negative.
        self._idf = {
            term: max(0.0, math.log((n - count + 0.5) / (count + 0.5) + 1.0))
            for term, count in df.items()
        }

    def idf(self, term: str) -> float:
        """Corpus IDF of a term (0.0 for unseen terms)."""
        return self._idf.get(term, 0.0)

    @property
    def mean_idf(self) -> float:
        """Average IDF across the corpus vocabulary — a scale-free rarity unit."""
        return sum(self._idf.values()) / len(self._idf) if self._idf else 1.0

    # Saturation constant for score normalization. We deliberately do NOT
    # normalize by the per-query max: that would hand the best match a 1.0
    # even for garbage queries, destroying out-of-scope detection. Saturation
    # (raw / (raw + K)) is monotone and keeps weak absolute matches weak.
    _SATURATION = 3.0

    def scores(self, query_tokens: list[str]) -> list[float]:
        """BM25 score per document, saturation-normalized to 0..1."""
        out: list[float] = []
        for counts, dlen in zip(self._docs, self._doc_lens):
            score = 0.0
            denom_len = K1 * (1 - B + B * (dlen / self._avg_len)) if self._avg_len else K1
            for term in set(query_tokens):
                tf = counts.get(term, 0)
                if tf == 0:
                    continue
                idf = self._idf.get(term, 0.0)
                score += idf * (tf * (K1 + 1)) / (tf + denom_len)
            out.append(score / (score + self._SATURATION))
        return out

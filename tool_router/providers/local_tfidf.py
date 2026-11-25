"""Local deterministic embedding provider: TF-IDF sparse vectors.

Why this exists (design rationale):
- The engine must route with ZERO external dependencies or credentials
  (degradation guarantee — works on any machine, no API key required).
- At catalog scale (~60–100 tools) TF-IDF cosine is a strong dense-ish
  signal: tool descriptions and queries share vocabulary heavily.
- Deterministic: same corpus -> same vectors -> reproducible benchmarks.

The Gemini embedding provider implements the same interface; the
engine does not know which one it is using. This is the default.
"""

from __future__ import annotations

import math
from collections import Counter

from tool_router.domain.models import Vector
from tool_router.engine.text import content_tokens


class LocalTfidfEmbedding:
    """Sparse TF-IDF vectors over the fitted tool corpus."""

    def __init__(self) -> None:
        self._idf: dict[str, float] = {}
        self._n_docs: int = 0
        self._corpus_stamp: str = "unfitted"

    @property
    def stamp(self) -> str:
        """Cache stamp. TF-IDF vectors are corpus-dependent, so the stamp
        includes a corpus hash — any catalog change correctly invalidates
        the persisted vectors (they'd be incomparable otherwise)."""
        return f"local-tfidf:{self._corpus_stamp}"

    # -- EmbeddingProvider interface -------------------------------------

    def fit_documents(self, texts: list[str]) -> None:
        """Compute smoothed IDF over the tool corpus."""
        import hashlib
        self._corpus_stamp = hashlib.sha256("\x00".join(texts).encode()).hexdigest()[:16]
        self._n_docs = len(texts)
        df: Counter[str] = Counter()
        for text in texts:
            df.update(set(content_tokens(text)))
        self._idf = {
            term: math.log((1 + self._n_docs) / (1 + count)) + 1.0
            for term, count in df.items()
        }

    def embed_documents(self, texts: list[str]) -> list[Vector]:
        return [self._vectorize(t) for t in texts]

    def embed_query(self, text: str) -> Vector:
        # Unseen query terms get a neutral IDF of 1.0 instead of being
        # dropped: they still contribute if a doc shares them via BM25, and
        # keeping them makes vectors robust to vocabulary drift.
        return self._vectorize(text, default_idf=1.0)

    # -- internals ---------------------------------------------------------

    def _vectorize(self, text: str, default_idf: float | None = None) -> Vector:
        counts = Counter(content_tokens(text))
        if not counts:
            return {}
        max_tf = max(counts.values())
        vec: dict[str, float] = {}
        for term, tf in counts.items():
            idf = self._idf.get(term, default_idf if default_idf is not None else 0.0)
            if idf > 0.0:
                vec[term] = (0.5 + 0.5 * tf / max_tf) * idf  # augmented TF * IDF
        return vec

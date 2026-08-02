"""Provider interfaces (ports, in clean-architecture terms)."""

from __future__ import annotations

from typing import Protocol

from tool_router.domain.models import Vector


class EmbeddingProvider(Protocol):
    """Turns texts into vectors living in one consistent space.

    Contract:
    - ``fit_documents`` is called ONCE per registry (re)index with all tool
      texts; implementations may use it to build corpus statistics (IDF) or
      may ignore it (hosted dense models).
    - ``embed_documents`` / ``embed_query`` must produce vectors comparable
      via cosine within the same fitted space.
    """

    def fit_documents(self, texts: list[str]) -> None: ...

    def embed_documents(self, texts: list[str]) -> list[Vector]: ...

    def embed_query(self, text: str) -> Vector: ...

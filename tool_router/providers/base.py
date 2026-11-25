"""Provider interfaces (ports, in clean-architecture terms) and shared helpers."""

from __future__ import annotations

import json
from typing import Any, Protocol

from tool_router.domain.errors import ProviderError
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


class ChatClient(Protocol):
    """A generation LLM: one system prompt + one user turn in, text out.

    Implemented by ``GeminiChat``. Implementations must
    raise ``ProviderError`` (never a raw SDK exception) on any failure so the
    engine can degrade to its local planner.
    """

    def complete(self, system: str, user: str, max_tokens: int = 700) -> str: ...


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object out of an LLM reply (tolerates code fences)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start == -1:
        raise ProviderError("LLM reply contained no JSON object")
    # Balanced-brace scan: take the FIRST complete object (models sometimes
    # emit trailing prose or a second object — "first { to last }" breaks then).
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"LLM reply was not valid JSON: {exc}") from exc
    raise ProviderError("LLM reply had an unterminated JSON object")

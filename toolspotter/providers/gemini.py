"""Google Gemini provider (chat planner + embeddings) — swappable via config.

Same interfaces as the local provider — the engine is provider-agnostic.
stdlib-only (urllib) — no new dependencies. Key from GEMINI_API_KEY / stored
secret; never logged. 429 rate limits are retried with bounded backoff; every
other failure raises ProviderError so the engine degrades to local components.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from toolspotter.domain.errors import ProviderError
from toolspotter.log import get_logger
from toolspotter.store import load_secret

logger = get_logger("providers.gemini")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_RETRIES = (10, 25, 45)  # seconds of backoff on 429
DEFAULT_CHAT_MODEL = "gemini-3.5-flash-lite"
# These models "think" before answering unless told not to, and the thinking is
# taken from the reply limit, which cuts our short replies off.
_THINKING_CAN_BE_OFF = ("gemini-2.5-flash",)


def _key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or load_secret("gemini_key")
    if not key:
        raise ProviderError("No Gemini credentials (GEMINI_API_KEY) found.")
    return key


def _post(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    for attempt, delay in enumerate((0,) + _RETRIES):
        if delay:
            logger.warning("gemini rate-limited — retrying in %ss", delay)
            time.sleep(delay)
        req = urllib.request.Request(
            url, data=body,
            headers={"content-type": "application/json", "x-goog-api-key": _key()},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < len(_RETRIES):
                continue
            raise ProviderError(f"Gemini call failed: HTTP {exc.code}") from exc
        except Exception as exc:
            raise ProviderError(f"Gemini call failed: {exc}") from exc
    raise ProviderError("Gemini call failed after retries")


def _check_not_cut_off_by_thinking(data: dict, model: str) -> None:
    """Raise if a model spent the reply limit on thinking and returned a fragment."""
    try:
        cut_off = data["candidates"][0].get("finishReason") == "MAX_TOKENS"
        thinking = data.get("usageMetadata", {}).get("thoughtsTokenCount")
    except (KeyError, IndexError, TypeError, AttributeError):
        return                       # an unexpected shape is reported by the caller
    if cut_off and thinking:
        raise ProviderError(
            f"{model} used {thinking} tokens on thinking and its reply was cut off "
            f"(try the model {DEFAULT_CHAT_MODEL})"
        )


class GeminiChat:
    """Chat client with the same .complete(system, user, max_tokens) contract."""

    def __init__(self, model_id: str | None = None) -> None:
        self._model = model_id or os.environ.get("TOOLSPOTTER_LLM_MODEL") or DEFAULT_CHAT_MODEL
        _key()  # fail fast if unconfigured
        logger.info("gemini chat ready: model=%s key=present", self._model)

    def complete(self, system: str, user: str, max_tokens: int = 700) -> str:
        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.0},
        }
        if self._model.startswith(_THINKING_CAN_BE_OFF):
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        # Gemma models on this API don't accept systemInstruction — fold the
        # system prompt into the user turn instead (same effective prompt).
        if self._model.startswith("gemma"):
            payload["contents"][0]["parts"][0]["text"] = f"{system}\n\n{user}"
        else:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = _post(f"{_BASE}/{self._model}:generateContent", payload)
        _check_not_cut_off_by_thinking(data, self._model)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError(f"Gemini reply had no text (finish={data.get('candidates')})") from exc


class GeminiEmbedding:
    """EmbeddingProvider over gemini-embedding-001 (dense, per-tool cacheable)."""

    def __init__(self, model_id: str | None = None) -> None:
        self._model = model_id or os.environ.get(
            "TOOLSPOTTER_EMBEDDING_MODEL_GEMINI", "gemini-embedding-001"
        )
        _key()
        logger.info("gemini embeddings ready: model=%s", self._model)

    @property
    def stamp(self) -> str:
        return f"gemini:{self._model}"

    def fit_documents(self, texts: list[str]) -> None:
        return None

    def _embed_one(self, text: str) -> list[float]:
        data = _post(f"{_BASE}/{self._model}:embedContent",
                     {"content": {"parts": [{"text": text[:8000]}]}})
        try:
            values = data["embedding"]["values"]
        except (KeyError, TypeError) as exc:
            raise ProviderError("Gemini embedding reply had no vector") from exc
        if not (isinstance(values, list) and values and all(isinstance(v, (int, float)) for v in values)):
            raise ProviderError("Gemini embedding reply had an unusable vector")
        return values

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)

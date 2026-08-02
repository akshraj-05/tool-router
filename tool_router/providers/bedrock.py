"""AWS Bedrock provider: chat planner (BedrockChat) and Titan embeddings
(BedrockEmbedding). Implemented but NOT live-tested end-to-end (the team's
actual key turned out to be Google Gemini — see providers/gemini.py, which
is live-verified and is the recommended hosted provider today).

Design (see storage.md / engine-design.md):
- Credentials come ONLY from the environment (`AWS_BEARER_TOKEN_BEDROCK` or the
  standard AWS credential chain). Never logged, never persisted.
- Every failure surfaces as a typed ``ProviderError`` — callers (the LLM
  planner) catch it and degrade to the local planner, so the engine never
  crashes because of the LLM.
- ``boto3`` is an optional extra (``pip install .[bedrock]``); importing this
  module without it is fine — construction fails with a clear ProviderError.
- Budget guard: requests cap ``max_tokens`` and the planner sends compact
  shortlists only (~8 tools), keeping each call cheap.
"""

from __future__ import annotations

import json
import os
from typing import Any

from tool_router.domain.errors import ProviderError
from tool_router.log import get_logger

logger = get_logger("providers.bedrock")

DEFAULT_MODEL = "anthropic.claude-3-5-haiku-20241022-v1:0"


class BedrockChat:
    """Minimal Bedrock Converse-API chat client for structured planning calls."""

    def __init__(self, model_id: str | None = None, region: str | None = None) -> None:
        self._model_id = model_id or os.environ.get("TOOL_ROUTER_LLM_MODEL", DEFAULT_MODEL)
        region = region or os.environ.get("AWS_REGION", "us-east-1")
        try:
            import boto3  # optional extra — deferred import by design
        except ImportError as exc:
            raise ProviderError(
                "boto3 is not installed — install with `pip install .[bedrock]`"
            ) from exc

        has_bearer = bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK"))
        has_keys = bool(os.environ.get("AWS_ACCESS_KEY_ID"))
        if not (has_bearer or has_keys):
            raise ProviderError(
                "No Bedrock credentials found (AWS_BEARER_TOKEN_BEDROCK or AWS "
                "access keys) — running without the LLM planner."
            )
        try:
            self._client = boto3.client("bedrock-runtime", region_name=region)
        except Exception as exc:  # boto3 raises many config error types
            raise ProviderError(f"Could not create Bedrock client: {exc}") from exc
        logger.info("bedrock chat ready: model=%s region=%s key=present", self._model_id, region)

    def complete(self, system: str, user: str, max_tokens: int = 700) -> str:
        """One chat completion; returns the raw text of the reply."""
        try:
            response = self._client.converse(
                modelId=self._model_id,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0},
            )
            parts = response["output"]["message"]["content"]
            return "".join(p.get("text", "") for p in parts)
        except Exception as exc:
            raise ProviderError(f"Bedrock converse call failed: {exc}") from exc


class BedrockEmbedding:
    """Titan text embeddings on Bedrock (dense vectors).

    Same EmbeddingProvider interface as the local TF-IDF; the engine doesn't
    know which is active. ``stamp`` is the model id, so the content-hash cache
    reuses vectors across restarts AND mutations (only changed tools re-embed).
    """

    def __init__(self, model_id: str | None = None, region: str | None = None) -> None:
        self._model_id = model_id or os.environ.get(
            "TOOL_ROUTER_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0"
        )
        region = region or os.environ.get("AWS_REGION", "us-east-1")
        try:
            import boto3
        except ImportError as exc:
            raise ProviderError("boto3 missing — pip install .[bedrock]") from exc
        if not (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or os.environ.get("AWS_ACCESS_KEY_ID")):
            raise ProviderError("No Bedrock credentials for the embedding provider.")
        try:
            self._client = boto3.client("bedrock-runtime", region_name=region)
        except Exception as exc:
            raise ProviderError(f"Could not create Bedrock client: {exc}") from exc
        logger.info("bedrock embeddings ready: model=%s", self._model_id)

    @property
    def stamp(self) -> str:
        return f"bedrock:{self._model_id}"

    def fit_documents(self, texts: list[str]) -> None:  # dense model: nothing to fit
        return None

    def _embed_one(self, text: str) -> list[float]:
        try:
            response = self._client.invoke_model(
                modelId=self._model_id,
                body=json.dumps({"inputText": text[:8000]}),
            )
            return json.loads(response["body"].read())["embedding"]
        except Exception as exc:
            raise ProviderError(f"Bedrock embedding call failed: {exc}") from exc

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)


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

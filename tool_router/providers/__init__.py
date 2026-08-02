"""Providers layer: swappable external capabilities behind small interfaces.

Default implementations are local and dependency-free so the engine always
runs (graceful degradation is a core design requirement). Hosted providers
(Gemini, AWS Bedrock) plug in via the same interfaces.
"""

from tool_router.providers.base import EmbeddingProvider
from tool_router.providers.local_tfidf import LocalTfidfEmbedding


def make_embedding_provider(name: str) -> EmbeddingProvider:
    """Factory: resolve an embedding provider by configured name.

    Degradation guarantee: an unavailable hosted provider (no boto3/creds)
    falls back to the local TF-IDF with a WARNING — the engine always runs.
    """
    if name == "local":
        return LocalTfidfEmbedding()
    if name == "gemini":
        try:
            from tool_router.providers.gemini import GeminiEmbedding
            return GeminiEmbedding()
        except Exception as exc:
            from tool_router.log import get_logger
            get_logger("providers").warning(
                "gemini embeddings unavailable (%s) — using local TF-IDF fallback", exc
            )
            return LocalTfidfEmbedding()
    if name == "bedrock":
        try:
            from tool_router.providers.bedrock import BedrockEmbedding
            return BedrockEmbedding()
        except Exception as exc:
            from tool_router.log import get_logger
            get_logger("providers").warning(
                "bedrock embeddings unavailable (%s) — using local TF-IDF fallback", exc
            )
            return LocalTfidfEmbedding()
    raise ValueError(f"Unknown embedding provider: {name!r}")


__all__ = ["EmbeddingProvider", "LocalTfidfEmbedding", "make_embedding_provider"]

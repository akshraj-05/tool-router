"""Engine configuration.

All tunables live here (env-overridable, sane defaults) so behaviour is
reproducible and reviewable in one place. Precedence (per design docs):
explicit argument > environment variable > default. Secrets are read from the
environment only and are never logged or persisted by this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RetrievalWeights:
    """Weights of the fused retrieval score (see engine-design.md §2).

    fused = dense*w_dense + bm25*w_bm25 + capability*w_capability
            - side_effect_penalty (destructive tools on non-destructive queries)
            - deprecated_penalty  (deprecated tools without a legacy signal)
    """

    dense: float = 0.45
    bm25: float = 0.45
    capability: float = 0.10
    side_effect_penalty: float = 0.15
    deprecated_penalty: float = 0.15


@dataclass(frozen=True)
class ConfidenceThresholds:
    """Dynamic-K / confidence gate tunables (engine-design.md §5)."""

    out_of_scope_floor: float = 0.18   # top fused score below this => no tool applies
    hyde_max: float = 0.38             # fire conditional HyDE when top fused < this (no floor lower-bound)
    shortlist_ratio: float = 0.55      # keep candidates scoring >= top * ratio
    shortlist_max: int = 8             # hard cap on candidates handed to the planner
    dup_similarity: float = 0.60       # cosine threshold for auto near-duplicate detection
    dup_param_overlap: float = 0.34    # param-name Jaccard threshold for auto near-dups
    # Arbitration: when the LLM planner answers "clarify" but the local gate already
    # scored the top tool at or above this, the LLM refusal is treated as an
    # over-clarification and the local plan is used instead (eval_report.md: the step
    # that lifted accuracy from 69% to 86%). Set very high (e.g. 2.0) to never override.
    arbitration_min_fused: float = 0.45


@dataclass(frozen=True)
class Settings:
    """Top-level engine settings."""

    weights: RetrievalWeights = field(default_factory=RetrievalWeights)
    confidence: ConfidenceThresholds = field(default_factory=ConfidenceThresholds)
    embedding_provider: str = "local"   # "local" | "gemini"
    llm_provider: str = "none"          # "none" (local planner) | "gemini"
    llm_model: str = ""                 # "" = the provider's own default model
    # Default OFF: live testing found LLM ingest enrichment can blur near-duplicate
    # tools (e.g. routed "total revenue" to the wrong near-identical finance tool)
    # and adds a one-time cold-start cost per tool — see eval_report.md. Opt in only
    # for thin/vague catalogs where it's likely to help more than it blurs.
    llm_enrich: bool = False
    max_widen_rounds: int = 1           # bounded shortlist-widening re-plans (LLM cost cap)
    rebuild: bool = False               # wipe caches and re-derive (escape hatch)

    @staticmethod
    def from_env() -> "Settings":
        """Resolve settings with precedence: env var > stored config > default.

        Stored config is written by `tool-router init` / `config set` / the
        /setup page (storage.md: users never hand-edit files).
        """
        from tool_router.store import load_stored_config
        stored = load_stored_config()

        def pick(env_name: str, stored_key: str, default: str) -> str:
            return os.environ.get(env_name) or stored.get(stored_key) or default

        return Settings(
            weights=RetrievalWeights(
                dense=_env_float("TOOL_ROUTER_W_DENSE", RetrievalWeights.dense),
                bm25=_env_float("TOOL_ROUTER_W_BM25", RetrievalWeights.bm25),
                capability=_env_float("TOOL_ROUTER_W_CAPABILITY", RetrievalWeights.capability),
                side_effect_penalty=_env_float(
                    "TOOL_ROUTER_W_SIDE_EFFECT", RetrievalWeights.side_effect_penalty
                ),
                deprecated_penalty=_env_float(
                    "TOOL_ROUTER_W_DEPRECATED", RetrievalWeights.deprecated_penalty
                ),
            ),
            confidence=ConfidenceThresholds(
                out_of_scope_floor=_env_float(
                    "TOOL_ROUTER_FLOOR", ConfidenceThresholds.out_of_scope_floor
                ),
                hyde_max=_env_float("TOOL_ROUTER_HYDE_MAX", ConfidenceThresholds.hyde_max),
                arbitration_min_fused=_env_float(
                    "TOOL_ROUTER_ARBITRATE_MIN", ConfidenceThresholds.arbitration_min_fused
                ),
                shortlist_ratio=_env_float(
                    "TOOL_ROUTER_SHORTLIST_RATIO", ConfidenceThresholds.shortlist_ratio
                ),
                shortlist_max=int(_env_float(
                    "TOOL_ROUTER_SHORTLIST_MAX", ConfidenceThresholds.shortlist_max
                )),
                dup_similarity=_env_float(
                    "TOOL_ROUTER_DUP_SIM", ConfidenceThresholds.dup_similarity
                ),
                dup_param_overlap=_env_float(
                    "TOOL_ROUTER_DUP_PARAM_OVERLAP", ConfidenceThresholds.dup_param_overlap
                ),
            ),
            embedding_provider=pick("TOOL_ROUTER_EMBEDDING_PROVIDER", "embedding_provider", "local"),
            llm_provider=pick("TOOL_ROUTER_LLM_PROVIDER", "llm_provider", "none"),
            llm_model=pick("TOOL_ROUTER_LLM_MODEL", "llm_model", ""),
            llm_enrich=pick("TOOL_ROUTER_LLM_ENRICH", "llm_enrich", "off") in ("on", "1", "true"),
            max_widen_rounds=int(_env_float("TOOL_ROUTER_MAX_WIDEN_ROUNDS", 1)),
            rebuild=os.environ.get("TOOL_ROUTER_REBUILD", "") in ("1", "true", "yes"),
        )

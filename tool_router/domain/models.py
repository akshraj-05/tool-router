"""Core domain models.

Design notes
------------
- ``Tool`` is our *normalized* view of a catalog tool. We keep the ORIGINAL
  raw dict alongside it in ``IndexedTool.raw`` because clients (and any
  external token accounting) must receive the tool exactly as registered —
  enrichment is internal to retrieval only (see onboarding.md).
- Vectors are ``dict[str, float]`` sparse maps for the default local TF-IDF
  provider. The provider interface also permits dense ``list[float]``
  (Gemini embeddings); similarity helpers handle both.
- ``RouteResult`` is the routing decision: the core fields returned to clients
  plus ``trace`` and ``tokens_saved_pct``.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

Vector = Union[dict[str, float], list[float]]


class ToolParam(BaseModel):
    """A single tool parameter."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: str = "string"
    required: bool = False
    description: str = ""


class Tool(BaseModel):
    """Normalized tool definition (subset of fields the engine reasons about)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    cluster: str = ""
    description: str = ""
    version: Optional[str] = None
    deprecated: bool = False
    replaced_by: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    side_effects: str = "read"  # read | write | destructive
    parameters: list[ToolParam] = Field(default_factory=list)


class GuardrailIssue(BaseModel):
    """One validation failure, using the guardrail's canonical reason codes."""

    code: str
    message: str


class GuardrailResult(BaseModel):
    """Verdict for a single submitted tool."""

    tool_id: Optional[str] = None
    accepted: bool
    issues: list[GuardrailIssue] = Field(default_factory=list)

    @property
    def primary_reason(self) -> Optional[str]:
        """First (highest-precedence) failure code, or None if accepted."""
        return self.issues[0].code if self.issues else None


class IndexedTool(BaseModel):
    """A tool prepared for retrieval: normalized + enriched + vectorized."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool: Tool
    raw: dict[str, Any]                    # original registration payload (returned to clients)
    search_text: str                       # enriched text used for BM25 + embedding
    tokens: list[str]                      # tokenized search_text
    capability_tokens: set[str]            # from params/tags — capability matching
    embedding: Optional[Vector] = None     # filled by the embedding provider
    family_id: Optional[str] = None        # version-family key, if any
    is_current: bool = True                # False for deprecated/superseded versions


class DerivedMaps(BaseModel):
    """Registry-level structures auto-derived at onboarding (engine-design.md)."""

    clusters: dict[str, list[str]] = Field(default_factory=dict)        # cluster -> tool ids
    dup_groups: list[list[str]] = Field(default_factory=list)           # near-duplicate groups
    version_families: dict[str, list[str]] = Field(default_factory=dict)  # family -> member ids


class RouteResult(BaseModel):
    """Routing decision."""

    selected_tools: list[str] = Field(default_factory=list)
    plan: list[str] = Field(default_factory=list)
    clarify: bool = False
    clarify_question: Optional[str] = None
    # --- diagnostics ---
    tokens_saved_pct: Optional[float] = None
    trace: dict[str, Any] = Field(default_factory=dict)

    def to_response_dict(self) -> dict[str, Any]:
        """The core decision fields of an API response."""
        return {
            "selected_tools": list(self.selected_tools),
            "plan": list(self.plan),
            "clarify": self.clarify,
            "clarify_question": self.clarify_question,
        }

"""Ingestion: raw catalog tool → normalized, index-ready entry.

Deterministic, built from the tool's own fields:
- search_text: every routable text signal a tool carries — name (split),
  description, tags, cluster, parameter names/descriptions. This is what gets
  embedded and BM25-indexed. The tool NAME is
  deliberately diluted by all other signals so naming decoys (cross-cluster
  traps like `data.email_report`) can't dominate matching.
- param_tag_words: words from parameter names, parameter descriptions and tags
  only — the inputs a tool takes (attachment, format, xlsx, ssn...). Used by the
  sensitive-data penalty and by the planner.

The raw dict is preserved as registered (returned to clients; search text never
leaves the retrieval layer).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from toolspotter.domain.errors import CatalogError
from toolspotter.domain.models import IndexedTool, Tool
from toolspotter.engine.text import content_tokens, tokenize
from toolspotter.log import get_logger

logger = get_logger("ingest")


def normalize_tool(raw: dict[str, Any]) -> Tool:
    """Parse a raw catalog dict into the normalized Tool model."""
    try:
        return Tool.model_validate(raw)
    except ValidationError as exc:
        raise CatalogError(f"Unparseable tool definition {raw.get('id')!r}: {exc}") from exc


def build_search_text(tool: Tool) -> str:
    """Compose the retrieval document for a tool."""
    param_bits: list[str] = []
    for p in tool.parameters:
        param_bits.append(p.name)
        if p.description:
            param_bits.append(p.description)
    parts = [
        tool.name.replace("_", " "),
        tool.description,
        " ".join(tool.tags),
        tool.cluster.replace("_", " "),
        " ".join(param_bits),
    ]
    return " ".join(p for p in parts if p)


def param_tag_words(tool: Tool) -> set[str]:
    """Words from a tool's parameter names, parameter descriptions and tags."""
    bits: list[str] = []
    for p in tool.parameters:
        bits.append(p.name)
        if p.description:
            bits.append(p.description)
    bits.extend(tool.tags)
    tokens: set[str] = set()
    for bit in bits:
        tokens.update(content_tokens(bit))
    return tokens


def ingest_tools(raw_tools: list[dict[str, Any]]) -> list[IndexedTool]:
    """Normalize a catalog into index-ready entries (embedding filled later)."""
    if not raw_tools:
        raise CatalogError("Catalog contains no tools.")
    entries: list[IndexedTool] = []
    for raw in raw_tools:
        tool = normalize_tool(raw)
        text = build_search_text(tool)
        entries.append(
            IndexedTool(
                tool=tool,
                raw=raw,
                search_text=text,
                tokens=tokenize(text),
                param_tag_words=param_tag_words(tool),
            )
        )
    logger.info("ingested %d tools", len(entries))
    return entries

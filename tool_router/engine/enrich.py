"""One-time LLM ingest enrichment (onboarding.md Stage 2 — internal only).

For each tool, the LLM produces a clearer description, extra tags/synonyms and
synthetic example queries — merged into the INTERNAL search text so retrieval
matches more phrasings. Safety properties:

- **Grounded**: the prompt forbids inventing capabilities; the model only
  rephrases/expands what the tool's own metadata says. A hallucinated ability
  would mis-route queries, so this is a correctness rule, not a disclosure one.
- **Internal only**: the ORIGINAL registered schema is what clients receive;
  enrichment never leaves the retrieval layer.
- **One-time & cached**: results persist per content-hash (`enrichment.json`
  in the project data dir) — a tool is enriched once per content change,
  keeping the $ cost negligible.
- **Optional & degradable**: no LLM configured / any failure → skipped with a
  warning; the engine works identically minus the recall boost.
"""

from __future__ import annotations

import json

from tool_router.domain.errors import ProviderError
from tool_router.domain.models import IndexedTool
from tool_router.engine.text import tokenize
from tool_router.log import get_logger
from tool_router.store import JsonCache, content_hash

logger = get_logger("enrich")

_SYSTEM = """\
You expand tool metadata for a search index. Given ONE tool's JSON metadata, reply with
STRICT JSON only: {"description": "...", "tags": ["..."], "example_queries": ["...", "..."]}

Rules:
- Rephrase and clarify ONLY what the metadata itself says. NEVER invent capabilities,
  parameters, or behaviours the tool does not declare.
- tags: up to 6 short synonyms/related terms for what the tool does.
- example_queries: 2 short user requests this tool would correctly serve.
"""


def enrich_entries(entries: list[IndexedTool], chat, cache: JsonCache) -> int:
    """Enrich search texts in place. Returns how many tools were enriched."""
    from tool_router.providers.base import extract_json_object

    enriched = 0
    dirty = False
    for entry in entries:
        key = content_hash(entry.search_text)
        result = cache.get(key)
        if result is None:
            tool = entry.tool
            payload = {
                "name": tool.name, "description": tool.description, "tags": tool.tags,
                "parameters": [{"name": p.name, "type": p.type} for p in tool.parameters],
            }
            try:
                reply = chat.complete(_SYSTEM, json.dumps(payload), max_tokens=400)
                result = extract_json_object(reply)
            except ProviderError as exc:
                logger.warning("LLM enrichment stopped (%s) — continuing without it", exc)
                break  # one hard failure ends the pass; engine works without enrichment
            cache.put(key, result)
            dirty = True

        extra_bits = [
            str(result.get("description") or ""),
            " ".join(result.get("tags") or []),
            " ".join(result.get("example_queries") or []),
        ]
        extra = " ".join(b for b in extra_bits if b).strip()
        if extra:
            entry.search_text = f"{entry.search_text} {extra}"
            entry.tokens = tokenize(entry.search_text)
            enriched += 1

    if dirty:
        cache.save()
    if enriched:
        logger.info("LLM-enriched %d tools (cached one-time)", enriched)
    return enriched

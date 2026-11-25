# Onboarding & Auto-Import — Adding Tools

How a user gets their tools into the router. **Users never add tools one-by-one.** Ingestion is
a two-stage pipeline — **guardrail first, then enrichment** — and the same pipeline runs whether
tools arrive via API bulk-upload or auto-import.

```
tool in  →  [1. GUARDRAIL: validate / reject]  →  [2. ENRICH + INDEX]  →  stored
```

Order matters: validate first (reject bad tools), then enrich only the good ones.

Legend: ✅ **NOW** (current plan) · 🟨 **LATER, if time (Day 5–6)** · 🔮 **FUTURE**

---

## Stage 1 — Guardrail (validation)  ✅ NOW

The registry intake validator. Reject with a clear reason:
- **Structural:** valid JSON, required fields present, valid id/name format, valid param types,
  not too many params, valid semver.
- **Referential:** no duplicate id, no dangling `replaced_by`.
- **Quality:** description not too vague, not too long.

Runs on every path (bulk API + auto-import), so automation never sneaks in junk.

## Stage 2 — Enrichment (make it routable)

### Deterministic enrichment  ✅ NOW
- **Embedding** (meaning vector) · **BM25 tokens**
- **Capability signature** — keywords from params/description (`attachment`, `format:excel`, `ssn/pii`)
- **Side-effect tag** — read / write / destructive (from field or inferred from verb)
- **Cluster assignment** + **near-dup group** + **version family** derivation
- **Normalization** (canonical schema shape). A dotted-id → MCP-safe-name sanitizer exists
  (`importers/common.py::mcp_safe_name`) but isn't invoked in the live path today — see
  `integrations.md`.

### LLM enrichment  ✅ BUILT — **OFF by default**
Runs once per tool at onboarding (not per query), so cost is one-time when enabled — but live
testing found it can **blur near-duplicate tools** (a benchmark query that passes without
enrichment mis-routed with it on; see `eval_report.md`), so `TOOL_ROUTER_LLM_ENRICH` defaults to
`off`. Opt in for catalogs with genuinely thin/vague descriptions, where the recall gain is more
likely to outweigh the risk.
- **Description rewrite/expand** for thin/vague tools → better retrieval + passes the guardrail
- **Auto-tags / synonyms / aliases** → boosts recall (an "email" tool gets `notify, message, send`)
- **Synthetic example queries** per tool, embedded alongside → tool-side retrieval boost
- **Infer missing side-effects / capabilities** when metadata is absent

**How we keep it safe (correctness, not disclosure):**
- **Internal only.** We store BOTH the **original** description (returned to the client in
  `selected_tools` — their real tool, unchanged) and the **enriched** version (used ONLY inside
  our matching/embedding layer). Enrichment never leaves the retrieval layer, so the user's tool
  is never altered. → No external "flagging" needed; it's an internal retrieval aid, not a
  functionality claim.
- **Grounded.** Enrichment only rephrases/expands from the tool's real metadata (name, params,
  existing description); it **never invents a capability the tool lacks** (that would mis-route).

---

## Import paths (how the catalog arrives)

| Source | How | Status |
|---|---|---|
| **Bulk API / JSON file** | one `POST /registry` with the whole catalog | ✅ NOW |
| **MCP server, scanned at startup** | `serve` / `mcp` with `--import-mcp "<command>"`: all pages of `list_tools`, strict guardrail, loaded into memory | ✅ BUILT, tested with sample servers |
| **MCP server, to a file** | `tool-router import-mcp` writes the accepted tools for review | ✅ BUILT, tested with sample servers |
| **LangChain / Python** | introspect `.name` / `.description` / `.args_schema` / signatures | 🟨 LATER |
| **Other registries / gateways** | dedicated connectors | 🔮 FUTURE |

---

## Current status (as built)

1. ✅ **Guardrail validation** on every ingest path.
2. ✅ **Deterministic enrichment** (embeddings, capability sig, side-effects, clusters,
   dedup/version maps, normalization).
3. ✅ **LLM enrichment** built (description cleanup, auto-tags/synonyms, example queries) —
   one-time at ingest, internal-only, grounded, original description preserved for what we
   return — but **defaults OFF** (measured to blur near-duplicates; see `eval_report.md`).
4. ✅ **Import paths:** bulk API / JSON file and MCP-server auto-import are built and
   verified end-to-end.

## What we add later

- 🟨 **LangChain/Python introspection** (import tools directly from code, not a spec file).
- 🔮 **More connectors** for other tool registries / gateways.

---

**In one line:** every tool is validated (guardrail) then enriched — deterministically always,
and with one-time internal LLM enrichment available but off by default — arriving via bulk API
or MCP auto-import (both built), with code-introspection import later.

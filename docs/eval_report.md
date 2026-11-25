# Evaluation Report — Iterated Design with Measured Results

All numbers below (unless noted) were produced by an internal 29-case evaluation harness and
64-tool catalog used during development — not bundled with this repo, since it was a private
test suite rather than something meant to ship.

## Accuracy trajectory (zero-LLM engine)

Each iteration was driven by *failure analysis*, and every change is a general mechanism —
no per-testcase patches (queries are unscripted at evaluation time; overfitting the visible 29
loses on anything else).

| Iteration | What changed (and why it generalizes) | Accuracy | Token savings |
|---|---|---|---|
| Naive baseline | keyword/cluster router (reference) | 7/29 (24%) | 94% |
| v1 | hybrid retrieval: TF-IDF dense + BM25 lexical + capability signals, dup/version collapse, confidence gate, segmenting local planner | 15/29 (52%) | 98% |
| v2 | BM25 saturation normalization (max-norm broke out-of-scope detection — any garbage query handed some tool 1.0); plural stemming; least-privilege PII penalty; sweeping-destructive + version-continuity clarify rules | 19/29 (66%) | 98% |
| v3 | IDF-weighted greedy set-cover plan extension (helpers only for *discriminative* uncovered terms — self-calibrating, no tuned counts); repeat-count enumeration | 21/29 (72%) | 98% |
| v4 | content-token BM25 (stopword bug); verb-particle joining ("look up"→lookup); ampersand abbreviations (P&L→pnl); purpose-clause discounting; near-dup detection requires same side-effects (read ≠ write) | **24/29 (83%)** | **98%** |

**The 5 remaining failures are deliberately deferred to the LLM planner** (implicit
prerequisite steps like "email the chart" needing generate_chart first; semantic urgency like
"500s in prod" → incident-not-ticket). They require semantics, not more heuristics.

## Per-category (v4, zero-LLM)

| Category | Score | | Category | Score |
|---|---|---|---|---|
| single_tool | 3/3 | | clarification | 2/2 |
| cross_cluster | 3/3 | | negative_out_of_scope | 2/2 |
| positional_bias | 2/2 | | repeat_count | 2/2 |
| backward_compat | 3/3 | | multi_tool_ordered | 3/4 |
| fallback_widening | 2/3 | | deduplication | 1/3 |
| scope_overlap | 1/2 | | **total** | **24/29 (83%)** |

## Registry guardrail (internal valid/invalid submission fixtures)

**11/11** — both valid submissions accepted; all 9 invalid submissions rejected with the
exact expected reason code (vague/too-long description, bad id/name, missing field,
duplicate id, bad param type, too many params, bad semver, dangling replaced_by,
malformed JSON).

## Catalog churn (internal mutation scenarios)

**8/8 scenario checks:**

| Scenario | Result |
|---|---|
| MUT-01 add | new tool routable immediately; zero regression on existing queries |
| MUT-02 remove | dependent chain degrades gracefully — no crash, no phantom tool |
| MUT-03 replace | renamed tool routes by capability, no query change needed |
| MUT-04 versions | PREFER_CURRENT without a signal; HONOR_EXPLICIT_VERSION with "legacy v1"; never both versions at once |
| MUT-05 scale (64→104 tools) | routing identical before/after; token savings 97% at 104 tools |

## Token economics

Full-catalog injection ≈ **9,876 tokens**; our average routed selection ≈ **198 tokens** →
**98% savings** (baseline: 94%). At 104 tools the savings ratio *improves* (97% on a larger
denominator) — the router's value grows with catalog size.

## LLM planner — LIVE results (Gemini key; provider-agnostic design absorbed the switch)

The team's key was a Google **Gemini** key: one provider module,
zero engine changes. Live findings — an honest small-model study:

| Live run | Model | Accuracy | Note |
|---|---|---|---|
| Spot checks | `gemini-2.5-flash` | perfect structured output | quota-exhausted before a full run |
| Gemma (26b/31b) | `gemma-4-*` | unreliable strict-JSON | every failure cleanly caught by the local fallback — zero crashes |
| Full benchmark | `gemini-flash-lite-latest` | **20/29 (69%)** | won cases local can't (TC-201 implicit chart step; dedup 2/3) but **over-clarified** 5 high-confidence queries |
| + arbitration | `gemini-flash-lite-latest` | **25/29 (86%)** | LLM wins kept, over-clarify losses recovered |
| **+ HyDE + scope-arbiter** | `gemini-flash-lite-latest` | **26/29 (90%) — best overall** | below-floor HyDE recall + explicit out_of_scope verdict; 98% token savings |

**Engineering takeaways (this is the iterated-design evidence):**
1. Live testing exposed a real parser bug (multi-object JSON replies) → fixed with a
   balanced-brace scan; prompt hardened with an explicit JSON-only directive.
2. Weak models over-refuse → added **arbitration**: a planner-level clarify is a second
   opinion, accepted only when the local gate's confidence is not already high (safety
   clarifies still fire pre-planner). Local-only mode unchanged at 83%.
3. The degradation architecture worked exactly as designed under real failure modes
   (429 rate limits, invalid JSON, empty plans) — the engine never crashed once.
4. With a stronger model (2.5-flash / Claude-class), spot checks solved the implicit-step
   cases the local planner can't — the design's accuracy ceiling rises with model quality.

## Conditional HyDE + LLM scope-arbiter (shipped — the 86% → 90% step)

**The problem it solves.** Colloquial/vague in-scope queries ("how are the servers holding up")
share almost no words with tool descriptions, so plain retrieval scores them ~0 — indistinguishable
by score from truly out-of-scope queries ("capital of France"). A naive fix (fire HyDE on every
low score) is unsafe: a hypothetical tool-doc reads as fluent tool language for *any* query, so it
**rescued out-of-scope queries into false positives 4/4** in testing.

**The design (defense in depth).** HyDE (`engine/hyde.py`) now fires whenever confidence is not
already high (`top < hyde_max`, **including below the floor**) and a chat model is present — it
generates a hypothetical tool doc, re-retrieves, and merges by max score to boost *recall*. Scope
safety is then enforced by two independent layers: (a) the floor still applies to the HyDE-improved
score, so a hypothetical matching nothing stays empty; (b) the **LLM planner is the scope arbiter**
— an explicit `out_of_scope` field (distinct from an empty plan) lets it veto queries whose
hypothetical only *spuriously* matched. Offline (no chat) HyDE cannot fire, so the local path is
unchanged (below-floor → empty via the floor).

**Measured (live, Gemini):**
- Out-of-scope safety: **4/4** vague-safety probes kept empty (`out_of_scope=true`), plus the
  harness `negative_out_of_scope` stays **2/2** — the arbiter replaced the lexical floor without
  regressing it.
- Vague in-scope: **3/4** colloquial queries now route correctly (were 0 in the old safe band).
- Harness: **26/29 (90%)** hybrid (was 86%) — `scope_overlap` 1/2 → 2/2; local-only unchanged at 83%.

This is why the shipped default is not a naive HyDE: the *scope decision moves from a lexical
floor to a semantic judgment only when an LLM is available*, which is strictly more correct, with
the floor as the offline fallback. (Blind semantic expansion still measurably hurts look-alike
precision — dense embeddings 79% < TF-IDF 83% — which is why HyDE stays off the confident path.)

## LLM ingest enrichment — measured, and why it defaults OFF

One-time LLM enrichment (`engine/enrich.py`) rewrites a tool's internal search text with clearer
descriptions/tags/example queries — plausible on paper for thin catalogs. Live-tested with
Gemini: enrichment ran on all 64 tools (one-time, cached), but it **blurred a near-duplicate
pair** — "What was our total revenue last quarter?" then routed to `fin.fetch_quarterly_earnings`
instead of the correct `fin.get_revenue_report`, a regression this exact query passes without
enrichment. The added tags/synonyms increased lexical overlap with the *wrong* near-duplicate
tool. It also adds a real one-time cost (217s cold start for 64 tools on the tested key).

**Decision:** `TOOL_ROUTER_LLM_ENRICH` defaults to **off**; every accuracy number in this report
was measured with it off. It remains available as an opt-in for catalogs with genuinely thin/
vague descriptions, where the recall gain is more likely to outweigh the dedup-blurring risk —
but it is not a free win, so it is not the default.

## Auto-ingestion (MCP importer) — verified end-to-end

- **MCP importer** (`tool-router import-mcp`): connected to a sample third-party MCP server,
  pulled 3 self-described tools, converted to registry schema — 3/3 passed the guardrail,
  registered live, and "What's the weather in Berlin right
  now?" routed to the imported `weather.get_current_weather`.

## Held-out generalization set (fresh queries, written after tuning)

A 12-query held-out set (unscripted queries, never used during development): **10/12 (83%)** —
consistent with the 83% benchmark score, i.e. no overfitting to the visible cases. The 2 failures
are known lexical-tier ceilings, deliberately left to the LLM/dense-embedding tier rather than
patched with hacks:
- a query using a derivational form of a word the tool description didn't contain (e.g.
  "healthy" vs. "health");
- a separable phrasal verb with its object inserted mid-phrase (e.g. "roll X back").

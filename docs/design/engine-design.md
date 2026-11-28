# Routing engine design

The engine (`toolspotter/engine/`) works in two phases: **build an index** when the registry
changes, then **route** each query against it.

## Building the index (`pipeline.py::_build_index`)

Input: tools that have passed the [guardrail](onboarding.md). Output: one read-only snapshot.

| Step | Code | Result |
|---|---|---|
| 1. Search entry | `ingest.py` | `search_text`, tokens and `param_tag_words` per tool |
| 2. Vectors | `providers/`, `index_db.py` | One vector per tool |
| 3. Keyword index | `bm25.py` | Word counts and rarity |
| 4. Relations | `derive.py` | Clusters, version families, near-duplicate groups |
| 5. Planner data | `planner.py` | Verbs from tool names, word rarity |

**Search entry.** `search_text` joins the name (`_` as spaces), description, tags, cluster, and
parameter names and descriptions. Tokens are lowercase words, with singular forms, the parts of
`snake_case` names and joined two-word verbs (`look up` → `lookup`). `param_tag_words` is the
set of words from parameters and tags only. The registered tool is kept unchanged and is what
callers receive.

**Vectors.** Local TF-IDF weights each word as `(0.5 + 0.5 · tf/max_tf) · idf`, with
`idf = ln((1 + N) / (1 + df)) + 1`, and stores `{word: weight}`. Because `idf` depends on all
tools, any change recomputes every local vector. Gemini vectors are dense lists and only new or
changed texts are sent. One index never mixes the two: if Gemini fails while indexing, the whole
index uses local vectors.

**Concurrency.** The new snapshot is built beside the live one and published with a single
assignment, so a query sees either the old or the new index. Changes are serialised by a lock.
A failed build or catalog save leaves the previous snapshot in place.

### Auto-derivation

- **Clusters** — each tool's `cluster` field. Only the count is reported.
- **Version families** — tools linked by `replaced_by`, chains included. Names and `version`
  are not interpreted. A tool that is `deprecated` or has `replaced_by` is not current.
- **Near-duplicate groups** — declared `near_duplicate_groups`, plus pairs that share all of:
  cluster, `side_effects`, parameter-name overlap (Jaccard ≥ 0.34) and vector similarity
  (cosine ≥ 0.60). Pairs are joined into groups.

Finding pairs is the slowest step. Only tools with the same cluster and side effects that share
a parameter name are compared. On a change, pairs between tools whose cluster, side effects,
parameters and vector did not change are reused, and only the others are compared; the result is
the same as comparing everything. The pairs are saved with a fingerprint of the tools, vector
method, thresholds and `DUPLICATE_RULES_VERSION`, and reused only if all are unchanged. **Raise
that version when changing `find_duplicate_pairs`.**

## 1. Query signals (`retrieval.py::analyze_query`)

Word lists and patterns detect destructive intent, bulk scope, legacy wording, "as usual"
wording, pronouns, identifiers (ids, emails, names) and lists such as "A, B and C".

## 2. Fused scoring (`retrieval.py::score_tools`)

Every tool is scored. Text after "so that", "so" or "in order to" is ignored when enough words
remain before it.

```
fused = 0.50 · vector similarity
      + 0.50 · BM25
      − 0.15  destructive tool, no destructive word in the query
      − 0.15  tool not current, no legacy word in the query
      − 0.10  tool handles sensitive data the query did not mention
```

BM25 (`k1 = 1.5`, `b = 0.75`) is normalised as `score / (score + 3)`, so a weak match stays
weak. Weights can be changed with
`TOOLSPOTTER_W_DENSE`, `_BM25`, `_SIDE_EFFECT` and `_DEPRECATED`.

## 3. HyDE (`hyde.py`, LLM only)

If an LLM is configured and the best score is below `0.38`, the LLM writes a short description
of an ideal tool. Tools are scored against it, each tool keeps its higher score, and
duplicates are removed again. There is no lower bound: a colloquial request can score near zero.
The score floor still applies afterwards, and the LLM planner can answer "out of scope". A
failed call skips this step.

## 4. Version and duplicate collapse (`dedup.py`)

One tool per version family: the current one, or an old one when the query uses legacy wording.
Then one tool per near-duplicate group: the highest scoring.

## 5. Confidence gate (`confidence.py`)

Checked in order, using only local data:

| Condition | Result |
|---|---|
| Best score below `0.18` | Nothing |
| Destructive and bulk wording | Ask to confirm |
| Destructive tool and wording, no identifier | Ask which item |
| Best tool is in a version family and the query says "as usual" | Ask which version |
| Pronoun object, no identifier, three or fewer content words | Ask for detail |
| Otherwise | Shortlist: tools scoring at least `0.55 ×` the best, up to 8 |

The destructive-word list is in `domain/naming.py`.

## 6. Planning

### LLM planner (`llm_planner.py`)

One call with the query and the shortlist (id, description, parameters, side effects, version
flags). It returns a plan or one of: clarify, insufficient, out of scope. Ids outside the
shortlist are dropped. An empty plan without a reason, or any provider failure, uses the local
planner. If it asks to clarify while the best local score is at least `0.45` and there is no
destructive wording, the local plan is used instead (`TOOLSPOTTER_ARBITRATE_MIN`).

### Local planner (`planner.py`)

1. Split at `;`, "then", "after that", and at "and" or a comma followed by a verb that starts a
   tool name. Then split a part at "and" or a comma if each side has two or more content words,
   scores at least `0.18`, and picks a different tool. A side that picks a destructive tool is
   not split off this way.
2. Score each part. A weak part (below `0.12`) is merged; neighbours choosing the same tool are
   joined.
3. Take the best tool for each part. Add up to two helpers that score at least half as much,
   are not destructive, and cover enough rare words the chosen tools do not.
4. Within a part, order reads before writes.
5. Repeat a lone tool once per item in a list.

## 7. Widening

If the LLM planner reports a missing ability, matching tools and the next best candidates are
added and it plans again, at most `TOOLSPOTTER_MAX_WIDEN_ROUNDS` times (default 1).

## 8. Result

`selected_tools` (unique ids), `plan` (ordered, ids may repeat), `clarify` and
`clarify_question`, `tokens_saved_pct`, and `trace` with the decision, the top candidates and
their score parts, and what was dropped. Token figures are estimates (characters ÷ 4) and do
not count the router's own LLM calls.

If Gemini embeddings fail during a query, local vectors are used for that request and the trace
records `embedding_fallback`.

## Not built

A second-stage reranker and a vector database. All vectors are compared in memory.

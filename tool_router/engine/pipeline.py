"""RoutingEngine — the orchestrator (architecture.md §7).

Onboard once:  guardrail (lenient for the trusted catalog) → ingest/enrich →
               embed + BM25-index → derive maps (clusters/dups/versions).
Route per query: analyze → fused scoring → version/dup collapse →
               confidence gate (empty/clarify/proceed) → plan → assemble.

Every route returns a full trace (why each tool was chosen) — this powers the
demo and the REST response; the harness adapter simply drops it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from tool_router.config import Settings
from tool_router.domain.errors import CatalogError
from tool_router.domain.models import DerivedMaps, IndexedTool, RouteResult
from tool_router.engine.bm25 import BM25Index
from tool_router.engine.confidence import decide
from tool_router.engine.dedup import collapse
from tool_router.engine.derive import derive_maps
from tool_router.engine.guardrail import validate_catalog, validate_submission
from tool_router.engine.ingest import ingest_tools
from tool_router.engine.planner import LocalPlanner, PlanOutcome
from tool_router.engine.retrieval import Candidate, analyze_query, score_tools
from tool_router.engine.text import STOPWORDS, strip_purpose_clause, tokenize
from tool_router.log import get_logger
from tool_router.providers import make_embedding_provider
from tool_router.store import content_hash, project_cache

logger = get_logger("pipeline")


def project_cache_for(provider, rebuild: bool = False):
    """Embedding cache bound to the provider's model stamp (storage.md)."""
    return project_cache("embeddings", getattr(provider, "stamp", "unknown"), rebuild=rebuild)


def _approx_tokens(obj: Any) -> int:
    """Same rough token estimate the harness uses (~4 chars/token)."""
    return max(1, len(json.dumps(obj)) // 4)


class RoutingEngine:
    """One instance per registered catalog. Thread-safe for reads after init."""

    def __init__(self, catalog: dict[str, Any], settings: Optional[Settings] = None) -> None:
        if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
            raise CatalogError("Catalog must be a dict with a 'tools' list.")
        self._settings = settings or Settings.from_env()
        self._catalog_meta = {k: v for k, v in catalog.items() if k != "tools"}
        self._raw_tools: list[dict[str, Any]] = list(catalog["tools"])

        validate_catalog(self._raw_tools)   # lenient: logs issues; trusted catalog is kept
        self._chat = self._make_chat()      # shared generation-LLM client (or None)
        self._build_index()
        self._planner = self._make_planner()
        self._log_summary()

    def _make_chat(self):
        """One Bedrock chat client shared by planner + enrichment (or None)."""
        provider = self._settings.llm_provider
        if provider not in ("bedrock", "gemini"):
            return None
        try:
            if provider == "gemini":
                from tool_router.providers.gemini import GeminiChat
                return GeminiChat()
            from tool_router.providers.bedrock import BedrockChat
            return BedrockChat()
        except Exception as exc:
            logger.warning("generation LLM unavailable (%s) — local-only mode", exc)
            return None

    def _log_summary(self) -> None:
        """Startup one-liner (storage.md): what's active — never the key value."""
        from tool_router.store import data_dir, load_secret
        logger.info(
            "tool-router ready · tools=%d · embedding=%s · planner=%s · "
            "llm=%s · key=%s · data=%s",
            self.tool_count,
            getattr(self._provider, "stamp", "?").split(":")[0],
            type(self._planner).__name__,
            self._settings.llm_provider,
            "present" if (load_secret("bedrock_key") or load_secret("gemini_key")) else "absent",
            data_dir(),
        )

    def _build_index(self) -> None:
        """(Re)build every derived structure from the current raw tool list.

        Called at construction AND after every mutation — churn resilience is
        literally "rebuild the index from the new truth" (catalog_mutations
        invariants). At catalog scale this is milliseconds; the persistent
        store phase adds content-hash caching to skip unchanged embeddings.
        """
        self._entries: list[IndexedTool] = ingest_tools(self._raw_tools)

        # --- one-time LLM enrichment (internal-only, cached, optional) ------
        if self._chat is not None and self._settings.llm_enrich:
            from tool_router.engine.enrich import enrich_entries
            from tool_router.store import project_cache
            enrich_entries(
                self._entries, self._chat,
                project_cache("enrichment", "llm-enrich-v1", rebuild=self._settings.rebuild),
            )

        # --- embed (content-hash cached) + lexical index ---------------------
        self._provider = make_embedding_provider(self._settings.embedding_provider)
        texts = [e.search_text for e in self._entries]
        self._provider.fit_documents(texts)
        cache = project_cache_for(self._provider, rebuild=self._settings.rebuild)
        hashes = [content_hash(t) for t in texts]
        missing = [i for i, h in enumerate(hashes) if cache.get(h) is None]
        if missing:
            vectors = self._provider.embed_documents([texts[i] for i in missing])
            for i, vec in zip(missing, vectors):
                cache.put(hashes[i], vec)
            cache.save()
        if len(missing) < len(texts):
            logger.info(
                "embedding cache: reused %d/%d vectors", len(texts) - len(missing), len(texts)
            )
        for entry, h in zip(self._entries, hashes):
            entry.embedding = cache.get(h)
        # Lexical index over CONTENT tokens only: stopwords carry no routing
        # signal and would otherwise inflate scores for unrelated queries
        # (breaking out-of-scope detection) — standard IR practice.
        self._bm25 = BM25Index(
            [[t for t in e.tokens if t not in STOPWORDS] for e in self._entries]
        )

        # --- derive registry maps ------------------------------------------
        self._maps: DerivedMaps = derive_maps(
            self._entries, catalog_meta=self._catalog_meta,
            thresholds=self._settings.confidence,
        )

        # Planner verb lexicon: leading verbs of tool names (catalog-derived).
        tool_verbs = {e.tool.name.split("_")[0] for e in self._entries}
        self._local_planner = LocalPlanner(
            tool_verbs=tool_verbs,
            idf=self._bm25.idf,
            mean_idf=self._bm25.mean_idf,
        )

        self._full_catalog_tokens = _approx_tokens(self._raw_tools)
        self._raw_by_id = {e.tool.id: e.raw for e in self._entries}
        logger.info(
            "index ready: %d tools, %d clusters, %d dup groups, %d version families",
            len(self._entries), len(self._maps.clusters),
            len(self._maps.dup_groups), len(self._maps.version_families),
        )

    # ------------------------------------------------------------------ API

    def apply_mutation(self, delta: dict[str, Any]) -> dict[str, Any]:
        """Apply a catalog mutation (resilience to churn): {"add": [tools], "remove": [ids]}.

        - Added tools pass the STRICT intake guardrail (new submissions are
          untrusted, unlike the initial catalog) — rejected ones are reported,
          not indexed.
        - Removals of unknown ids are reported, not errors (idempotent).
        - The whole index is rebuilt from the new truth: add/remove/replace/
          upgrade all reduce to "the registry changed, re-derive everything",
          which is what keeps routing keyed to capability, not to ids.
        """
        if not isinstance(delta, dict):
            raise CatalogError("Mutation delta must be a dict with 'add' and/or 'remove'.")
        to_add = delta.get("add") or []
        to_remove = delta.get("remove") or []
        if not isinstance(to_add, list) or not isinstance(to_remove, list):
            raise CatalogError("'add' must be a list of tools; 'remove' a list of ids.")

        removed, missing = [], []
        remove_set = set(to_remove)
        if remove_set:
            before = {t.get("id") for t in self._raw_tools}
            self._raw_tools = [t for t in self._raw_tools if t.get("id") not in remove_set]
            removed = sorted(remove_set & before)
            missing = sorted(remove_set - before)

        accepted, rejected = [], []
        known_ids = {t.get("id") for t in self._raw_tools}
        known_tools = {t.get("id"): t for t in self._raw_tools}
        for raw in to_add:
            verdict = validate_submission(raw, known_ids=known_ids, known_tools=known_tools)
            if verdict.accepted:
                self._raw_tools.append(raw)
                known_ids.add(raw["id"])
                known_tools[raw["id"]] = raw
                accepted.append(raw["id"])
            else:
                rejected.append({
                    "id": verdict.tool_id,
                    "reason": verdict.primary_reason,
                    "issues": [i.message for i in verdict.issues],
                })

        self._build_index()
        self._planner = self._make_planner()   # rebind fallback to the new index
        logger.info(
            "mutation applied: +%d accepted, %d rejected, -%d removed (%d unknown)",
            len(accepted), len(rejected), len(removed), len(missing),
        )
        return {
            "added": accepted,
            "rejected": rejected,
            "removed": removed,
            "unknown_remove_ids": missing,
            "tool_count": len(self._raw_tools),
        }

    @property
    def tool_count(self) -> int:
        return len(self._entries)

    @property
    def maps(self) -> DerivedMaps:
        """Derived registry maps (clusters / dup groups / version families)."""
        return self._maps

    @property
    def planner(self):
        """The active planner (LLMPlanner or LocalPlanner)."""
        return self._planner

    def raw_tool(self, tool_id: str) -> dict[str, Any]:
        """The ORIGINAL registered schema for a tool — what clients receive
        (enrichment never leaves the retrieval layer, see onboarding.md)."""
        return self._raw_by_id[tool_id]

    def route(self, query: str, context: Optional[dict] = None) -> RouteResult:
        """Route one query to a minimal, ordered set of tools."""
        if not isinstance(query, str) or not query.strip():
            return RouteResult(
                clarify=True,
                clarify_question="The request is empty — what would you like to do?",
                trace={"reason": "empty query"},
            )

        signals = analyze_query(query)
        ranked = self._score_and_collapse(query)
        candidates, dropped = ranked

        # Conditional HyDE: fires whenever confidence is NOT already high and a
        # chat model exists — INCLUDING below the floor, because colloquial
        # in-scope queries can score ~0 (no lexical overlap) yet be perfectly
        # routable. Out-of-scope safety is enforced AFTER HyDE by defense in
        # depth: (a) the floor still applies to the HyDE-improved score, so a
        # hypothetical that matches nothing stays empty; (b) the LLM planner is
        # the scope arbiter — it returns out_of_scope for queries whose
        # hypothetical only spuriously matched. Offline (no chat) HyDE cannot
        # fire, so the local path is unchanged (below-floor → empty via floor).
        hyde_info: dict[str, Any] = {"fired": False}
        conf = self._settings.confidence
        top0 = candidates[0].fused if candidates else 0.0
        if self._chat is not None and top0 < conf.hyde_max:
            from tool_router.engine.hyde import apply_hyde
            candidates, hyde_info = apply_hyde(
                query, candidates, self._chat, self._score_and_collapse,
                self._maps, signals,
            )

        decision = decide(candidates, signals, self._settings.confidence)
        trace: dict[str, Any] = {
            "decision": decision.kind,
            "reason": decision.reason,
            "hyde": hyde_info,
            "top_candidates": [
                {"tool": c.tool_id, "fused": round(c.fused, 4), "dense": round(c.dense, 4),
                 "bm25": round(c.bm25, 4), "capability": round(c.capability, 4)}
                for c in candidates[:8]
            ],
            "dropped": dropped,
        }

        if decision.kind == "empty":
            return RouteResult(trace=trace)
        if decision.kind == "clarify":
            return RouteResult(
                clarify=True, clarify_question=decision.clarify_question, trace=trace
            )

        scorer = lambda text: self._score_and_collapse(text)[0]  # noqa: E731
        shortlist = decision.shortlist
        outcome: PlanOutcome = self._planner.plan(query, signals, shortlist, scorer)

        # Scope arbiter: the LLM's explicit out_of_scope verdict is how we stay
        # safe after HyDE lifts a query above the floor — a hypothetical can
        # spuriously match real tools for a genuinely out-of-scope request, and
        # only a semantic judgment can catch that. Honor it → return nothing.
        if outcome.out_of_scope:
            trace["out_of_scope"] = True
            trace["segments"] = outcome.segments
            return RouteResult(trace=trace)

        # Widen loop (bounded): the LLM planner may flag that the shortlist
        # lacks a needed capability — broaden the candidate set and re-plan.
        widen_rounds = 0
        while (
            outcome.insufficient
            and widen_rounds < self._settings.max_widen_rounds
        ):
            widened = self._widen_shortlist(shortlist, candidates, outcome.needed_capability)
            if [c.tool_id for c in widened] == [c.tool_id for c in shortlist]:
                break  # nothing new to offer
            widen_rounds += 1
            trace.setdefault("widened", []).append({
                "round": widen_rounds,
                "needed_capability": outcome.needed_capability,
                "added": [c.tool_id for c in widened if c not in shortlist],
            })
            shortlist = widened
            outcome = self._planner.plan(query, signals, shortlist, scorer)

        # Arbitration: a planner-level clarify is a SECOND opinion. When the
        # local gate already established high confidence (strong top score,
        # no destructive intent), an LLM refusal is more likely a weak-model
        # over-clarification than a real ambiguity — prefer the local plan.
        # (Safety-critical clarifies fire BEFORE the planner, in the gate.)
        if (
            outcome.clarify
            and shortlist
            and shortlist[0].fused >= 0.45
            and not signals.destructive_intent
        ):
            logger.warning(
                "planner clarified but local confidence is high (%.2f) — using local plan",
                shortlist[0].fused,
            )
            outcome = self._local_planner.plan(query, signals, shortlist, scorer)

        # The planner can raise its own (semantic) clarify — honor it.
        if outcome.clarify:
            trace["segments"] = outcome.segments
            return RouteResult(
                clarify=True,
                clarify_question=outcome.clarify_question
                or "Could you give me a bit more detail so I can route this safely?",
                trace=trace,
            )

        selected = list(dict.fromkeys(outcome.plan))   # unique, order-preserving
        trace["segments"] = outcome.segments

        routed_tokens = _approx_tokens([self._raw_by_id[t] for t in selected])
        return RouteResult(
            selected_tools=selected,
            plan=outcome.plan,
            tokens_saved_pct=round(1 - routed_tokens / self._full_catalog_tokens, 4),
            trace=trace,
        )

    # ------------------------------------------------------------- helpers

    def _make_planner(self):
        """Select the planner per config: LLM (with local fallback) or local.

        Failure to initialize the LLM (no boto3 / no credentials) is a
        WARNING, never an error — the engine must run keyless (degradation
        guarantee), just with the deterministic planner.
        """
        if self._chat is not None:
            from tool_router.engine.llm_planner import LLMPlanner
            logger.info("planner: LLM (%s) with local fallback", self._settings.llm_provider)
            return LLMPlanner(self._chat, fallback=self._local_planner)
        return self._local_planner

    def _widen_shortlist(
        self,
        shortlist: list[Candidate],
        all_candidates: list[Candidate],
        needed_capability: str | None,
    ) -> list[Candidate]:
        """Broaden the shortlist for a re-plan (fallback/widening resilience).

        Adds (a) tools whose name/capability tokens match the planner's stated
        missing capability, then (b) the next best-scored candidates — capped,
        order-preserving, no duplicates.
        """
        result = list(shortlist)
        have = {c.tool_id for c in result}
        cap = self._settings.confidence.shortlist_max + 4

        if needed_capability:
            need_tokens = set(tokenize(needed_capability))
            matches = [
                c for c in all_candidates
                if c.tool_id not in have
                and need_tokens & (c.entry.capability_tokens | set(tokenize(c.entry.tool.name)))
            ]
            matches.sort(key=lambda c: -c.fused)
            for c in matches[:3]:
                result.append(c)
                have.add(c.tool_id)

        for c in all_candidates:
            if len(result) >= cap:
                break
            if c.tool_id not in have:
                result.append(c)
                have.add(c.tool_id)
        return result[:cap]

    def _score_and_collapse(
        self, text: str
    ) -> tuple[list[Candidate], dict[str, list[str]]]:
        """Fused scoring + version/dup collapse for any piece of text.

        Purpose clauses are stripped before scoring: the command lives in the
        main clause ("store this file ..."), while purpose vocabulary ("... so
        people can search it") would otherwise pull the ranking toward the
        wrong action (see text.strip_purpose_clause).
        """
        scored_text = strip_purpose_clause(text)
        signals = analyze_query(scored_text)
        query_vec = self._provider.embed_query(scored_text)
        candidates = score_tools(
            self._entries, self._bm25, query_vec, signals, self._settings.weights
        )
        return collapse(candidates, self._maps, signals)

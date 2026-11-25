"""RoutingEngine — the orchestrator (architecture.md §7).

Onboard once:  guardrail (strict) → ingest/enrich →
               embed + BM25-index → derive maps (clusters/dups/versions).
Route per query: analyze → fused scoring → version/dup collapse →
               confidence gate (empty/clarify/proceed) → plan → assemble.

Every route returns a full trace (why each tool was chosen) — this powers the
demo and the REST response.

Concurrency model
-----------------
Everything derived from the catalog (entries, BM25 index, maps, planners) lives
in one immutable ``_Index`` snapshot. ``route()`` reads ``self._index`` exactly
once and uses that snapshot for the whole request. ``apply_mutation()`` builds a
complete new snapshot off to the side, under a lock that serializes writers, and
publishes it with a single reference assignment. A reader therefore sees either
the complete old index or the complete new one, never a mixture, and never has
to wait for a rebuild.
"""

from __future__ import annotations

import functools
import json
import threading
from dataclasses import dataclass
from typing import Any, Optional

from tool_router.config import Settings
from tool_router.domain.errors import (
    CatalogError,
    DuplicateToolError,
    InvalidCatalogError,
    InvalidToolError,
    ProviderError,
    ToolInUseError,
    ToolNotFoundError,
)
from tool_router.domain.models import DerivedMaps, IndexedTool, RouteResult
from tool_router.domain.naming import prepare_tool
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
from tool_router.providers.base import ChatClient
from tool_router.providers.local_tfidf import LocalTfidfEmbedding
from tool_router.store import content_hash, project_cache

logger = get_logger("pipeline")


def project_cache_for(provider, rebuild: bool = False):
    """Embedding cache bound to the provider's model stamp (storage.md)."""
    return project_cache("embeddings", getattr(provider, "stamp", "unknown"), rebuild=rebuild)


def _approx_tokens(obj: Any) -> int:
    """Rough token estimate (~4 chars/token)."""
    return max(1, len(json.dumps(obj)) // 4)


@dataclass(frozen=True)
class _Index:
    """Everything derived from one version of the catalog. Never mutated after build."""

    entries: list[IndexedTool]
    provider: Any
    bm25: BM25Index
    maps: DerivedMaps
    local_planner: LocalPlanner
    planner: Any                      # LLMPlanner (with local fallback) or the LocalPlanner
    full_catalog_tokens: int
    raw_by_id: dict[str, dict[str, Any]]
    # Local TF-IDF (provider, entries) used if a hosted embedding provider fails
    # during a query. None when the primary provider is already local.
    fallback: Optional[tuple[Any, list[IndexedTool]]] = None


def _local_vectors(entries: list[IndexedTool]) -> tuple[Any, list[Any]]:
    """A fitted local TF-IDF provider and one vector per entry (no network, no cache)."""
    provider = LocalTfidfEmbedding()
    texts = [e.search_text for e in entries]
    provider.fit_documents(texts)
    return provider, provider.embed_documents(texts)


class RoutingEngine:
    """One instance per registered catalog. Safe to route from many threads,
    including while the catalog is being mutated (see the module docstring)."""

    def __init__(
        self,
        catalog: dict[str, Any],
        settings: Optional[Settings] = None,
        chat: Optional[ChatClient] = None,
    ) -> None:
        """``chat`` injects a generation-LLM client (tests, custom providers).
        When omitted, one is built from ``settings.llm_provider`` or left unset."""
        if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
            raise CatalogError("Catalog must be a dict with a 'tools' list.")
        self._settings = settings or Settings.from_env()
        self._catalog_meta = {k: v for k, v in catalog.items() if k != "tools"}
        self._raw_tools: list[dict[str, Any]] = [prepare_tool(t) for t in catalog["tools"]]
        self._check_size(len(self._raw_tools))
        self._check_catalog(self._raw_tools, self._catalog_meta)
        self._mutation_lock = threading.Lock()
        self._chat = chat if chat is not None else self._make_chat()
        self._index: _Index = self._build_index(self._raw_tools)
        self._log_summary()

    def _check_size(self, count: int) -> None:
        if count > self._settings.max_tools:
            raise CatalogError(
                f"{count} tools exceeds the limit of {self._settings.max_tools} "
                "(set TOOL_ROUTER_MAX_TOOLS to change it)."
            )

    @staticmethod
    def _check_catalog(tools: list[Any], meta: dict[str, Any]) -> None:
        """Strict intake for a whole catalog: every tool, and the catalog metadata."""
        if not tools:
            raise CatalogError("Catalog contains no tools.")
        rejected = [
            {"index": i, "id": r.tool_id, "reason": r.primary_reason,
             "issues": [issue.message for issue in r.issues]}
            for i, r in enumerate(validate_catalog(tools)) if not r.accepted
        ]
        if rejected:
            raise InvalidCatalogError(rejected)
        groups = meta.get("near_duplicate_groups", [])
        if not (isinstance(groups, list) and all(
            isinstance(g, list) and all(isinstance(i, str) for i in g) for g in groups
        )):
            raise CatalogError("near_duplicate_groups must be a list of lists of tool ids.")
        unknown = sorted({i for g in groups for i in g} - {t["id"] for t in tools})
        if unknown:
            raise CatalogError(f"near_duplicate_groups names unknown tool id(s): {', '.join(unknown)}")

    def _make_chat(self):
        """One chat client shared by planner, HyDE and enrichment (or None)."""
        provider = self._settings.llm_provider
        if provider == "none":
            return None
        if provider != "gemini":
            logger.warning("unknown llm provider %r (supported: none, gemini) — local-only mode", provider)
            return None
        model = self._settings.llm_model or None     # None = Gemini's default model
        try:
            from tool_router.providers.gemini import GeminiChat
            return GeminiChat(model_id=model)
        except Exception as exc:
            logger.warning("generation LLM unavailable (%s) — local-only mode", exc)
            return None

    def _log_summary(self) -> None:
        """Startup one-liner (storage.md): what's active — never the key value."""
        from tool_router.store import data_dir, load_secret
        idx = self._index
        logger.info(
            "tool-router ready · tools=%d · embedding=%s · planner=%s · "
            "llm=%s · key=%s · data=%s",
            len(idx.entries),
            getattr(idx.provider, "stamp", "?").split(":")[0],
            type(idx.planner).__name__,
            self._settings.llm_provider,
            "present" if (load_secret("gemini_key")) else "absent",
            data_dir(),
        )

    def _build_index(self, raw_tools: list[dict[str, Any]]) -> _Index:
        """Build every derived structure from a raw tool list and return it.

        Pure with respect to the engine's published state: nothing is assigned
        to ``self`` here, so a failure part-way leaves the live index untouched.
        Called at construction AND for every mutation — churn resilience is
        literally "rebuild the index from the new truth" (catalog_mutations
        invariants). At catalog scale this is milliseconds.
        """
        entries: list[IndexedTool] = ingest_tools(raw_tools)

        # --- one-time LLM enrichment (internal-only, cached, optional) ------
        if self._chat is not None and self._settings.llm_enrich:
            from tool_router.engine.enrich import enrich_entries
            enrich_entries(
                entries, self._chat,
                project_cache("enrichment", "llm-enrich-v1", rebuild=self._settings.rebuild),
            )

        # --- embed (content-hash cached) + lexical index ---------------------
        provider = make_embedding_provider(self._settings.embedding_provider)
        try:
            self._embed_entries(provider, entries)
        except ProviderError as exc:
            # The whole index must live in one vector space, so use local for all of it.
            logger.warning(
                "embedding provider failed while indexing (%s) — this index uses local "
                "TF-IDF until the next successful rebuild", exc,
            )
            provider = LocalTfidfEmbedding()
            self._embed_entries(provider, entries)
        fallback = None
        if not isinstance(provider, LocalTfidfEmbedding):
            local, vectors = _local_vectors(entries)
            fallback = (local, [
                entry.model_copy(update={"embedding": vector})
                for entry, vector in zip(entries, vectors)
            ])
        # Lexical index over CONTENT tokens only: stopwords carry no routing
        # signal and would otherwise inflate scores for unrelated queries
        # (breaking out-of-scope detection) — standard IR practice.
        bm25 = BM25Index([[t for t in e.tokens if t not in STOPWORDS] for e in entries])

        # --- derive registry maps ------------------------------------------
        maps: DerivedMaps = derive_maps(
            entries, catalog_meta=self._catalog_meta,
            thresholds=self._settings.confidence,
        )

        # Planner verb lexicon: leading verbs of tool names (catalog-derived).
        tool_verbs = {e.tool.name.split("_")[0] for e in entries}
        local_planner = LocalPlanner(
            tool_verbs=tool_verbs, idf=bm25.idf, mean_idf=bm25.mean_idf,
        )
        planner: Any = local_planner
        if self._chat is not None:
            from tool_router.engine.llm_planner import LLMPlanner
            logger.info("planner: LLM (%s) with local fallback", self._settings.llm_provider)
            planner = LLMPlanner(self._chat, fallback=local_planner)

        logger.info(
            "index ready: %d tools, %d clusters, %d dup groups, %d version families",
            len(entries), len(maps.clusters), len(maps.dup_groups), len(maps.version_families),
        )
        return _Index(
            entries=entries,
            provider=provider,
            bm25=bm25,
            maps=maps,
            local_planner=local_planner,
            planner=planner,
            full_catalog_tokens=_approx_tokens(raw_tools),
            raw_by_id={e.tool.id: e.raw for e in entries},
            fallback=fallback,
        )

    def _embed_entries(self, provider: Any, entries: list[IndexedTool]) -> None:
        """Set every entry's vector, reusing the content-hash cache where possible."""
        texts = [e.search_text for e in entries]
        provider.fit_documents(texts)
        cache = project_cache_for(provider, rebuild=self._settings.rebuild)
        hashes = [content_hash(t) for t in texts]
        missing = [i for i, h in enumerate(hashes) if cache.get(h) is None]
        if missing:
            vectors = provider.embed_documents([texts[i] for i in missing])
            for i, vec in zip(missing, vectors):
                cache.put(hashes[i], vec)
            cache.save()
        if len(missing) < len(texts):
            logger.info(
                "embedding cache: reused %d/%d vectors", len(texts) - len(missing), len(texts)
            )
        for entry, h in zip(entries, hashes):
            entry.embedding = cache.get(h)

    # ------------------------------------------------------------------ API

    def apply_mutation(self, delta: dict[str, Any]) -> dict[str, Any]:
        """Apply a catalog mutation (resilience to churn): {"add": [tools], "remove": [ids]}.

        - Added tools pass the STRICT intake guardrail (new submissions are
          the same rules as the initial catalog) — rejected ones are reported,
          not indexed.
        - Removing and adding the same id replaces that tool. If the new version
          is rejected, the old one is KEPT (reported in ``kept_previous``).
        - Removals of unknown ids are reported, not errors (idempotent).
        - The whole index is rebuilt from the new truth: add/remove/replace/
          upgrade all reduce to "the registry changed, re-derive everything",
          which is what keeps routing keyed to capability, not to ids.
        - The rebuild happens off to the side and is published in one step, so
          concurrent ``route()`` calls are never affected, and a failure while
          rebuilding leaves the live catalog exactly as it was.
        """
        if not isinstance(delta, dict):
            raise CatalogError("Mutation delta must be a dict with 'add' and/or 'remove'.")
        to_add = delta.get("add") or []
        to_remove = delta.get("remove") or []
        if not isinstance(to_add, list) or not isinstance(to_remove, list):
            raise CatalogError("'add' must be a list of tools; 'remove' a list of ids.")

        with self._mutation_lock:
            current = self._raw_tools
            remove_set = set(to_remove)
            existing = {t.get("id") for t in current}
            removed = remove_set & existing
            missing = sorted(remove_set - existing)

            known = {t.get("id"): t for t in current if t.get("id") not in removed}
            accepted: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            failed_ids: set[str] = set()
            previous = {t.get("id"): t for t in current if t.get("id") in removed}
            pending: list[Any] = []
            for submitted in to_add:
                tool = prepare_tool(submitted)
                # A replacement that omits side_effects keeps the declared value:
                # guessing again could quietly downgrade a destructive tool.
                if isinstance(tool, dict) and isinstance(tool.get("id"), str) \
                        and tool["id"] in previous and not submitted.get("side_effects"):
                    tool["side_effects"] = previous[tool["id"]]["side_effects"]
                pending.append(tool)

            # Tools in one batch may point at each other in any order, so keep
            # passing over the rest for as long as something new gets accepted.
            failures: list[Any] = []
            while pending:
                failures = []
                for tool in pending:
                    verdict = validate_submission(tool, known_ids=set(known), known_tools=known)
                    if verdict.accepted:
                        accepted.append(tool)
                        known[tool["id"]] = tool
                    else:
                        failures.append((tool, verdict))
                if len(failures) == len(pending):
                    break
                pending = [tool for tool, _ in failures]
            for _, verdict in failures:
                failed_ids.add(verdict.tool_id)
                rejected.append({
                    "id": verdict.tool_id,
                    "reason": verdict.primary_reason,
                    "issues": [i.message for i in verdict.issues],
                })

            # A replacement that failed must not delete the tool it was meant to replace.
            added_ids = {t["id"] for t in accepted}
            kept = (removed & failed_ids) - added_ids
            removed -= kept

            # A removal must not leave another tool pointing at nothing.
            while True:
                raw_tools = [t for t in current if t.get("id") not in removed] + accepted
                present = {t["id"] for t in raw_tools}
                broken = {t["replaced_by"] for t in raw_tools if t.get("replaced_by")} - present
                blocked = broken & removed
                if not blocked:
                    break
                for tool_id in sorted(blocked):
                    rejected.append({
                        "id": tool_id,
                        "reason": "REFERENCED_BY_OTHER_TOOL",
                        "issues": [f"'{tool_id}' was not removed: another tool's replaced_by points to it."],
                    })
                removed -= blocked
                kept |= blocked

            self._check_size(len(raw_tools))
            self._publish(raw_tools)

        logger.info(
            "mutation applied: +%d accepted, %d rejected, -%d removed (%d unknown, %d kept)",
            len(accepted), len(rejected), len(removed), len(missing), len(kept),
        )
        return {
            "added": [t["id"] for t in accepted],
            "rejected": rejected,
            "removed": sorted(removed),
            "kept_previous": sorted(kept),
            "unknown_remove_ids": missing,
            "tool_count": len(raw_tools),
        }

    # ---------------------------------------------------------------- CRUD

    def list_tools(self) -> list[dict[str, Any]]:
        """Every registered tool definition."""
        return list(self._index.raw_by_id.values())

    def get_tool(self, tool_id: str) -> dict[str, Any]:
        try:
            return self._index.raw_by_id[tool_id]
        except KeyError:
            raise ToolNotFoundError(f"No tool with id {tool_id!r}.") from None

    def create_tool(self, submitted: dict[str, Any]) -> dict[str, Any]:
        """Register one new tool (strict guardrail). Returns it as stored."""
        with self._mutation_lock:
            tool = prepare_tool(submitted)
            self._check_size(len(self._raw_tools) + 1)
            known = {t.get("id"): t for t in self._raw_tools}
            verdict = validate_submission(tool, known_ids=set(known), known_tools=known)
            if not verdict.accepted:
                if verdict.primary_reason == "DUPLICATE_ID":
                    raise DuplicateToolError(f"A tool with id {verdict.tool_id!r} already exists.")
                raise InvalidToolError(verdict.primary_reason, [i.message for i in verdict.issues])
            self._publish([*self._raw_tools, tool])
        return tool

    def update_tool(self, tool_id: str, submitted: dict[str, Any]) -> dict[str, Any]:
        """Replace one tool's definition (strict guardrail). Its id cannot change.

        An omitted ``cluster`` or ``side_effects`` keeps the current value. If the new
        definition is rejected, the current one stays in place.
        """
        with self._mutation_lock:
            old = next((t for t in self._raw_tools if t.get("id") == tool_id), None)
            if old is None:
                raise ToolNotFoundError(f"No tool with id {tool_id!r}.")
            if not isinstance(submitted, dict):
                raise CatalogError("A tool must be a JSON object.")
            if submitted.get("id") not in (None, "", tool_id):
                raise CatalogError(
                    f"The id in the body ({submitted['id']!r}) does not match {tool_id!r}. "
                    "To rename a tool, delete it and create a new one."
                )
            candidate = {**submitted, "id": tool_id}
            for field in ("cluster", "side_effects"):      # omitted = keep the current value
                if not candidate.get(field):
                    candidate[field] = old.get(field)
            tool = prepare_tool(candidate)
            known = {t.get("id"): t for t in self._raw_tools if t.get("id") != tool_id}
            verdict = validate_submission(tool, known_ids=set(known), known_tools=known)
            if not verdict.accepted:
                raise InvalidToolError(verdict.primary_reason, [i.message for i in verdict.issues])
            self._publish([tool if t.get("id") == tool_id else t for t in self._raw_tools])
        return tool

    def delete_tool(self, tool_id: str) -> None:
        """Remove one tool. The last tool cannot be removed (a registry is never empty)."""
        with self._mutation_lock:
            remaining = [t for t in self._raw_tools if t.get("id") != tool_id]
            if len(remaining) == len(self._raw_tools):
                raise ToolNotFoundError(f"No tool with id {tool_id!r}.")
            if not remaining:
                raise CatalogError("Cannot delete the last tool; register a new catalog instead.")
            users = sorted(t["id"] for t in remaining if t.get("replaced_by") == tool_id)
            if users:
                raise ToolInUseError(
                    f"{tool_id!r} cannot be deleted: replaced_by in {', '.join(users)} points to it. "
                    "Update or delete those first."
                )
            self._publish(remaining)

    def _publish(self, raw_tools: list[dict[str, Any]]) -> None:
        """Rebuild and swap in a new index. Call only while holding the mutation lock."""
        new_index = self._build_index(raw_tools)   # may raise: nothing is published yet
        self._raw_tools = raw_tools
        self._index = new_index                    # the single publishing step

    @property
    def tool_count(self) -> int:
        return len(self._index.entries)

    @property
    def maps(self) -> DerivedMaps:
        """Derived registry maps (clusters / dup groups / version families)."""
        return self._index.maps

    @property
    def planner(self):
        """The active planner (LLMPlanner or LocalPlanner)."""
        return self._index.planner

    def raw_tool(self, tool_id: str) -> dict[str, Any]:
        """The ORIGINAL registered schema for a tool — what clients receive
        (enrichment never leaves the retrieval layer, see onboarding.md)."""
        return self._index.raw_by_id[tool_id]

    def route(self, query: str, context: Optional[dict] = None) -> RouteResult:
        """Route one query to a minimal, ordered set of tools.

        ``context`` is accepted so callers can already pass it, but the engine
        does not use it yet: routing depends on ``query`` and the catalog only.
        """
        if not isinstance(query, str) or not query.strip():
            return RouteResult(
                clarify=True,
                clarify_question="The request is empty — what would you like to do?",
                trace={"reason": "empty query"},
            )

        idx = self._index      # one snapshot for the whole request (see module docstring)
        signals = analyze_query(query)
        notes: dict[str, Any] = {}        # facts about this request, added to the trace
        candidates, dropped = self._score_and_collapse(query, idx, notes)

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
                query, candidates, self._chat,
                functools.partial(self._score_and_collapse, idx=idx, notes=notes),
                idx.maps, signals,
            )

        decision = decide(candidates, signals, conf)
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

        trace.update(notes)

        if decision.kind == "empty":
            return RouteResult(trace=trace)
        if decision.kind == "clarify":
            return RouteResult(
                clarify=True, clarify_question=decision.clarify_question, trace=trace
            )

        scorer = lambda text: self._score_and_collapse(text, idx, notes)[0]  # noqa: E731
        shortlist = decision.shortlist
        outcome: PlanOutcome = idx.planner.plan(query, signals, shortlist, scorer)

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
            outcome = idx.planner.plan(query, signals, shortlist, scorer)

        # Arbitration: a planner-level clarify is a SECOND opinion. When the
        # local gate already established high confidence (strong top score,
        # no destructive intent), an LLM refusal is more likely a weak-model
        # over-clarification than a real ambiguity — prefer the local plan.
        # (Safety-critical clarifies fire BEFORE the planner, in the gate.)
        if (
            outcome.clarify
            and shortlist
            and shortlist[0].fused >= conf.arbitration_min_fused
            and not signals.destructive_intent
        ):
            logger.warning(
                "planner clarified but local confidence is high (%.2f) — using local plan",
                shortlist[0].fused,
            )
            outcome = idx.local_planner.plan(query, signals, shortlist, scorer)

        # The planner can raise its own (semantic) clarify — honor it.
        if outcome.clarify:
            trace["segments"] = outcome.segments
            return RouteResult(
                clarify=True,
                clarify_question=outcome.clarify_question
                or "Could you give me a bit more detail so I can route this safely?",
                trace=trace,
            )

        trace.update(notes)
        selected = list(dict.fromkeys(outcome.plan))   # unique, order-preserving
        trace["segments"] = outcome.segments

        routed_tokens = _approx_tokens([idx.raw_by_id[t] for t in selected])
        return RouteResult(
            selected_tools=selected,
            plan=outcome.plan,
            tokens_saved_pct=round(1 - routed_tokens / idx.full_catalog_tokens, 4),
            trace=trace,
        )

    # ------------------------------------------------------------- helpers

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
        self, text: str, idx: Optional[_Index] = None, notes: Optional[dict[str, Any]] = None
    ) -> tuple[list[Candidate], dict[str, list[str]]]:
        """Fused scoring + version/dup collapse for any piece of text.

        Purpose clauses are stripped before scoring: the command lives in the
        main clause ("store this file ..."), while purpose vocabulary ("... so
        people can search it") would otherwise pull the ranking toward the
        wrong action (see text.strip_purpose_clause).

        If a hosted embedding provider fails here, this text is scored with local
        TF-IDF instead, and ``notes["embedding_fallback"]`` is set. Once one call
        in a request has failed, the rest of that request stays local, so it does
        not wait on a broken provider again.
        """
        idx = idx or self._index
        notes = notes if notes is not None else {}
        scored_text = strip_purpose_clause(text)
        signals = analyze_query(scored_text)
        entries = idx.entries
        query_vec = None
        if not notes.get("embedding_fallback"):
            try:
                query_vec = idx.provider.embed_query(scored_text)
            except ProviderError as exc:
                if idx.fallback is None:
                    raise
                logger.warning("embedding provider failed (%s) — using local TF-IDF for this query", exc)
                notes["embedding_fallback"] = True
        if query_vec is None:
            local, entries = idx.fallback
            query_vec = local.embed_query(scored_text)
        candidates = score_tools(
            entries, idx.bm25, query_vec, signals, self._settings.weights
        )
        return collapse(candidates, idx.maps, signals)

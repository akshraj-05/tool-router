"""Engine layer: pure routing logic (guardrail, ingest, derive, retrieve, plan).

No I/O, no network, no framework imports — everything here is deterministic
and unit-testable. Providers (embeddings/LLM) are injected via interfaces.
"""

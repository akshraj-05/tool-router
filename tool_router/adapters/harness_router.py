"""Benchmark adapter — the class an external evaluation harness imports.

Deliberately thin: constructs the engine once with the catalog, converts each
RouteResult to the exact dict shape a benchmark harness contract requires.
All intelligence lives in the engine.

Usage (a harness following the `--router module:ClassName` convention):
    PYTHONPATH=<repo> python some_benchmark.py \
        --router tool_router.adapters.harness_router:Router
"""

from __future__ import annotations

from typing import Any, Optional

from tool_router.config import Settings
from tool_router.engine.pipeline import RoutingEngine


class Router:
    """Harness-contract adapter over the RoutingEngine."""

    def __init__(self, catalog: dict[str, Any]) -> None:
        self._engine = RoutingEngine(catalog, settings=Settings.from_env())

    def route(self, query: str, context: Optional[dict] = None) -> dict[str, Any]:
        return self._engine.route(query, context).to_harness_dict()

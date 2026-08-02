"""tool_router — intelligent tool routing engine (routing-as-a-service).

Filters a large enterprise tool catalog down to a small, high-precision,
ordered tool plan per user query. One engine, three entry points:

- ``tool_router.adapters.harness_router.Router`` — benchmark/programmatic adapter
- REST API — ``tool-router serve``
- MCP server — ``find_tools``

See ``docs/design/architecture.md`` for the full architecture.
"""

from tool_router.adapters.harness_router import Router

__version__ = "0.1.0"
__all__ = ["Router", "__version__"]

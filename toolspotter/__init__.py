"""toolspotter — intelligent tool routing engine (routing-as-a-service).

Filters a large enterprise tool catalog down to a small, high-precision,
ordered tool plan per user query. One engine, two entry points:

- REST API — ``toolspotter serve``
- MCP server — ``toolspotter mcp`` (exposes ``find_tools``)

See ``docs/design/architecture.md`` for the full architecture.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]

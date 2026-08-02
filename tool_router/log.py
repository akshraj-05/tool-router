"""Central logging setup.

One place to configure logging for every entry point (harness adapter, CLI,
REST service). Uses stdlib logging so downstream consumers can reconfigure;
never logs secrets (config code logs only presence booleans, never values).
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring root handler once."""
    global _CONFIGURED
    if not _CONFIGURED:
        level = os.environ.get("TOOL_ROUTER_LOG_LEVEL", "INFO").upper()
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root = logging.getLogger("tool_router")
        root.setLevel(getattr(logging, level, logging.INFO))
        root.addHandler(handler)
        root.propagate = False
        _CONFIGURED = True
    return logging.getLogger(f"tool_router.{name}" if not name.startswith("tool_router") else name)

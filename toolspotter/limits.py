"""Size limits on what the router accepts as input."""

from __future__ import annotations

import json
import os
from typing import Any

from toolspotter.domain.errors import CatalogError

DEFAULT_MAX_BYTES = 5_000_000


def max_input_bytes() -> int:
    """Largest catalog file or request body (``TOOLSPOTTER_MAX_BYTES``)."""
    try:
        value = int(os.environ.get("TOOLSPOTTER_MAX_BYTES", ""))
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def read_catalog_file(path: str) -> dict[str, Any]:
    """Load a catalog file, checking its size and basic shape."""
    limit = max_input_bytes()
    with open(path, "rb") as fh:
        data = fh.read(limit + 1)          # bounded even for pipes and growing files
    if len(data) > limit:
        raise CatalogError(
            f"{path} is larger than {limit} bytes (set TOOLSPOTTER_MAX_BYTES to change it)."
        )
    try:
        catalog = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CatalogError(f"{path} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
        raise CatalogError("Catalog must be a JSON object with a 'tools' list.")
    return catalog

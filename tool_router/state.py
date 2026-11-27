"""The ``.tool-router`` folder: the saved registry and the vector cache.

    .tool-router/
        .gitignore        "*", so nothing in here is committed by accident
        catalog.json      current tools plus the history of older definitions
        vectors.db        saved vectors and duplicate pairs (see index_db.py)

``catalog.json`` is also a valid catalog file (its extra keys are ignored when
loaded with ``--catalog``).

Rules
- Settings and API keys are never written here; they stay in the user's config
  folder or keyring.
- Files are readable by their owner only.
- Every write replaces the whole file in one step, so a crash cannot leave half a
  file behind.
- A damaged saved catalog stops startup. It is never silently replaced.
- One running router per folder: there is no cross-process locking.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tool_router.domain.errors import StateError

DEFAULT_STATE_DIR = ".tool-router"
FORMAT_VERSION = 1


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, readable by the owner only."""
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:                                       # mkstemp creates the file with mode 0600
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class SavedState:
    tools: list[dict[str, Any]]
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    near_duplicate_groups: list[list[str]] = field(default_factory=list)


class StateStore:
    """Reads and writes one state folder."""

    def __init__(self, directory: Path | str = DEFAULT_STATE_DIR) -> None:
        self.directory = Path(directory)
        self.catalog_path = self.directory / "catalog.json"

    @property
    def database_path(self) -> Path:
        return self.directory / "vectors.db"

    def prepare(self) -> None:
        """Create the folder if needed. Raises ``StateError`` if that is impossible."""
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            ignore = self.directory / ".gitignore"
            if not ignore.exists():
                write_atomic(ignore, "*\n")
        except OSError as exc:
            raise StateError(
                f"cannot use the state folder {self.directory}: {exc} "
                "(choose another with --state-dir, or use --no-persist)"
            ) from exc

    def load(self) -> Optional[SavedState]:
        """The saved registry, or None if nothing has been saved yet."""
        try:
            text = self.catalog_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise self._damaged(str(exc)) from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise self._damaged(f"not valid JSON ({exc})") from exc
        if not isinstance(data, dict) or data.get("format_version") != FORMAT_VERSION:
            raise self._damaged(f"expected format_version {FORMAT_VERSION}")
        tools = data.get("tools")
        history = data.get("history", {})
        groups = data.get("near_duplicate_groups", [])
        if not (isinstance(tools, list) and tools
                and all(isinstance(t, dict) and isinstance(t.get("id"), str) for t in tools)):
            raise self._damaged("'tools' must be a non-empty list of tools with ids")
        if not (isinstance(history, dict) and all(
                isinstance(v, list) and all(isinstance(t, dict) for t in v) for v in history.values())):
            raise self._damaged("'history' must map tool ids to lists of tools")
        if not isinstance(groups, list):
            raise self._damaged("'near_duplicate_groups' must be a list")
        return SavedState(tools, history, groups)

    def save(self, state: SavedState) -> None:
        self.prepare()
        payload = {
            "format_version": FORMAT_VERSION,
            "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "near_duplicate_groups": state.near_duplicate_groups,
            "tools": state.tools,
            "history": state.history,
        }
        try:
            write_atomic(self.catalog_path, json.dumps(payload, indent=1) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            raise StateError(f"could not save {self.catalog_path}: {exc}") from exc

    def _damaged(self, why: str) -> StateError:
        return StateError(
            f"the saved catalog {self.catalog_path} cannot be read: {why}. "
            "Fix the file, or move it aside to start fresh."
        )

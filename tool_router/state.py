"""The ``.tool-router`` folder and the registry saved in it.

    .tool-router/
        .gitignore   "*", so nothing in here is committed by accident
        router.db    tools, their history, saved vectors and recent requests
                     (see database.py)

Rules
- Settings and API keys are never written here; they stay in the user's config
  folder or keyring.
- Files are readable by their owner only.
- Tools, history and groups are saved in one transaction, so a crash cannot leave
  half a change behind.
- A damaged saved registry stops startup. It is never silently replaced.
- One running router per folder: two would overwrite each other's changes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tool_router.database import FILE_NAME, open_database
from tool_router.domain.errors import StateError

DEFAULT_STATE_DIR = ".tool-router"


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
    """Reads and writes the registry in one state folder."""

    def __init__(self, directory: Path | str = DEFAULT_STATE_DIR) -> None:
        self.directory = Path(directory)
        self.database_path = self.directory / FILE_NAME

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
            if not self.database_path.exists():
                return None
            if self.database_path.stat().st_size == 0:
                raise self._damaged("the file is empty")
            with open_database(self.database_path) as db:
                db.execute("BEGIN")                  # all three reads see the same version
                tool_rows = db.execute(
                    "SELECT id, definition FROM tools ORDER BY position").fetchall()
                history_rows = db.execute(
                    "SELECT tool_id, definition FROM tool_history ORDER BY position").fetchall()
                group_rows = db.execute(
                    "SELECT tool_ids FROM duplicate_groups ORDER BY position").fetchall()
            tools = [json.loads(definition) for _, definition in tool_rows]
            history: dict[str, list[dict[str, Any]]] = {}
            for tool_id, definition in history_rows:
                history.setdefault(tool_id, []).append(json.loads(definition))
            groups = [json.loads(ids) for (ids,) in group_rows]
        except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError) as exc:
            raise self._damaged(str(exc)) from exc
        if not tools:
            return None
        if not all(isinstance(tool, dict) and tool.get("id") == tool_id
                   for (tool_id, _), tool in zip(tool_rows, tools)):
            raise self._damaged("a saved tool does not match its id")
        if not all(isinstance(old, dict) for versions in history.values() for old in versions):
            raise self._damaged("the tool history holds something that is not a tool")
        if not all(isinstance(group, list) for group in groups):
            raise self._damaged("a saved duplicate group is not a list")
        return SavedState(tools, history, groups)

    def save(self, state: SavedState) -> None:
        """Replace the saved registry in one transaction: all of it, or nothing."""
        self.prepare()
        try:
            tools = [(i, t["id"], json.dumps(t)) for i, t in enumerate(state.tools)]
            history = [(tool_id, json.dumps(old))
                       for tool_id, versions in state.history.items() for old in versions]
            groups = [(json.dumps(group),) for group in state.near_duplicate_groups]
            with open_database(self.database_path) as db:
                for table in ("tools", "tool_history", "duplicate_groups"):
                    db.execute(f"DELETE FROM {table}")
                db.executemany(
                    "INSERT INTO tools (position, id, definition) VALUES (?, ?, ?)", tools)
                db.executemany(
                    "INSERT INTO tool_history (tool_id, definition) VALUES (?, ?)", history)
                db.executemany("INSERT INTO duplicate_groups (tool_ids) VALUES (?)", groups)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise StateError(f"could not save {self.database_path}: {exc}") from exc

    def _damaged(self, why: str) -> StateError:
        return StateError(
            f"the saved registry {self.database_path} cannot be read: {why}. "
            "It has not been changed. Move it aside to start fresh."
        )

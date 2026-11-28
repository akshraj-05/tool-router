"""``router.db``: the one SQLite file the router saves to.

    table              holds                              if it is lost
    tools              the current tool definitions       the registry is gone
    tool_history       older definitions                  version history is gone
    duplicate_groups   groups declared in the catalog     they must be declared again
    vectors            one vector per tool                computed again
    cache              other computed results             computed again
    requests           the recent routed requests         history is gone

Because the registry lives here, the file is never deleted or replaced by the
router, whatever is wrong with it.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Iterator

FILE_NAME = "router.db"
FORMAT_VERSION = 1

_CREATE = f"""
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS tools (
    position INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, definition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_history (
    position INTEGER PRIMARY KEY, tool_id TEXT NOT NULL, definition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS duplicate_groups (
    position INTEGER PRIMARY KEY, tool_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vectors (hash TEXT PRIMARY KEY, vector NOT NULL);
CREATE TABLE IF NOT EXISTS cache (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, query TEXT NOT NULL, result TEXT NOT NULL,
    plan TEXT NOT NULL, ms REAL NOT NULL, tokens_saved_pct REAL);
PRAGMA user_version = {FORMAT_VERSION};
COMMIT;
"""


@contextlib.contextmanager
def open_database(path: Path) -> Iterator[sqlite3.Connection]:
    """One transaction on ``path``: committed on success, rolled back on error.

    A missing file is created, readable by its owner only, with all its tables.
    Raises ``sqlite3.Error`` or ``OSError``.
    """
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    connection = sqlite3.connect(path, timeout=5)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            connection.executescript(_CREATE)
        elif version != FORMAT_VERSION:
            raise sqlite3.DatabaseError(
                f"format {version} is not supported (this version reads format {FORMAT_VERSION})"
            )
        with connection:
            yield connection
    finally:
        connection.close()

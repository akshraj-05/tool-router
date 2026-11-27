"""``vectors.db``: the SQLite file holding results that are costly to compute.

    vectors(hash, vector)   one vector per tool, keyed by a hash of its search text.
                            Local vectors are JSON text (word -> weight); hosted,
                            dense vectors are 64-bit floats in a BLOB.
    meta(name, value)       ``model_stamp`` (which method made the vectors) and
                            saved results such as the duplicate pairs.

Everything here can be computed again from the registry, so a problem with this
file never stops the router: unusable rows are recomputed, a damaged file is
replaced, and a file that cannot be written is left alone with a warning.
"""

from __future__ import annotations

import array
import contextlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from tool_router.log import get_logger

logger = get_logger("index_db")

_TABLES = (
    "CREATE TABLE IF NOT EXISTS vectors (hash TEXT PRIMARY KEY, vector NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


@contextlib.contextmanager
def _database(path: Path) -> Iterator[sqlite3.Connection]:
    """One transaction on ``path``: committed on success, rolled back on error."""
    if not path.exists():                          # readable by the owner only
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    connection = sqlite3.connect(path, timeout=5)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _is_damage(error: sqlite3.Error) -> bool:
    """True if the file itself is unusable (as opposed to busy, full or read-only)."""
    return not isinstance(error, sqlite3.OperationalError) or "column" in str(error)


def _write(path: Path, action: Callable[[sqlite3.Connection, bool], None]) -> None:
    """Run ``action(db, fresh)``. A damaged file is replaced and written from scratch."""
    for fresh in (False, True):
        try:
            with _database(path) as db:
                for statement in _TABLES:
                    db.execute(statement)
                action(db, fresh)
            return
        except (sqlite3.Error, OSError) as exc:
            damaged = isinstance(exc, sqlite3.Error) and _is_damage(exc)
            if fresh or not damaged:
                logger.warning("could not save %s (%s) — continuing in memory", path, exc)
                return
            logger.warning("%s is damaged (%s) — replacing it", path, exc)
            with contextlib.suppress(OSError):
                path.unlink()


def _encode(vector: Any) -> Any:
    if isinstance(vector, dict):
        return json.dumps(vector)
    return array.array("d", vector).tobytes()


def _decode(stored: Any) -> Any:
    """The saved vector, or None if it cannot be read."""
    try:
        if isinstance(stored, str):
            return json.loads(stored)
        if isinstance(stored, bytes):
            values = array.array("d")
            values.frombytes(stored)
            return values.tolist()
    except (ValueError, RecursionError):
        pass
    return None


class VectorCache:
    """Vectors made by one method (``model_stamp``), saved if ``path`` is given.

    The file holds one method's vectors at a time: saving with a different stamp
    replaces them all.
    """

    def __init__(
        self,
        path: Optional[Path],
        model_stamp: str,
        rebuild: bool = False,
        valid: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        """``path=None`` keeps the vectors in memory only. Saved vectors that fail
        ``valid`` are computed again."""
        self._path = path
        self._stamp = model_stamp
        self._data: dict[str, Any] = {}
        self._changed: set[str] = set()
        self._removed: set[str] = set()
        self._replace_all = True                   # until a usable file is read
        if path is None or rebuild or not path.exists():
            return
        try:
            with _database(path) as db:
                saved = db.execute("SELECT value FROM meta WHERE name = 'model_stamp'").fetchone()
                rows = db.execute("SELECT hash, vector FROM vectors").fetchall()
        except (sqlite3.Error, OSError, UnicodeDecodeError) as exc:
            logger.warning("could not read %s (%s) — computing vectors again", path, exc)
            return
        if saved is None or saved[0] != model_stamp:   # stored vectors are incompatible
            return
        self._replace_all = False
        for key, stored in rows:
            vector = _decode(stored)
            if vector is None or (valid is not None and not valid(vector)):
                self._removed.add(key)
            else:
                self._data[key] = vector
        if self._removed:
            logger.warning(
                "dropped %d unusable vectors from %s — they will be computed again",
                len(self._removed), path.name,
            )

    def get(self, key: str) -> Any:
        return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._changed.add(key)

    def keep(self, keys: set[str]) -> bool:
        """Drop every other entry. Returns True if anything was dropped."""
        stale = self._data.keys() - keys
        for key in stale:
            del self._data[key]
        self._removed |= stale
        self._changed -= stale
        return bool(stale)

    def save(self) -> None:
        """Write what changed, in one transaction."""
        if self._path is None:
            return

        def action(db: sqlite3.Connection, fresh: bool) -> None:
            everything = fresh or self._replace_all
            if everything:
                db.execute("DELETE FROM vectors")
            else:
                db.executemany("DELETE FROM vectors WHERE hash = ?", [(k,) for k in self._removed])
            db.executemany(
                "INSERT OR REPLACE INTO vectors (hash, vector) VALUES (?, ?)",
                [(k, _encode(self._data[k])) for k in (self._data if everything else self._changed)],
            )
            db.execute(
                "INSERT OR REPLACE INTO meta (name, value) VALUES ('model_stamp', ?)", (self._stamp,)
            )

        _write(self._path, action)

    @property
    def size(self) -> int:
        return len(self._data)


def load_result(path: Path, name: str, fingerprint: str) -> Any:
    """A saved result, only if it was computed from exactly this ``fingerprint``."""
    if not path.exists():
        return None
    try:
        with _database(path) as db:
            row = db.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        saved = json.loads(row[0]) if row else None
    except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError) as exc:
        logger.warning("could not read %s from %s (%s) — computing it again", name, path, exc)
        return None
    if not isinstance(saved, dict) or saved.get("fingerprint") != fingerprint:
        return None
    return saved.get("value")


def save_result(path: Path, name: str, fingerprint: str, value: Any) -> None:
    text = json.dumps({"fingerprint": fingerprint, "value": value})
    _write(path, lambda db, fresh: db.execute(
        "INSERT OR REPLACE INTO meta (name, value) VALUES (?, ?)", (name, text)
    ))

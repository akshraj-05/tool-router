"""Recently routed requests, kept in ``router.db``.

``record`` only puts the request in a queue, so answering never waits for the
disk. One background thread collects requests for ``_COLLECT_SECONDS`` and writes
them in a single transaction: writing each one separately slowed the server down.
A killed process can therefore lose that last fraction of a second of history.

Only the newest ``MAX_ROWS`` requests are kept, each query cut to
``MAX_QUERY_CHARS`` characters. History is a convenience: a problem with it is
logged and never fails a request.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tool_router.database import open_database
from tool_router.domain.errors import StateError
from tool_router.domain.models import RouteResult
from tool_router.log import get_logger

logger = get_logger("history")

MAX_ROWS = 1000
MAX_QUERY_CHARS = 500
RESULTS = ("plan", "question", "none")
_QUEUE_SIZE = 5000          # requests waiting to be written; more than this are dropped
_COLLECT_SECONDS = 0.5
_WAIT_SECONDS = 10.0
_STOP = object()


class RequestHistory:
    """The request history in one database file. ``path=None`` records nothing."""

    def __init__(self, path: Optional[Path]) -> None:
        self._path = path
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=_QUEUE_SIZE)
        self._writer: Optional[threading.Thread] = None
        self._write_now = threading.Event()      # someone is waiting: stop collecting
        self._lock = threading.Lock()
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(self, query: str, result: RouteResult, ms: float) -> None:
        """Queue one routed request to be saved. Returns at once."""
        if self._path is None:
            return
        kind = "question" if result.clarify else "plan" if result.plan else "none"
        row = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Text that is not valid Unicode cannot be stored as it is.
            query[:MAX_QUERY_CHARS].encode("utf-8", "replace").decode("utf-8"),
            kind,
            json.dumps(result.plan),
            round(ms, 3),
            result.tokens_saved_pct,
        )
        with self._lock:
            if self._closed:
                return
            if self._writer is None:
                self._writer = threading.Thread(
                    target=self._write_queued, name="request-history", daemon=True)
                self._writer.start()
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            logger.warning("request history is falling behind — one request was not saved")

    def flush(self) -> None:
        """Wait until everything recorded so far has been written."""
        writer = self._writer
        if writer is None or not writer.is_alive():
            return
        written = threading.Event()
        try:
            self._queue.put(written, timeout=_WAIT_SECONDS)
        except queue.Full:
            return
        self._write_now.set()
        written.wait(_WAIT_SECONDS)

    def close(self) -> None:
        """Write what is queued and stop the background thread."""
        with self._lock:
            self._closed = True
            writer = self._writer
        if writer is None or not writer.is_alive():
            return
        try:
            self._queue.put(_STOP, timeout=_WAIT_SECONDS)
        except queue.Full:
            return
        self._write_now.set()
        writer.join(_WAIT_SECONDS)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """The newest requests first."""
        rows = self._read(
            "SELECT id, at, query, result, plan, ms, tokens_saved_pct FROM requests "
            "ORDER BY id DESC LIMIT ?", (max(0, limit),))
        keys = ("id", "at", "query", "result", "plan", "ms", "tokens_saved_pct")
        try:
            return [{**dict(zip(keys, row)), "plan": json.loads(row[4])} for row in rows]
        except (ValueError, TypeError) as exc:
            raise StateError(f"the request history in {self._path} cannot be read: {exc}") from exc

    def summary(self) -> dict[str, Any]:
        """Totals over the saved requests. The database does the arithmetic.

        ``average_tokens_saved_pct`` covers only requests that returned tools: a
        question or an empty answer has no saving.
        """
        rows = self._read(
            "SELECT result, COUNT(*), SUM(ms), SUM(tokens_saved_pct), COUNT(tokens_saved_pct) "
            "FROM requests GROUP BY result")
        counts = dict.fromkeys(RESULTS, 0)
        counts.update({row[0]: row[1] for row in rows})
        total = sum(row[1] for row in rows)
        with_saving = sum(row[4] for row in rows)
        return {
            "requests": total,
            "kept_at_most": MAX_ROWS,
            "average_ms": round(sum(row[2] for row in rows) / total, 3) if total else None,
            "average_tokens_saved_pct": round(
                sum(row[3] or 0 for row in rows) / with_saving, 4) if with_saving else None,
            "results": counts,
        }

    def clear(self) -> int:
        """Delete the saved requests and return how many there were."""
        if self._path is None:
            return 0
        self.flush()
        if not self._path.exists():
            return 0
        try:
            with open_database(self._path) as db:
                db.execute("PRAGMA secure_delete = ON")     # overwrite the text, not just unlink it
                return db.execute("DELETE FROM requests").rowcount
        except (sqlite3.Error, OSError) as exc:
            raise StateError(f"could not clear the request history in {self._path}: {exc}") from exc

    def _read(self, statement: str, values: tuple = ()) -> list[tuple]:
        if self._path is None:
            return []
        self.flush()
        if not self._path.exists():
            return []
        try:
            with open_database(self._path) as db:
                return db.execute(statement, values).fetchall()
        except (sqlite3.Error, OSError) as exc:
            raise StateError(f"the request history in {self._path} cannot be read: {exc}") from exc

    def _write_queued(self) -> None:
        """The background thread: collect requests briefly, then write them together."""
        while True:
            items = [self._queue.get()]
            if isinstance(items[0], tuple):
                self._write_now.wait(_COLLECT_SECONDS)
            self._write_now.clear()
            while True:
                try:
                    items.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            rows = [item for item in items if isinstance(item, tuple)]
            if rows:
                try:
                    self._insert(rows)
                except Exception as exc:        # the thread must outlive any one failure
                    logger.warning("could not save %d request(s) to %s (%s)",
                                   len(rows), self._path, exc)
            for item in items:
                if isinstance(item, threading.Event):
                    item.set()
            if _STOP in items:
                return

    def _insert(self, rows: list[tuple]) -> None:
        with open_database(self._path) as db:
            db.execute("PRAGMA secure_delete = ON")
            db.executemany(
                "INSERT INTO requests (at, query, result, plan, ms, tokens_saved_pct) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
            db.execute("DELETE FROM requests WHERE id <= (SELECT MAX(id) FROM requests) - ?",
                       (MAX_ROWS,))

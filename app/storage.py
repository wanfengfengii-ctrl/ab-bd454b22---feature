"""Durable snapshot store.

Only *terminal* rows are persisted:

* ``COMPLETE`` - a full snapshot read from a single controller revision;
* ``FAILED``   - an attempt that never stabilised; it carries a stable
                 failure reason but never any values or digest, so it is
                 not queryable as evidence.

No in-progress state is written, which means a crash mid-read leaves no
zombie rows: a restarted process simply performs the read again and the
UNIQUE ``snapshot_id`` constraint arbitrates the final insert.

Within one gateway process, concurrent requests for the same
snapshotId are coalesced by :class:`InflightRegistry` so only one read
happens. Across processes both may read, but the database still allows
exactly one canonical row.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

COMPLETE = "COMPLETE"
FAILED = "FAILED"

_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id   TEXT PRIMARY KEY,
                device_id     TEXT NOT NULL,
                start_addr    INTEGER NOT NULL,
                count         INTEGER NOT NULL,
                revision      INTEGER,
                values_json   TEXT,
                sha256        TEXT,
                state         TEXT NOT NULL,
                error         TEXT,
                created_at    REAL NOT NULL
            )
            """
        )


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def same_params(row: dict[str, Any], device_id: str, start_addr: int, count: int) -> bool:
    return (
        row["device_id"] == device_id
        and row["start_addr"] == start_addr
        and row["count"] == count
    )


def row_to_snapshot(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if data.get("values_json"):
        data["values"] = json.loads(data["values_json"])
    return data


def get(snapshot_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        return row_to_snapshot(row)


def save_complete(
    snapshot_id: str,
    device_id: str,
    start_addr: int,
    count: int,
    revision: int,
    values: list[int],
    digest: str,
) -> tuple[str, dict[str, Any]]:
    """Persist a complete snapshot.

    Returns ``(outcome, row)`` where outcome is one of:

    * ``created``  - this caller's snapshot is now the canonical row;
    * ``replayed`` - an identical-parameter COMPLETE row already existed;
    * ``failed``   - an identical-parameter FAILED row already existed;
    * ``conflict`` - a row with different parameters already existed.
    """
    with _db_lock, _connect() as conn:
        with _immediate(conn):
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if row is not None:
                existing = dict(row)
                if (existing["device_id"], existing["start_addr"], existing["count"]) != (
                    device_id,
                    start_addr,
                    count,
                ):
                    return "conflict", existing
                return ("replayed" if existing["state"] == COMPLETE else "failed"), existing

            conn.execute(
                """
                INSERT INTO snapshots
                    (snapshot_id, device_id, start_addr, count, revision,
                     values_json, sha256, state, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    snapshot_id,
                    device_id,
                    start_addr,
                    count,
                    revision,
                    json.dumps(values),
                    digest,
                    COMPLETE,
                    time.time(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            return "created", dict(row)


def save_failure(
    snapshot_id: str,
    device_id: str,
    start_addr: int,
    count: int,
    error: str,
) -> tuple[str, dict[str, Any]]:
    """Persist a terminal failure (never queryable as evidence).

    Returns the same outcome tuples as :func:`save_complete`; a complete
    row that another worker committed in the meantime wins (``replayed``).
    """
    with _db_lock, _connect() as conn:
        with _immediate(conn):
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if row is not None:
                existing = dict(row)
                if (existing["device_id"], existing["start_addr"], existing["count"]) != (
                    device_id,
                    start_addr,
                    count,
                ):
                    return "conflict", existing
                return ("replayed" if existing["state"] == COMPLETE else "failed"), existing

            conn.execute(
                """
                INSERT INTO snapshots
                    (snapshot_id, device_id, start_addr, count, revision,
                     values_json, sha256, state, error, created_at)
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                """,
                (snapshot_id, device_id, start_addr, count, FAILED, error, time.time()),
            )
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            return "failed", dict(row)


class InflightRegistry:
    """Coalesces same-snapshotId requests inside one process.

    The first request for a snapshotId becomes the worker; the rest wait
    on an :class:`threading.Event` and replay its outcome. The durable
    guarantee still comes from the database UNIQUE constraint, so a
    restart simply starts with an empty registry.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._slots: dict[str, _Slot] = {}

    def acquire(
        self, snapshot_id: str, device_id: str, start_addr: int, count: int
    ) -> tuple[str, "_Slot"]:
        """Claim or join an in-flight slot.

        Returns ``(state, slot)`` with state ``"worker"`` (this caller
        does the read), ``"waiter"`` (same params, replay the worker's
        outcome) or ``"conflict"`` (same snapshotId, different params).
        """
        with self._guard:
            slot = self._slots.get(snapshot_id)
            if slot is not None:
                if (slot.device_id, slot.start_addr, slot.count) != (
                    device_id,
                    start_addr,
                    count,
                ):
                    return "conflict", slot
                return "waiter", slot
            slot = _Slot(device_id, start_addr, count)
            self._slots[snapshot_id] = slot
            return "worker", slot

    def release(self, snapshot_id: str, slot: "_Slot") -> None:
        with self._guard:
            if self._slots.get(snapshot_id) is slot:
                del self._slots[snapshot_id]


class _Slot:
    def __init__(self, device_id: str, start_addr: int, count: int) -> None:
        self.device_id = device_id
        self.start_addr = start_addr
        self.count = count
        self.event = threading.Event()
        self.result: Any = None
        self.exc: BaseException | None = None

    def fulfill(self, result: Any) -> None:
        self.result = result
        self.event.set()

    def fail(self, exc: BaseException) -> None:
        self.exc = exc
        self.event.set()

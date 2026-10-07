"""Snapshot acquisition: paged, revision-conditioned reads.

One *attempt* reads every register from the first page to the last:

1. First page is requested without a revision condition; the revision
   it reports is pinned for the whole attempt.
2. Every following page carries that pinned revision. A revision
   mismatch, timeout, short page, duplicated/mis-echoed address range
   or non-u16 value abandons the **entire** attempt and a fresh one
   restarts from the first page.
3. After ``MAX_ATTEMPTS`` unstable attempts the failure is terminal.

Only a complete, single-revision read is hashed and handed to the
store, so no partial data can ever become evidence.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from . import config, storage
from .controller_client import (
    NonRetryableReadError,
    ReadError,
    RevisionMismatch,
    read_page,
)


@dataclass
class Failure(Exception):
    """A stable, persisted failure reason."""

    reason: str
    kind: str  # "revision_conflict" | "upstream_error" | "parameter_conflict"

    def __str__(self) -> str:
        return self.reason


def _read_one_attempt(
    client: httpx.Client, device_id: str, start_addr: int, count: int
) -> tuple[int, list[int]]:
    """Read all pages in order under one pinned revision.

    Raises RevisionMismatch / ReadError on any inconsistency.
    """
    values: list[int] = []
    revision: int | None = None
    offset = 0
    while offset < count:
        n = min(config.PAGE_SIZE, count - offset)
        page_start = start_addr + offset
        page_rev, page_values = read_page(
            client,
            config.CONTROLLER_URL,
            device_id,
            page_start,
            n,
            revision=revision,
        )
        if revision is None:
            # First page: pin whatever revision it reports.
            revision = page_rev
        elif page_rev != revision:
            raise RevisionMismatch(revision, page_rev)
        values.extend(page_values)
        offset += n
    assert revision is not None
    return revision, values


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=config.CONTROLLER_TIMEOUT)


def _evidence_hash(values: list[int]) -> str:
    # Big-endian concatenation of the 16-bit register values.
    blob = b"".join(v.to_bytes(2, byteorder="big") for v in values)
    return hashlib.sha256(blob).hexdigest()


def acquire(
    snapshot_id: str,
    device_id: str,
    start_addr: int,
    count: int,
    client_factory: "callable | None" = None,
) -> dict:
    """Return the canonical snapshot row, or raise :class:`Failure`."""
    last_problem = "unknown controller error"
    last_kind = "upstream_error"

    factory = client_factory or _make_client
    with factory() as client:
        for attempt in range(1, config.MAX_ATTEMPTS + 1):
            try:
                revision, values = _read_one_attempt(
                    client, device_id, start_addr, count
                )
            except RevisionMismatch as exc:
                # Discard everything gathered this round; retry page 1.
                last_problem = (
                    f"controller revision changed during read after "
                    f"{attempt} attempt(s): {exc}"
                )
                last_kind = "revision_conflict"
                continue
            except NonRetryableReadError as exc:
                # A definitive rejection (e.g. address range out of
                # bounds): retrying cannot help; persist nothing.
                raise Failure(f"controller rejected the read: {exc}", "bad_range")
            except ReadError as exc:
                last_problem = f"controller read failed after {attempt} attempt(s): {exc}"
                last_kind = "upstream_error"
                continue

            if len(values) != count:
                # Should be impossible after page validation, but never
                # persist a partial snapshot.
                last_problem = "short or duplicated register set discarded"
                last_kind = "upstream_error"
                continue

            digest = _evidence_hash(values)
            outcome, row = storage.save_complete(
                snapshot_id, device_id, start_addr, count, revision, values, digest
            )
            if outcome == "conflict":
                raise Failure(
                    "snapshotId already exists with different parameters "
                    f"(deviceId={row['device_id']!r}, startAddr={row['start_addr']}, "
                    f"count={row['count']})",
                    "parameter_conflict",
                )
            if outcome == "failed":
                # Another worker (e.g. a second gateway process) already
                # persisted a terminal failure for this snapshotId.
                reason = row.get("error") or "snapshot previously failed"
                kind = (
                    reason[1:].split("]", 1)[0]
                    if reason.startswith("[")
                    else "upstream_error"
                )
                raise Failure(reason, kind)
            row["outcome"] = outcome  # created | replayed
            return storage.row_to_snapshot(row)  # type: ignore[return-value]

    failure = Failure(
        f"{last_problem}; snapshot discarded after "
        f"{config.MAX_ATTEMPTS} attempts",
        last_kind,
    )
    outcome, row = storage.save_failure(
        snapshot_id,
        device_id,
        start_addr,
        count,
        f"[{failure.kind}] {failure.reason}",
    )
    if outcome == "conflict":
        raise Failure(
            "snapshotId already exists with different parameters "
            f"(deviceId={row['device_id']!r}, startAddr={row['start_addr']}, "
            f"count={row['count']})",
            "parameter_conflict",
        )
    if outcome == "replayed":
        # Another worker committed a complete snapshot while we were failing.
        return storage.row_to_snapshot(row)  # type: ignore[return-value]
    # outcome == "failed": raise using the persisted reason (either the
    # row just inserted by us or a terminal failure another process won).
    reason = row.get("error") or str(failure)
    kind = (
        reason[1:].split("]", 1)[0]
        if reason.startswith("[")
        else last_kind
    )
    raise Failure(reason, kind)

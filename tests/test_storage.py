"""Unit tests for the durable snapshot store."""
from __future__ import annotations

import os
import tempfile

import pytest

from app import config, storage


@pytest.fixture(autouse=True)
def fresh_db():
    tmp = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmp, "snapshots.db")
    storage.init_db()
    yield


VALUES = [1, 2, 3, 65535]
DIGEST = "deadbeef"


def _save(reason_created=True, **kw):
    params = dict(
        snapshot_id="s1",
        device_id="buoy-A",
        start_addr=0,
        count=4,
        revision=7,
        values=VALUES,
        digest=DIGEST,
    )
    params.update(kw)
    return storage.save_complete(**params)


def test_save_complete_then_get():
    outcome, row = _save()
    assert outcome == "created"
    got = storage.get("s1")
    assert got is not None
    assert got["state"] == storage.COMPLETE
    assert got["values"] == VALUES
    assert got["sha256"] == DIGEST
    assert got["revision"] == 7


def test_same_id_same_params_replays_canonical_row():
    _save(revision=7)
    outcome, row = _save(revision=99, digest="00")
    assert outcome == "replayed"
    # The second writer must not overwrite the canonical evidence.
    assert row["revision"] == 7
    assert row["sha256"] == DIGEST


def test_same_id_different_params_is_conflict():
    _save()
    outcome, row = _save(device_id="buoy-B")
    assert outcome == "conflict"
    assert row["device_id"] == "buoy-A"

    outcome, row = _save(start_addr=10)
    assert outcome == "conflict"
    outcome, row = _save(count=5)
    assert outcome == "conflict"


def test_failure_row_has_no_values_and_is_not_evidence():
    outcome, row = storage.save_failure("s1", "buoy-A", 0, 4, "[revision_conflict] boom")
    assert outcome == "failed"
    assert row["state"] == storage.FAILED
    assert row["values_json"] is None
    assert row["sha256"] is None
    assert storage.row_to_snapshot(row)["state"] == storage.FAILED


def test_terminal_rows_never_overwritten():
    # A failure persisted first wins: the later complete is not stored.
    storage.save_failure("s1", "buoy-A", 0, 4, "x")
    outcome, row = _save()
    assert outcome == "failed"
    assert row["state"] == storage.FAILED

    # A complete snapshot persisted first wins: the later failure is
    # suppressed and the canonical evidence is replayed.
    _save(snapshot_id="s2")
    outcome, row = storage.save_failure("s2", "buoy-A", 0, 4, "x")
    assert outcome == "replayed"
    assert row["state"] == storage.COMPLETE
    assert row["sha256"] == DIGEST


def test_same_params_helper():
    _, row = _save()
    assert storage.same_params(row, "buoy-A", 0, 4)
    assert not storage.same_params(row, "buoy-A", 1, 4)


def test_inflight_registry_coalesces_same_params_and_rejects_others():
    reg = storage.InflightRegistry()
    state1, slot1 = reg.acquire("x", "d", 0, 4)
    assert state1 == "worker"
    state2, slot2 = reg.acquire("x", "d", 0, 4)
    assert state2 == "waiter" and slot2 is slot1
    state3, slot3 = reg.acquire("x", "d", 0, 5)
    assert state3 == "conflict"
    state4, slot4 = reg.acquire("y", "d", 0, 4)
    assert state4 == "worker"

    slot1.fulfill({"ok": True})
    assert slot2.event.is_set() and slot2.result == {"ok": True}
    reg.release("x", slot1)
    state5, _ = reg.acquire("x", "d", 0, 4)
    assert state5 == "worker"

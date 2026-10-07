"""Unit tests for paged acquisition, revision pinning and retries."""
from __future__ import annotations

import hashlib
import json as jsonlib
import os
import tempfile
from typing import Any

import pytest

from app import config, snapshot_service, storage
from app.controller_client import ReadError, RevisionMismatch


# ---------------------------------------------------------------- fakes

class FakeResponse:
    def __init__(self, status_code: int, body: Any, text: str | None = None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else jsonlib.dumps(body)

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeController:
    """Scriptable stand-in for the buoy controller."""

    def __init__(self, size: int = 256):
        self.registers = [v & 0xFFFF for v in range(size)]
        self.revision = 1
        self.calls: list[dict[str, Any]] = []
        self.bounds_error = False
        # hook(params, ctx) -> ("ok" | "rev_bump" | "timeout" | "500" |
        #                        "short" | "bad_value" | "bad_echo", response?)
        self.hook = None

    def client(self) -> "FakeClient":
        return FakeClient(self)

    def get(self, params: dict[str, Any]) -> FakeResponse:
        idx = len(self.calls)
        self.calls.append(dict(params))
        start, count = params["start"], params["count"]
        rev = params.get("revision")

        if self.bounds_error or start < 0 or start + count > len(self.registers):
            return FakeResponse(400, {"error": "range out of bounds"})

        action = "ok"
        if self.hook is not None:
            action = self.hook(params, idx) or "ok"

        if action == "timeout":
            raise _Timeout("read timed out")
        if action == "500":
            return FakeResponse(500, {"error": "boom"})

        if action == "rev_bump":
            self.revision += 1

        if rev is not None and rev != self.revision:
            return FakeResponse(
                409, {"error": "revision_mismatch", "current": self.revision}
            )

        values = list(self.registers[start:start + count])
        echo_start, echo_count = start, count
        if action == "short":
            values = values[:-1]
        elif action == "bad_value":
            values[0] = 0x10000
        elif action == "bad_echo":
            echo_start = start + 1

        return FakeResponse(200, {
            "deviceId": params["deviceId"],
            "start": echo_start,
            "count": echo_count,
            "revision": self.revision,
            "values": values,
        })


class _Timeout(Exception):
    pass


class FakeClient:
    def __init__(self, ctrl: FakeController):
        self.ctrl = ctrl

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url: str, params: dict[str, Any] | None = None):
        try:
            return self.ctrl.get(params or {})
        except _Timeout as exc:
            import httpx
            raise httpx.ReadTimeout(str(exc))


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tmp, "s.db"))
    monkeypatch.setattr(config, "PAGE_SIZE", 64)
    monkeypatch.setattr(config, "MAX_ATTEMPTS", 3)
    storage.init_db()
    yield


def _factory(ctrl: FakeController):
    return lambda: ctrl.client()


def _expected_digest(values: list[int]) -> str:
    blob = b"".join(v.to_bytes(2, "big") for v in values)
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------- tests

def test_stable_multipage_read_is_one_revision_with_valid_hash():
    ctrl = FakeController()
    row = snapshot_service.acquire(
        "s1", "buoy-A", 10, 100, client_factory=_factory(ctrl)
    )
    assert row["outcome"] == "created"
    assert row["revision"] == 1
    assert row["values"] == [v & 0xFFFF for v in range(10, 110)]
    assert len(row["values"]) == 100
    assert row["sha256"] == _expected_digest(row["values"])
    # 64 + 36 = two pages, both conditioned after the first.
    assert len(ctrl.calls) == 2
    assert ctrl.calls[0].get("revision") is None
    assert ctrl.calls[1]["revision"] == 1
    got = storage.get("s1")
    assert got["state"] == storage.COMPLETE


def test_revision_change_mid_read_discards_round_and_retries():
    ctrl = FakeController()

    def hook(params, idx):
        # Bump on the second page (conditional) of the first attempt.
        if idx == 1:
            return "rev_bump"
        return "ok"

    ctrl.hook = hook
    row = snapshot_service.acquire(
        "s2", "buoy-A", 0, 100, client_factory=_factory(ctrl)
    )
    # Attempt 1 discarded; attempt 2 reads revision 2 fully.
    assert row["revision"] == 2
    assert row["sha256"] == _expected_digest(row["values"])
    assert len(ctrl.calls) == 4  # 2 attempts x 2 pages
    assert ctrl.calls[2].get("revision") is None  # restarted from page 1


def test_continuous_revision_changes_fail_stably_and_persist_nothing():
    ctrl = FakeController()

    def hook(params, idx):
        # Every second page always sees a newer revision.
        if params.get("revision") is not None and params["start"] == 64:
            return "rev_bump"
        return "ok"

    ctrl.hook = hook
    with pytest.raises(snapshot_service.Failure) as ei:
        snapshot_service.acquire("s3", "buoy-A", 0, 100, client_factory=_factory(ctrl))
    assert ei.value.kind == "revision_conflict"
    # 3 full attempts, each restarted at page 1.
    assert len(ctrl.calls) == 6
    row = storage.get("s3")
    assert row["state"] == storage.FAILED
    assert row["values_json"] is None
    assert row["sha256"] is None
    assert "revision_conflict" in row["error"]
    # The failed snapshotId carries no values and is not evidence.
    assert "values" not in storage.row_to_snapshot(row)


def test_timeout_then_recovery_succeeds_on_retry():
    ctrl = FakeController()

    def hook(params, idx):
        return "timeout" if idx == 1 else "ok"

    ctrl.hook = hook
    row = snapshot_service.acquire(
        "s4", "buoy-A", 0, 100, client_factory=_factory(ctrl)
    )
    assert row["revision"] == 1
    assert len(ctrl.calls) == 4


@pytest.mark.parametrize("action", ["short", "bad_value", "bad_echo", "500"])
def test_malformed_pages_are_discarded_and_retried(action):
    ctrl = FakeController()
    ctrl.hook = lambda params, idx: action if idx == 1 else "ok"
    row = snapshot_service.acquire(
        f"s-{action}", "buoy-A", 0, 100, client_factory=_factory(ctrl)
    )
    assert row["sha256"] == _expected_digest(row["values"])
    assert len(ctrl.calls) == 4


def test_persistent_malformed_pages_fail_as_upstream_error():
    ctrl = FakeController()
    ctrl.hook = lambda params, idx: "short"
    with pytest.raises(snapshot_service.Failure) as ei:
        snapshot_service.acquire("s5", "buoy-A", 0, 100, client_factory=_factory(ctrl))
    assert ei.value.kind == "upstream_error"
    row = storage.get("s5")
    assert row["state"] == storage.FAILED
    assert row["sha256"] is None


def test_bad_range_is_rejected_without_persistence():
    ctrl = FakeController(size=10)
    with pytest.raises(snapshot_service.Failure) as ei:
        snapshot_service.acquire("s6", "buoy-A", 0, 50, client_factory=_factory(ctrl))
    assert ei.value.kind == "bad_range"
    assert storage.get("s6") is None
    assert len(ctrl.calls) == 1  # not retried


def test_single_page_512_registers_pages_into_8_requests(monkeypatch):
    monkeypatch.setattr(config, "PAGE_SIZE", 64)
    ctrl = FakeController(size=1024)
    row = snapshot_service.acquire(
        "s7", "buoy-A", 0, 512, client_factory=_factory(ctrl)
    )
    assert len(row["values"]) == 512
    assert len(ctrl.calls) == 8
    assert all(c["count"] <= 64 for c in ctrl.calls)


def test_duplicate_concurrent_acquisitions_replay_one_canonical_row(monkeypatch):
    import threading

    monkeypatch.setattr(config, "PAGE_SIZE", 64)
    ctrl = FakeController(size=1024)
    results: list[dict] = []
    errors: list[Exception] = []

    def worker():
        try:
            results.append(
                snapshot_service.acquire(
                    "s8", "buoy-A", 0, 300, client_factory=_factory(ctrl)
                )
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 4
    digests = {r["sha256"] for r in results}
    assert digests == {_expected_digest([v & 0xFFFF for v in range(300)])}
    assert storage.get("s8")["state"] == storage.COMPLETE


def test_prior_terminal_failure_is_not_overwritten_by_a_good_read():
    ctrl = FakeController(size=256)
    # Simulate another process having persisted a terminal failure first.
    storage.save_failure("s-race", "buoy-A", 0, 100,
                         "[revision_conflict] persisted by another worker")
    with pytest.raises(snapshot_service.Failure) as ei:
        snapshot_service.acquire(
            "s-race", "buoy-A", 0, 100, client_factory=_factory(ctrl)
        )
    assert ei.value.kind == "revision_conflict"
    row = storage.get("s-race")
    assert row["state"] == storage.FAILED
    assert row["sha256"] is None


def test_exception_types_are_hierarchy():
    assert issubclass(RevisionMismatch, Exception)
    assert issubclass(ReadError, Exception)

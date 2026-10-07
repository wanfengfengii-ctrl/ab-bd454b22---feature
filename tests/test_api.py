"""API-level tests against the real simulator over real HTTP."""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import config


# ---------------------------------------------------------------- simulator

class _Server(uvicorn.Server):
    def install_signal_handlers(self):  # no signal handling in threads
        pass


@pytest.fixture(scope="module")
def simulator():
    from app import simulator as sim_mod

    config.REGISTER_SPACE = 4096
    app = sim_mod.app
    server = _Server(uvicorn.Config(app, host="127.0.0.1", port=8791,
                                    log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield "http://127.0.0.1:8791"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def client(simulator, monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tmp, "api.db"))
    monkeypatch.setattr(config, "CONTROLLER_URL", simulator)
    monkeypatch.setattr(config, "MAX_ATTEMPTS", 3)
    monkeypatch.setattr(config, "CONTROLLER_TIMEOUT", 2)
    monkeypatch.setattr(config, "FAULT_SLEEP_SECONDS", 5)

    from app import main as main_mod
    with TestClient(main_mod.app) as c:
        yield c, simulator


def _ctrl(base, path, json=None):
    with httpx.Client() as h:
        return h.post(f"{base}/control/{path}", json=json)


def _sha(values):
    return hashlib.sha256(
        b"".join(v.to_bytes(2, "big") for v in values)
    ).hexdigest()


# ---------------------------------------------------------------- tests

def test_create_snapshot_returns_evidence(client):
    c, _ = client
    r = c.post("/api/snapshots", json={
        "snapshotId": "snap-1", "deviceId": "buoy-1",
        "startAddr": 0, "count": 100,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["snapshotId"] == "snap-1"
    assert body["revision"] >= 1
    assert len(body["values"]) == 100
    assert all(0 <= v <= 0xFFFF for v in body["values"])
    assert body["sha256"] == _sha(body["values"])


def test_replay_same_params_is_idempotent(client):
    c, _ = client
    payload = {"snapshotId": "snap-2", "deviceId": "buoy-1",
               "startAddr": 5, "count": 70}
    r1 = c.post("/api/snapshots", json=payload)
    assert r1.status_code == 201
    r2 = c.post("/api/snapshots", json=payload)
    assert r2.status_code == 200
    assert r2.json() == r1.json()

    g = c.get("/api/snapshots/snap-2")
    assert g.status_code == 200
    assert g.json()["sha256"] == r1.json()["sha256"]


def test_different_params_same_id_conflict(client):
    c, _ = client
    base = {"snapshotId": "snap-3", "deviceId": "buoy-1",
            "startAddr": 0, "count": 10}
    assert c.post("/api/snapshots", json=base).status_code == 201
    for changed in (
        {"deviceId": "buoy-2"},
        {"startAddr": 1},
        {"count": 11},
    ):
        p = dict(base, **changed)
        r = c.post("/api/snapshots", json=p)
        assert r.status_code == 409, (changed, r.text)
        assert r.json()["error"] == "parameter_conflict"


def test_validation_bounds(client):
    c, _ = client
    r = c.post("/api/snapshots", json={
        "snapshotId": "x", "deviceId": "d", "startAddr": 0, "count": 0})
    assert r.status_code == 422
    r = c.post("/api/snapshots", json={
        "snapshotId": "x", "deviceId": "d", "startAddr": 0, "count": 513})
    assert r.status_code == 422
    r = c.post("/api/snapshots", json={
        "snapshotId": "x", "deviceId": "d", "startAddr": -1, "count": 1})
    assert r.status_code == 422
    assert c.get("/api/snapshots/x").status_code == 404


def test_revision_change_then_recovery_replays_single_revision(client):
    c, base = client
    _ctrl(base, "fault", {"mode": "rev_change", "n": 1, "skip": 1})
    r = c.post("/api/snapshots", json={
        "snapshotId": "snap-4", "deviceId": "buoy-4",
        "startAddr": 0, "count": 100})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sha256"] == _sha(body["values"])
    # Controller is now at revision 2 (first attempt failed on page 2).
    assert body["revision"] == 2


def test_continuous_revision_changes_give_stable_conflict(client):
    c, base = client
    # Inject a revision bump on every 2nd page forever.
    _ctrl(base, "fault", {"mode": "rev_change", "n": 100, "skip": 1})
    r = c.post("/api/snapshots", json={
        "snapshotId": "snap-5", "deviceId": "buoy-5",
        "startAddr": 0, "count": 100})
    assert r.status_code == 409
    assert r.json()["error"] == "revision_conflict"
    # No evidence may be queryable.
    assert c.get("/api/snapshots/snap-5").status_code == 404
    # Retrying after controller keeps changing -> same stable failure.
    r2 = c.post("/api/snapshots", json={
        "snapshotId": "snap-5", "deviceId": "buoy-5",
        "startAddr": 0, "count": 100})
    assert r2.status_code == 409
    assert r2.json()["error"] == "revision_conflict"
    _ctrl(base, "fault", {"mode": "none", "n": 0})


def test_transport_faults_are_retried_then_fail_stably(client):
    c, base = client
    _ctrl(base, "fault", {"mode": "short_page", "n": 100, "skip": 1})
    r = c.post("/api/snapshots", json={
        "snapshotId": "snap-6", "deviceId": "buoy-6",
        "startAddr": 0, "count": 100})
    assert r.status_code == 502
    assert r.json()["error"] == "upstream_error"
    assert c.get("/api/snapshots/snap-6").status_code == 404
    _ctrl(base, "fault", {"mode": "none", "n": 0})


def test_one_shot_fault_recovers(client):
    c, base = client
    # One bad page on the first attempt; second attempt is clean.
    _ctrl(base, "fault", {"mode": "short_page", "n": 1, "skip": 1})
    r = c.post("/api/snapshots", json={
        "snapshotId": "snap-7", "deviceId": "buoy-7",
        "startAddr": 0, "count": 100})
    assert r.status_code == 201, r.text
    assert r.json()["sha256"] == _sha(r.json()["values"])


def test_out_of_bounds_range_not_persisted(client):
    c, _ = client
    r = c.post("/api/snapshots", json={
        "snapshotId": "snap-8", "deviceId": "buoy-8",
        "startAddr": 4090, "count": 50})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_range"
    assert c.get("/api/snapshots/snap-8").status_code == 404


def test_concurrent_same_snapshotid_coalesces_to_one_evidence(client):
    c, _ = client
    payload = {"snapshotId": "snap-9", "deviceId": "buoy-9",
               "startAddr": 0, "count": 300}
    results: list[httpx.Response] = []
    errors: list[Exception] = []

    def worker():
        try:
            results.append(c.post("/api/snapshots", json=payload))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert {r.status_code for r in results} <= {200, 201}
    assert len({r.json()["sha256"] for r in results}) == 1
    assert len(results[0].json()["values"]) == 300


def test_concurrent_different_params_same_id_conflict(client):
    c, _ = client
    outcomes: list[int] = []
    lock = threading.Lock()

    def worker(payload):
        r = c.post("/api/snapshots", json=payload)
        with lock:
            outcomes.append(r.status_code)

    # A big read keeps the first request in flight; the conflicting
    # request must be rejected without waiting or producing evidence.
    t1 = threading.Thread(target=worker, kwargs={"payload": {
        "snapshotId": "snap-10", "deviceId": "buoy-10",
        "startAddr": 0, "count": 512}})
    t2 = threading.Thread(target=worker, kwargs={"payload": {
        "snapshotId": "snap-10", "deviceId": "buoy-10",
        "startAddr": 0, "count": 511}})
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join()
    t2.join()

    assert sorted(outcomes) == [201, 409]

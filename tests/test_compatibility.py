"""Unit and API tests for the snapshot compatibility verdict."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import compatibility, config, storage
from app.compatibility import Rule, RuleError


# ---------------------------------------------------------------- pure logic

def test_validate_rules_accepts_exact_tiling_in_any_order():
    rules = [
        Rule(12, 4, 0xFFFF, 0x0000),
        Rule(0, 4, 0x00FF, 0x00FF),
        Rule(8, 4, 0x0000, 0xFFFF),
        Rule(4, 4, 0x0F0F, 0x0F0F),
    ]
    ordered = compatibility.validate_rules(0, 16, rules)
    assert [(r.start, r.count) for r in ordered] == [(0, 4), (4, 4), (8, 4), (12, 4)]


def test_validate_rules_single_rule_window():
    ordered = compatibility.validate_rules(100, 1, [Rule(100, 1, 0, 0)])
    assert len(ordered) == 1


@pytest.mark.parametrize(
    "rules, fragment",
    [
        ([Rule(2, 4, 0, 0)], "gap"),                            # starts late
        ([Rule(0, 4, 0, 0), Rule(2, 4, 0, 0)], "overlaps"),     # overlap
        ([Rule(0, 4, 0, 0), Rule(6, 2, 0, 0)], "gap"),          # hole at 4..5
        ([Rule(0, 4, 0, 0), Rule(4, 5, 0, 0)], "past"),         # runs past end
        ([Rule(0, 3, 0, 0)], "end at address 3"),               # stops short
        ([Rule(0, 8, 0, 0), Rule(8, 8, 0, 0)], "past"),         # too much cover
    ],
)
def test_validate_rules_rejects_misaligned_windows(rules, fragment):
    with pytest.raises(RuleError) as ei:
        compatibility.validate_rules(0, 8, rules)
    assert fragment in str(ei.value)


def test_validate_rules_rejects_rule_starting_before_window():
    with pytest.raises(RuleError) as ei:
        compatibility.validate_rules(4, 4, [Rule(2, 6, 0, 0)])
    assert "before the checked window" in str(ei.value)


def test_validate_rules_enforces_segment_budget():
    with pytest.raises(RuleError):
        compatibility.validate_rules(0, 1, [])
    with pytest.raises(RuleError):
        compatibility.validate_rules(
            0, 65, [Rule(i, 1, 0, 0) for i in range(65)]
        )


def test_compare_window_reports_actual_and_unauthorised_masks():
    # Window [10, 14); rule A allows only setting 0x00FF, rule B allows
    # only clearing 0xFF00.
    rules = compatibility.validate_rules(10, 4, [
        Rule(10, 2, 0x00FF, 0x0000),
        Rule(12, 2, 0x0000, 0xFF00),
    ])
    baseline = [0x00F0, 0x0FF0, 0xFF00, 0x1234]
    candidate = [0x00FF, 0x0F00, 0x0F00, 0x1234]
    compatible, changes = compatibility.compare_window(
        baseline, 10, candidate, 10, 10, 4, rules
    )
    assert compatible is False
    assert [c["addr"] for c in changes] == [10, 11, 12]

    ch = changes[0]  # set 0x000F, allowed by rule A
    assert (ch["setMask"], ch["clearMask"]) == (0x000F, 0x0000)
    assert (ch["unauthorizedSet"], ch["unauthorizedClear"]) == (0, 0)

    ch = changes[1]  # cleared 0x00F0: rule A allows no clearing at all
    assert (ch["setMask"], ch["clearMask"]) == (0x0000, 0x00F0)
    assert (ch["unauthorizedSet"], ch["unauthorizedClear"]) == (0, 0x00F0)

    ch = changes[2]  # cleared 0xF000, allowed by rule B
    assert (ch["setMask"], ch["clearMask"]) == (0x0000, 0xF000)
    assert (ch["unauthorizedSet"], ch["unauthorizedClear"]) == (0, 0)


def test_compare_window_uses_absolute_addresses_across_snapshot_offsets():
    # Baseline covers [0, 100), candidate covers [40, 60); window [45, 50).
    baseline = list(range(100))
    candidate = list(range(40, 60))
    candidate[48 - 40] = baseline[48] ^ 0x8000  # flip top bit at addr 48
    rules = compatibility.validate_rules(45, 5, [Rule(45, 5, 0x0000, 0x0000)])
    compatible, changes = compatibility.compare_window(
        baseline, 0, candidate, 40, 45, 5, rules
    )
    assert compatible is False
    assert len(changes) == 1
    ch = changes[0]
    assert ch["addr"] == 48
    assert ch["baseline"] == 48
    assert ch["candidate"] == 48 ^ 0x8000
    assert ch["setMask"] == 0x8000
    assert ch["unauthorizedSet"] == 0x8000


def test_compare_window_identical_snapshots_are_compatible():
    values = [0xABCD] * 8
    rules = compatibility.validate_rules(0, 8, [Rule(0, 8, 0, 0)])
    compatible, changes = compatibility.compare_window(
        values, 0, list(values), 0, 0, 8, rules
    )
    assert compatible is True
    assert changes == []


# ---------------------------------------------------------------- API layer

@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tmp, "compat.db"))
    from app import main as main_mod
    with TestClient(main_mod.app) as c:
        yield c


def _seed(snapshot_id, device_id, start_addr, values):
    import hashlib
    digest = hashlib.sha256(
        b"".join(v.to_bytes(2, "big") for v in values)
    ).hexdigest()
    outcome, _ = storage.save_complete(
        snapshot_id, device_id, start_addr, len(values), 1, values, digest
    )
    assert outcome == "created"


def _seed_pair(device_id="buoy-fw"):
    # Addresses 0..15, all zero in the baseline.
    _seed("fw-base", device_id, 0, [0x0000] * 16)
    cand = [0x0000] * 16
    cand[1] = 0x00FF            # set low byte
    cand[2] = 0x0F00            # set bits above the low byte
    cand[3] = 0xFFFF            # set everything
    _seed("fw-cand", device_id, 0, cand)


def _payload(**overrides):
    body = {
        "baselineId": "fw-base",
        "candidateId": "fw-cand",
        "startAddr": 0,
        "count": 16,
        "rules": [
            {"startAddr": 0, "count": 8,
             "allowSet": 0x00FF, "allowClear": 0x0000},
            {"startAddr": 8, "count": 8,
             "allowSet": 0xFFFF, "allowClear": 0xFFFF},
        ],
    }
    body.update(overrides)
    return body


def test_compatibility_verdict_locates_every_violating_bit(client):
    _seed_pair()
    r = client.post("/api/snapshots/compatibility", json=_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deviceId"] == "buoy-fw"
    assert body["compatible"] is False
    assert body["changeCount"] == 3
    assert body["violationCount"] == 2
    assert [c["addr"] for c in body["changes"]] == [1, 2, 3]

    ch = body["changes"][0]  # addr 1: set 0x00FF, allowed
    assert ch["setMask"] == 0x00FF and ch["clearMask"] == 0
    assert ch["unauthorizedSet"] == 0 and ch["unauthorizedClear"] == 0

    ch = body["changes"][1]  # addr 2: set 0x0F00, only 0x00FF allowed
    assert ch["setMask"] == 0x0F00
    assert ch["unauthorizedSet"] == 0x0F00

    ch = body["changes"][2]  # addr 3: set 0xFFFF, only low byte allowed
    assert ch["setMask"] == 0xFFFF
    assert ch["unauthorizedSet"] == 0xFF00


def test_compatibility_all_changes_allowed(client):
    _seed_pair()
    r = client.post("/api/snapshots/compatibility", json=_payload(
        rules=[{"startAddr": 0, "count": 16,
                "allowSet": 0xFFFF, "allowClear": 0xFFFF}],
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["compatible"] is True
    assert body["changeCount"] == 3
    assert body["violationCount"] == 0


def test_compatibility_clear_direction_violation(client):
    _seed("fw-base2", "buoy-fw", 0, [0xFFFF] * 4)
    _seed("fw-cand2", "buoy-fw", 0, [0x0F0F, 0xFFFF, 0xFFFF, 0xFFFF])
    r = client.post("/api/snapshots/compatibility", json={
        "baselineId": "fw-base2", "candidateId": "fw-cand2",
        "startAddr": 0, "count": 4,
        "rules": [{"startAddr": 0, "count": 4,
                   "allowSet": 0x0000, "allowClear": 0x00FF}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["compatible"] is False
    (ch,) = body["changes"]
    assert ch["addr"] == 0
    assert ch["clearMask"] == 0xF0F0
    assert ch["unauthorizedClear"] == 0xF0F0 & ~0x00FF


def test_compatibility_no_changes_is_compatible(client):
    _seed("fw-same", "buoy-fw", 0, [0x1234] * 4)
    r = client.post("/api/snapshots/compatibility", json={
        "baselineId": "fw-same", "candidateId": "fw-same",
        "startAddr": 0, "count": 4,
        "rules": [{"startAddr": 0, "count": 4,
                   "allowSet": 0, "allowClear": 0}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["compatible"] is True
    assert body["changes"] == []


def test_compatibility_sub_window_uses_absolute_addresses(client):
    _seed("fw-wide", "buoy-fw", 0, list(range(32)))
    changed = list(range(32))
    changed[20] ^= 0x0001
    _seed("fw-wide2", "buoy-fw", 8, changed[8:24])
    r = client.post("/api/snapshots/compatibility", json={
        "baselineId": "fw-wide", "candidateId": "fw-wide2",
        "startAddr": 16, "count": 8,
        "rules": [{"startAddr": 16, "count": 8,
                   "allowSet": 0x0001, "allowClear": 0x0000}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["compatible"] is True
    assert [c["addr"] for c in body["changes"]] == [20]
    assert body["changes"][0]["setMask"] == 0x0001


# ------------------------------------------------------ stable failures

@pytest.mark.parametrize("missing", ["baselineId", "candidateId"])
def test_compatibility_missing_snapshot_is_404(client, missing):
    _seed("fw-only", "buoy-fw", 0, [0] * 8)
    ids = {"baselineId": "fw-only", "candidateId": "fw-only"}
    ids[missing] = "fw-ghost"
    r = client.post("/api/snapshots/compatibility", json=_payload(
        count=8,
        rules=[{"startAddr": 0, "count": 8,
                "allowSet": 0, "allowClear": 0}],
        **ids,
    ))
    assert r.status_code == 404
    assert r.json()["error"] == "snapshot_not_found"
    assert missing.split("I")[0] in r.json()["reason"]
    assert "compatible" not in r.json()  # no partial verdict


def test_compatibility_failed_snapshot_is_not_evidence(client):
    _seed("fw-ok", "buoy-fw", 0, [0] * 8)
    outcome, _ = storage.save_failure(
        "fw-failed", "buoy-fw", 0, 8, "[revision_conflict] never stabilised"
    )
    assert outcome == "failed"
    r = client.post("/api/snapshots/compatibility", json=_payload(
        baselineId="fw-ok", candidateId="fw-failed", count=8,
        rules=[{"startAddr": 0, "count": 8,
                "allowSet": 0, "allowClear": 0}],
    ))
    assert r.status_code == 404
    assert r.json()["error"] == "snapshot_not_found"


def test_compatibility_device_mismatch_is_409(client):
    _seed("fw-a", "buoy-a", 0, [0] * 8)
    _seed("fw-b", "buoy-b", 0, [0] * 8)
    r = client.post("/api/snapshots/compatibility", json=_payload(
        baselineId="fw-a", candidateId="fw-b", count=8,
        rules=[{"startAddr": 0, "count": 8,
                "allowSet": 0, "allowClear": 0}],
    ))
    assert r.status_code == 409
    assert r.json()["error"] == "device_mismatch"
    assert "compatible" not in r.json()


@pytest.mark.parametrize(
    # base_span/cand_span are (start, size); the window only escapes one.
    "base_span, cand_span, start, count, label",
    [
        ((8, 8), (0, 32), 0, 8, "baseline"),   # baseline misses addresses 0..7
        ((0, 32), (8, 8), 8, 24, "candidate"),  # candidate ends at 16
    ],
)
def test_compatibility_insufficient_coverage_is_409(
    client, base_span, cand_span, start, count, label
):
    _seed("fw-narrow-base", "buoy-fw", base_span[0], [0] * base_span[1])
    _seed("fw-narrow-cand", "buoy-fw", cand_span[0], [0] * cand_span[1])
    r = client.post("/api/snapshots/compatibility", json={
        "baselineId": "fw-narrow-base", "candidateId": "fw-narrow-cand",
        "startAddr": start, "count": count,
        "rules": [{"startAddr": start, "count": count,
                   "allowSet": 0, "allowClear": 0}],
    })
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "range_not_covered"
    assert label in body["reason"]
    assert "compatible" not in body


@pytest.mark.parametrize(
    "rules",
    [
        # gap at address 4
        [{"startAddr": 0, "count": 4, "allowSet": 0, "allowClear": 0},
         {"startAddr": 5, "count": 3, "allowSet": 0, "allowClear": 0}],
        # overlap at addresses 2..3
        [{"startAddr": 0, "count": 4, "allowSet": 0, "allowClear": 0},
         {"startAddr": 2, "count": 6, "allowSet": 0, "allowClear": 0}],
        # covers only [0, 7) of the [0, 8) window
        [{"startAddr": 0, "count": 7, "allowSet": 0, "allowClear": 0}],
        # extends past the window end
        [{"startAddr": 0, "count": 9, "allowSet": 0, "allowClear": 0}],
    ],
)
def test_compatibility_misaligned_rule_window_is_400(client, rules):
    _seed_pair()
    r = client.post("/api/snapshots/compatibility", json=_payload(
        count=8, rules=rules,
    ))
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_rules"
    assert "compatible" not in r.json()


def test_compatibility_schema_bounds_are_422(client):
    _seed_pair()
    # zero rules / 65 rules / mask wider than 16 bits / zero count
    r = client.post("/api/snapshots/compatibility", json=_payload(rules=[]))
    assert r.status_code == 422
    r = client.post("/api/snapshots/compatibility", json=_payload(
        count=65,
        rules=[{"startAddr": i, "count": 1, "allowSet": 0, "allowClear": 0}
               for i in range(65)],
    ))
    assert r.status_code == 422
    r = client.post("/api/snapshots/compatibility", json=_payload(
        rules=[{"startAddr": 0, "count": 16,
                "allowSet": 0x10000, "allowClear": 0}],
    ))
    assert r.status_code == 422
    r = client.post("/api/snapshots/compatibility", json=_payload(count=0))
    assert r.status_code == 422


def test_compatibility_failure_reasons_are_stable(client):
    _seed_pair()
    payload = _payload(count=8, rules=[
        {"startAddr": 0, "count": 4, "allowSet": 0, "allowClear": 0},
        {"startAddr": 6, "count": 2, "allowSet": 0, "allowClear": 0},
    ])
    r1 = client.post("/api/snapshots/compatibility", json=payload)
    r2 = client.post("/api/snapshots/compatibility", json=payload)
    assert r1.status_code == 400 == r2.status_code
    assert r1.json() == r2.json()

"""Unit tests for the bit-direction compatibility engine."""
from __future__ import annotations

import pytest

from app import compatibility as compat


def _snap(sid, device, start, values, revision=1):
    return {
        "snapshot_id": sid,
        "device_id": device,
        "start_addr": start,
        "count": len(values),
        "revision": revision,
        "values": values,
        "sha256": "x",
    }


def _rule(s, e, set_allowed=0xFFFF, clear_allowed=0xFFFF):
    return {"startAddr": s, "endAddr": e,
            "setAllowed": set_allowed, "clearAllowed": clear_allowed}


def _check(baseline, candidate, start, count, rules):
    return compat.check_compatibility(baseline, candidate, start, count, rules)


# ------------------------------------------------------------- happy paths

def test_no_changes_is_compatible():
    v = [0x0000, 0xFFFF, 0x1234]
    verdict = _check(_snap("b", "d", 0, v), _snap("c", "d", 0, v), 0, 3,
                     [_rule(0, 2)])
    assert verdict["compatible"] is True
    assert verdict["changes"] == []
    assert verdict["window"] == {"startAddr": 0, "endAddr": 2, "count": 3}


def test_approved_set_and_clear_are_compatible():
    # addr0: bit0 0->1 permitted by setAllowed; addr1: bit0 1->0 permitted.
    baseline = _snap("b", "d", 0, [0x0000, 0x0001])
    candidate = _snap("c", "d", 0, [0x0001, 0x0000])
    verdict = _check(
        baseline, candidate, 0, 2,
        [{"startAddr": 0, "endAddr": 1,
          "setAllowed": 0x0001, "clearAllowed": 0x0001}],
    )
    assert verdict["compatible"] is True
    assert len(verdict["changes"]) == 2
    assert verdict["changes"][0]["setMask"] == "0x0001"
    assert verdict["changes"][0]["clearMask"] == "0x0000"
    assert verdict["changes"][1]["setMask"] == "0x0000"
    assert verdict["changes"][1]["clearMask"] == "0x0001"


def test_forbidden_set_is_pinpointed_amid_allowed_value_change():
    # Ordinary numeric difference (allowed bit) must not mask the forbidden
    # bit: addr0 flips allowed bit4 and forbidden bit0 at once.
    baseline = _snap("b", "d", 0, [0x0000])
    candidate = _snap("c", "d", 0, [0x0011])
    verdict = _check(
        baseline, candidate, 0, 1,
        [_rule(0, 0, set_allowed=0x0010, clear_allowed=0x0000)],
    )
    assert verdict["compatible"] is False
    change = verdict["changes"][0]
    assert change["addr"] == 0
    assert change["setMask"] == "0x0011"
    assert change["unauthorizedSet"] == "0x0001"
    assert change["violations"] == "0x0001"


def test_forbidden_clear_is_pinpointed():
    baseline = _snap("b", "d", 5, [0x0003])
    candidate = _snap("c", "d", 5, [0x0001])
    verdict = _check(
        baseline, candidate, 5, 1,
        [_rule(5, 5, set_allowed=0x0000, clear_allowed=0x0001)],
    )
    assert verdict["compatible"] is False
    change = verdict["changes"][0]
    assert change["clearMask"] == "0x0002"
    assert change["unauthorizedClear"] == "0x0002"
    assert int(change["violations"], 16) == 0x0002


def test_changes_are_returned_in_absolute_address_order():
    baseline = _snap("b", "d", 0, [0x0000, 0x0000, 0x0000])
    candidate = _snap("c", "d", 0, [0x0001, 0x0001, 0x0001])
    verdict = _check(baseline, candidate, 0, 3,
                     [_rule(0, 0, set_allowed=0), _rule(1, 1, set_allowed=0),
                      _rule(2, 2, set_allowed=0)])
    assert [c["addr"] for c in verdict["changes"]] == [0, 1, 2]


def test_snapshot_windows_may_span_different_ranges_than_check_window():
    # Both snapshots cover 10..19; the check only inspects 12..14.
    baseline = _snap("b", "d", 10, [0x0000] * 10)
    candidate = _snap("c", "d", 10, [0x0000, 0x0000, 0x000F] + [0x0000] * 7)
    verdict = _check(
        baseline, candidate, 12, 3,
        [_rule(12, 12, set_allowed=0x000F),
         _rule(13, 14, set_allowed=0x0000)],
    )
    assert verdict["compatible"] is True
    assert [c["addr"] for c in verdict["changes"]] == [12]


def test_snapshots_need_not_share_start_addr_as_long_as_window_covered():
    baseline = _snap("b", "d", 0, [0x0000, 0x0000, 0x0000])
    candidate = _snap("c", "d", 1, [0x000F, 0x0000])
    verdict = _check(baseline, candidate, 1, 2,
                     [_rule(1, 2, set_allowed=0x000F)])
    assert verdict["compatible"] is True


def test_hex_string_masks_are_accepted():
    baseline = _snap("b", "d", 0, [0x0000])
    candidate = _snap("c", "d", 0, [0x8000])
    verdict = _check(
        baseline, candidate, 0, 1,
        [{"startAddr": 0, "endAddr": 0,
          "setAllowed": "0x8000", "clearAllowed": "0x0000"}],
    )
    assert verdict["compatible"] is True


def test_multiple_violations_aggregate_and_overall_verdict_is_false():
    baseline = _snap("b", "d", 0, [0x0000, 0x0000, 0x0000])
    candidate = _snap("c", "d", 0, [0x0001, 0x0000, 0x0001])
    verdict = _check(
        baseline, candidate, 0, 3,
        [_rule(0, 0, set_allowed=0x0001),
         _rule(1, 1, set_allowed=0x0001),
         _rule(2, 2, set_allowed=0x0000)],
    )
    assert verdict["compatible"] is False
    bad = [c for c in verdict["changes"] if int(c["violations"], 16)]
    assert [c["addr"] for c in bad] == [2]


# ------------------------------------------------------------- rule tiling

def test_rules_must_tile_the_window_exactly():
    s = _snap("b", "d", 0, [0] * 4)
    with pytest.raises(compat.CompatibilityError) as ei:
        _check(s, s, 0, 4, [_rule(0, 2)])
    assert ei.value.kind == "invalid_rule"

    with pytest.raises(compat.CompatibilityError) as ei:
        _check(s, s, 0, 4, [_rule(0, 4)])
    assert ei.value.kind == "rule_outside_window"

    with pytest.raises(compat.CompatibilityError) as ei:
        _check(s, s, 0, 4, [_rule(0, 1), _rule(1, 3)])
    assert ei.value.kind == "invalid_rule"
    assert "overlap" in ei.value.reason


def test_rules_are_sorted_before_tiling_check():
    s = _snap("b", "d", 0, [0] * 2)
    verdict = _check(s, s, 0, 2, [_rule(1, 1), _rule(0, 0)])
    assert verdict["rules"][0]["startAddr"] == 0


def test_rule_count_bounds():
    s = _snap("b", "d", 0, [0])
    with pytest.raises(compat.CompatibilityError) as ei:
        _check(s, s, 0, 1, [])
    assert ei.value.kind == "invalid_rule"
    big = _snap("b", "d", 0, [0] * 65)
    rules = [_rule(i, i) for i in range(65)]
    with pytest.raises(compat.CompatibilityError) as ei:
        _check(big, big, 0, 65, rules)
    assert ei.value.kind == "invalid_rule"


def test_invalid_masks_are_rejected():
    s = _snap("b", "d", 0, [0])
    for bad in (0x10000, -1, "0xZZZZ", True, None, "0x"):
        with pytest.raises(compat.CompatibilityError) as ei:
            _check(s, s, 0, 1,
                   [{"startAddr": 0, "endAddr": 0,
                     "setAllowed": bad, "clearAllowed": 0}])
        assert ei.value.kind == "invalid_rule", bad


def test_inverted_rule_range_rejected():
    s = _snap("b", "d", 0, [0] * 2)
    with pytest.raises(compat.CompatibilityError):
        _check(s, s, 0, 2, [_rule(1, 0)])


# ------------------------------------------------------------- preconditions

def test_insufficient_coverage_is_rejected_without_partial_verdict():
    baseline = _snap("b", "d", 0, [0] * 4)
    candidate = _snap("c", "d", 0, [0] * 2)  # only covers 0..1
    with pytest.raises(compat.CompatibilityError) as ei:
        _check(baseline, candidate, 0, 4, [_rule(0, 3)])
    assert ei.value.kind == "insufficient_coverage"
    assert "2" in ei.value.reason


def test_window_crossing_16_bit_space_boundary_rejected():
    s = _snap("b", "d", 0xFFFE, [0, 0, 0])
    with pytest.raises(compat.CompatibilityError) as ei:
        _check(s, s, 0xFFFE, 3, [_rule(0xFFFE, 0x10000)])
    assert ei.value.kind == "bad_window"


def test_device_mismatch_rejected():
    baseline = _snap("b", "d1", 0, [0])
    candidate = _snap("c", "d2", 0, [0])
    with pytest.raises(compat.CompatibilityError) as ei:
        compat.require_same_device_snapshots(baseline, candidate, "b", "c")
    assert ei.value.kind == "device_mismatch"


def test_missing_snapshot_rejected():
    candidate = _snap("c", "d", 0, [0])
    with pytest.raises(compat.CompatibilityError) as ei:
        compat.require_same_device_snapshots(None, candidate, "b", "c")
    assert ei.value.kind == "snapshot_not_found"
    with pytest.raises(compat.CompatibilityError) as ei:
        compat.require_same_device_snapshots(candidate, None, "c", "x")
    assert ei.value.kind == "snapshot_not_found"

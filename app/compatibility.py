"""Register-direction compatibility checking between two sealed snapshots.

A firmware upgrade may only move register bits in pre-approved directions:
for every register address covered by the check window the operator supplies
a pair of 16-bit masks:

* ``setAllowed``   - bits that may go 0 -> 1 (baseline -> candidate);
* ``clearAllowed`` - bits that may go 1 -> 0.

A bit is *unauthorized* when it actually changes in a direction its mask
does not permit. Bits covered by both masks may move either way; a bit that
is 1 in both snapshots never moves and is never flagged, regardless of the
masks. Ordinary value differences that only flip approved bits must never
mask a forbidden set/clear on another bit: every changed register is
reported with its actual set/clear masks and the exact offending bits.

The mask rules form a tiling of the checked window: 1..64 non-overlapping
rules, whose ``[startAddr, endAddr]`` intervals exactly cover
``[startAddr, startAddr + count)``. Validation is all-or-nothing - any
illegal request raises :class:`CompatibilityError` *before* any verdict is
produced, so a partial decision can never be returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

U16_MAX = 0xFFFF
MAX_RULES = 64


class CompatibilityError(Exception):
    """A stable, client-facing rejection reason (never a partial verdict)."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True)
class MaskRule:
    """One tiling segment: inclusive address range + the two direction masks."""

    start_addr: int
    end_addr: int  # inclusive
    set_allowed: int
    clear_allowed: int

    @property
    def count(self) -> int:
        return self.end_addr - self.start_addr + 1

    def to_dict(self) -> dict[str, int]:
        return {
            "startAddr": self.start_addr,
            "endAddr": self.end_addr,
            "setAllowed": f"0x{self.set_allowed:04X}",
            "clearAllowed": f"0x{self.clear_allowed:04X}",
        }


def normalize_mask(value: Any, field: str, start_addr: int, end_addr: int) -> int:
    """Coerce a JSON number or ``0x``-prefixed hex string mask to an int."""
    if isinstance(value, bool):
        raise CompatibilityError(
            "invalid_rule",
            f"mask {field} of rule [{start_addr},{end_addr}] must be a 16-bit "
            "unsigned integer, not a boolean",
        )
    if isinstance(value, int):
        masked = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            masked = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
        except ValueError:
            raise CompatibilityError(
                "invalid_rule",
                f"mask {field} of rule [{start_addr},{end_addr}] is not a valid "
                f"16-bit mask: {value!r}",
            ) from None
    else:
        raise CompatibilityError(
            "invalid_rule",
            f"mask {field} of rule [{start_addr},{end_addr}] must be a 16-bit "
            "unsigned integer or hex string",
        )
    if not 0 <= masked <= U16_MAX:
        raise CompatibilityError(
            "invalid_rule",
            f"mask {field} of rule [{start_addr},{end_addr}] is outside the "
            "16-bit register range: "
            + (value if isinstance(value, str) else str(masked)),
        )
    return masked


def validate_rules(
    rules: Sequence[MaskRule], window_start: int, window_count: int
) -> list[MaskRule]:
    """Validate the tiling and return the rules in ascending address order.

    The rules must be non-empty (1..64), individually valid, mutually
    non-overlapping and together exactly cover the checked window.
    """
    if not 1 <= len(rules) <= MAX_RULES:
        raise CompatibilityError(
            "invalid_rule",
            f"between 1 and {MAX_RULES} mask rules are required, got {len(rules)}",
        )

    ordered = sorted(rules, key=lambda r: (r.start_addr, r.end_addr))
    window_end = window_start + window_count - 1

    for rule in ordered:
        if rule.start_addr < 0 or rule.end_addr < 0 or rule.end_addr < rule.start_addr:
            raise CompatibilityError(
                "invalid_rule",
                f"rule range [{rule.start_addr},{rule.end_addr}] is invalid: "
                "addresses must be non-negative with endAddr >= startAddr",
            )
        if rule.start_addr > U16_MAX or rule.end_addr > U16_MAX:
            raise CompatibilityError(
                "invalid_rule",
                f"rule range [{rule.start_addr},{rule.end_addr}] exceeds the "
                "16-bit address space",
            )
        if not (window_start <= rule.start_addr and rule.end_addr <= window_end):
            raise CompatibilityError(
                "rule_outside_window",
                f"rule [{rule.start_addr},{rule.end_addr}] does not lie within "
                f"the checked window [{window_start},{window_end}]",
            )

    if ordered[0].start_addr != window_start:
        raise CompatibilityError(
            "invalid_rule",
            "mask rules do not exactly cover the window: first rule starts at "
            f"{ordered[0].start_addr}, expected {window_start}",
        )
    cursor = window_start
    for rule in ordered:
        if rule.start_addr < cursor:
            other = next(
                r for r in ordered
                if r is not rule
                and r.start_addr <= rule.start_addr <= r.end_addr
            )
            raise CompatibilityError(
                "invalid_rule",
                f"mask rules overlap at address {rule.start_addr}: ranges "
                f"[{other.start_addr},{other.end_addr}] and "
                f"[{rule.start_addr},{rule.end_addr}]",
            )
        if rule.start_addr > cursor:
            raise CompatibilityError(
                "invalid_rule",
                "mask rules do not exactly cover the window: address "
                f"{cursor} is not covered by any rule",
            )
        cursor = rule.end_addr + 1
    if cursor != window_end + 1:
        raise CompatibilityError(
            "invalid_rule",
            "mask rules do not exactly cover the window: coverage ends at "
            f"{cursor - 1}, expected {window_end}",
        )
    return ordered


def _index_map(row: dict[str, Any]) -> dict[int, int]:
    """Map every absolute address of a snapshot to its register value."""
    base = int(row["start_addr"])
    values = row["values"]
    return {base + i: int(values[i]) for i in range(len(values))}


def check_compatibility(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    start_addr: int,
    count: int,
    raw_rules: Sequence[Any],
) -> dict[str, Any]:
    """Compare two complete snapshots under the supplied direction policy.

    Raises :class:`CompatibilityError` for any illegal precondition; the
    caller performs existence/device checks beforehand with
    :func:`require_same_device_snapshots`.
    """
    if count < 1 or start_addr < 0 or start_addr > U16_MAX:
        raise CompatibilityError(
            "bad_window",
            f"invalid checked window: startAddr={start_addr}, count={count}",
        )
    if start_addr + count - 1 > U16_MAX:
        raise CompatibilityError(
            "bad_window",
            f"checked window [{start_addr},{start_addr + count - 1}] exceeds the "
            "16-bit address space",
        )

    rules: list[MaskRule] = []
    for raw in raw_rules:
        rstart = raw["startAddr"]
        rend = raw["endAddr"]
        rules.append(
            MaskRule(
                rstart,
                rend,
                normalize_mask(raw.get("setAllowed"), "setAllowed", rstart, rend),
                normalize_mask(
                    raw.get("clearAllowed"), "clearAllowed", rstart, rend
                ),
            )
        )
    ordered = validate_rules(rules, start_addr, count)

    base_map = _index_map(baseline)
    cand_map = _index_map(candidate)
    window_end = start_addr + count - 1
    missing = sorted(
        a for a in range(start_addr, window_end + 1)
        if a not in base_map or a not in cand_map
    )
    if missing:
        raise CompatibilityError(
            "insufficient_coverage",
            "snapshots do not fully cover the checked window "
            f"[{start_addr},{window_end}]; first uncovered address: {missing[0]}",
        )

    changes: list[dict[str, Any]] = []
    rule_by_addr: dict[int, MaskRule] = {}
    for rule in ordered:
        for addr in range(rule.start_addr, rule.end_addr + 1):
            rule_by_addr[addr] = rule

    for addr in range(start_addr, window_end + 1):
        old = base_map[addr]
        new = cand_map[addr]
        set_mask = (~old) & new & U16_MAX
        clear_mask = old & (~new) & U16_MAX
        if set_mask == 0 and clear_mask == 0:
            continue
        rule = rule_by_addr[addr]
        unauthorized_set = set_mask & ~rule.set_allowed & U16_MAX
        unauthorized_clear = clear_mask & ~rule.clear_allowed & U16_MAX
        violations = unauthorized_set | unauthorized_clear
        changes.append({
            "addr": addr,
            "baseline": f"0x{old:04X}",
            "candidate": f"0x{new:04X}",
            "setMask": f"0x{set_mask:04X}",
            "clearMask": f"0x{clear_mask:04X}",
            "unauthorizedSet": f"0x{unauthorized_set:04X}",
            "unauthorizedClear": f"0x{unauthorized_clear:04X}",
            "violations": f"0x{violations:04X}",
            "rule": rule.to_dict(),
        })

    return {
        "baselineSnapshotId": baseline["snapshot_id"],
        "candidateSnapshotId": candidate["snapshot_id"],
        "deviceId": baseline["device_id"],
        "revision": {"baseline": baseline["revision"], "candidate": candidate["revision"]},
        "window": {"startAddr": start_addr, "endAddr": window_end, "count": count},
        "rules": [rule.to_dict() for rule in ordered],
        "changes": changes,
        "compatible": not changes
        or all(int(c["violations"], 16) == 0 for c in changes),
    }


def require_same_device_snapshots(
    baseline: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    baseline_id: str,
    candidate_id: str,
) -> None:
    """Reject missing evidence and cross-device comparisons (no verdict)."""
    if baseline is None:
        raise CompatibilityError(
            "snapshot_not_found",
            f"baseline snapshot {baseline_id!r} does not exist or is not complete",
        )
    if candidate is None:
        raise CompatibilityError(
            "snapshot_not_found",
            f"candidate snapshot {candidate_id!r} does not exist or is not complete",
        )
    if baseline["device_id"] != candidate["device_id"]:
        raise CompatibilityError(
            "device_mismatch",
            "snapshots belong to different devices: "
            f"baseline {baseline['snapshot_id']!r} -> {baseline['device_id']!r}, "
            f"candidate {candidate['snapshot_id']!r} -> {candidate['device_id']!r}",
        )

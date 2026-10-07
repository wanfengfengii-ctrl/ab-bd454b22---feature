"""Firmware-upgrade compatibility check between two sealed snapshots.

Maintenance staff seal a *baseline* snapshot before a firmware upgrade
and a *candidate* snapshot afterwards. This module decides whether every
register change inside a checked absolute-address window was sanctioned
by the upgrade policy:

* The window ``[startAddr, startAddr + count)`` must be tiled by 1..64
  mask rules with **no gaps and no overlaps** (:func:`validate_rules`).
* For each changed register the *actual* set mask (``candidate & ~baseline``)
  and clear mask (``baseline & ~candidate``) are computed. Bits set
  outside the rule's ``allowSet`` mask - or cleared outside its
  ``allowClear`` mask - are *unauthorised* and fail the verdict.

The functions here are pure: the API layer loads the snapshots, checks
device/coverage preconditions and never emits a partial verdict.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

U16_MAX = 0xFFFF

# Policy limits mirrored by the API schema.
MIN_RULES = 1
MAX_RULES = 64


@dataclass
class Rule:
    """One mask rule covering ``[start, start + count)``."""

    start: int
    count: int
    allow_set: int
    allow_clear: int

    @property
    def end(self) -> int:
        return self.start + self.count


class RuleError(Exception):
    """The rule set does not tile the checked window exactly."""


def validate_rules(start: int, count: int, rules: list[Rule]) -> list[Rule]:
    """Return the rules sorted by address, or raise :class:`RuleError`.

    A legal rule set tiles ``[start, start + count)`` contiguously: the
    first rule starts exactly at ``start``, every following rule starts
    where the previous one ended, and the last rule ends exactly at
    ``start + count``. Rules may be supplied in any order.
    """
    if not MIN_RULES <= len(rules) <= MAX_RULES:
        raise RuleError(
            f"need {MIN_RULES}..{MAX_RULES} mask rules, got {len(rules)}"
        )
    end = start + count
    ordered = sorted(rules, key=lambda r: (r.start, r.count))
    cursor = start
    for rule in ordered:
        if rule.start < cursor:
            if cursor == start:
                raise RuleError(
                    f"rule starts at address {rule.start}, before the "
                    f"checked window start {start}"
                )
            raise RuleError(
                f"rule at address {rule.start} overlaps the previous rule "
                f"(addresses below {cursor} are already covered)"
            )
        if rule.start > cursor:
            raise RuleError(
                f"rules leave a gap: address {cursor} is not covered "
                f"by any rule"
            )
        cursor = rule.end
        if cursor > end:
            raise RuleError(
                f"rule at address {rule.start} ends at {cursor}, past the "
                f"checked window end {end}"
            )
    if cursor != end:
        raise RuleError(
            f"rules end at address {cursor} but the checked window "
            f"ends at {end}"
        )
    return ordered


def compare_window(
    baseline_values: list[int],
    baseline_start: int,
    candidate_values: list[int],
    candidate_start: int,
    start: int,
    count: int,
    rules: list[Rule],
) -> tuple[bool, list[dict]]:
    """Compare one window of two snapshots.

    ``baseline_values``/``candidate_values`` are the u16 arrays stored in
    each snapshot, indexed from ``baseline_start``/``candidate_start``.
    ``rules`` must already be validated for ``[start, start + count)``.

    Returns ``(compatible, changes)`` where ``changes`` lists one entry
    per register whose value differs, ordered by absolute address. Each
    entry carries the actual set/clear masks and the unauthorised bits
    under the rule covering that address.
    """
    starts = [r.start for r in rules]
    changes: list[dict] = []
    compatible = True
    for offset in range(count):
        addr = start + offset
        base = baseline_values[addr - baseline_start]
        cand = candidate_values[addr - candidate_start]
        if base == cand:
            continue
        # The rules tile the window, so exactly one covers this address.
        rule = rules[bisect.bisect_right(starts, addr) - 1]
        set_mask = cand & ~base  # bits that went 0 -> 1
        clear_mask = base & ~cand  # bits that went 1 -> 0
        unauthorized_set = set_mask & ~rule.allow_set
        unauthorized_clear = clear_mask & ~rule.allow_clear
        if unauthorized_set or unauthorized_clear:
            compatible = False
        changes.append(
            {
                "addr": addr,
                "baseline": base,
                "candidate": cand,
                "setMask": set_mask,
                "clearMask": clear_mask,
                "unauthorizedSet": unauthorized_set,
                "unauthorizedClear": unauthorized_clear,
            }
        )
    return compatible, changes

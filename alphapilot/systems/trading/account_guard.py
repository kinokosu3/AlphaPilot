"""Dedicated-account ownership checks used before automated planning/routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from alphapilot.systems.trading.account_identity import account_identities_match
from alphapilot.systems.trading.contracts import AccountSnapshot, canonical_instrument


@dataclass(frozen=True)
class AccountBoundaryResult:
    ok: bool
    issues: tuple[dict[str, object], ...] = ()


class AccountBoundaryGuard:
    def validate(
        self,
        snapshot: AccountSnapshot,
        *,
        universe: Sequence[str],
        expected_account_id: str = "",
        expected_positions: Mapping[str, float] | None = None,
        allow_position_changes: bool = True,
    ) -> AccountBoundaryResult:
        issues: list[dict[str, object]] = []
        allowed = {canonical_instrument(item) for item in universe}
        if expected_account_id and not account_identities_match(
            expected_account_id, snapshot.account_id,
        ):
            issues.append({
                "rule": "account_binding",
                "reason": "account snapshot does not match deployment binding",
            })
        outside = sorted(
            canonical_instrument(key)
            for key, volume in snapshot.positions.items()
            if float(volume) != 0 and canonical_instrument(key) not in allowed
        )
        if outside:
            issues.append({
                "rule": "outside_universe_positions",
                "reason": "dedicated account contains holdings outside the instance universe",
                "instruments": outside,
            })
        if snapshot.external_orders:
            issues.append({
                "rule": "external_active_orders",
                "reason": "account contains activity not owned by this strategy instance",
                "references": list(snapshot.external_orders),
            })
        if expected_positions is not None and not allow_position_changes:
            actual = {
                canonical_instrument(key): float(value)
                for key, value in snapshot.positions.items() if float(value)
            }
            expected = {
                canonical_instrument(key): float(value)
                for key, value in expected_positions.items() if float(value)
            }
            if actual != expected:
                issues.append({
                    "rule": "position_reconciliation",
                    "reason": "account holdings differ from the last execution target",
                    "expected": expected,
                    "actual": actual,
                })
        return AccountBoundaryResult(ok=not issues, issues=tuple(issues))

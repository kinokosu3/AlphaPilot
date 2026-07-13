"""Account-level execution planning with idempotent child orders."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any

from alphapilot.systems.live.targets import AccountSnapshot, TargetPortfolio
from alphapilot.systems.live.types import Direction, OrderRequest, normalize_symbol, symbol_key


@dataclass(frozen=True)
class PlanIssue:
    rule: str
    reason: str
    instrument: str = ""


@dataclass
class ExecutionPlan:
    plan_id: str
    decision_id: str
    instance_id: str
    requests: list[OrderRequest] = field(default_factory=list)
    issues: list[PlanIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


class ExecutionPlanner:
    """Diff a complete target book against positions plus working orders."""

    def __init__(self, *, lot_size: int = 100, max_order_value: float = 0.0) -> None:
        self.lot_size = max(int(lot_size), 0)
        self.max_order_value = max(float(max_order_value), 0.0)

    def plan(self, target: TargetPortfolio, oms: Any) -> ExecutionPlan:
        decision_id = target.decision_id or _stable_id(
            target.instance_id, target.config_hash, target.date, target.holdings
        )
        plan_id = _stable_id("plan", decision_id)
        snapshot = AccountSnapshot.from_oms(oms, as_of=target.as_of)
        wanted: dict[str, float] = {}
        meta: dict[str, tuple[str, Any, float, int]] = {}
        issues: list[PlanIssue] = []

        for raw_symbol, shares in target.holdings.items():
            code, exchange = normalize_symbol(raw_symbol)
            key = symbol_key(code, exchange)
            contract = oms.get_contract(key) if hasattr(oms, "get_contract") else None
            lot = int(getattr(contract, "lot_size", 0) or self.lot_size)
            price = _price_for(target, raw_symbol, key, oms)
            wanted[key] = max(float(shares), 0.0)
            meta[key] = (code, exchange, price, lot)

        for position in oms.get_positions():
            if position.key not in meta:
                contract = oms.get_contract(position.key) if hasattr(oms, "get_contract") else None
                lot = int(getattr(contract, "lot_size", 0) or self.lot_size)
                meta[position.key] = (
                    position.code, position.exchange, _price_for(target, position.key, position.key, oms), lot
                )

        requests: list[OrderRequest] = []
        for key in sorted(set(wanted) | set(snapshot.positions)):
            code, exchange, price, lot = meta[key]
            effective = snapshot.positions.get(key, 0.0) + snapshot.active_order_deltas.get(key, 0.0)
            delta = wanted.get(key, 0.0) - effective
            if abs(delta) < max(lot, 1):
                continue
            if price <= 0 and delta > 0:
                issues.append(PlanIssue("missing_price", "positive live/reference price is required", key))
                continue
            if delta < 0:
                delta = -min(abs(delta), snapshot.sellable.get(key, 0.0))
            volume = _floor_lot(abs(delta), lot)
            if volume <= 0:
                continue
            side = "B" if delta > 0 else "S"
            chunks = self._chunks(volume, price, lot)
            for child_index, child_volume in enumerate(chunks):
                reference = (
                    f"{target.instance_id or 'legacy'}:{target.config_hash or '-'}:"
                    f"{decision_id}:{key}:{side}:{child_index}"
                )
                factory = OrderRequest.buy if delta > 0 else OrderRequest.sell
                requests.append(factory(code, exchange, child_volume, price, reference=reference))

        requests.sort(key=lambda req: (req.direction != Direction.SHORT, req.key, req.reference))
        return ExecutionPlan(plan_id, decision_id, target.instance_id or "legacy", requests, issues)

    def _chunks(self, volume: float, price: float, lot: int) -> list[float]:
        if self.max_order_value <= 0 or price <= 0:
            return [volume]
        max_volume = _floor_lot(self.max_order_value / price, lot)
        if max_volume <= 0:
            return []
        chunks: list[float] = []
        remaining = volume
        while remaining > 0:
            child = min(remaining, max_volume)
            child = _floor_lot(child, lot)
            if child <= 0:
                break
            chunks.append(child)
            remaining -= child
        return chunks


def _floor_lot(volume: float, lot: int) -> float:
    if lot > 0:
        return float(math.floor(float(volume) / lot) * lot)
    return float(math.floor(max(float(volume), 0.0)))


def _price_for(target: TargetPortfolio, raw_symbol: str, key: str, oms: Any) -> float:
    for candidate in (raw_symbol, key):
        try:
            price = float(target.prices.get(candidate, 0.0))
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            return price
    tick = oms.get_tick(key) if hasattr(oms, "get_tick") else None
    return float(getattr(tick, "last_price", 0.0) or 0.0)


def _stable_id(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]

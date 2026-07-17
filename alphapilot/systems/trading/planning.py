"""Broker-independent target-to-execution planning."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping

from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    ExecutionChild,
    ExecutionPhase,
    ExecutionPlan,
    InstrumentMetadata,
    PlanIssue,
    TargetPortfolio,
    TradableQuote,
    canonical_instrument,
)


class ExecutionPlanner:
    """Diff a complete target book against holdings and working orders."""

    def __init__(
        self,
        *,
        lot_size: int = 100,
        max_order_value: float = 0.0,
        max_order_equity_pct: float = 0.0,
    ) -> None:
        self.lot_size = max(int(lot_size), 0)
        self.max_order_value = max(float(max_order_value), 0.0)
        self.max_order_equity_pct = max(float(max_order_equity_pct), 0.0)

    def plan(
        self,
        target: TargetPortfolio,
        account: AccountSnapshot,
        *,
        quotes: Mapping[str, TradableQuote] | None = None,
        instruments: Mapping[str, InstrumentMetadata] | None = None,
        next_child_index: Mapping[str, int] | None = None,
    ) -> ExecutionPlan:
        decision_id = target.decision_id or _stable_id(
            target.instance_id, target.config_hash, target.date, target.holdings,
        )
        plan_id = _stable_id("plan", decision_id)
        wanted = {
            canonical_instrument(key): max(float(value), 0.0)
            for key, value in target.holdings.items()
        }
        current = {
            canonical_instrument(key): max(float(value), 0.0)
            for key, value in account.positions.items()
        }
        active = {
            canonical_instrument(key): float(value)
            for key, value in account.active_order_deltas.items()
        }
        sellable = {
            canonical_instrument(key): max(float(value), 0.0)
            for key, value in account.sellable.items()
        }
        children: list[ExecutionChild] = []
        issues: list[PlanIssue] = []
        counters = dict(next_child_index or {})
        for instrument in sorted(set(wanted) | set(current)):
            metadata = _lookup(instruments, instrument) or InstrumentMetadata(instrument)
            lot = int(metadata.lot_size or self.lot_size)
            effective = current.get(instrument, 0.0) + active.get(instrument, 0.0)
            delta = wanted.get(instrument, 0.0) - effective
            if abs(delta) < max(lot, 1):
                continue
            quote = _lookup(quotes, instrument)
            price = float(
                (quote.executable_price if quote is not None else 0.0)
                or target.prices.get(instrument)
                or 0.0
            )
            if quote is not None and (quote.stale or quote.suspended):
                issues.append(PlanIssue("untradable_quote", "quote is stale or suspended", instrument))
                continue
            if price <= 0:
                issues.append(PlanIssue("missing_price", "positive raw execution price is required", instrument))
                continue
            price = _round_price_tick(price, float(metadata.price_tick or 0.0))
            if delta < 0:
                available_to_sell = sellable.get(instrument, current.get(instrument, 0.0))
                delta = -min(abs(delta), available_to_sell)
            if quote is not None and delta > 0 and quote.limit_up > 0 and price >= quote.limit_up - 1e-9:
                issues.append(PlanIssue("limit_up", "cannot buy at a locked upper limit", instrument))
                continue
            if quote is not None and delta < 0 and quote.limit_down > 0 and price <= quote.limit_down + 1e-9:
                issues.append(PlanIssue("limit_down", "cannot sell at a locked lower limit", instrument))
                continue
            volume = _floor_lot(abs(delta), lot)
            if volume <= 0:
                continue
            side = "buy" if delta > 0 else "sell"
            counter_key = f"{instrument}:{side}"
            index = int(counters.get(counter_key, 0))
            chunks = self._chunks(volume, price, lot, account.balance)
            if not chunks:
                issues.append(
                    PlanIssue("max_order_value", "single lot exceeds max_order_value", instrument)
                )
                continue
            for chunk in chunks:
                reference = (
                    f"{target.instance_id or 'legacy'}:{target.config_hash or '-'}:"
                    f"{decision_id}:{instrument}:{'B' if side == 'buy' else 'S'}:{index}"
                )
                children.append(ExecutionChild(
                    reference=reference,
                    instrument=instrument,
                    side=side,
                    volume=chunk,
                    price=price,
                    child_index=index,
                ))
                index += 1
            counters[counter_key] = index
        children.sort(key=lambda child: (child.side != "sell", child.instrument, child.child_index))
        return ExecutionPlan(
            plan_id=plan_id,
            decision_id=decision_id,
            instance_id=target.instance_id or "legacy",
            config_hash=target.config_hash,
            phase=ExecutionPhase.PLANNED,
            children=tuple(children),
            issues=tuple(issues),
        )

    def _chunks(
        self,
        volume: float,
        price: float,
        lot: int,
        account_equity: float,
    ) -> list[float]:
        caps = [
            value
            for value in (
                self.max_order_value,
                self.max_order_equity_pct * max(float(account_equity), 0.0),
            )
            if value > 0
        ]
        if not caps:
            return [volume]
        cap = _floor_lot(min(caps) / price, lot)
        if cap <= 0:
            return []
        chunks: list[float] = []
        remaining = volume
        while remaining > 0:
            child = _floor_lot(min(remaining, cap), lot)
            if child <= 0:
                break
            chunks.append(child)
            remaining -= child
        return chunks


def _lookup(values: Mapping[str, object] | None, instrument: str):  # noqa: ANN202
    if not values:
        return None
    return values.get(instrument) or next(
        (value for key, value in values.items() if canonical_instrument(key) == instrument),
        None,
    )


def _floor_lot(volume: float, lot: int) -> float:
    if lot > 0:
        return float(math.floor(float(volume) / lot) * lot)
    return float(math.floor(max(float(volume), 0.0)))


def _round_price_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return float(price)
    decimal_tick = Decimal(str(tick))
    ticks = (Decimal(str(price)) / decimal_tick).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP,
    )
    return float(ticks * decimal_tick)


def _stable_id(*parts: object) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]

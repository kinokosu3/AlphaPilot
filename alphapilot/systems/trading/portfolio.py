"""Signal-to-portfolio construction shared by rule and model strategies."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Iterable

from alphapilot.systems.live.targets import AccountSnapshot, TargetPortfolio
from alphapilot.systems.live.types import normalize_symbol, symbol_key
from alphapilot.systems.trading.domain import SignalRecord, StrategyInstanceConfig, TargetWeights


@dataclass(frozen=True)
class EqualWeightPortfolioPolicy:
    capital_fraction: float = 0.90
    max_position_weight: float = 0.30

    def build(self, signals: Iterable[SignalRecord], *, as_of: str) -> TargetWeights:
        active = sorted({row.instrument for row in signals if int(row.signal) > 0})
        scores = {row.instrument: float(row.score) for row in signals}
        if not active:
            return TargetWeights(as_of=as_of, weights={}, scores=scores)
        capital = min(max(float(self.capital_fraction), 0.0), 1.0)
        cap = min(max(float(self.max_position_weight), 0.0), 1.0)
        weight = min(capital / len(active), cap)
        return TargetWeights(as_of=as_of, weights={key: weight for key in active}, scores=scores)


class AccountSizer:
    def __init__(self, *, lot_size: int = 100) -> None:
        self.lot_size = max(int(lot_size), 0)

    def size(
        self,
        weights: TargetWeights,
        account: AccountSnapshot,
        prices: dict[str, float],
        instance: StrategyInstanceConfig,
    ) -> TargetPortfolio:
        holdings: dict[str, float] = {}
        normalized_prices: dict[str, float] = {}
        for raw_symbol, weight in sorted(weights.weights.items()):
            code, exchange = normalize_symbol(raw_symbol)
            key = symbol_key(code, exchange)
            price = float(prices.get(raw_symbol) or prices.get(key) or 0.0)
            if price <= 0:
                raise ValueError(f"missing positive execution price for {raw_symbol}")
            shares = _floor_lot((float(account.balance) * float(weight)) / price, self.lot_size)
            if shares > 0:
                holdings[key] = shares
                normalized_prices[key] = price
        decision_id = hashlib.sha256(
            f"{instance.instance_id}:{instance.config_hash}:{weights.as_of}".encode("utf-8")
        ).hexdigest()[:24]
        return TargetPortfolio(
            date=weights.as_of,
            holdings=holdings,
            prices=normalized_prices,
            source=instance.strategy_id,
            decision_id=decision_id,
            instance_id=instance.instance_id,
            as_of=weights.as_of,
            config_hash=instance.config_hash,
            target_weights=dict(weights.weights),
            price_source="live_or_raw",
        )


class SingleWriterAllocator:
    """V1 allocator: enforce one live writer and pass its target unchanged."""

    def allocate(self, targets: list[TargetPortfolio]) -> TargetPortfolio:
        if len(targets) != 1:
            raise ValueError("exactly one account-level strategy target is allowed in v1")
        return targets[0]


def _floor_lot(value: float, lot: int) -> float:
    if lot > 0:
        return float(math.floor(value / lot) * lot)
    return float(math.floor(max(value, 0.0)))

"""Signal-to-portfolio construction shared by rule and model strategies."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Iterable, Mapping

from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    canonical_instrument,
    FeeSchedule,
    InstrumentMetadata,
    PortfolioContext,
    PortfolioInputs,
    SignalKind,
    TargetPortfolio,
    TradableQuote,
)
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


@dataclass(frozen=True)
class TimingFixedExposurePolicy:
    """Translate standalone timing states into explicit, validated weights."""

    target_percent: float = 0.20
    cash_buffer: float = 0.10
    max_position_weight: float = 0.30
    exposure_mode: str = "per_instrument"
    policy_id: str = "timing_fixed_exposure"
    version: str = "1.0.0"

    def build(self, inputs: PortfolioInputs, context: PortfolioContext) -> TargetWeights:
        if inputs.selection is not None or inputs.market_timing:
            raise ValueError("timing_fixed_exposure accepts one instrument-timing source only")
        if len(inputs.instrument_timing) != 1:
            raise ValueError("timing_fixed_exposure requires exactly one signal envelope")
        envelope = inputs.instrument_timing[0]
        if envelope.kind != SignalKind.INSTRUMENT_TIMING:
            raise ValueError("timing_fixed_exposure requires instrument timing signals")
        payload = envelope.payload
        states = getattr(payload, "states", {})
        scores = {str(key): float(value) for key, value in getattr(payload, "scores", {}).items()}
        active = sorted(
            instrument for instrument, value in states.items()
            if str(value).strip().lower() in {"1", "true", "long", "bull", "on"}
        )
        target = float(self.target_percent)
        cap = float(self.max_position_weight)
        if target < 0 or target > cap + 1e-12:
            raise ValueError("target_percent must be non-negative and <= max_position_weight")
        investable = 1.0 - float(self.cash_buffer)
        if investable < 0 or investable > 1:
            raise ValueError("cash_buffer must be between 0 and 1")
        mode = str(self.exposure_mode).strip().lower()
        if mode == "per_instrument":
            total = target * len(active)
            if total > investable + 1e-12:
                raise ValueError(
                    f"timing target weights sum to {total:.2%}, above investable {investable:.2%}"
                )
            weight = target
        elif mode == "equal_active_budget":
            weight = min(target / len(active), cap) if active else 0.0
            if target > investable + 1e-12:
                raise ValueError(
                    f"timing exposure budget {target:.2%} is above investable {investable:.2%}"
                )
        else:
            raise ValueError("exposure_mode must be per_instrument or equal_active_budget")
        return TargetWeights(
            as_of=envelope.as_of,
            weights={instrument: weight for instrument in active},
            scores=scores,
            policy_id=self.policy_id,
            policy_version=self.version,
        )


@dataclass(frozen=True)
class SelectionTopKDropoutEqualWeightPolicy:
    """Qlib-style bounded turnover followed by equal-weight construction."""

    topk: int = 10
    n_drop: int = 2
    cash_buffer: float = 0.10
    max_position_weight: float = 0.20
    policy_id: str = "selection_topk_dropout_equal_weight"
    version: str = "1.0.0"

    def build(self, inputs: PortfolioInputs, context: PortfolioContext) -> TargetWeights:
        if inputs.selection is None or inputs.instrument_timing or inputs.market_timing:
            raise ValueError("selection policy accepts one cross-sectional source only")
        envelope = inputs.selection
        if envelope.kind != SignalKind.CROSS_SECTIONAL_SELECTION:
            raise ValueError("selection policy requires cross-sectional scores")
        scores = {
            str(key): float(value)
            for key, value in getattr(envelope.payload, "scores", {}).items()
        }
        ranked = sorted(scores, key=lambda key: (-scores[key], key))
        topk = int(self.topk)
        n_drop = int(self.n_drop)
        if topk <= 0 or n_drop < 0 or n_drop > topk:
            raise ValueError("topk must be positive and n_drop must be between 0 and topk")
        current = [
            key for key, volume in context.account.positions.items()
            if float(volume) > 0 and key in scores
        ]
        if not current:
            selected = ranked[:topk]
        else:
            ordered_current = sorted(current, key=lambda key: (-scores[key], key))
            forced_excess = max(len(ordered_current) - topk, 0)
            drop_count = min(max(n_drop, forced_excess), len(ordered_current))
            kept = ordered_current[:len(ordered_current) - drop_count] if drop_count else ordered_current
            selected = list(kept[:topk])
            for instrument in ranked:
                if instrument not in selected:
                    selected.append(instrument)
                if len(selected) >= topk:
                    break
        investable = 1.0 - float(self.cash_buffer)
        cap = float(self.max_position_weight)
        if investable < 0 or investable > 1 or cap <= 0 or cap > 1:
            raise ValueError("cash_buffer/max_position_weight are outside safe bounds")
        weight = min(investable / len(selected), cap) if selected else 0.0
        return TargetWeights(
            as_of=envelope.as_of,
            weights={instrument: weight for instrument in selected},
            scores=scores,
            policy_id=self.policy_id,
            policy_version=self.version,
        )


class AccountSizer:
    def __init__(self, *, lot_size: int = 100) -> None:
        self.lot_size = max(int(lot_size), 0)

    def size(
        self,
        weights: TargetWeights,
        account: AccountSnapshot,
        prices: dict[str, float],
        instance: StrategyInstanceConfig,
        *,
        quotes: Mapping[str, TradableQuote] | None = None,
        instruments: Mapping[str, InstrumentMetadata] | None = None,
        fees: FeeSchedule | None = None,
        decision_id: str | None = None,
        effective_session: str | None = None,
        valid_until: str | None = None,
    ) -> TargetPortfolio:
        if float(account.balance) <= 0:
            raise ValueError("account equity must be positive before sizing")
        total_weight = sum(max(float(value), 0.0) for value in weights.weights.values())
        if total_weight > 1.0 + 1e-9:
            raise ValueError("target weights exceed account equity")
        fee_schedule = fees or FeeSchedule()
        holdings: dict[str, float] = {}
        normalized_prices: dict[str, float] = {}
        for raw_symbol, weight in sorted(weights.weights.items()):
            key = canonical_instrument(raw_symbol)
            quote = (quotes or {}).get(raw_symbol) or (quotes or {}).get(key)
            if quote is not None and (quote.stale or quote.suspended):
                raise ValueError(f"execution quote is not tradable for {raw_symbol}")
            price = float(
                (quote.executable_price if quote is not None else 0.0)
                or prices.get(raw_symbol) or prices.get(key) or 0.0
            )
            if price <= 0:
                raise ValueError(f"missing positive execution price for {raw_symbol}")
            metadata = (instruments or {}).get(raw_symbol) or (instruments or {}).get(key)
            if metadata is not None and not metadata.long_only:
                raise ValueError(f"instrument metadata is outside the long-only launch scope: {key}")
            lot_size = int(metadata.lot_size) if metadata is not None else self.lot_size
            shares = _floor_lot((float(account.balance) * float(weight)) / price, lot_size)
            if shares > 0:
                holdings[key] = shares
                normalized_prices[key] = price
        holdings = self._fit_to_available_cash(
            holdings,
            account,
            prices,
            quotes=quotes,
            instruments=instruments,
            fees=fee_schedule,
        )
        normalized_prices = {
            key: value for key, value in normalized_prices.items() if key in holdings
        }
        stable_decision_id = decision_id or hashlib.sha256(
            f"{instance.instance_id}:{instance.config_hash}:{weights.as_of}".encode("utf-8")
        ).hexdigest()[:24]
        return TargetPortfolio(
            date=effective_session or weights.as_of,
            holdings=holdings,
            prices=normalized_prices,
            source=instance.strategy_id,
            decision_id=stable_decision_id,
            instance_id=instance.instance_id,
            as_of=weights.as_of,
            effective_session=effective_session,
            valid_until=valid_until,
            config_hash=instance.config_hash,
            target_weights=dict(weights.weights),
            price_source="raw",
        )

    def _fit_to_available_cash(
        self,
        desired: dict[str, float],
        account: AccountSnapshot,
        prices: Mapping[str, float],
        *,
        quotes: Mapping[str, TradableQuote] | None,
        instruments: Mapping[str, InstrumentMetadata] | None,
        fees: FeeSchedule,
    ) -> dict[str, float]:
        current = {
            canonical_instrument(key): max(float(value), 0.0)
            for key, value in account.positions.items()
        }
        for key, delta in account.active_order_deltas.items():
            instrument = canonical_instrument(key)
            current[instrument] = max(current.get(instrument, 0.0) + float(delta), 0.0)
        sellable = {
            canonical_instrument(key): max(float(value), 0.0)
            for key, value in account.sellable.items()
        }
        proceeds = 0.0
        buys: list[tuple[str, float, float, int]] = []
        for instrument in sorted(set(current) | set(desired)):
            actual = current.get(instrument, 0.0)
            target = desired.get(instrument, 0.0)
            delta = target - actual
            if abs(delta) <= 1e-9:
                continue
            quote = _lookup(quotes, instrument)
            if quote is not None and (quote.stale or quote.suspended):
                raise ValueError(f"execution quote is not tradable for {instrument}")
            price = float(
                (quote.executable_price if quote is not None else 0.0)
                or _lookup(prices, instrument)
                or 0.0
            )
            if price <= 0:
                raise ValueError(f"missing positive execution price for {instrument}")
            metadata = _lookup(instruments, instrument)
            lot = int(metadata.lot_size) if metadata is not None else self.lot_size
            if delta < 0:
                volume = min(abs(delta), sellable.get(instrument, actual))
                notional = volume * price
                proceeds += max(
                    notional - fees.sell_fee(notional, lot_notional=lot * price),
                    0.0,
                )
            else:
                buys.append((instrument, delta, price, lot))
        capacity = max(float(account.available), 0.0) + proceeds
        required = sum(
            volume * price + fees.buy_fee(volume * price, lot_notional=lot * price)
            for _, volume, price, lot in buys
        )
        if required <= capacity + 1e-9:
            return desired

        def scaled(scale: float) -> tuple[dict[str, float], float]:
            adjusted = dict(desired)
            cost = 0.0
            for instrument, volume, price, lot in buys:
                increment = _floor_lot(volume * scale, lot)
                target = current.get(instrument, 0.0) + increment
                if target > 0:
                    adjusted[instrument] = target
                else:
                    adjusted.pop(instrument, None)
                notional = increment * price
                cost += notional + fees.buy_fee(
                    notional,
                    lot_notional=lot * price,
                )
            return adjusted, cost

        # Minimum fees do not scale linearly, so a direct capacity ratio can
        # still overspend. Find the largest common scale whose rounded lots and
        # minimum fees fit. A common scale keeps the result symbol-order neutral.
        low, high = 0.0, min(capacity / required, 1.0) if required > 0 else 0.0
        candidate, candidate_cost = scaled(high)
        if candidate_cost <= capacity + 1e-9:
            return candidate
        for _ in range(64):
            middle = (low + high) / 2.0
            _, cost = scaled(middle)
            if cost <= capacity + 1e-9:
                low = middle
            else:
                high = middle
        return scaled(low)[0]


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


def _lookup(values: Mapping[str, object] | None, instrument: str):  # noqa: ANN202
    if not values:
        return None
    return values.get(instrument) or next(
        (value for key, value in values.items() if canonical_instrument(key) == instrument),
        None,
    )

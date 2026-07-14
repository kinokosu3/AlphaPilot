"""Pure contracts shared by research, strategy, backtest and live execution.

This module deliberately has no dependency on another AlphaPilot system.  It is
the stable boundary between signal generation, portfolio construction and
execution.  Concrete timing/selection algorithms and broker adapters live in
their respective systems.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol, TypeAlias


def canonical_instrument(symbol: str) -> str:
    """Normalize common Chinese-market symbols without importing live types."""

    raw = str(symbol).strip().upper().replace(" ", "")
    aliases = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
    exchanges = {"SSE", "SZSE", "BSE", "SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"}
    for separator in (".", "-", "_"):
        if separator not in raw:
            continue
        left, right = raw.split(separator, 1)
        left_exchange = aliases.get(left, left if left in exchanges else "")
        right_exchange = aliases.get(right, right if right in exchanges else "")
        if left_exchange:
            return f"{right}.{left_exchange}"
        if right_exchange:
            return f"{left}.{right_exchange}"
    for prefix, exchange in aliases.items():
        if raw.startswith(prefix) and raw[len(prefix):].isdigit():
            return f"{raw[len(prefix):]}.{exchange}"
    if raw.isdigit():
        exchange = (
            "SSE" if raw[0] in {"5", "6"} or raw.startswith("11")
            else "SZSE" if raw[0] in {"0", "1", "2", "3"}
            else "BSE" if raw[0] in {"4", "8"} or raw.startswith("920")
            else "UNKNOWN"
        )
        return f"{raw}.{exchange}"
    return f"{raw}.UNKNOWN"


class PriceAdjustment(str, Enum):
    NONE = "none"
    FORWARD = "forward"
    BACKWARD = "backward"


class OrderStatus(str, Enum):
    SUBMITTING = "submitting"
    NOTTRADED = "nottraded"
    PARTTRADED = "parttraded"
    ALLTRADED = "alltraded"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    SUBMITTED = "submitting"
    FILLED = "alltraded"


@dataclass(frozen=True)
class CompletedBar:
    """A completed, versioned market bar safe for strategy evaluation."""

    datetime: str
    instrument: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    frequency: str = "day"
    adjustment: PriceAdjustment = PriceAdjustment.NONE
    data_version: str = ""
    complete: bool = True

    def __post_init__(self) -> None:
        if not self.instrument.strip():
            raise ValueError("completed bar instrument is required")
        if not self.datetime:
            raise ValueError("completed bar datetime is required")
        if not self.complete:
            raise ValueError("strategy input bars must be complete")
        if min(float(self.open), float(self.high), float(self.low), float(self.close)) <= 0:
            raise ValueError("completed bar prices must be positive")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["adjustment"] = self.adjustment.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompletedBar":
        raw_adjustment = data.get("adjustment") or PriceAdjustment.NONE
        adjustment = (
            raw_adjustment
            if isinstance(raw_adjustment, PriceAdjustment)
            else PriceAdjustment(str(raw_adjustment))
        )
        return cls(
            datetime=str(data.get("datetime") or data.get("date") or ""),
            instrument=str(data.get("instrument") or ""),
            open=float(data.get("open") or 0.0),
            high=float(data.get("high") or 0.0),
            low=float(data.get("low") or 0.0),
            close=float(data.get("close") or 0.0),
            volume=float(data.get("volume") or 0.0),
            amount=float(data.get("amount") or 0.0),
            frequency=str(data.get("frequency") or "day"),
            adjustment=adjustment,
            data_version=str(data.get("data_version") or ""),
            complete=bool(data.get("complete", True)),
        )


@dataclass(frozen=True)
class TradableQuote:
    """Unadjusted quote used exclusively for sizing, valuation and execution."""

    instrument: str
    as_of: str
    last: float
    open: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    limit_up: float = 0.0
    limit_down: float = 0.0
    suspended: bool = False
    stale: bool = False
    data_version: str = ""
    price_source: str = "raw"

    def __post_init__(self) -> None:
        if not self.instrument.strip() or not self.as_of:
            raise ValueError("tradable quote instrument and as_of are required")
        if self.price_source != "raw":
            raise ValueError("tradable quotes must use unadjusted raw prices")

    @property
    def executable_price(self) -> float:
        return float(self.open or self.last)


@dataclass(frozen=True)
class InstrumentMetadata:
    instrument: str
    asset_type: str = "equity"
    lot_size: int = 100
    price_tick: float = 0.01
    settlement_days: int = 1
    long_only: bool = True

    def __post_init__(self) -> None:
        if not str(self.instrument).strip():
            raise ValueError("instrument metadata requires an instrument")
        if int(self.lot_size) <= 0:
            raise ValueError("instrument lot_size must be positive")
        if float(self.price_tick) <= 0:
            raise ValueError("instrument price_tick must be positive")
        if int(self.settlement_days) < 0:
            raise ValueError("instrument settlement_days must not be negative")


@dataclass(frozen=True)
class FeeSchedule:
    """Conservative fee assumptions used before an order reaches a Broker."""

    buy_rate: float = 0.0
    sell_rate: float = 0.0
    min_fee: float = 0.0
    max_order_value: float = 0.0

    def __post_init__(self) -> None:
        if min(
            float(self.buy_rate),
            float(self.sell_rate),
            float(self.min_fee),
            float(self.max_order_value),
        ) < 0:
            raise ValueError("fee rates, minimum fee and order cap must not be negative")

    def buy_fee(self, notional: float, *, lot_notional: float = 0.0) -> float:
        return self._fee(notional, self.buy_rate, lot_notional=lot_notional)

    def sell_fee(self, notional: float, *, lot_notional: float = 0.0) -> float:
        return self._fee(notional, self.sell_rate, lot_notional=lot_notional)

    def _fee(self, notional: float, rate: float, *, lot_notional: float) -> float:
        value = max(float(notional), 0.0)
        if value <= 0:
            return 0.0
        cap = float(self.max_order_value)
        if cap <= 0:
            return max(value * float(rate), float(self.min_fee))
        one_lot = max(float(lot_notional), 0.0)
        if one_lot > 0:
            lots_per_child = int(cap // one_lot)
            if lots_per_child <= 0:
                # The planner will block this instrument. Infinite cost makes
                # sizing remove it rather than reserve too little cash.
                return float("inf")
            child_cap = lots_per_child * one_lot
        else:
            child_cap = cap
        full_children = int(value // child_cap)
        remaining = value - full_children * child_cap
        total = full_children * max(
            child_cap * float(rate),
            float(self.min_fee),
        )
        if remaining > 1e-9:
            total += max(remaining * float(rate), float(self.min_fee))
        return total


@dataclass(frozen=True)
class StrategyEvaluationContext:
    instance_id: str
    config_hash: str
    as_of: str
    effective_session: str
    frequency: str
    history: tuple[CompletedBar, ...] = ()
    data_version: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PortfolioContext:
    as_of: str
    account: "AccountSnapshot"
    quotes: Mapping[str, TradableQuote] = field(default_factory=dict)
    instruments: Mapping[str, InstrumentMetadata] = field(default_factory=dict)
    constraints: Mapping[str, Any] = field(default_factory=dict)


class SignalKind(str, Enum):
    CROSS_SECTIONAL_SELECTION = "cross_sectional_selection"
    INSTRUMENT_TIMING = "instrument_timing"
    MARKET_TIMING = "market_timing"


@dataclass(frozen=True)
class CrossSectionalSignal:
    """A point-in-time comparison across an instrument universe."""

    scores: Mapping[str, float]
    ranks: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TimingSignal:
    """Time-series observations for instruments or a market benchmark.

    Scores are intentionally not defined as target exposure.  A future
    ``PortfolioPolicy`` decides how (or whether) to translate them to weights.
    """

    scores: Mapping[str, float]
    states: Mapping[str, str] = field(default_factory=dict)


SignalPayload: TypeAlias = CrossSectionalSignal | TimingSignal


@dataclass(frozen=True)
class SignalEnvelope:
    kind: SignalKind
    source_instance_id: str
    as_of: str
    payload: SignalPayload
    valid_until: str | None = None
    frequency: str = "day"
    data_version: str = ""
    model_version: str = ""

    def __post_init__(self) -> None:
        if not str(self.source_instance_id).strip():
            raise ValueError("source_instance_id is required")
        if not str(self.as_of).strip():
            raise ValueError("as_of is required")
        if self.kind == SignalKind.CROSS_SECTIONAL_SELECTION:
            if not isinstance(self.payload, CrossSectionalSignal):
                raise TypeError("cross-sectional envelope requires CrossSectionalSignal")
        elif not isinstance(self.payload, TimingSignal):
            raise TypeError("timing envelope requires TimingSignal")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SignalEnvelope":
        kind = SignalKind(str(data["kind"]))
        raw_payload = data.get("payload") or {}
        if not isinstance(raw_payload, Mapping):
            raise TypeError("signal payload must be an object")
        if kind == SignalKind.CROSS_SECTIONAL_SELECTION:
            payload: SignalPayload = CrossSectionalSignal(
                scores={str(k): float(v) for k, v in (raw_payload.get("scores") or {}).items()},
                ranks={str(k): int(v) for k, v in (raw_payload.get("ranks") or {}).items()},
            )
        else:
            payload = TimingSignal(
                scores={str(k): float(v) for k, v in (raw_payload.get("scores") or {}).items()},
                states={str(k): str(v) for k, v in (raw_payload.get("states") or {}).items()},
            )
        return cls(
            kind=kind,
            source_instance_id=str(data.get("source_instance_id") or ""),
            as_of=str(data.get("as_of") or ""),
            payload=payload,
            valid_until=(None if data.get("valid_until") is None else str(data["valid_until"])),
            frequency=str(data.get("frequency") or "day"),
            data_version=str(data.get("data_version") or ""),
            model_version=str(data.get("model_version") or ""),
        )


@dataclass(frozen=True)
class PortfolioInputs:
    """Parallel inputs reserved for a future portfolio-composition policy."""

    selection: SignalEnvelope | None = None
    instrument_timing: tuple[SignalEnvelope, ...] = ()
    market_timing: tuple[SignalEnvelope, ...] = ()

    def __post_init__(self) -> None:
        if self.selection is not None and self.selection.kind != SignalKind.CROSS_SECTIONAL_SELECTION:
            raise ValueError("selection must use CROSS_SECTIONAL_SELECTION")
        if any(item.kind != SignalKind.INSTRUMENT_TIMING for item in self.instrument_timing):
            raise ValueError("instrument_timing contains a non-instrument timing signal")
        if any(item.kind != SignalKind.MARKET_TIMING for item in self.market_timing):
            raise ValueError("market_timing contains a non-market timing signal")

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection": None if self.selection is None else self.selection.to_dict(),
            "instrument_timing": [item.to_dict() for item in self.instrument_timing],
            "market_timing": [item.to_dict() for item in self.market_timing],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PortfolioInputs":
        selection = data.get("selection")
        return cls(
            selection=(
                SignalEnvelope.from_dict(selection)
                if isinstance(selection, Mapping) else None
            ),
            instrument_timing=tuple(
                SignalEnvelope.from_dict(item) for item in (data.get("instrument_timing") or [])
            ),
            market_timing=tuple(
                SignalEnvelope.from_dict(item) for item in (data.get("market_timing") or [])
            ),
        )


@dataclass(frozen=True)
class TargetWeights:
    as_of: str
    weights: dict[str, float]
    scores: dict[str, float] = field(default_factory=dict)
    policy_id: str = ""
    policy_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TargetWeights":
        return cls(
            as_of=str(data.get("as_of") or ""),
            weights={str(key): float(value) for key, value in (data.get("weights") or {}).items()},
            scores={str(key): float(value) for key, value in (data.get("scores") or {}).items()},
            policy_id=str(data.get("policy_id") or ""),
            policy_version=str(data.get("policy_version") or ""),
        )


class PortfolioPolicy(Protocol):
    def build(self, inputs: PortfolioInputs, context: PortfolioContext) -> TargetWeights: ...


@dataclass(frozen=True)
class PortfolioDecision:
    decision_id: str
    instance_id: str
    config_hash: str
    as_of: str
    effective_session: str
    valid_until: str
    signal: SignalEnvelope
    target_weights: TargetWeights
    data_version: str = ""
    model_version: str = ""
    strategy_code_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "signal": self.signal.to_dict(),
            "target_weights": asdict(self.target_weights),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PortfolioDecision":
        weights = data.get("target_weights") or {}
        if not isinstance(weights, Mapping):
            raise TypeError("target_weights must be an object")
        return cls(
            decision_id=str(data.get("decision_id") or ""),
            instance_id=str(data.get("instance_id") or ""),
            config_hash=str(data.get("config_hash") or ""),
            as_of=str(data.get("as_of") or ""),
            effective_session=str(data.get("effective_session") or ""),
            valid_until=str(data.get("valid_until") or ""),
            signal=SignalEnvelope.from_dict(data.get("signal") or {}),
            target_weights=TargetWeights.from_dict({
                **weights,
                "as_of": weights.get("as_of") or data.get("as_of") or "",
            }),
            data_version=str(data.get("data_version") or ""),
            model_version=str(data.get("model_version") or ""),
            strategy_code_hash=str(data.get("strategy_code_hash") or ""),
        )


class ExecutionPhase(str, Enum):
    PLANNED = "planned"
    SELLING = "selling"
    WAITING_SELL_REPORTS = "waiting_sell_reports"
    REFRESHING_ACCOUNT = "refreshing_account"
    BUYING = "buying"
    FINAL_RECONCILE = "final_reconcile"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass(frozen=True)
class PlanIssue:
    rule: str
    reason: str
    instrument: str = ""


@dataclass(frozen=True)
class ExecutionChild:
    reference: str
    instrument: str
    side: str
    volume: float
    price: float
    child_index: int
    status: str = "planned"
    order_id: str = ""


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    decision_id: str
    instance_id: str
    config_hash: str
    phase: ExecutionPhase = ExecutionPhase.PLANNED
    children: tuple[ExecutionChild, ...] = ()
    issues: tuple[PlanIssue, ...] = ()
    recovery_version: int = 1

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["phase"] = self.phase.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionPlan":
        return cls(
            plan_id=str(data.get("plan_id") or ""),
            decision_id=str(data.get("decision_id") or ""),
            instance_id=str(data.get("instance_id") or ""),
            config_hash=str(data.get("config_hash") or ""),
            phase=ExecutionPhase(str(data.get("phase") or ExecutionPhase.PLANNED.value)),
            children=tuple(ExecutionChild(**dict(item)) for item in (data.get("children") or ())),
            issues=tuple(PlanIssue(**dict(item)) for item in (data.get("issues") or ())),
            recovery_version=int(data.get("recovery_version") or 1),
        )


@dataclass(frozen=True)
class OperatorContext:
    operator_id: str
    request_id: str
    reason: str
    auth_source: str = "token"

    def __post_init__(self) -> None:
        if not self.operator_id or not self.request_id:
            raise ValueError("operator_id and request_id are required")


@dataclass
class TargetPosition:
    symbol: str
    target_volume: float
    direction: Any = "net"
    price: float = 0.0
    offset_policy: str = "auto"


@dataclass
class TargetPortfolio:
    """Broker-independent desired account holdings."""

    date: str
    holdings: dict[str, float] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    cash: float | None = None
    source: str = ""
    market: str | None = None
    positions: list[TargetPosition] = field(default_factory=list)
    decision_id: str = ""
    instance_id: str = "legacy"
    as_of: str | None = None
    effective_session: str | None = None
    valid_until: str | None = None
    config_hash: str = ""
    data_version: str = ""
    model_version: str = ""
    target_weights: dict[str, float] = field(default_factory=dict)
    price_source: str = ""

    @classmethod
    def from_holdings(
        cls,
        date: str,
        records: Any,
        *,
        source: str = "",
        market: str | None = None,
    ) -> "TargetPortfolio":
        holdings: dict[str, float] = {}
        prices: dict[str, float] = {}
        for row in records:
            code = row.get("instrument") or row.get("code")
            if code is None:
                continue
            amount = float(row.get("amount", 0) or 0)
            if amount <= 0:
                continue
            holdings[str(code)] = amount
            if row.get("price") is not None:
                try:
                    prices[str(code)] = float(row["price"])
                except (TypeError, ValueError):
                    pass
        return cls(date=date, holdings=holdings, prices=prices, source=source, market=market)


@dataclass(frozen=True)
class AccountSnapshot:
    """Immutable account truth used for sizing and planning."""

    account_id: str
    as_of: str
    balance: float
    available: float
    positions: dict[str, float] = field(default_factory=dict)
    sellable: dict[str, float] = field(default_factory=dict)
    active_order_deltas: dict[str, float] = field(default_factory=dict)
    external_orders: tuple[str, ...] = ()
    data_version: str = ""

    @classmethod
    def from_oms(cls, oms: Any, *, as_of: str | None = None) -> "AccountSnapshot":
        account = oms.account
        if account is None:
            raise RuntimeError("OMS account snapshot is not ready")
        positions = {p.key: float(p.volume) for p in oms.get_positions() if float(p.volume)}
        sellable = {p.key: float(oms.available_shares(p.key)) for p in oms.get_positions()}
        active: dict[str, float] = {}
        for order in oms.get_active_orders():
            remaining = float(order.remaining)
            direction = getattr(order.direction, "value", order.direction)
            sign = 1.0 if str(direction) == "long" else -1.0
            active[order.key] = active.get(order.key, 0.0) + sign * remaining
        return cls(
            account_id=str(account.account_id),
            as_of=str(as_of or datetime.now().isoformat(timespec="seconds")),
            balance=float(account.balance),
            available=float(account.available),
            positions=positions,
            sellable=sellable,
            active_order_deltas=active,
        )

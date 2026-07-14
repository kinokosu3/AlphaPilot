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


class PortfolioPolicy(Protocol):
    def build(self, inputs: PortfolioInputs, context: Any) -> TargetWeights: ...


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

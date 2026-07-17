"""Shared types and protocols for timing strategies and live-ready execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import pandas as pd

from alphapilot.systems.trading.contracts import OrderStatus

OrderAction = Literal["buy", "sell", "target_percent", "target_shares", "close"]


@dataclass(frozen=True)
class OrderIntent:
    """Strategy output that can be consumed by backtest or live adapters."""

    datetime: pd.Timestamp | str
    instrument: str
    action: OrderAction
    quantity: float | None = None
    target_percent: float | None = None
    reason: str = ""


@dataclass
class TimingContext:
    """Runtime context passed to strategies."""

    params: dict[str, Any] = field(default_factory=dict)
    freq: str = "day"
    metadata: dict[str, Any] = field(default_factory=dict)


class TimingStrategy(Protocol):
    """Batch strategy interface used by the v1 timing backtest engine."""

    name: str

    def generate_signals(self, bars: pd.DataFrame, context: TimingContext) -> pd.DataFrame:
        """Return columns ``datetime/instrument/signal/target_percent/score/reason``."""


class EventTimingStrategy(Protocol):
    """Event-style protocol reserved for later live/vn.py integration."""

    name: str

    def on_bar(self, bar: pd.Series, context: TimingContext) -> list[OrderIntent]:
        """Return order intents for one bar."""


# ``OrderStatus`` lives in the dependency-free trading contracts and is imported
# above; re-exported here (and via ``systems/timing/__init__``) so
# existing timing code keeps its ``OrderStatus.SUBMITTED`` / ``.CANCELLED`` usage.
#
# The old request/reply broker abstractions (``ExecutionReport`` + a second
# ``BrokerGateway`` protocol + ``timing/broker.py``'s PaperBroker) were removed:
# execution now goes through the event-driven live stack —
# ``systems/live/gateway.BrokerGateway`` + ``systems/live/executor
# .orders_from_intents`` — with ``OrderIntent`` as the strategy-side contract
# and ``systems/timing/live_adapter.BatchStrategyAdapter`` as the bridge.

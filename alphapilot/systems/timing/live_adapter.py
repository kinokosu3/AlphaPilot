"""BatchStrategyAdapter — run batch timing strategies bar-by-bar in live mode.

The 8 rule strategies in :mod:`alphapilot.systems.timing.strategies` implement
the *batch* :class:`TimingStrategy` protocol (whole-history DataFrame in,
signal DataFrame out). Live trading needs the *event* shape reserved by
:class:`EventTimingStrategy` (bar in, intents out). This adapter bridges the
two without touching any strategy code: it keeps a rolling window of bars per
instrument, recomputes the batch signals on each completed bar, and emits an
:class:`OrderIntent` only when the latest signal *changes* — so a strategy that
stays long for 30 bars produces one intent, not 30.

Signal → intent semantics match the backtest engine's target-percent model:
signal 1 ⇒ ``target_percent`` from the strategy's signal row; signal 0 ⇒
``target_percent=0`` (flat). Execution timing (this bar's close? next open?)
is the runner's concern, mirroring the backtest's next-bar-open ``shift(1)``.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Protocol

import pandas as pd

from alphapilot.systems.timing.base import OrderIntent, TimingContext, TimingStrategy
from alphapilot.systems.trading.contracts import canonical_instrument


class BarLike(Protocol):
    datetime: Any
    instrument: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float

    def as_row(self) -> dict[str, Any]: ...


class BatchStrategyAdapter:
    """Wrap a batch :class:`TimingStrategy` into an ``on_bar -> intents`` interface."""

    def __init__(
        self,
        strategy: TimingStrategy,
        context: TimingContext | None = None,
        *,
        min_bars: int = 30,
        window: int = 250,
    ) -> None:
        self.strategy = strategy
        self.context = context or TimingContext()
        self.min_bars = int(min_bars)
        self.window = int(window)
        self._history: dict[str, deque[dict]] = {}
        self._last_signal: dict[str, int] = {}
        self._initialized = False
        self._stopped = False

    @property
    def name(self) -> str:
        return getattr(self.strategy, "name", type(self.strategy).__name__)

    def on_bar(self, bar: BarLike) -> list[OrderIntent]:
        """Feed one completed bar; return intents when the signal flips."""
        if self._stopped:
            return []
        instrument = _instrument_key(bar.instrument)
        history = self._history.setdefault(instrument, deque(maxlen=self.window))
        history.append({**bar.as_row(), "instrument": instrument})
        if len(history) < self.min_bars:
            return []

        bars_df = pd.DataFrame(history)
        signals = self.strategy.generate_signals(bars_df, self.context)
        if signals.empty:
            return []
        latest = signals.iloc[-1]
        signal = int(latest["signal"])

        previous = self._last_signal.get(instrument)
        self._last_signal[instrument] = signal
        if previous is not None and signal == previous:
            return []
        target = float(latest["target_percent"]) if signal else 0.0
        return [
            OrderIntent(
                datetime=pd.Timestamp(bar.datetime),
                instrument=instrument,
                action="target_percent",
                target_percent=target,
                reason=str(latest.get("reason", "")) or self.name,
            )
        ]

    def warm_up(self, bars: pd.DataFrame) -> None:
        """Preload history (e.g. recent daily bars from storage) before going live.

        ``bars`` uses the BAR_COLUMNS shape. Signals are computed once at the end
        so the first live bar diffs against the warmed-up state instead of firing
        a spurious entry intent.
        """
        normalized = bars.copy()
        normalized["instrument"] = normalized["instrument"].map(_instrument_key)
        for instrument, group in normalized.sort_values("datetime").groupby("instrument"):
            history = self._history.setdefault(instrument, deque(maxlen=self.window))
            for row in group.to_dict("records"):
                history.append(row)
            if len(history) >= self.min_bars:
                signals = self.strategy.generate_signals(pd.DataFrame(history), self.context)
                if not signals.empty:
                    self._last_signal[instrument] = int(signals.iloc[-1]["signal"])

    def initialize(self, context: TimingContext | None = None) -> None:
        if context is not None:
            self.context = context
        self._initialized = True
        self._stopped = False

    def warmup(self, history: pd.DataFrame) -> None:
        self.warm_up(history)

    def on_bars(self, completed_bars: list[BarLike] | pd.DataFrame) -> list[OrderIntent]:
        bars = (
            [_FrameBar(row) for row in completed_bars.to_dict("records")]
            if isinstance(completed_bars, pd.DataFrame)
            else list(completed_bars)
        )
        intents: list[OrderIntent] = []
        for bar in bars:
            intents.extend(self.on_bar(bar))
        return intents

    def stop(self, reason: str = "") -> None:
        self._stopped = True

    def synchronize_positions(self, held_instruments: set[str]) -> None:
        """Force the next bar to emit when warmed signal and OMS truth disagree."""
        normalized_holdings = {_instrument_key(item) for item in held_instruments}
        for instrument, signal in list(self._last_signal.items()):
            is_held = instrument in normalized_holdings
            if bool(signal) != is_held:
                self._last_signal.pop(instrument, None)

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "history": {key: list(rows) for key, rows in self._history.items()},
            "last_signal": dict(self._last_signal),
        }

    def restore(self, state: dict | None) -> None:
        if not state:
            return
        if int(state.get("version") or 0) != 1:
            raise ValueError("unsupported BatchStrategyAdapter state version")
        merged_history: dict[str, list[dict]] = {}
        for key, rows in (state.get("history") or {}).items():
            instrument = _instrument_key(str(key))
            merged_history.setdefault(instrument, []).extend(
                {**dict(row), "instrument": instrument}
                for row in rows
            )
        self._history = {
            instrument: deque(
                sorted(rows, key=lambda row: str(row.get("datetime") or ""))[-self.window:],
                maxlen=self.window,
            )
            for instrument, rows in merged_history.items()
        }
        self._last_signal = {}
        conflicts: set[str] = set()
        for key, value in (state.get("last_signal") or {}).items():
            instrument = _instrument_key(str(key))
            signal = int(value)
            if instrument in self._last_signal and self._last_signal[instrument] != signal:
                conflicts.add(instrument)
            self._last_signal[instrument] = signal
        for instrument in conflicts:
            self._last_signal.pop(instrument, None)


def _instrument_key(value: str) -> str:
    return canonical_instrument(str(value))


class _FrameBar:
    def __init__(self, row: dict[str, Any]) -> None:
        for key in ("datetime", "instrument", "open", "high", "low", "close", "volume", "amount"):
            setattr(self, key, row.get(key, 0.0))

    def as_row(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in ("datetime", "instrument", "open", "high", "low", "close", "volume", "amount")
        }

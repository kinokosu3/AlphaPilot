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

import pandas as pd

from alphapilot.systems.live.bars import Bar
from alphapilot.systems.timing.base import OrderIntent, TimingContext, TimingStrategy


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

    @property
    def name(self) -> str:
        return getattr(self.strategy, "name", type(self.strategy).__name__)

    def on_bar(self, bar: Bar) -> list[OrderIntent]:
        """Feed one completed bar; return intents when the signal flips."""
        history = self._history.setdefault(bar.instrument, deque(maxlen=self.window))
        history.append(bar.as_row())
        if len(history) < self.min_bars:
            return []

        bars_df = pd.DataFrame(history)
        signals = self.strategy.generate_signals(bars_df, self.context)
        if signals.empty:
            return []
        latest = signals.iloc[-1]
        signal = int(latest["signal"])

        previous = self._last_signal.get(bar.instrument)
        self._last_signal[bar.instrument] = signal
        if previous is not None and signal == previous:
            return []
        if previous is None and signal == 0:
            # Nothing held, nothing signalled: don't emit a redundant flat intent.
            return []

        target = float(latest["target_percent"]) if signal else 0.0
        return [
            OrderIntent(
                datetime=pd.Timestamp(bar.datetime),
                instrument=bar.instrument,
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
        for instrument, group in bars.sort_values("datetime").groupby("instrument"):
            history = self._history.setdefault(instrument, deque(maxlen=self.window))
            for row in group.to_dict("records"):
                history.append(row)
            if len(history) >= self.min_bars:
                signals = self.strategy.generate_signals(pd.DataFrame(history), self.context)
                if not signals.empty:
                    self._last_signal[instrument] = int(signals.iloc[-1]["signal"])

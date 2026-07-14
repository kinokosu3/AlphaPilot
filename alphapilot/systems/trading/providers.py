"""Provider protocol adapters that depend only on trading contracts."""

from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace
from typing import Any, Iterable

import pandas as pd

from alphapilot.systems.trading.contracts import (
    CompletedBar,
    SignalEnvelope,
    SignalKind,
    StrategyEvaluationContext,
    TimingSignal,
)


class V1BatchProviderAdapter:
    """Adapt a legacy ``generate_signals(DataFrame)`` strategy to provider v2."""

    def __init__(self, strategy: Any, *, params: dict[str, Any], max_history: int = 4096) -> None:
        self.strategy = strategy
        self.params = dict(params)
        self.max_history = max(int(max_history), 1)
        self._history: dict[str, list[CompletedBar]] = defaultdict(list)
        self._initialized = False
        self._stopped = False

    def initialize(self, context: StrategyEvaluationContext) -> None:
        del context
        self._initialized = True
        self._stopped = False

    def warmup(self, history: Iterable[CompletedBar]) -> None:
        for bar in history:
            self._append(bar)

    def evaluate(self, context: StrategyEvaluationContext) -> SignalEnvelope:
        if self._stopped:
            raise RuntimeError("strategy provider is stopped")
        if not self._initialized:
            self.initialize(context)
        for bar in context.history:
            self._append(bar)
        rows = [
            bar.to_dict()
            for instrument in sorted(self._history)
            for bar in self._history[instrument]
        ]
        frame = pd.DataFrame(rows)
        if frame.empty:
            raise ValueError("strategy evaluation history is empty")
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        legacy_context = SimpleNamespace(
            params=dict(self.params),
            freq=context.frequency,
            metadata={
                "instance_id": context.instance_id,
                "config_hash": context.config_hash,
                **dict(context.metadata),
            },
        )
        signals = self.strategy.generate_signals(frame, legacy_context)
        if signals is None or signals.empty:
            states: dict[str, str] = {}
            scores: dict[str, float] = {}
        else:
            latest = (
                signals.sort_values(["instrument", "datetime"])
                .groupby("instrument", sort=True)
                .tail(1)
            )
            states = {
                str(row.instrument): "long" if int(row.signal) > 0 else "flat"
                for row in latest.itertuples()
            }
            scores = {
                str(row.instrument): float(row.score or 0.0)
                for row in latest.itertuples()
            }
        return SignalEnvelope(
            kind=SignalKind.INSTRUMENT_TIMING,
            source_instance_id=context.instance_id,
            as_of=context.as_of,
            valid_until=context.effective_session,
            frequency=context.frequency,
            data_version=context.data_version,
            payload=TimingSignal(scores=scores, states=states),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "history": {
                instrument: [bar.to_dict() for bar in bars]
                for instrument, bars in self._history.items()
            },
        }

    def restore(self, state: dict[str, Any]) -> None:
        if int(state.get("version") or 0) != 1:
            raise ValueError("unsupported v1 provider state version")
        self._history.clear()
        for instrument, rows in (state.get("history") or {}).items():
            self._history[str(instrument)] = [CompletedBar.from_dict(row) for row in rows]

    def stop(self, reason: str) -> None:
        del reason
        self._stopped = True

    def _append(self, bar: CompletedBar) -> None:
        rows = self._history[bar.instrument]
        identity = (bar.datetime, bar.frequency, bar.adjustment.value)
        matching = next(
            (
                index for index, existing in enumerate(rows)
                if (existing.datetime, existing.frequency, existing.adjustment.value) == identity
            ),
            None,
        )
        if matching is None:
            rows.append(bar)
            rows.sort(key=lambda item: item.datetime)
        else:
            rows[matching] = bar
        del rows[:-self.max_history]

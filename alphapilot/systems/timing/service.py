"""Timing system service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

from alphapilot.kernel.base import BaseSystem
from alphapilot.systems.timing.base import TimingBacktestRequest, TimingBacktestResult
from alphapilot.systems.timing.compatibility import LegacyTimingCompatibilityAdapter
from alphapilot.systems.timing.data import load_bars

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context


class TimingSystem(BaseSystem):
    """Rule-based timing strategies over local AlphaPilot market data."""

    name = "timing"

    def setup(self, context: "Context") -> None:
        self.context = context
        self._compatibility = LegacyTimingCompatibilityAdapter(context)

    def list_strategies(self) -> list[dict[str, Any]]:
        return self._compatibility.list_strategies()

    def load_bars(self, **options: Any) -> pd.DataFrame:
        return load_bars(self.context, **options)

    def generate_signals(self, request: TimingBacktestRequest) -> pd.DataFrame:
        return self._compatibility.generate_signals(request)

    def run_backtest(self, request: TimingBacktestRequest) -> TimingBacktestResult:
        return self._compatibility.run_backtest(request)

    def ensure_legacy_replay_instance(self, request: TimingBacktestRequest) -> dict[str, Any]:
        """Compatibility-boundary helper used by the one-time Portal job importer."""

        return self._compatibility.ensure_replay_instance(request)

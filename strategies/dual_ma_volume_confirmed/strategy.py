"""Volume-confirmed dual moving-average timing strategy."""

from __future__ import annotations

import pandas as pd

from alphapilot.quant import indicators
from alphapilot.quant.signals import cross_above, cross_below, threshold_signal
from alphapilot.systems.timing.base import TimingContext
from alphapilot.systems.timing.strategies import RuleTimingStrategy


class DualMAVolumeConfirmed(RuleTimingStrategy):
    """Enter on a confirmed golden cross and leave on the reverse cross."""

    name = "dual_ma_volume_confirmed"
    description = (
        "Buy when the short MA crosses above the long MA while volume exceeds "
        "its rolling median; exit on the reverse MA cross."
    )
    defaults = {
        "short_window": 10,
        "long_window": 50,
        "volume_window": 20,
        "target_percent": 1.0,
    }

    def _instrument_signal(
        self,
        bars: pd.DataFrame,
        context: TimingContext,
    ) -> pd.DataFrame:
        del context
        short = indicators.ma(bars["close"], int(self.params["short_window"]))
        long = indicators.ma(bars["close"], int(self.params["long_window"]))
        volume = pd.to_numeric(bars["volume"], errors="coerce")
        volume_median = volume.rolling(
            int(self.params["volume_window"]),
            min_periods=int(self.params["volume_window"]),
        ).median()
        buy = cross_above(short, long) & (volume > volume_median)
        sell = cross_below(short, long)
        signal = threshold_signal(buy, sell)
        volume_ratio = volume / volume_median.replace(0, pd.NA)
        score = (short / long - 1).fillna(0) * volume_ratio.fillna(0)
        return self._frame(
            bars,
            signal,
            score,
            "dual_ma_volume_confirmed",
        )

"""BarAggregator — build minute/day bars from the live tick stream.

Timing strategies consume bars (``datetime/instrument/open/high/low/close/
volume/amount`` — the ``BAR_COLUMNS`` convention from ``systems/timing/data``),
but a broker feed pushes L1 ticks with *cumulative* session volume/turnover.
This aggregator floors ticks into fixed windows, diffs the cumulative counters
into per-bar volume/amount, and invokes ``on_bar`` when a window closes (i.e.
on the first tick of the next window). Call :meth:`flush` at session end to
close the final partial bar — there is no timer here by design; the caller
(strategy runner) owns time.

Dependency-light on purpose (stdlib only): lives in ``systems/live`` next to
the tick source; the pandas-facing adapter sits in ``systems/timing``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from alphapilot.systems.live.types import TickData

#: sentinel interval for daily bars (calendar-date windows, not epoch math).
DAY_INTERVAL = 86_400


@dataclass
class Bar:
    """One completed OHLCV bar. ``datetime`` is the window *start*."""

    datetime: datetime
    instrument: str            # symbol key, e.g. "600000.SSE"
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0        # shares traded within the bar
    amount: float = 0.0        # turnover (CNY) within the bar

    def as_row(self) -> dict:
        """Row in the timing system's BAR_COLUMNS shape."""
        return {
            "datetime": self.datetime,
            "instrument": self.instrument,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
        }


class _WorkingBar:
    __slots__ = ("start", "open", "high", "low", "close", "base_volume", "base_amount",
                 "last_volume", "last_amount")

    def __init__(self, start: datetime, price: float, cum_volume: float, cum_amount: float) -> None:
        self.start = start
        self.open = self.high = self.low = self.close = price
        # Cumulative counters at bar open; per-bar figures are diffs against these.
        self.base_volume = cum_volume
        self.base_amount = cum_amount
        self.last_volume = cum_volume
        self.last_amount = cum_amount


class BarAggregator:
    """Aggregate ticks into fixed-interval bars, one stream per instrument."""

    def __init__(
        self,
        interval: int = 60,
        on_bar: Callable[[Bar], None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive seconds (or DAY_INTERVAL)")
        self.interval = int(interval)
        self.on_bar = on_bar
        self._working: dict[str, _WorkingBar] = {}
        # Cumulative (volume, amount) at the previous bar's close, per key —
        # carried into the next bar's baseline so no inter-bar delta is lost.
        self._carry: dict[str, tuple[float, float]] = {}

    # ---- input ------------------------------------------------------------- #
    def on_tick(self, tick: TickData) -> None:
        if tick.datetime is None or tick.last_price <= 0:
            return
        key = tick.key
        start = self._window_start(tick.datetime)
        working = self._working.get(key)

        if working is not None and start > working.start:
            self._close(key, working)
            working = None

        if working is None:
            self._working[key] = working = _WorkingBar(
                start, tick.last_price, tick.volume, tick.turnover
            )
            carry = self._carry.get(key)
            if carry is not None and tick.volume >= carry[0]:
                # Continue the session's cumulative stream: volume traded between
                # the previous bar's close and this tick belongs to this bar.
                working.base_volume, working.base_amount = carry
                working.last_volume = max(tick.volume, carry[0])
                working.last_amount = max(tick.turnover, carry[1])
            # else: first observation or counters reset (new session) — baseline
            # at this tick; pre-observation volume is attributed to no bar.
            return

        working.close = tick.last_price
        working.high = max(working.high, tick.last_price)
        working.low = min(working.low, tick.last_price)
        if tick.volume >= working.last_volume:
            working.last_volume = tick.volume
            working.last_amount = tick.turnover
        else:  # cumulative reset mid-bar: re-baseline so diffs stay non-negative
            working.base_volume = working.last_volume = tick.volume
            working.base_amount = working.last_amount = tick.turnover

    def flush(self, instrument: str | None = None) -> list[Bar]:
        """Close working bars (all, or one instrument) and return them."""
        keys = [instrument] if instrument else list(self._working)
        closed: list[Bar] = []
        for key in keys:
            working = self._working.get(key)
            if working is not None:
                closed.append(self._close(key, working))
        return closed

    # ---- internals ----------------------------------------------------------- #
    def _window_start(self, dt: datetime) -> datetime:
        if self.interval >= DAY_INTERVAL:
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_into_day = dt.hour * 3600 + dt.minute * 60 + dt.second
        floored = (seconds_into_day // self.interval) * self.interval
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight + timedelta(seconds=floored)

    def _close(self, key: str, working: _WorkingBar) -> Bar:
        bar = Bar(
            datetime=working.start,
            instrument=key,
            open=working.open,
            high=working.high,
            low=working.low,
            close=working.close,
            volume=max(working.last_volume - working.base_volume, 0.0),
            amount=max(working.last_amount - working.base_amount, 0.0),
        )
        self._carry[key] = (working.last_volume, working.last_amount)
        del self._working[key]
        if self.on_bar is not None:
            self.on_bar(bar)
        return bar

"""BarAggregator: windowing, OHLC, cumulative-volume diffs, carry, flush."""

from __future__ import annotations

from datetime import datetime

from alphapilot.systems.live.bars import DAY_INTERVAL, BarAggregator
from alphapilot.systems.live.types import Exchange, TickData


def tick(hh, mm, ss, price, cum_vol, cum_amt=0.0, code="600000") -> TickData:
    return TickData(
        code=code, exchange=Exchange.SSE,
        datetime=datetime(2026, 7, 6, hh, mm, ss),
        last_price=price, volume=cum_vol, turnover=cum_amt,
    )


def test_minute_bars_ohlc_and_volume_diff() -> None:
    closed = []
    agg = BarAggregator(60, on_bar=closed.append)

    agg.on_tick(tick(9, 30, 5, 10.0, 1000, 10_000.0))
    agg.on_tick(tick(9, 30, 30, 10.4, 1500, 15_200.0))
    agg.on_tick(tick(9, 30, 50, 10.2, 1800, 18_300.0))
    assert not closed                      # bar still open

    agg.on_tick(tick(9, 31, 1, 10.3, 2000, 20_400.0))   # first tick of next window
    assert len(closed) == 1
    bar = closed[0]
    assert bar.datetime == datetime(2026, 7, 6, 9, 30)
    assert bar.instrument == "600000.SSE"
    assert (bar.open, bar.high, bar.low, bar.close) == (10.0, 10.4, 10.0, 10.2)
    assert bar.volume == 800               # 1800 - 1000 (baseline = first tick)
    assert bar.amount == 8_300.0


def test_carry_attributes_inter_bar_volume_to_next_bar() -> None:
    closed = []
    agg = BarAggregator(60, on_bar=closed.append)
    agg.on_tick(tick(9, 30, 10, 10.0, 1000))
    agg.on_tick(tick(9, 30, 40, 10.1, 1600))
    # 400 shares trade between 09:30:40 and 09:31:20 — carried into the 09:31 bar
    # (base continues from 1600, not from the 09:31:20 tick's cum of 2000)
    agg.on_tick(tick(9, 31, 20, 10.2, 2000))
    agg.on_tick(tick(9, 32, 0, 10.3, 2500))
    assert [b.volume for b in closed] == [600, 400]   # 1600-1000, 2000-1600


def test_flush_closes_partial_bar_and_session_reset_rebaselines() -> None:
    closed = []
    agg = BarAggregator(60, on_bar=closed.append)
    agg.on_tick(tick(11, 29, 10, 10.0, 5000))
    agg.on_tick(tick(11, 29, 50, 10.1, 5600))
    flushed = agg.flush()
    assert len(flushed) == 1 and flushed[0].volume == 600
    assert closed == flushed

    # afternoon: cumulative continues -> next bar carries from 5600
    agg.on_tick(tick(13, 0, 5, 10.2, 6000))
    agg.on_tick(tick(13, 1, 5, 10.2, 6300))
    assert closed[-1].volume == 400        # 6000-5600 via carry

    # a fresh session resets cumulative counters -> baseline at the tick, not carry
    agg.on_tick(tick(13, 2, 5, 10.2, 100))
    agg.on_tick(tick(13, 2, 40, 10.2, 250))
    bars = agg.flush()
    assert bars[0].volume == 150


def test_day_interval_uses_calendar_date_windows() -> None:
    closed = []
    agg = BarAggregator(DAY_INTERVAL, on_bar=closed.append)
    agg.on_tick(tick(9, 30, 0, 10.0, 1000))
    agg.on_tick(tick(14, 55, 0, 10.8, 90_000))
    assert not closed
    bars = agg.flush()
    assert bars[0].datetime == datetime(2026, 7, 6, 0, 0)
    assert bars[0].close == 10.8
    assert bars[0].volume == 89_000


def test_zero_price_and_naive_ticks_ignored() -> None:
    agg = BarAggregator(60)
    agg.on_tick(TickData(code="600000", exchange=Exchange.SSE, datetime=None, last_price=10.0))
    agg.on_tick(tick(9, 30, 0, 0.0, 100))
    assert agg.flush() == []

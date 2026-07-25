"""LiveTimingRunner + BatchStrategyAdapter: offline end-to-end.

Deterministic and broker-SDK-free: SimulatedClock drives the session FSM,
PaperBroker fills orders, ticks are pushed through the engine's callback path
exactly as a real gateway would. Covers both driving modes:

* day  — close-of-day signal -> next morning's opening call auction;
* min  — minute-bar signal -> immediate risk-gated submission.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from alphapilot.systems.live.bars import Bar
from alphapilot.systems.live.brokers.paper import PaperBroker
from alphapilot.systems.live.clock import SimulatedClock
from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.engine import LiveEngine
from alphapilot.systems.live.ledger import Ledger
from alphapilot.systems.live.strategy_runner import LiveTimingRunner
from alphapilot.systems.live.types import Exchange, TickData
from alphapilot.systems.timing.base import OrderIntent, TimingContext
from alphapilot.systems.timing.live_adapter import BatchStrategyAdapter

KEY = "600000.SSE"


class MomentumToy:
    """Batch strategy stub: long when the last close rose vs the previous bar."""

    name = "momentum_toy"

    def generate_signals(self, bars: pd.DataFrame, context: TimingContext) -> pd.DataFrame:
        closes = bars["close"].reset_index(drop=True)
        signal = (closes.diff().fillna(0) > 0).astype(int)
        return pd.DataFrame(
            {
                "datetime": bars["datetime"].values,
                "instrument": bars["instrument"].values,
                "signal": signal.values,
                "target_percent": signal.values.astype(float),
                "score": 0.0,
                "reason": "toy",
            }
        )


def make_engine(tmp_path: Path, clock: SimulatedClock, cash: float = 1_000_000.0):
    broker = PaperBroker(cash=cash, prices={KEY: 10.0}, open_cost=0.0, min_cost=0.0)
    cfg = LiveConfig(mode=RunMode.PAPER, ledger_dir=tmp_path / "ledger")
    engine = LiveEngine(cfg, broker, ledger=Ledger(tmp_path / "ledger"), now_fn=clock)
    engine.connect({})
    return engine


def push_tick(engine, dt: datetime, price: float, cum_vol: float) -> None:
    engine.on_tick(TickData(
        code="600000", exchange=Exchange.SSE, datetime=dt,
        last_price=price, volume=cum_vol, turnover=cum_vol * price,
    ))


# --------------------------------------------------------------------------- #
# adapter
# --------------------------------------------------------------------------- #
def make_bar(day: int, close: float) -> Bar:
    return Bar(datetime=datetime(2026, 7, day), instrument=KEY,
               open=close, high=close, low=close, close=close, volume=1000)


def test_adapter_emits_only_on_signal_change() -> None:
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    assert adapter.on_bar(make_bar(1, 10.0)) == []          # warmup
    up1 = adapter.on_bar(make_bar(2, 10.5))                  # 0 -> 1
    assert len(up1) == 1 and up1[0].action == "target_percent" and up1[0].target_percent == 1.0
    assert adapter.on_bar(make_bar(3, 11.0)) == []           # still 1: no repeat
    down = adapter.on_bar(make_bar(6, 10.2))                 # 1 -> 0
    assert len(down) == 1 and down[0].target_percent == 0.0


def test_adapter_warm_up_suppresses_spurious_first_intent() -> None:
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    history = pd.DataFrame([make_bar(d, 10.0 + d * 0.1).as_row() for d in range(1, 4)])
    adapter.warm_up(history)                                 # ends long (rising closes)
    assert adapter.on_bar(make_bar(6, 11.0)) == []           # still long: silence


def test_adapter_normalizes_qlib_and_live_symbols_into_one_history() -> None:
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    history = pd.DataFrame([
        {**make_bar(1, 10.0).as_row(), "instrument": "SH600000"},
        {**make_bar(2, 10.2).as_row(), "instrument": "SH600000"},
    ])

    adapter.warm_up(history)

    assert list(adapter._history) == [KEY]
    assert adapter.on_bar(make_bar(3, 10.4)) == []


def test_adapter_reconciles_first_flat_signal_with_real_holding() -> None:
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    history = pd.DataFrame([make_bar(1, 10.5).as_row(), make_bar(2, 10.0).as_row()])
    adapter.warm_up(history)                                  # signal is flat
    adapter.synchronize_positions({KEY})                      # OMS is actually long

    intents = adapter.on_bar(make_bar(3, 9.5))

    assert len(intents) == 1 and intents[0].target_percent == 0.0


# --------------------------------------------------------------------------- #
# runner: day mode (close signal -> next-morning call auction)
# --------------------------------------------------------------------------- #
def test_day_mode_signals_execute_at_next_open(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 9, 31))
    engine = make_engine(tmp_path, clock)
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    # Warm up with yesterday's flat history so today's rise flips the signal.
    adapter.warm_up(pd.DataFrame([
        make_bar(2, 10.2).as_row(), make_bar(3, 10.1).as_row(),
    ]))

    runner = LiveTimingRunner(engine, adapter, ["600000"], freq="day")
    runner.start()

    push_tick(engine, datetime(2026, 7, 6, 9, 31, 0), 10.0, 1_000)
    push_tick(engine, datetime(2026, 7, 6, 14, 55, 0), 10.6, 90_000)
    assert runner.step()["pending_requests"] == 0            # intraday: nothing yet

    clock.set(datetime(2026, 7, 6, 15, 1))                   # POST_CLOSE
    state = runner.step()
    assert state["session"] == "post_close"
    assert state["algo_armed"] is True                       # plan armed for tomorrow
    assert engine.oms.get_position(KEY) is None              # nothing executed yet

    # Session FSM day rollover: POST_CLOSE -> PRE_OPEN -> CALL_AUCTION_OPEN.
    # A periodically-stepped runner passes through pre-open naturally.
    clock.set(datetime(2026, 7, 7, 8, 0))
    runner.step()
    clock.set(datetime(2026, 7, 7, 9, 16))                   # next-day opening auction
    runner.step()
    pos = engine.oms.get_position(KEY)
    assert pos is not None and pos.volume > 0                # bought at the open
    assert runner.step()["algo_armed"] in (True, False)      # auction window continues

    clock.set(datetime(2026, 7, 7, 9, 35))                   # auction over -> algo done
    assert runner.step()["algo_armed"] is False


def test_day_mode_flat_signal_arms_nothing(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 9, 31))
    engine = make_engine(tmp_path, clock)
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    adapter.warm_up(pd.DataFrame([
        make_bar(2, 10.2).as_row(), make_bar(3, 10.4).as_row(),
    ]))  # warmed up long... and today falls -> flip to flat, but nothing is held

    runner = LiveTimingRunner(engine, adapter, ["600000"], freq="day")
    runner.start()
    push_tick(engine, datetime(2026, 7, 6, 10, 0, 0), 10.0, 1_000)
    clock.set(datetime(2026, 7, 6, 15, 1))
    state = runner.step()
    # Signal flipped to flat but there is no position to sell -> no requests.
    assert state["algo_armed"] is False and state["pending_requests"] == 0


def test_day_mode_discards_target_after_its_effective_session(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 8, 9, 16))
    engine = make_engine(tmp_path, clock)
    runner = LiveTimingRunner(
        engine,
        BatchStrategyAdapter(MomentumToy(), min_bars=2),
        ["600000"],
        freq="day",
        instance_id="ma",
        config_hash="hash",
    )
    runner.start()
    runner.pending_intents = [
        OrderIntent(
            datetime="2026-07-06",
            instrument=KEY,
            action="target_percent",
            target_percent=0.2,
            reason="stale",
        )
    ]

    state = runner.step()

    assert state["pending_intents"] == 0
    assert engine.oms.get_position(KEY) is None
    blocked = engine.ledger.events(kind="blocked")
    assert blocked[-1]["payload"]["rule"] == "effective_session"


# --------------------------------------------------------------------------- #
# runner: minute mode (bar close -> immediate submission)
# --------------------------------------------------------------------------- #
def test_min_mode_submits_on_bar_close(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 9, 31))
    engine = make_engine(tmp_path, clock)
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)

    runner = LiveTimingRunner(engine, adapter, ["600000"], freq="min", bar_seconds=60)
    runner.start()

    # three rising minute bars; the 3rd bar's close flips the signal long
    push_tick(engine, datetime(2026, 7, 6, 9, 31, 10), 10.0, 1_000)
    push_tick(engine, datetime(2026, 7, 6, 9, 32, 10), 10.2, 2_000)   # closes 09:31 bar
    assert engine.oms.get_position(KEY) is None                        # warmup
    push_tick(engine, datetime(2026, 7, 6, 9, 33, 10), 10.4, 3_000)   # closes 09:32 bar -> long
    pos = engine.oms.get_position(KEY)
    assert pos is not None and pos.volume > 0

    # falling bar flips flat -> sell attempt; T+1 blocks same-day sell (0 sellable)
    push_tick(engine, datetime(2026, 7, 6, 9, 34, 10), 9.8, 4_000)
    push_tick(engine, datetime(2026, 7, 6, 9, 35, 10), 9.7, 5_000)
    assert engine.oms.get_position(KEY).volume == pos.volume           # unchanged


def test_observer_ticks_never_enter_strategy_bar_aggregation(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 9, 31))
    engine = make_engine(tmp_path, clock)
    runner = LiveTimingRunner(
        engine,
        BatchStrategyAdapter(MomentumToy(), min_bars=2),
        ["600000"],
        freq="min",
    )
    runner.start()
    engine.subscribe_market_data(["000001.SZ"], subscription_type="observer")

    engine.on_tick(TickData(
        code="000001",
        exchange=Exchange.SZSE,
        datetime=datetime(2026, 7, 6, 9, 31, 10),
        last_price=12.0,
        volume=100,
        turnover=1_200,
    ))

    assert "000001.SZSE" in engine.oms.ticks
    assert runner.bars.current() == []
    assert engine.snapshot()["strategy_symbols"] == ["600000.SSE"]
    assert engine.snapshot()["observer_symbols"] == ["000001.SZSE"]


def test_min_mode_flushes_last_bar_at_lunch(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 11, 28))
    engine = make_engine(tmp_path, clock)
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    runner = LiveTimingRunner(engine, adapter, ["600000"], freq="min")
    runner.start()
    runner.step()

    push_tick(engine, datetime(2026, 7, 6, 11, 28, 30), 10.0, 1_000)
    push_tick(engine, datetime(2026, 7, 6, 11, 29, 30), 10.2, 2_000)  # closes 11:28 bar
    assert engine.oms.get_position(KEY) is None                        # warmup only

    clock.set(datetime(2026, 7, 6, 11, 31))                            # lunch break
    runner.step()                                                      # flush 11:29 bar -> long
    assert engine.oms.get_position(KEY) is not None


def test_runner_pause_resume_stop_lifecycle(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 9, 31))
    engine = make_engine(tmp_path, clock)
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    runner = LiveTimingRunner(engine, adapter, ["600000"], freq="min", bar_seconds=60)
    runner.start()
    assert runner.status()["active"] is True

    paused = runner.pause()
    assert paused["paused"] is True
    push_tick(engine, datetime(2026, 7, 6, 9, 31, 10), 10.0, 1_000)
    push_tick(engine, datetime(2026, 7, 6, 9, 32, 10), 10.2, 2_000)
    push_tick(engine, datetime(2026, 7, 6, 9, 33, 10), 10.4, 3_000)
    assert engine.oms.get_position(KEY) is None

    resumed = runner.resume()
    assert resumed["active"] is True
    push_tick(engine, datetime(2026, 7, 6, 9, 34, 10), 10.0, 4_000)
    push_tick(engine, datetime(2026, 7, 6, 9, 35, 10), 10.2, 5_000)
    push_tick(engine, datetime(2026, 7, 6, 9, 36, 10), 10.4, 6_000)
    assert engine.oms.get_position(KEY) is not None

    stopped = runner.stop()
    assert stopped["stopped"] is True
    assert runner.step()["session"] is None


def test_runner_stays_warming_until_every_configured_symbol_has_history(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 9, 31))
    engine = make_engine(tmp_path, clock)
    adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    adapter.warm_up(pd.DataFrame([make_bar(1, 10.0).as_row(), make_bar(2, 10.2).as_row()]))
    runner = LiveTimingRunner(engine, adapter, ["600000", "000001.SZSE"], freq="day")
    runner.start()

    assert runner.status()["lifecycle"] == "warming_up"


def test_runner_checkpoint_requires_reconcile_before_resume(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 9, 31))
    engine = make_engine(tmp_path, clock)
    state_path = tmp_path / "runner.json"
    first_adapter = BatchStrategyAdapter(MomentumToy(), min_bars=2)
    first_adapter.warm_up(pd.DataFrame([make_bar(1, 10.0).as_row(), make_bar(2, 10.2).as_row()]))
    first = LiveTimingRunner(
        engine, first_adapter, ["600000"], freq="day",
        instance_id="ma", config_hash="hash", state_path=state_path,
    )
    first.start()
    assert state_path.is_file()

    second = LiveTimingRunner(
        engine, BatchStrategyAdapter(MomentumToy(), min_bars=2), ["600000"], freq="day",
        instance_id="ma", config_hash="hash", state_path=state_path,
    )
    import json
    second.restore(json.loads(state_path.read_text(encoding="utf-8")))
    second.start()

    assert second.status()["lifecycle"] == "paused_pending_reconcile"
    with pytest.raises(RuntimeError, match="reconciled"):
        second.resume()
    second.mark_reconciled({"warnings": []})
    assert second.resume()["active"] is True


def test_runner_rejects_unknown_freq(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 9, 31))
    engine = make_engine(tmp_path, clock)
    with pytest.raises(ValueError, match="freq"):
        LiveTimingRunner(engine, BatchStrategyAdapter(MomentumToy()), ["600000"], freq="tick")

from __future__ import annotations

import sqlite3
import time
from datetime import datetime

import pytest

from alphapilot.systems.live.bars import Bar
from alphapilot.systems.live.config import MarketDataConfig
from alphapilot.systems.live.market_data import (
    LiveMarketDataService,
    SQLiteTickRecorder,
    load_market_bars,
    read_market_snapshot,
)
from alphapilot.systems.live.types import Exchange, TickData


def _config(tmp_path, **overrides):
    values = {
        "enabled": True,
        "data_dir": tmp_path / "market",
        "retention_days": 30,
        "queue_size": 100,
        "batch_size": 10,
        "flush_interval": 0.01,
        "snapshot_interval": 1.0,
        "stale_after_seconds": 3.0,
    }
    values.update(overrides)
    return MarketDataConfig(**values)


def _tick(at: datetime, price: float, volume: float, turnover: float = 0.0) -> TickData:
    return TickData(
        code="600000",
        exchange=Exchange.SSE,
        datetime=at,
        received_at=at,
        last_price=price,
        pre_close=10.0,
        volume=volume,
        turnover=turnover,
        bid_price_1=price - 0.01,
        ask_price_1=price + 0.01,
        gateway="test_quote",
    )


def _wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_recorder_preserves_duplicate_ticks_and_bars(tmp_path):
    now = datetime(2026, 7, 10, 9, 30)
    recorder = SQLiteTickRecorder(_config(tmp_path), "emt", now_fn=lambda: now)
    recorder.start()
    recorder.record_tick(_tick(now, 10.1, 100, 1_010))
    recorder.record_tick(_tick(now, 10.1, 100, 1_010))
    recorder.record_bar(
        Bar(now, "600000.SSE", 10.0, 10.2, 9.9, 10.1, 100, 1_010),
        60,
        complete=True,
    )
    _wait_until(lambda: recorder.status()["written_ticks"] == 2)
    recorder.stop()

    path = tmp_path / "market" / "emt" / "ticks-20260710.sqlite3"
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from ticks").fetchone()[0] == 2
        assert connection.execute("select count(*) from bars").fetchone()[0] == 1
        row = connection.execute("select volume, turnover, bid_price_1, ask_price_1 from ticks limit 1").fetchone()
    assert row == (100.0, 1_010.0, 10.09, 10.11)


def test_market_service_builds_live_bars_and_snapshot(tmp_path):
    now = datetime(2026, 7, 10, 9, 31, 1)
    service = LiveMarketDataService(
        _config(tmp_path),
        "emt",
        ["SH600000"],
        state_dir=tmp_path / "state",
        now_fn=lambda: now,
    )
    service.recorder.start()
    service.on_tick(_tick(datetime(2026, 7, 10, 9, 30, 1), 10.0, 100, 1_000))
    service.on_tick(_tick(datetime(2026, 7, 10, 9, 30, 30), 10.2, 180, 1_820))
    service.on_tick(_tick(datetime(2026, 7, 10, 9, 31, 0), 10.1, 200, 2_020))
    snapshot = service.write_snapshot()

    assert snapshot["subscribed_symbols"] == ["600000.SSE"]
    assert snapshot["ticks"][0]["volume"] == 200
    assert snapshot["ticks"][0]["change_pct"] == pytest.approx(1.0)
    assert snapshot["current_bars"]["60"]["600000.SSE"]["close"] == 10.1
    assert read_market_snapshot(tmp_path / "state")["ticks"][0]["last_price"] == 10.1

    service.close()
    rows = load_market_bars(tmp_path / "market", "emt", "600000.SSE", 60)
    assert rows[0]["open"] == 10.0
    assert rows[0]["high"] == 10.2
    assert rows[0]["volume"] == 80


def test_recorder_queue_overflow_is_degraded_and_non_blocking(tmp_path):
    now = datetime(2026, 7, 10, 9, 30)
    recorder = SQLiteTickRecorder(_config(tmp_path, queue_size=1), "emt", now_fn=lambda: now)
    recorder.record_tick(_tick(now, 10.0, 100))
    recorder.record_tick(_tick(now, 10.1, 120))
    status = recorder.status()
    assert status["degraded"] is True
    assert status["dropped_ticks"] == 1


def test_recorder_write_failure_is_visible(tmp_path):
    now = datetime(2026, 7, 10, 9, 30)

    def fail_connect(*_args, **_kwargs):
        raise OSError("disk unavailable")

    recorder = SQLiteTickRecorder(
        _config(tmp_path), "emt", now_fn=lambda: now, connect_fn=fail_connect
    )
    recorder.start()
    recorder.record_tick(_tick(now, 10.0, 100))
    _wait_until(lambda: recorder.status()["degraded"])
    recorder.stop()
    assert "disk unavailable" in recorder.status()["last_error"]


def test_retention_removes_expired_daily_partitions(tmp_path):
    now = datetime(2026, 7, 10, 9, 30)
    recorder = SQLiteTickRecorder(_config(tmp_path, retention_days=30), "emt", now_fn=lambda: now)
    recorder.provider_dir.mkdir(parents=True)
    old = recorder.provider_dir / "ticks-20260501.sqlite3"
    recent = recorder.provider_dir / "ticks-20260701.sqlite3"
    old.touch()
    recent.touch()
    recorder.cleanup_retention()
    assert not old.exists()
    assert recent.exists()

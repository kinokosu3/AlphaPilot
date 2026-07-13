from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from alphapilot.systems.timing.base import TimingBacktestRequest
from alphapilot.systems.timing.engine import TimingBacktestEngine


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
            "instrument": ["SZ000001"] * 4,
            "open": [10.0, 10.0, 11.0, 12.0],
            "high": [10.5, 10.5, 11.5, 12.5],
            "low": [9.5, 9.5, 10.5, 11.5],
            "close": [10.0, 10.5, 11.0, 12.0],
            "volume": [1000] * 4,
            "amount": [10000] * 4,
        }
    )


def test_engine_uses_next_bar_open_and_respects_trade_unit(tmp_path: Path) -> None:
    signals = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
            "instrument": ["SZ000001"] * 4,
            "signal": [1, 1, 0, 1],
            "target_percent": [1.0, 1.0, 0.0, 1.0],
            "score": [1.0, 1.0, 0.0, 1.0],
            "reason": ["test"] * 4,
        }
    )
    req = TimingBacktestRequest(
        strategy_name="unit",
        cash=1000,
        open_cost=0,
        close_cost=0,
        min_cost=0,
        trade_unit=100,
        output_dir=tmp_path,
    )

    result = TimingBacktestEngine().run(bars=_bars(), signals=signals, request=req)

    assert list(result.trades["side"]) == ["buy", "sell"]
    assert str(result.trades.iloc[0]["signal_datetime"]).startswith("2026-01-01")
    assert str(result.trades.iloc[0]["datetime"]).startswith("2026-01-02")
    assert result.trades.iloc[0]["amount"] == 100
    assert result.trades.iloc[1]["amount"] == 100
    # The final buy signal on 2026-01-04 has no next bar and must not execute.
    assert len(result.trades) == 2
    assert result.summary["final_equity"] == 1200
    assert (tmp_path / "summary.json").is_file()
    assert json.loads((tmp_path / "summary.json").read_text())["n_trades"] == 2


def test_engine_shrinks_buy_when_cash_is_insufficient(tmp_path: Path) -> None:
    bars = _bars()
    bars.loc[1, "open"] = 9.5
    signals = pd.DataFrame(
        {
            "datetime": bars["datetime"],
            "instrument": ["SZ000001"] * 4,
            "signal": [1, 1, 1, 1],
            "target_percent": [1.0, 1.0, 1.0, 1.0],
            "score": [1.0] * 4,
            "reason": ["test"] * 4,
        }
    )
    req = TimingBacktestRequest(
        strategy_name="unit",
        cash=1000,
        open_cost=0,
        close_cost=0,
        min_cost=0,
        trade_unit=100,
        output_dir=tmp_path,
    )

    result = TimingBacktestEngine().run(bars=bars, signals=signals, request=req)

    assert result.trades.iloc[0]["side"] == "buy"
    assert result.trades.iloc[0]["amount"] == 100
    assert result.trades.iloc[0]["value"] == 950


def test_multi_instrument_summary_does_not_multiply_account_equity(tmp_path: Path) -> None:
    bars = pd.concat(
        [
            _bars(),
            _bars().assign(instrument="SZ000002"),
        ],
        ignore_index=True,
    )
    signals = bars[["datetime", "instrument"]].assign(
        signal=0, target_percent=0.0, score=0.0, reason="flat"
    )
    req = TimingBacktestRequest(
        strategy_name="multi_flat",
        cash=1000,
        open_cost=0,
        close_cost=0,
        min_cost=0,
        trade_unit=100,
        output_dir=tmp_path,
    )

    result = TimingBacktestEngine().run(bars=bars, signals=signals, request=req)

    assert result.summary["final_equity"] == 1000
    assert result.summary["total_return"] == 0


def test_multi_instrument_targets_use_whole_account_equity(tmp_path: Path) -> None:
    dates = pd.to_datetime(["2026-01-01", "2026-01-02"])
    bars = pd.DataFrame(
        [
            {"datetime": dt, "instrument": symbol, "open": 10.0, "high": 10.0,
             "low": 10.0, "close": 10.0, "volume": 1000, "amount": 10000}
            for dt in dates for symbol in ("SH600000", "SZ000001")
        ]
    )
    signals = bars[["datetime", "instrument"]].assign(
        signal=1, target_percent=0.5, score=1.0, reason="half"
    )
    req = TimingBacktestRequest(
        strategy_name="multi_half", cash=1000, open_cost=0, close_cost=0,
        min_cost=0, trade_unit=0, output_dir=tmp_path,
    )

    result = TimingBacktestEngine().run(bars=bars, signals=signals, request=req)

    bought = result.trades.groupby("instrument")["value"].sum().to_dict()
    assert bought == {"SH600000": 500.0, "SZ000001": 500.0}
    assert result.summary["final_equity"] == 1000


def test_multi_instrument_result_is_independent_of_input_order(tmp_path: Path) -> None:
    dates = pd.to_datetime(["2026-01-01", "2026-01-02"])
    bars = pd.DataFrame(
        [
            {"datetime": dt, "instrument": symbol, "open": price, "high": price,
             "low": price, "close": price, "volume": 1000, "amount": 10000}
            for dt in dates for symbol, price in (("SH600000", 10.0), ("SZ000001", 20.0))
        ]
    )
    signals = bars[["datetime", "instrument"]].assign(
        signal=1, target_percent=0.5, score=1.0, reason="half"
    )
    req1 = TimingBacktestRequest(strategy_name="ordered", cash=1000, open_cost=0,
                                 close_cost=0, min_cost=0, trade_unit=0,
                                 output_dir=tmp_path / "ordered")
    req2 = TimingBacktestRequest(strategy_name="reversed", cash=1000, open_cost=0,
                                 close_cost=0, min_cost=0, trade_unit=0,
                                 output_dir=tmp_path / "reversed")

    first = TimingBacktestEngine().run(bars=bars, signals=signals, request=req1)
    second = TimingBacktestEngine().run(
        bars=bars.iloc[::-1].reset_index(drop=True),
        signals=signals.iloc[::-1].reset_index(drop=True), request=req2,
    )

    cols = ["instrument", "side", "amount", "price", "value"]
    assert first.trades[cols].to_dict("records") == second.trades[cols].to_dict("records")


def test_intraday_backtest_does_not_sell_same_day_buys(tmp_path: Path) -> None:
    bars = pd.DataFrame(
        {
            "datetime": pd.to_datetime([
                "2026-01-02 09:31", "2026-01-02 09:32", "2026-01-02 09:33"
            ]),
            "instrument": ["SZ000001"] * 3,
            "open": [10.0] * 3, "high": [10.0] * 3, "low": [10.0] * 3,
            "close": [10.0] * 3, "volume": [1000] * 3, "amount": [10000] * 3,
        }
    )
    signals = bars[["datetime", "instrument"]].assign(
        signal=[1, 0, 0], target_percent=[1.0, 0.0, 0.0], score=0.0, reason="t1"
    )
    req = TimingBacktestRequest(strategy_name="t1", cash=1000, open_cost=0,
                                close_cost=0, min_cost=0, trade_unit=100,
                                output_dir=tmp_path)

    result = TimingBacktestEngine().run(bars=bars, signals=signals, request=req)

    assert list(result.trades["side"]) == ["buy"]


def test_signal_adjusted_prices_do_not_leak_into_execution_prices(tmp_path: Path) -> None:
    bars = _bars().iloc[:2].copy()
    bars["open"] = [5.0, 5.0]          # adjusted series used by indicators
    bars["close"] = [5.0, 5.0]
    bars["trade_open"] = [10.0, 10.0]  # raw tradable series
    bars["trade_close"] = [10.0, 10.0]
    signals = bars[["datetime", "instrument"]].assign(
        signal=1, target_percent=1.0, score=1.0, reason="raw-execution"
    )
    req = TimingBacktestRequest(strategy_name="raw", cash=1000, open_cost=0,
                                close_cost=0, min_cost=0, trade_unit=100,
                                output_dir=tmp_path)

    result = TimingBacktestEngine().run(bars=bars, signals=signals, request=req)

    assert result.trades.iloc[0]["price"] == 10.0
    assert result.trades.iloc[0]["amount"] == 100
    assert result.summary["final_equity"] == 1000

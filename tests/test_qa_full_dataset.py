"""Acceptance checks for the persistent 30-stock QA dataset.

This module intentionally does not download data.  The dataset builder and the
checks are separate so a failed assertion cannot destroy or partially rebuild
the evidence it is inspecting.  Point ``ALPHAPILOT_QA_ROOT`` at a run created
under ``git_ignore_folder/qa`` and run with ``pytest -m real_data``.

Unlike the historical mini-universe smoke tests, an opted-in run has no
``skip`` or tolerated Qlib/data-error path: missing or malformed artifacts are
test failures.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


pytestmark = [pytest.mark.real_data, pytest.mark.slow]

MARKET = "qa_stock_pool_30"
EXPECTED_SYMBOLS = {
    "sh600000", "sh600027", "sh600057", "sh600085", "sh600113",
    "sh600135", "sh600163", "sh600188", "sh600216", "sh600239",
    "sh600271", "sh600300", "sh600326", "sh600350", "sh600375",
    "sh600400", "sh600435", "sh600469", "sh600496", "sh600517",
    "sh600519", "sh600540", "sh600566", "sh600588", "sh600611",
    "sh600638", "sh600661", "sh600685", "sh600711", "sh600731",
}
CROSS_SOURCE_SYMBOLS = {"sh600000", "sh600085", "sh600188", "sh600588", "sh600711"}
MINUTE_SYMBOLS = {"sh600000", "sh600027", "sh600057"}


@pytest.fixture(scope="module")
def qa_root() -> Path:
    configured = os.getenv("ALPHAPILOT_QA_ROOT", "")
    assert configured, "ALPHAPILOT_QA_ROOT is required for the full QA acceptance suite"
    root = Path(configured).expanduser().resolve()
    assert root.is_dir(), f"QA run root does not exist: {root}"
    assert "git_ignore_folder/qa" in root.as_posix(), f"unsafe QA run root: {root}"
    return root


def _csv_symbols(directory: Path) -> set[str]:
    return {path.stem for path in directory.glob("*.csv")}


def _assert_bar_invariants(frame: pd.DataFrame, *, intraday: bool = False) -> None:
    required = {"date", "open", "high", "low", "close", "volume", "amount"}
    assert required <= set(frame.columns)
    assert not frame.empty
    dates = pd.to_datetime(frame["date"], errors="raise")
    assert dates.is_monotonic_increasing
    assert not dates.duplicated().any()
    numeric = frame[["open", "high", "low", "close", "volume", "amount"]].apply(
        pd.to_numeric, errors="raise"
    )
    # Baostock emits the last known price and blank volume/amount for suspended
    # sessions.  This is valid only when its explicit trade-status flag is 0.
    missing = numeric.isna().any(axis=1)
    if missing.any():
        assert "tradestatus" in frame
        assert (pd.to_numeric(frame.loc[missing, "tradestatus"], errors="raise") == 0).all()
        assert numeric.loc[missing, ["open", "high", "low", "close"]].notna().all().all()
        assert numeric.loc[missing, ["volume", "amount"]].isna().all().all()
    finite = numeric.loc[~missing]
    assert np.isfinite(finite.to_numpy()).all()
    assert (numeric[["open", "high", "low", "close"]] > 0).all().all()
    assert (finite[["volume", "amount"]] >= 0).all().all()
    assert (numeric["high"] >= numeric[["open", "close", "low"]].max(axis=1)).all()
    assert (numeric["low"] <= numeric[["open", "close", "high"]].min(axis=1)).all()
    if intraday:
        counts = pd.Series(1, index=dates).groupby(dates.dt.normalize()).sum()
        assert (counts > 1).all(), "each intraday session must contain multiple bars"


def test_daily_download_and_adjustment_are_complete(qa_root: Path) -> None:
    raw = qa_root / "raw_none"
    backward = qa_root / "raw_backward"
    factors = qa_root / "adjust_factors"
    assert _csv_symbols(raw) == EXPECTED_SYMBOLS
    assert _csv_symbols(backward) == EXPECTED_SYMBOLS
    assert _csv_symbols(factors) == EXPECTED_SYMBOLS

    for symbol in sorted(EXPECTED_SYMBOLS):
        unadjusted = pd.read_csv(raw / f"{symbol}.csv")
        adjusted = pd.read_csv(backward / f"{symbol}.csv")
        factor = pd.read_csv(factors / f"{symbol}.csv")
        _assert_bar_invariants(unadjusted)
        _assert_bar_invariants(adjusted)
        assert len(unadjusted) == len(adjusted) >= 4_000
        assert unadjusted["date"].iloc[0] <= "2009-01-05"
        assert unadjusted["date"].iloc[-1] >= "2026-07-09"
        assert not factor.empty


def test_daily_qlib_layout_and_market_contract(qa_root: Path) -> None:
    qlib = qa_root / "qlib"
    market = qlib / "instruments" / f"{MARKET}.txt"
    calendar = qlib / "calendars" / "day.txt"
    assert market.is_file() and calendar.is_file()
    rows = [line.split("\t") for line in market.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 30
    assert {row[0].lower() for row in rows} == EXPECTED_SYMBOLS
    assert all(len(row) == 3 and row[1] <= row[2] for row in rows)
    assert {path.name.lower() for path in (qlib / "features").iterdir()} >= EXPECTED_SYMBOLS
    assert len(calendar.read_text(encoding="utf-8").splitlines()) >= 4_000


def test_tushare_cross_source_prices_and_canonical_units(qa_root: Path) -> None:
    for symbol in sorted(CROSS_SOURCE_SYMBOLS):
        bao = pd.read_csv(qa_root / "raw_none" / f"{symbol}.csv")
        ts = pd.read_csv(qa_root / "tushare_raw_none" / f"{symbol}.csv")
        merged = bao.merge(ts, on="date", suffixes=("_bao", "_ts"))
        assert len(merged) >= 4_000
        sample = merged.tail(60)
        for field in ("open", "high", "low", "close"):
            left = sample[f"{field}_bao"].astype(float)
            right = sample[f"{field}_ts"].astype(float)
            tolerance = np.maximum(0.02, left.abs() * 0.001)
            assert ((left - right).abs() <= tolerance).all(), f"{symbol} {field} mismatch"
        for field in ("volume", "amount"):
            left = sample[f"{field}_bao"].astype(float)
            right = sample[f"{field}_ts"].astype(float)
            relative = (left - right).abs() / left.abs().clip(lower=1.0)
            assert (relative <= 0.01).all(), f"{symbol} {field} unit/value mismatch"


def test_minute_download_and_qlib_frequency_are_complete(qa_root: Path) -> None:
    raw = qa_root / "minute_raw_5min"
    qlib = qa_root / "minute_qlib_5min"
    assert _csv_symbols(raw) == MINUTE_SYMBOLS
    calendar = pd.to_datetime(
        (qlib / "calendars" / "5min.txt").read_text(encoding="utf-8").splitlines(),
        errors="raise",
    )
    assert len(calendar) == 240
    assert len(pd.Index(calendar.normalize()).unique()) == 5
    for symbol in sorted(MINUTE_SYMBOLS):
        frame = pd.read_csv(raw / f"{symbol}.csv")
        _assert_bar_invariants(frame, intraday=True)
        assert len(frame) == 240
        assert (qlib / "features" / symbol / "close.5min.bin").is_file()

    minute_ic_paths = list(
        (qa_root / "runs").glob("*qa_minute_3*/workspaces/*/factor_ic_leaderboard.csv")
    )
    assert minute_ic_paths, "5-minute single_ic result is missing"
    minute_ic = pd.read_csv(minute_ic_paths[-1])
    assert set(minute_ic["factor_name"]) == {"qa_minute_momentum", "qa_minute_volume_ratio"}
    assert minute_ic[["IC", "RankIC", "ICIR", "RankICIR"]].apply(np.isfinite).all().all()


def test_factor_cache_and_all_backtest_modes_have_finite_results(qa_root: Path) -> None:
    h5_files = sorted((qa_root / "factor_h5_cache").glob("*/all/daily_pv.h5"))
    assert h5_files
    latest_h5 = max(h5_files, key=lambda path: path.stat().st_mtime_ns)
    manifest = json.loads((latest_h5.parent.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["generator_version"] >= 2
    assert manifest["source_data_hash"]
    cached = pd.read_hdf(latest_h5)
    assert len(cached) >= 80_000
    instruments = cached.index.get_level_values(-1).astype(str).str.lower()
    assert set(instruments) == EXPECTED_SYMBOLS

    ic_tables = list((qa_root / "runs").glob("*/workspaces/*/factor_ic_leaderboard.csv"))
    assert ic_tables
    daily_ic = [pd.read_csv(path) for path in ic_tables if "qa_stock_pool_30" in path.parents[2].name]
    assert daily_ic and any(len(frame) == 3 for frame in daily_ic)

    combined = list((qa_root / "runs").glob("*/workspaces/*/qlib_res.csv"))
    assert combined and any(
        pd.read_csv(path).select_dtypes("number").apply(np.isfinite).all().all()
        for path in combined
    )

    sequential_paths = list((qa_root / "runs").glob("*/workspaces/*/factor_portfolio_leaderboard.csv"))
    assert sequential_paths
    sequential = pd.read_csv(sequential_paths[-1])
    assert set(sequential["factor_name"]) == {"qa_momentum", "qa_volatility", "qa_volume_ratio"}
    assert sequential.select_dtypes("number").apply(np.isfinite).all().all()

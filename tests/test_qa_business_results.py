"""Strong acceptance contracts for real mining, strategy, trading, and timing artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


pytestmark = [pytest.mark.real_data, pytest.mark.slow]


@pytest.fixture(scope="module")
def qa_root() -> Path:
    configured = os.getenv("ALPHAPILOT_QA_ROOT", "")
    assert configured, "ALPHAPILOT_QA_ROOT is required"
    root = Path(configured).expanduser().resolve()
    assert root.is_dir() and "git_ignore_folder/qa" in root.as_posix()
    return root


def _json(path: Path):  # noqa: ANN202
    return json.loads(path.read_text(encoding="utf-8"))


def test_gp_aff_and_rl_finished_with_one_stable_result_contract(qa_root: Path) -> None:
    expectations = {
        "gp_result.json": ("alphaforge_gp", 3),
        "aff_result.json": ("alphaforge_aff", 0),
        "rl_result.json": ("alphaforge_rl", 2),
    }
    for filename, (source, mined) in expectations.items():
        result = _json(qa_root / filename)
        assert result["source"] == source
        assert result["mined"] == mined
        assert isinstance(result["accepted"], list)
        assert isinstance(result["rejected"], list)
        assert result["n_accepted"] == len(result["accepted"])
        assert result["n_rejected"] == len(result["rejected"])
        assert result["mined"] == result["n_accepted"] + result["n_rejected"] + result["untranslatable"]


def test_strategy_retrain_reuse_and_daily_session_artifacts_are_complete(qa_root: Path) -> None:
    strategy = qa_root / "strategy_zoo" / "qa_combined_3"
    assert (strategy / "artifacts" / "fitted_model.pkl").is_file()
    successful: dict[str, tuple[Path, dict]] = {}
    for record_path in sorted((strategy / "retests").glob("*.json")):
        record = _json(record_path)
        details = record.get("details") or {}
        artifact_dir = details.get("artifacts_dir")
        if artifact_dir and details.get("artifact_files"):
            successful[record["mode"]] = (strategy / artifact_dir, record)
    assert set(successful) == {"retrain", "reuse_model"}
    required = {
        "daily_report.csv",
        "portfolio_summary.json",
        "qlib_metrics.csv",
        "daily_trades.csv",
        "daily_holdings.csv",
        "manifest.json",
    }
    for artifact_dir, record in successful.values():
        assert required <= {path.name for path in artifact_dir.iterdir() if path.is_file()}
        metrics = pd.Series(record["metrics"], dtype=float)
        assert np.isfinite(metrics.to_numpy()).all()

    session = qa_root / "trade_sessions" / "qa_daily_2d"
    history = sorted((session / "history").glob("*.json"))
    assert [path.stem for path in history] == ["2026-07-08", "2026-07-09"]
    assert (session / "cashflows.jsonl").is_file()
    daily = _json(qa_root / "daily_trade_results.json")
    assert daily["cash"]["delta"] == 50_000.0
    assert len(daily["results"]) == 2
    for result in daily["results"]:
        assert result["n_positions"] > 0
        for trade in result["trades"]:
            assert np.isfinite(float(trade["price"]))
            lots = float(trade["amount"]) / 100.0
            assert abs(lots - round(lots)) < 1e-9


def test_all_builtin_timing_strategies_have_finite_next_bar_lot_artifacts(qa_root: Path) -> None:
    expected = {
        "arbr_reversion",
        "aroon_trend",
        "boll_mean_reversion",
        "dual_ma",
        "kdj_cross",
        "rsi_reversion",
        "sma_filter",
        "stoch_rsi_reversion",
    }
    timing = _json(qa_root / "timing_results.json")
    assert set(timing) == expected
    for strategy in sorted(expected):
        artifact_dir = qa_root / "timing" / strategy
        assert {path.name for path in artifact_dir.iterdir() if path.is_file()} == {
            "summary.json", "equity_curve.csv", "trades.csv", "positions.csv", "signals.csv"
        }
        summary = _json(artifact_dir / "summary.json")
        for key in ("initial_cash", "final_equity", "total_return", "annual_return", "max_drawdown", "total_fee"):
            assert np.isfinite(float(summary[key]))
        trades = pd.read_csv(artifact_dir / "trades.csv")
        if not trades.empty:
            signal_time = pd.to_datetime(trades["signal_datetime"], errors="raise")
            fill_time = pd.to_datetime(trades["datetime"], errors="raise")
            assert (fill_time > signal_time).all()
            lots = trades["amount"].astype(float) / 100.0
            assert np.allclose(lots, lots.round())


def test_llm_round_one_completed_and_round_two_has_a_resume_boundary(qa_root: Path) -> None:
    sessions = [path for path in (qa_root / "logs").iterdir() if (path / "rounds" / "round_01").is_dir()]
    assert sessions
    session = max(sessions, key=lambda path: path.stat().st_mtime_ns)
    scoring = session / "rounds" / "round_01" / "04_backtest" / "scoring_model"
    assert (scoring / "fitted_model.pkl").is_file()
    metrics = pd.read_csv(scoring / "qlib_metrics.csv")
    assert metrics.select_dtypes("number").apply(np.isfinite).all().all()
    snapshots = session / "session_snapshots"
    assert (snapshots / "round_01" / "step_05_05_feedback" / "workflow.snapshot.pkl").is_file()
    assert (snapshots / "round_02" / "step_01_01_hypothesis" / "workflow.snapshot.pkl").is_file()
    accepted = [path for path in (qa_root / "strategy_zoo").iterdir() if path.name.startswith("mine_round_01_")]
    assert accepted and (accepted[-1] / "artifacts" / "fitted_model.pkl").is_file()

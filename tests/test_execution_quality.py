from __future__ import annotations

from pathlib import Path

import pandas as pd

from alphapilot.systems.research.execution_quality import (
    evaluate_implementation_shortfall,
)
from alphapilot.systems.trading.domain import StrategyInstanceConfig
from alphapilot.systems.trading.service import TradingStrategySystem
from alphapilot.systems.trading.store import StrategyRuntimeStore


def test_execution_quality_aggregates_partial_fills_by_order() -> None:
    fills = pd.DataFrame(
        [
            {"order_reference": "buy-1", "side": "buy", "arrival_price": 10.0, "fill_price": 10.01, "volume": 50},
            {"order_reference": "buy-1", "side": "buy", "arrival_price": 10.0, "fill_price": 10.02, "volume": 50},
            {"order_reference": "sell-1", "side": "sell", "arrival_price": 10.0, "fill_price": 9.99, "volume": 100},
        ]
    )

    result = evaluate_implementation_shortfall(fills)

    assert result["passed"] is True
    assert result["order_count"] == 2
    assert 10 < result["median_bp"] < 20
    assert result["p95_bp"] < 20


def test_execution_quality_fails_closed_above_p95_limit() -> None:
    fills = pd.DataFrame(
        [
            {"order_reference": f"order-{index}", "side": "buy", "arrival_price": 10.0, "fill_price": 10.01, "volume": 100}
            for index in range(9)
        ]
        + [
            {"order_reference": "outlier", "side": "buy", "arrival_price": 10.0, "fill_price": 10.20, "volume": 100}
        ]
    )

    result = evaluate_implementation_shortfall(fills)

    assert result["passed"] is False
    assert "p95_implementation_shortfall" in result["failures"]


def _live_system(path: Path) -> tuple[TradingStrategySystem, StrategyRuntimeStore, str]:
    store = StrategyRuntimeStore(path)
    instance = StrategyInstanceConfig(
        instance_id="live-quality",
        strategy_id="sma_filter",
        strategy_version="1.0.0",
        universe=("600000.SSE",),
        deployment_level="live",
    )
    store.create_instance(instance)
    system = object.__new__(TradingStrategySystem)
    system.store = store
    return system, store, instance.instance_id


def test_live_stage_overwrites_manually_entered_execution_summary(tmp_path: Path) -> None:
    system, store, instance_id = _live_system(tmp_path / "manual-summary.sqlite3")
    run = store.start_stage_run(instance_id, "live")

    finished = store.finish_stage_run(
        run["run_id"],
        trading_sessions=5,
        metrics={
            "execution_quality_order_count": 10,
            "median_implementation_shortfall_bp": 1.0,
            "p95_implementation_shortfall_bp": 2.0,
        },
    )

    assert finished["metrics"]["execution_quality_order_count"] == 0
    assert finished["metrics"]["median_implementation_shortfall_bp"] is None
    assert finished["metrics"]["execution_quality_source"] == "broker_fill_reconciliation"
    assert store.evaluate_stage(instance_id, "live", minimum_sessions=5)["passed"] is False


def test_live_stage_derives_quality_from_raw_fills_and_requires_sessions(
    tmp_path: Path,
) -> None:
    system, store, instance_id = _live_system(tmp_path / "raw-fills.sqlite3")
    run = store.start_stage_run(instance_id, "live")
    current = store.get_instance(instance_id)
    for day in range(1, 6):
        assert store.record_stage_session(
            instance_id,
            config_hash=current["config_hash"],
            stage="live",
            session=f"2026-07-{day:02d}",
        )
    for index in range(5):
        plan_id = f"plan-{index}"
        reference = f"order-{index}"
        store.save_execution_plan_state(
            plan_id,
            f"decision-{index}",
            instance_id,
            current["config_hash"],
            phase="completed",
            payload={},
        )
        store.record_child_order(
            reference,
            plan_id,
            {
                "reference": reference,
                "side": "buy",
                "price": 10.0,
                "volume": 100,
            },
            status="filled",
            order_id=f"broker-{index}",
        )
        store.record_fill_reconciliation(
            f"fill-{index}",
            plan_id,
            reference,
            order_id=f"broker-{index}",
            volume=100,
            price=10.005,
        )

    finished = store.finish_stage_run(
        run["run_id"],
        trading_sessions=5,
        metrics={
            # These values must be replaced by the raw-fill calculation.
            "execution_quality_order_count": 999,
            "median_implementation_shortfall_bp": 999.0,
            "p95_implementation_shortfall_bp": 999.0,
        },
    )

    assert finished["metrics"]["execution_quality_order_count"] == 5
    assert finished["metrics"]["median_implementation_shortfall_bp"] < 20
    assert finished["metrics"]["p95_implementation_shortfall_bp"] < 50
    assert finished["metrics"]["execution_quality_source"] == "broker_fill_reconciliation"
    assert len(finished["metrics"]["execution_quality_fingerprint"]) == 64
    evidence = store.evaluate_stage(instance_id, "live", minimum_sessions=5)
    assert evidence["passed"] is True
    assert evidence["trading_sessions"] == 5
    assert evidence["execution_quality"]["passed"] is True


def test_live_stage_gate_fails_without_execution_quality_evidence(tmp_path: Path) -> None:
    _, store, instance_id = _live_system(tmp_path / "missing-quality.sqlite3")
    run = store.start_stage_run(instance_id, "live")
    current = store.get_instance(instance_id)
    for day in range(1, 6):
        store.record_stage_session(
            instance_id,
            config_hash=current["config_hash"],
            stage="live",
            session=f"2026-07-{day:02d}",
        )
    store.finish_stage_run(run["run_id"], trading_sessions=5)

    evidence = store.evaluate_stage(instance_id, "live", minimum_sessions=5)
    assert evidence["passed"] is False
    assert evidence["failures"]["execution_quality_breaches"] == 1

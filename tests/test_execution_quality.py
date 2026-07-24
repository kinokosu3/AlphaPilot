from __future__ import annotations

from pathlib import Path

import pandas as pd

from alphapilot.systems.research.execution_quality import (
    evaluate_implementation_shortfall,
)
from alphapilot.systems.trading.domain import DeploymentSpec, StrategyInstanceConfig
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


def _live_store(path: Path) -> tuple[StrategyRuntimeStore, str]:
    store = StrategyRuntimeStore(path)
    instance = StrategyInstanceConfig(
        instance_id="live-quality",
        strategy_id="sma_filter",
        strategy_version="1.0.0",
        universe=("600000.SSE",),
    )
    store.create_instance(instance)
    store.set_validation_state(instance.instance_id, "validated")
    store.configure_deployment(DeploymentSpec(
        instance_id=instance.instance_id,
        config_hash=instance.config_hash,
        run_mode="live",
        execution_environment="live",
        trade_provider="xtp",
        quote_provider="xtp",
        account_id="quality-account",
        quote_data_kind="realtime",
    ))
    return store, instance.instance_id


def test_live_run_overwrites_manually_entered_execution_summary(tmp_path: Path) -> None:
    store, instance_id = _live_store(tmp_path / "manual-summary.sqlite3")
    run = store.start_runtime_run(instance_id, "live")

    finished = store.finish_runtime_run(
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
    assert store.runtime_diagnostics(instance_id)["modes"]["live"]["execution_quality"][0][
        "order_count"
    ] == 0


def test_live_run_derives_quality_from_raw_fills_and_records_sessions(
    tmp_path: Path,
) -> None:
    store, instance_id = _live_store(tmp_path / "raw-fills.sqlite3")
    run = store.start_runtime_run(instance_id, "live")
    current = store.get_instance(instance_id)
    for day in range(1, 6):
        assert store.record_runtime_session(
            instance_id,
            config_hash=current["config_hash"],
            run_mode="live",
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

    finished = store.finish_runtime_run(
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
    diagnostics = store.runtime_diagnostics(instance_id)
    assert diagnostics["modes"]["live"]["trading_sessions"] == 5
    assert diagnostics["modes"]["live"]["execution_quality"][0]["order_count"] == 5


def test_live_diagnostics_do_not_turn_missing_quality_into_a_deployment_gate(tmp_path: Path) -> None:
    store, instance_id = _live_store(tmp_path / "missing-quality.sqlite3")
    run = store.start_runtime_run(instance_id, "live")
    current = store.get_instance(instance_id)
    for day in range(1, 6):
        store.record_runtime_session(
            instance_id,
            config_hash=current["config_hash"],
            run_mode="live",
            session=f"2026-07-{day:02d}",
        )
    store.finish_runtime_run(run["run_id"], trading_sessions=5)

    diagnostics = store.runtime_diagnostics(instance_id)
    assert diagnostics["modes"]["live"]["trading_sessions"] == 5
    assert diagnostics["modes"]["live"]["execution_quality"][0]["order_count"] == 0
    assert store.get_deployment_spec(instance_id)["stale"] is False

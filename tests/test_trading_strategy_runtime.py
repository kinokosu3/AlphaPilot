from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alphapilot.systems.live.oms import OMS
from alphapilot.systems.live.planner import ExecutionPlanner
from alphapilot.systems.live.targets import TargetPortfolio
from alphapilot.systems.live.types import (
    Account, Direction, Exchange, Order, OrderStatus, Position,
)
from alphapilot.systems.trading.domain import StrategyInstanceConfig
from alphapilot.systems.trading.registry import StrategyRegistry
from alphapilot.systems.trading.store import StrategyRuntimeStore


def _oms() -> OMS:
    oms = OMS()
    oms.on_account(Account("acc", balance=1_000_000, available=1_000_000))
    oms.on_position(Position("600000", Exchange.SSE, volume=1000, yd_volume=1000))
    return oms


def test_builtin_registry_exposes_schema_and_creates_parameterized_instances() -> None:
    registry = StrategyRegistry(local_root="/does-not-exist").discover()
    definition = registry.get("dual_ma")

    assert definition.required_history == 21
    assert definition.parameter_schema["properties"]["short_window"]["default"] == 5
    assert registry.create("dual_ma", {"short_window": 10, "long_window": 30}).params["long_window"] == 30
    with pytest.raises(ValueError, match="less than"):
        registry.create("dual_ma", {"short_window": 30, "long_window": 10})


def test_local_manifest_is_explicit_and_duplicate_is_quarantined(tmp_path: Path) -> None:
    local = tmp_path / "strategies" / "custom"
    local.mkdir(parents=True)
    (local / "strategy.py").write_text(
        "class Demo:\n    def __init__(self, window=3): self.window=window\n",
        encoding="utf-8",
    )
    (local / "strategy.toml").write_text(
        """
[strategy]
id = "custom_demo"
version = "0.1.0"
factory = "strategy:Demo"
required_history = 4
parameter_schema_json = '''{"type":"object","properties":{"window":{"type":"integer","default":3}},"additionalProperties":false}'''
""",
        encoding="utf-8",
    )
    duplicate = tmp_path / "strategies" / "duplicate"
    duplicate.mkdir()
    (duplicate / "strategy.toml").write_text(
        "[strategy]\nid='dual_ma'\nversion='9.9.9'\nfactory='strategy:Demo'\n",
        encoding="utf-8",
    )

    registry = StrategyRegistry(local_root=tmp_path / "strategies").discover()

    assert registry.get("custom_demo").code_hash
    assert registry.create("custom_demo", {"window": 7}, isolated=False).window == 7
    assert any(item["strategy_id"] == "dual_ma" for item in registry.quarantined())


def test_execution_planner_counts_working_orders_and_splits_large_orders() -> None:
    oms = _oms()
    oms.on_order(Order(
        order_id="working", code="600000", exchange=Exchange.SSE,
        direction=Direction.LONG, volume=500, traded=100,
        price=10.0, status=OrderStatus.PARTTRADED, reference="old",
    ))
    target = TargetPortfolio(
        date="2026-07-12", holdings={"SH600000": 2500}, prices={"SH600000": 10.0},
        instance_id="ma_5_20", config_hash="abc", decision_id="decision-1",
    )

    plan = ExecutionPlanner(lot_size=100, max_order_value=5_000).plan(target, oms)

    assert plan.ok
    assert [request.volume for request in plan.requests] == [500, 500, 100]
    assert len({request.reference for request in plan.requests}) == 3
    assert all("decision-1" in request.reference for request in plan.requests)


def test_runtime_store_enforces_stage_evidence_and_single_live_writer(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    for instance_id in ("a", "b"):
        store.create_instance(StrategyInstanceConfig(
            instance_id, "dual_ma", "1.0.0", {"short_window": 5, "long_window": 20},
            ("SH600000",),
        ))

    with pytest.raises(ValueError, match="evidence"):
        store.promote("a", "paper")
    store.record_stage("a", "replay", passed=True)
    assert store.promote("a", "paper")["deployment_level"] == "paper"
    store.record_stage("a", "paper", passed=True)
    store.promote("a", "shadow")
    store.record_stage("a", "shadow", passed=True)
    assert store.promote("a", "live", account_id="acc", broker="paper", approval="ok")["deployment_level"] == "live"

    store.record_stage("b", "replay", passed=True)
    store.promote("b", "paper")
    store.record_stage("b", "paper", passed=True)
    store.promote("b", "shadow")
    store.record_stage("b", "shadow", passed=True)
    with pytest.raises(ValueError, match="already has writer"):
        store.promote("b", "live", account_id="acc", broker="paper", approval="ok")

    assert store.record_decision("d1", "a", "hash", {}) is True
    assert store.record_decision("d1", "a", "hash", {}) is False


def test_instance_configuration_change_demotes_live_to_shadow(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    store.create_instance(StrategyInstanceConfig(
        "a", "dual_ma", "1.0.0", {"short_window": 5, "long_window": 20}, ("SH600000",),
    ))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE strategy_instances SET deployment_level='live' WHERE instance_id='a'")
        db.commit()

    updated = store.update_instance("a", {"params": {"short_window": 10, "long_window": 30}})

    assert updated["deployment_level"] == "shadow"
    assert updated["config_hash"] == updated["config"]["config_hash"]
    assert len(updated["config_hash"]) == 64


def test_model_artifact_requires_explicit_trusted_root(tmp_path: Path, monkeypatch) -> None:
    from alphapilot.systems.trading.security import verify_trusted_model

    model = tmp_path / "model.pkl"
    model.write_bytes(b"trusted-test-model")
    monkeypatch.delenv("ALPHAPILOT_TRUSTED_MODEL_DIRS", raising=False)
    with pytest.raises(ValueError, match="trusted roots"):
        verify_trusted_model(model)

    monkeypatch.setenv("ALPHAPILOT_TRUSTED_MODEL_DIRS", str(tmp_path))
    assert len(verify_trusted_model(model)) == 64


def test_live_runner_accepts_only_promoted_instance(engine, tmp_path: Path) -> None:
    from datetime import datetime

    from alphapilot.systems.live.brokers.paper import PaperBroker
    from alphapilot.systems.live.clock import SimulatedClock
    from alphapilot.systems.live.config import LiveConfig, RunMode
    from alphapilot.systems.live.daemon import _build_timing_runner
    from alphapilot.systems.live.engine import LiveEngine
    from alphapilot.systems.live.ledger import Ledger

    trading = engine.get_system("trading")
    instance_id = f"live-{tmp_path.name}"
    trading.create_instance({
        "instance_id": instance_id,
        "strategy_id": "dual_ma",
        "params": {"short_window": 5, "long_window": 20, "target_percent": 0.2},
        "universe": ["SH600000"],
        "frequency": "day",
    })
    trading.validate_instance(instance_id)
    for stage, target in (("replay", "paper"), ("paper", "shadow")):
        trading.store.record_stage(instance_id, stage, passed=True)
        trading.store.promote(instance_id, target)
    trading.store.record_stage(instance_id, "shadow", passed=True)
    trading.store.promote(
        instance_id, "live", account_id="acc", broker="paper", approval="test-approval"
    )

    clock = SimulatedClock(datetime(2026, 7, 6, 9, 10))
    cfg = LiveConfig(mode=RunMode.LIVE, state_dir=tmp_path / "state", ledger_dir=tmp_path / "ledger")
    broker = PaperBroker(cash=100_000, prices={"600000.SSE": 10.0})
    live_engine = LiveEngine(
        cfg, broker, ledger=Ledger(cfg.ledger_dir), now_fn=clock, is_trading_day_fn=lambda _dt: True,
    )
    live_engine.connect({})

    with pytest.raises(ValueError, match="legacy"):
        _build_timing_runner(
            live_engine, ["600000"], timing_strategy="dual_ma",
            timing_params={"target_percent": 0.2}, timing_freq="day",
            bar_seconds=60, min_bars=30, window=250,
            kernel_engine=engine, state_dir=cfg.state_dir,
        )
    runner = _build_timing_runner(
        live_engine, ["600000"], timing_strategy=None, timing_params=None,
        timing_freq="day", bar_seconds=60, min_bars=30, window=250,
        kernel_engine=engine, state_dir=cfg.state_dir, strategy_instance_id=instance_id,
    )
    assert runner.status()["instance_id"] == instance_id


def test_qlib_model_scores_share_the_signal_record_contract() -> None:
    import pandas as pd

    from alphapilot.systems.trading.model import QlibModelSignalProvider

    scores = pd.Series({"SH600000": 0.8, "SZ000001": 0.2})
    records = QlibModelSignalProvider.signal_records(scores, topk=1)

    assert [(item.instrument, item.signal) for item in records] == [
        ("SH600000", 1), ("SZ000001", 0)
    ]

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
from alphapilot.systems.trading.registry import StrategyRegistry, resolve_required_history
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
    assert resolve_required_history(
        definition,
        {"short_window": 20, "long_window": 60},
    ) == 61
    assert resolve_required_history(
        registry.get("stoch_rsi_reversion"),
        {"rsi_window": 14, "stoch_window": 14},
    ) == 29
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
    helper = local / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
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

    initial_hash = registry.get("custom_demo").code_hash
    assert initial_hash
    assert registry.create("custom_demo", {"window": 7}, isolated=False).window == 7
    assert any(item["strategy_id"] == "dual_ma" for item in registry.quarantined())

    helper.write_text("VALUE = 2\n", encoding="utf-8")
    refreshed = StrategyRegistry(local_root=tmp_path / "strategies").discover()
    assert refreshed.get("custom_demo").code_hash != initial_hash


def test_local_v2_manifest_uses_lifecycle_worker_without_forced_artifact_argument(
    tmp_path: Path,
) -> None:
    local = tmp_path / "strategies" / "v2"
    local.mkdir(parents=True)
    (local / "provider.py").write_text(
        "class Provider:\n"
        "    def __init__(self, window=3): self.window = window; self.calls = 0\n"
        "    def initialize(self, context): self.calls += 1\n"
        "    def warmup(self, history): self.calls += 1\n"
        "    def evaluate(self, context): self.calls += 1; return None\n"
        "    def snapshot(self): return {'window': self.window, 'calls': self.calls}\n"
        "    def restore(self, state): self.calls = state['calls']\n"
        "    def stop(self, reason): pass\n",
        encoding="utf-8",
    )
    (local / "strategy.toml").write_text(
        """
[strategy]
id = "custom_v2"
version = "0.1.0"
factory = "provider:Provider"
api_version = 2
signal_kind = "instrument_timing"
parameter_schema_json = '''{"type":"object","properties":{"window":{"type":"integer","default":3}},"additionalProperties":false}'''
""",
        encoding="utf-8",
    )
    registry = StrategyRegistry(local_root=tmp_path / "strategies").discover(
        builtin_contributions=[],
    )
    definition = registry.get("custom_v2")
    assert definition.provider_api_version == 2
    provider = registry.create_provider("custom_v2", {"window": 7}, factory_context={})
    provider.initialize(None)
    provider.warmup([])
    assert provider.snapshot() == {"window": 7, "calls": 2}
    provider.stop("test")


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


def test_instance_configuration_change_resets_live_to_replay(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    store.create_instance(StrategyInstanceConfig(
        "a", "dual_ma", "1.0.0", {"short_window": 5, "long_window": 20}, ("SH600000",),
    ))
    for source, target in (("replay", "paper"), ("paper", "shadow")):
        store.record_stage("a", source, passed=True)
        store.promote("a", target)
    store.record_stage("a", "shadow", passed=True)
    store.promote("a", "live", account_id="account", broker="paper", approval="test")

    updated = store.update_instance("a", {"params": {"short_window": 10, "long_window": 30}})

    assert updated["deployment_level"] == "replay"
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
    assert runner.step()["instance_id"] == instance_id


def test_qlib_model_scores_share_the_signal_record_contract() -> None:
    import pandas as pd

    from alphapilot.systems.trading.model import QlibModelSignalProvider

    scores = pd.Series({"SH600000": 0.8, "SZ000001": 0.2})
    records = QlibModelSignalProvider.signal_records(scores, topk=1)

    assert [(item.instrument, item.signal) for item in records] == [
        ("SH600000", 1), ("SZ000001", 0)
    ]


def test_legacy_paper_daemon_strategy_is_adapted_to_authorized_temporary_instance(
    engine,
) -> None:
    from datetime import datetime, timezone

    from alphapilot.systems.live.daemon import _build_timing_runner
    from alphapilot.systems.live.types import Exchange, OrderRequest

    live = engine.get_system("live")
    runtime = live.create_runtime(mode="paper", broker="paper", trade_broker="paper")
    runtime.connect(paper_cash=100_000)
    runner = _build_timing_runner(
        runtime.engine,
        ["600000"],
        timing_strategy="dual_ma",
        timing_params={"short_window": 5, "long_window": 20, "target_percent": 0.2},
        timing_freq="day",
        bar_seconds=60,
        min_bars=30,
        window=250,
        kernel_engine=engine,
        state_dir=runtime.config.state_dir,
        runtime=runtime,
    )
    trading = engine.get_system("trading")
    temporary_id = runner.status()["instance_id"]
    assert temporary_id.startswith("legacy-paper-dual_ma-")
    temporary = trading.store.get_instance(temporary_id)
    assert temporary["deployment_level"] == "paper"
    assert trading.store.get_active_stage_run(temporary_id, stage="paper") is not None

    trading.store.transition_runtime(
        temporary_id,
        lifecycle="running",
        desired_state="running",
        observed_state="running",
        runner_heartbeat_at=datetime.now(timezone.utc).isoformat(),
    )
    request = OrderRequest.buy(
        "600000",
        Exchange.SSE,
        100,
        10.0,
        reference=(
            f"{temporary_id}:{temporary['config_hash']}:"
            "decision:600000.SSE:B:0"
        ),
    )
    runner.route_port.submit(request)

    assert runner.route_port.last_authorization is not None
    assert runner.route_port.last_authorization.allowed is True
    runner.stop()
    runtime.close()


def test_real_daemon_legacy_paper_name_uses_formal_instance_runner(
    engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timedelta

    from alphapilot.systems.live.daemon import _build_timing_runner
    from alphapilot.systems.live.instance_runner import StrategyInstanceRunner
    from alphapilot.systems.trading.contracts import CompletedBar, PriceAdjustment

    class History:
        def with_data_dir(self, _data_dir):  # noqa: ANN001, ANN201
            return self

        def load_completed_bars(self, **_kwargs):  # noqa: ANN003, ANN201
            start = datetime(2026, 5, 1)
            return [
                CompletedBar(
                    datetime=(start + timedelta(days=index)).isoformat(),
                    instrument="600000.SSE",
                    open=10 + index * 0.01,
                    high=10.2 + index * 0.01,
                    low=9.8 + index * 0.01,
                    close=10 + index * 0.01,
                    volume=10_000,
                    amount=100_000,
                    frequency="day",
                    adjustment=PriceAdjustment.BACKWARD,
                    data_version="formal-history-v1",
                )
                for index in range(30)
            ]

    class BarSource:
        def __init__(self) -> None:
            self.listeners = []

        def add_bar_listener(self, interval, listener):  # noqa: ANN001, ANN201
            self.listeners.append((interval, listener))

        def remove_bar_listener(self, interval, listener):  # noqa: ANN001, ANN201
            self.listeners.remove((interval, listener))

    live = engine.get_system("live")
    runtime = live.create_runtime(mode="paper", broker="paper", trade_broker="paper")
    runtime.connect(paper_cash=100_000)
    trading = engine.get_system("trading")
    monkeypatch.setattr(trading, "historical_data", History())
    bar_source = BarSource()

    runner = _build_timing_runner(
        runtime.engine,
        ["600000"],
        timing_strategy="dual_ma",
        timing_params={"short_window": 5, "long_window": 20, "target_percent": 0.2},
        timing_freq="day",
        bar_seconds=60,
        min_bars=30,
        window=250,
        kernel_engine=engine,
        state_dir=runtime.config.state_dir,
        runtime=runtime,
        bar_source=bar_source,
    )

    assert isinstance(runner, StrategyInstanceRunner)
    assert runner.status()["required_history"] == 21
    assert runner.status()["available_history"] == 21
    assert bar_source.listeners
    runner.stop()
    runtime.close()

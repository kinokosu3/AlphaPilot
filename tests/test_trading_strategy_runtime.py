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
from alphapilot.systems.trading.domain import DeploymentSpec, StrategyInstanceConfig
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


def test_legacy_manifest_deployable_modes_is_rejected_without_alias(
    tmp_path: Path,
) -> None:
    local = tmp_path / "strategies" / "legacy"
    local.mkdir(parents=True)
    (local / "strategy.py").write_text("class Demo: pass\n", encoding="utf-8")
    (local / "strategy.toml").write_text(
        "[strategy]\n"
        "id='legacy_modes'\n"
        "version='0.1.0'\n"
        "factory='strategy:Demo'\n"
        "deployable_modes=['replay','paper']\n",
        encoding="utf-8",
    )

    registry = StrategyRegistry(local_root=tmp_path / "strategies").discover(
        builtin_contributions=[],
    )
    assert "legacy_modes" not in {item.strategy_id for item in registry.list()}
    assert "deployable_modes was removed" in registry.quarantined()[0]["reason"]


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


def test_execution_planner_applies_dynamic_equity_order_cap() -> None:
    oms = _oms()
    target = TargetPortfolio(
        date="2026-07-12",
        holdings={"SH600000": 5000},
        prices={"SH600000": 10.0},
        instance_id="canary",
        config_hash="risk-bound",
        decision_id="dynamic-cap",
    )

    plan = ExecutionPlanner(
        lot_size=100,
        max_order_value=50_000,
        max_order_equity_pct=0.02,
    ).plan(target, oms)

    # Current equity is 1m, so the 2% dynamic cap (20k) is tighter than 50k.
    assert [request.volume for request in plan.requests] == [2000, 2000]


def test_runtime_store_allows_direct_live_and_enforces_single_writer(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    for instance_id in ("a", "b"):
        created = store.create_instance(StrategyInstanceConfig(
            instance_id, "dual_ma", "1.0.0", {"short_window": 5, "long_window": 20},
            ("SH600000",),
        ))
        store.set_validation_state(instance_id, "validated")
        store.configure_deployment(DeploymentSpec(
            instance_id=instance_id,
            config_hash=created["config_hash"],
            run_mode="live",
            execution_environment="live",
            trade_provider="xtp",
            quote_provider="xtp",
            account_id="acc",
            quote_data_kind="realtime",
        ))

    store.update_runtime_state("a", binding_active=True)
    with pytest.raises(ValueError, match="active automated writer"):
        store.update_runtime_state("b", binding_active=True)

    assert store.record_decision("d1", "a", "hash", {}) is True
    assert store.record_decision("d1", "a", "hash", {}) is False


def test_instance_configuration_change_marks_deployment_stale(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    created = store.create_instance(StrategyInstanceConfig(
        "a", "dual_ma", "1.0.0", {"short_window": 5, "long_window": 20}, ("SH600000",),
    ))
    store.set_validation_state("a", "validated")
    store.configure_deployment(DeploymentSpec(
        instance_id="a", config_hash=created["config_hash"], run_mode="paper",
    ))

    updated = store.update_instance("a", {"params": {"short_window": 10, "long_window": 30}})

    assert updated["validation_state"] == "created"
    assert store.get_deployment_spec("a")["stale"] is True
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


def test_daemon_runner_accepts_only_validated_deployed_persistent_instance(engine) -> None:
    from alphapilot.systems.live.daemon import _build_strategy_instance_runner

    trading = engine.get_system("trading")
    instance_id = "live-promoted-instance"
    trading.create_instance({
        "instance_id": instance_id,
        "strategy_id": "dual_ma",
        "params": {"short_window": 5, "long_window": 20, "target_percent": 0.2},
        "universe": ["SH600000"],
        "frequency": "day",
    })
    runtime = engine.get_system("live").create_runtime(
        mode="paper",
        broker="paper",
        trade_broker="paper",
    )
    runtime.enable_market_data(["600000"])
    runtime.connect(paper_cash=100_000)
    try:
        assert _build_strategy_instance_runner(
            runtime.engine,
            ["600000"],
            bar_seconds=60,
            kernel_engine=engine,
            runtime=runtime,
            bar_source=runtime.market_data,
        ) is None
        with pytest.raises(KeyError, match="unknown strategy instance"):
            _build_strategy_instance_runner(
                runtime.engine,
                ["600000"],
                bar_seconds=60,
                kernel_engine=engine,
                strategy_instance_id="missing",
                runtime=runtime,
                bar_source=runtime.market_data,
            )
        with pytest.raises(ValueError, match="must be validated"):
            _build_strategy_instance_runner(
                runtime.engine,
                ["600000"],
                bar_seconds=60,
                kernel_engine=engine,
                strategy_instance_id=instance_id,
                runtime=runtime,
                bar_source=runtime.market_data,
            )
        validated = trading.validate_instance(instance_id)["instance"]
        with pytest.raises(KeyError, match="deployment is not configured"):
            _build_strategy_instance_runner(
                runtime.engine,
                ["600000"],
                bar_seconds=60,
                kernel_engine=engine,
                strategy_instance_id=instance_id,
                runtime=runtime,
                bar_source=runtime.market_data,
            )
        trading.store.configure_deployment(DeploymentSpec(
            instance_id=instance_id,
            config_hash=validated["config_hash"],
            run_mode="paper",
        ))
        runner = _build_strategy_instance_runner(
            runtime.engine,
            ["600000"],
            bar_seconds=60,
            kernel_engine=engine,
            strategy_instance_id=instance_id,
            runtime=runtime,
            bar_source=runtime.market_data,
        )
        assert runner.status()["instance_id"] == instance_id
        runner.stop()
    finally:
        runtime.close()


def test_qlib_model_scores_share_the_signal_record_contract() -> None:
    import pandas as pd

    from alphapilot.systems.trading.model import QlibModelSignalProvider

    scores = pd.Series({"SH600000": 0.8, "SZ000001": 0.2})
    records = QlibModelSignalProvider.signal_records(scores, topk=1)

    assert [(item.instrument, item.signal) for item in records] == [
        ("SH600000", 1), ("SZ000001", 0)
    ]


def test_daemon_runner_has_no_anonymous_strategy_construction_path() -> None:
    import inspect

    from alphapilot.systems.live.daemon import _build_strategy_instance_runner

    parameters = inspect.signature(_build_strategy_instance_runner).parameters
    assert "strategy_instance_id" in parameters
    assert {
        "timing_strategy",
        "timing_params",
        "timing_freq",
        "min_bars",
        "window",
    }.isdisjoint(parameters)
    assert _build_strategy_instance_runner(
        object(),
        ["600000"],
        bar_seconds=60,
    ) is None


def test_real_daemon_persistent_paper_instance_uses_formal_instance_runner(
    engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timedelta

    from alphapilot.systems.live.daemon import _build_strategy_instance_runner
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
    instance_id = "formal-paper-dual-ma"
    trading.create_instance({
        "instance_id": instance_id,
        "strategy_id": "dual_ma",
        "params": {"short_window": 5, "long_window": 20, "target_percent": 0.2},
        "universe": ["SH600000"],
        "frequency": "day",
    })
    validated = trading.validate_instance(instance_id)["instance"]
    trading.store.configure_deployment(DeploymentSpec(
        instance_id=instance_id,
        config_hash=validated["config_hash"],
        run_mode="paper",
    ))
    monkeypatch.setattr(trading, "historical_data", History())
    bar_source = BarSource()

    runner = _build_strategy_instance_runner(
        runtime.engine,
        ["600000"],
        bar_seconds=60,
        kernel_engine=engine,
        strategy_instance_id=instance_id,
        runtime=runtime,
        bar_source=bar_source,
    )

    assert isinstance(runner, StrategyInstanceRunner)
    assert runner.status()["required_history"] == 21
    assert runner.status()["available_history"] == 21
    assert bar_source.listeners
    runner.stop()
    runtime.close()


def test_timing_policy_rejects_unsafe_exposure_configurations() -> None:
    from alphapilot.systems.trading.contracts import (
        AccountSnapshot,
        PortfolioContext,
        PortfolioInputs,
        SignalEnvelope,
        SignalKind,
        TimingSignal,
    )
    from alphapilot.systems.trading.portfolio import TimingFixedExposurePolicy

    envelope = SignalEnvelope(
        kind=SignalKind.INSTRUMENT_TIMING,
        source_instance_id="timing-policy",
        as_of="2026-07-17",
        payload=TimingSignal(
            scores={"600000.SSE": 1.0, "000001.SZ": 0.5},
            states={"600000.SSE": "long", "000001.SZ": "long"},
        ),
    )
    inputs = PortfolioInputs(instrument_timing=(envelope,))
    context = PortfolioContext(
        as_of=envelope.as_of,
        account=AccountSnapshot("paper", envelope.as_of, 100_000, 100_000),
    )

    with pytest.raises(ValueError, match="above investable"):
        TimingFixedExposurePolicy(
            target_percent=0.3, max_position_weight=0.3, cash_buffer=0.5,
        ).build(inputs, context)
    with pytest.raises(ValueError, match="exposure budget"):
        TimingFixedExposurePolicy(
            target_percent=0.3,
            max_position_weight=0.3,
            cash_buffer=0.8,
            exposure_mode="equal_active_budget",
        ).build(inputs, context)
    with pytest.raises(ValueError, match="exposure_mode"):
        TimingFixedExposurePolicy(exposure_mode="unsupported").build(inputs, context)


def test_contract_redaction_release_and_comparison_validation_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alphapilot.systems.live.redaction import redact_secrets
    from alphapilot.systems.trading.contracts import CompletedBar
    from alphapilot.systems.trading.comparison import DecisionComparisonService
    from alphapilot.systems.trading.release_verification import (
        report_path_for,
        required_checks_for,
    )

    with pytest.raises(ValueError, match="ISO date"):
        CompletedBar(
            datetime="not-a-date",
            instrument="600000.SSE",
            open=10,
            high=11,
            low=9,
            close=10,
        )
    monkeypatch.setenv("TEST_API_TOKEN", "private-token")
    assert redact_secrets({
        "password": "plain",
        "nested": ["private-token", {"safe": "token=inline"}],
    }) == {
        "password": "********",
        "nested": ["********", {"safe": "token=********"}],
    }
    with pytest.raises(ValueError, match="build_kind"):
        report_path_for("unknown")
    with pytest.raises(ValueError, match="build_kind"):
        required_checks_for("unknown")

    template = {
        "config_hash": "current",
        "as_of": "2026-07-17T15:00:00+08:00",
        "observation_id": "observation",
        "history_hash": "history",
        "provider_state_before_hash": "before",
        "provider_state_after_hash": "after",
        "signal_hash": "signal",
        "weights_hash": "weights",
        "data_version": "data-v1",
        "model_version": "model-v1",
        "policy_version": "policy-v1",
        "account_hash": "",
        "quote_hash": "",
        "instrument_hash": "",
        "plan_hash": "",
    }
    grouped = DecisionComparisonService._group_observations(
        [
            {**template, "observation_id": "left-1"},
            {**template, "observation_id": "left-2"},
            {**template, "config_hash": "stale", "observation_id": "stale"},
        ],
        config_hash="current",
        daily=True,
    )
    assert len(grouped["2026-07-17"]) == 2
    missing = DecisionComparisonService._compare_observations(None, template)
    assert missing[2] == {"missing": "left"}
    missing = DecisionComparisonService._compare_observations(template, None)
    assert missing[2] == {"missing": "right"}
    mismatch = DecisionComparisonService._compare_observations(
        template, {**template, "signal_hash": "different"},
    )
    assert mismatch[0] == "mismatch"
    incomparable = DecisionComparisonService._compare_observations(
        template, {**template, "data_version": "revised"},
    )
    assert incomparable[0] == "not_comparable"

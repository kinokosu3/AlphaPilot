from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from alphapilot.systems.selection.definitions import strategy_definitions as selection_definitions
from alphapilot.systems.timing.definitions import strategy_definitions as timing_definitions
from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    CompletedBar,
    ExecutionPhase,
    FeeSchedule,
    InstrumentMetadata,
    PriceAdjustment,
    PortfolioDecision,
    TargetWeights,
    TargetPortfolio,
    TradableQuote,
    OperatorContext,
    SignalEnvelope,
    SignalKind,
    TimingSignal,
)
from alphapilot.systems.trading.application import DecisionPipeline
from alphapilot.systems.trading.data_adapters import SequenceCalendar
from alphapilot.systems.trading.domain import (
    DeploymentSpec,
    StrategyDefinition,
    StrategyInstanceConfig,
)
from alphapilot.systems.trading.execution import ExecutionCoordinator
from alphapilot.systems.trading.planning import ExecutionPlanner
from alphapilot.systems.trading.portfolio import AccountSizer
from alphapilot.systems.trading.policy_registry import PortfolioPolicyRegistry
from alphapilot.systems.trading.registry import StrategyRegistry
from alphapilot.systems.trading.replay import ReplayConfig, ReplayRuntime, _ReplayAccount
from alphapilot.systems.trading.store import StrategyRuntimeStore
from alphapilot.systems.trading.worker import PersistentStrategyWorker


class _LifecycleProvider:
    latest: "_LifecycleProvider | None" = None

    def __init__(self, *, artifact_binding=None) -> None:  # noqa: ANN001
        self.artifact_binding = artifact_binding or {}
        self.initialize_count = 0
        self.evaluate_count = 0
        self.stopped = False
        _LifecycleProvider.latest = self

    def initialize(self, context) -> None:  # noqa: ANN001
        del context
        self.initialize_count += 1

    def warmup(self, history) -> None:  # noqa: ANN001
        self.last_history_size = len(history)

    def evaluate(self, context) -> SignalEnvelope:  # noqa: ANN001
        self.evaluate_count += 1
        return SignalEnvelope(
            kind=SignalKind.INSTRUMENT_TIMING,
            source_instance_id=context.instance_id,
            as_of=context.as_of,
            frequency=context.frequency,
            data_version=context.data_version,
            payload=TimingSignal(
                scores={context.history[-1].instrument: 1.0},
                states={context.history[-1].instrument: "long"},
            ),
        )

    def snapshot(self) -> dict[str, int]:
        return {"evaluate_count": self.evaluate_count}

    def restore(self, state: dict[str, int]) -> None:
        self.evaluate_count = int(state["evaluate_count"])

    def stop(self, reason: str) -> None:
        del reason
        self.stopped = True


def _policy_registry(tmp_path: Path) -> PortfolioPolicyRegistry:
    return PortfolioPolicyRegistry(local_root=tmp_path / "missing-policies").discover()


def _bars(
    instruments: tuple[str, ...],
    timestamps: list[str],
    *,
    adjustment: PriceAdjustment,
    frequency: str,
    data_version: str,
) -> list[CompletedBar]:
    rows: list[CompletedBar] = []
    for offset, timestamp in enumerate(timestamps):
        for index, instrument in enumerate(instruments):
            price = 10.0 + offset + index
            rows.append(CompletedBar(
                datetime=timestamp,
                instrument=instrument,
                open=price,
                high=price + 0.1,
                low=price - 0.1,
                close=price,
                volume=10_000,
                amount=price * 10_000,
                frequency=frequency,
                adjustment=adjustment,
                data_version=data_version,
            ))
    return rows


def test_rule_timing_replay_runs_signal_to_recoverable_fills(tmp_path: Path) -> None:
    registry = StrategyRegistry(local_root=tmp_path / "missing-strategies").discover(
        builtin_contributions=timing_definitions(),
    )
    policies = _policy_registry(tmp_path)
    definition = registry.get("sma_filter")
    policy = policies.get("timing_fixed_exposure")
    instance = StrategyInstanceConfig(
        instance_id="sma-rule-golden",
        strategy_id="sma_filter",
        strategy_version=definition.version,
        params={"window": 2, "target_percent": 0.2},
        universe=("600000.SSE",),
        frequency="day",
        data_policy={"feature_adjustment": "backward", "data_version": "feature-v1"},
        portfolio_policy={
            "policy_id": policy.policy_id,
            "version": policy.version,
            "code_hash": policy.code_hash,
            "params": {
                "target_percent": 0.2,
                "cash_buffer": 0.1,
                "max_position_weight": 0.3,
            },
        },
        strategy_code_hash=definition.code_hash,
    )
    sessions = [f"2026-01-{day:02d}" for day in range(5, 14)]
    feature = _bars(
        instance.universe, sessions, adjustment=PriceAdjustment.BACKWARD,
        frequency="day", data_version="feature-v1",
    )
    raw = _bars(
        instance.universe, sessions, adjustment=PriceAdjustment.NONE,
        frequency="day", data_version="raw-v1",
    )

    result = ReplayRuntime(
        strategy_registry=registry,
        policy_registry=policies,
        store=StrategyRuntimeStore(tmp_path / "control.sqlite3"),
        output_root=tmp_path / "runs",
    ).run(
        "rule-run",
        instance,
        feature,
        raw,
        config=ReplayConfig(initial_cash=100_000, partial_fill_ratio=0.5),
    )

    assert result.summary["decisions"] > 0
    assert result.summary["orders"] > 0
    assert result.summary["fills"] >= result.summary["orders"]
    assert (result.artifact_dir / "runtime.sqlite3").is_file()
    plans = pd.read_json(result.artifact_dir / "plans.json")
    assert set(plans["final_phase"]) == {ExecutionPhase.COMPLETED.value}


def test_replay_honors_historical_suspension_quotes(tmp_path: Path) -> None:
    registry = StrategyRegistry(local_root=tmp_path / "missing-strategies").discover(
        builtin_contributions=timing_definitions(),
    )
    policies = _policy_registry(tmp_path)
    definition = registry.get("sma_filter")
    policy = policies.get("timing_fixed_exposure")
    instrument = "600000.SSE"
    instance = StrategyInstanceConfig(
        instance_id="suspended-replay", strategy_id="sma_filter",
        strategy_version=definition.version,
        params={"window": 2, "target_percent": 0.2}, universe=(instrument,),
        frequency="day", data_policy={"feature_adjustment": "backward"},
        portfolio_policy={
            "policy_id": policy.policy_id, "version": policy.version,
            "code_hash": policy.code_hash,
            "params": {"target_percent": 0.2, "cash_buffer": 0.1, "max_position_weight": 0.3},
        },
        strategy_code_hash=definition.code_hash,
    )
    sessions = [f"2026-02-{day:02d}" for day in range(2, 10)]
    feature = _bars(
        instance.universe, sessions, adjustment=PriceAdjustment.BACKWARD,
        frequency="day", data_version="feature-v1",
    )
    raw = _bars(
        instance.universe, sessions, adjustment=PriceAdjustment.NONE,
        frequency="day", data_version="raw-v1",
    )
    quotes = {
        session: {
            instrument: TradableQuote(
                instrument, session, 10.0, open=10.0, suspended=True,
            ),
        }
        for session in sessions
    }
    result = ReplayRuntime(
        strategy_registry=registry, policy_registry=policies,
        store=StrategyRuntimeStore(tmp_path / "suspended.sqlite3"),
        output_root=tmp_path / "suspended-runs",
    ).run(
        "suspended-run", instance, feature, raw,
        config=ReplayConfig(quote_overrides=quotes),
    )
    assert result.summary["orders"] == 0
    assert result.summary["fills"] == 0
    plans = pd.read_json(result.artifact_dir / "plans.json")
    assert set(plans["final_phase"]) == {ExecutionPhase.PAUSED.value}


def test_minute_replay_uses_next_observed_bar_across_lunch(tmp_path: Path) -> None:
    calendar = SequenceCalendar([
        "2026-07-13T11:29:00",
        "2026-07-13T13:00:00",
        "2026-07-13T13:01:00",
    ])
    assert calendar.next_effective("2026-07-13T11:29:00", "min") == "2026-07-13T13:00:00"
    assert calendar.valid_until("2026-07-13T13:00:00", "min") == "2026-07-13T13:01:00"

    registry = StrategyRegistry(local_root=tmp_path / "missing-strategies").discover(
        builtin_contributions=timing_definitions(),
    )
    policies = _policy_registry(tmp_path)
    definition = registry.get("sma_filter")
    policy = policies.get("timing_fixed_exposure")
    instance = StrategyInstanceConfig(
        instance_id="minute-rule-golden",
        strategy_id="sma_filter",
        strategy_version=definition.version,
        params={"window": 2, "target_percent": 0.2},
        universe=("600000.SSE",),
        frequency="min",
        data_policy={"feature_adjustment": "backward", "data_version": "min-feature-v1"},
        portfolio_policy={
            "policy_id": policy.policy_id, "version": policy.version,
            "code_hash": policy.code_hash,
            "params": {"target_percent": 0.2, "cash_buffer": 0.1, "max_position_weight": 0.3},
        },
        strategy_code_hash=definition.code_hash,
    )
    timestamps = [
        "2026-07-13T11:27:00", "2026-07-13T11:28:00",
        "2026-07-13T11:29:00", "2026-07-13T13:00:00",
        "2026-07-13T13:01:00", "2026-07-13T13:02:00",
    ]
    feature = _bars(
        instance.universe, timestamps, adjustment=PriceAdjustment.BACKWARD,
        frequency="min", data_version="min-feature-v1",
    )
    raw = _bars(
        instance.universe, timestamps, adjustment=PriceAdjustment.NONE,
        frequency="min", data_version="min-raw-v1",
    )
    result = ReplayRuntime(
        strategy_registry=registry, policy_registry=policies,
        store=StrategyRuntimeStore(tmp_path / "minute-control.sqlite3"),
        output_root=tmp_path / "minute-runs",
    ).run("minute-run", instance, feature, raw)
    assert result.summary["decisions"] >= 1
    decisions = pd.read_json(result.artifact_dir / "signals.json")
    assert not decisions.empty


def test_replay_rejects_duplicate_bars_instead_of_silently_overwriting(
    tmp_path: Path,
) -> None:
    registry = StrategyRegistry(local_root=tmp_path / "missing-strategies").discover(
        builtin_contributions=timing_definitions(),
    )
    policies = _policy_registry(tmp_path)
    definition = registry.get("sma_filter")
    policy = policies.get("timing_fixed_exposure")
    instance = StrategyInstanceConfig(
        instance_id="duplicate-bars", strategy_id="sma_filter",
        strategy_version=definition.version, params={"window": 2},
        universe=("600000.SSE",), frequency="day",
        data_policy={"feature_adjustment": "backward"},
        portfolio_policy={
            "policy_id": policy.policy_id, "version": policy.version,
            "code_hash": policy.code_hash, "params": {"target_percent": 0.1},
        },
        strategy_code_hash=definition.code_hash,
    )
    sessions = ["2026-07-10", "2026-07-13", "2026-07-14"]
    feature = _bars(
        instance.universe, sessions, adjustment=PriceAdjustment.BACKWARD,
        frequency="day", data_version="feature-v1",
    )
    raw = _bars(
        instance.universe, sessions, adjustment=PriceAdjustment.NONE,
        frequency="day", data_version="raw-v1",
    )
    with pytest.raises(ValueError, match="duplicate feature bar"):
        ReplayRuntime(
            strategy_registry=registry, policy_registry=policies,
            store=StrategyRuntimeStore(tmp_path / "duplicate.sqlite3"),
            output_root=tmp_path / "duplicate-runs",
        ).run("duplicate-run", instance, [*feature, feature[0]], raw)


def test_replay_account_sellable_uses_contract_settlement_cycle() -> None:
    account = _ReplayAccount(
        cash=0,
        positions={"510300.SSE": 100, "600000.SSE": 100},
        acquired_session={"510300.SSE": "2026-07-14", "600000.SSE": "2026-07-14"},
        settlement_days={"510300.SSE": 0, "600000.SSE": 1},
    )
    snapshot = account.snapshot(
        "2026-07-14T13:00:00",
        {"510300.SSE": 4.0, "600000.SSE": 10.0},
    )
    assert snapshot.sellable["510300.SSE"] == 100
    assert snapshot.sellable["600000.SSE"] == 0


def test_replay_requires_positive_initial_capital() -> None:
    with pytest.raises(ValueError, match="initial_cash must be positive"):
        ReplayConfig(initial_cash=0)


def test_live_metadata_adapter_preserves_t_plus_zero_contract() -> None:
    from alphapilot.systems.live.instance_runner import LiveInstrumentMetadataAdapter
    from alphapilot.systems.live.oms import OMS
    from alphapilot.systems.live.types import Contract, Exchange, Product

    oms = OMS()
    oms.on_contract(Contract(
        "510300", Exchange.SSE, product=Product.FUND, settlement_days=0,
    ))
    metadata = LiveInstrumentMetadataAdapter(oms).get_instruments(("510300.SSE",))
    assert metadata["510300.SSE"].settlement_days == 0
    assert metadata["510300.SSE"].long_only is True


def test_execution_planner_normalizes_price_to_contract_tick() -> None:
    target = TargetPortfolio(
        date="2026-07-14", holdings={"600000.SSE": 100},
        prices={"600000.SSE": 10.007}, decision_id="tick-decision",
        instance_id="tick-instance", config_hash="tick-config",
    )
    account = AccountSnapshot(
        account_id="tick-account", as_of="2026-07-14", balance=100_000,
        available=100_000,
    )
    plan = ExecutionPlanner().plan(
        target,
        account,
        instruments={
            "600000.SSE": InstrumentMetadata(
                "600000.SSE", lot_size=100, price_tick=0.01,
            ),
        },
    )
    assert plan.children[0].price == 10.01


def test_v2_provider_lives_for_pipeline_session_and_restores_checkpoint(
    tmp_path: Path,
) -> None:
    registry = StrategyRegistry(local_root=tmp_path / "missing-strategies").discover(
        builtin_contributions=[],
    )
    definition = StrategyDefinition(
        strategy_id="lifecycle-v2", version="1.0.0", kind="rule",
        factory=_LifecycleProvider, provider_api_version=2, api_version=2,
        signal_kind=SignalKind.INSTRUMENT_TIMING, required_history=1,
        code_hash="lifecycle-code-hash", source="builtin",
    )
    registry.register(definition)
    policies = _policy_registry(tmp_path)
    policy = policies.get("timing_fixed_exposure")
    instance = StrategyInstanceConfig(
        instance_id="lifecycle-instance", strategy_id=definition.strategy_id,
        strategy_version=definition.version, universe=("600000.SSE",),
        data_policy={"feature_adjustment": "backward", "data_version": "feature-v1"},
        portfolio_policy={
            "policy_id": policy.policy_id, "version": policy.version,
            "code_hash": policy.code_hash, "params": {"target_percent": 0.1},
        },
        strategy_code_hash=definition.code_hash,
    )
    store = StrategyRuntimeStore(tmp_path / "lifecycle.sqlite3")
    store.create_instance(instance)
    calendar = SequenceCalendar(["2026-07-10", "2026-07-13", "2026-07-14"])
    bars = _bars(
        instance.universe, ["2026-07-10", "2026-07-13"],
        adjustment=PriceAdjustment.BACKWARD, frequency="day", data_version="feature-v1",
    )
    pipeline = DecisionPipeline(
        strategy_registry=registry, policy_registry=policies,
        store=store, calendar=calendar,
    )
    pipeline.evaluate(instance, bars[:1])
    first = _LifecycleProvider.latest
    assert first is not None
    assert first.initialize_count == 1
    assert first.evaluate_count == 1
    assert first.stopped is False
    pipeline.evaluate(instance, bars)
    assert _LifecycleProvider.latest is first
    assert first.initialize_count == 1
    assert first.evaluate_count == 2
    pipeline.close("test")
    assert first.stopped is True

    restarted = DecisionPipeline(
        strategy_registry=registry, policy_registry=policies,
        store=store, calendar=calendar,
    )
    duplicate = restarted.evaluate(instance, bars)
    # The immutable observation is returned before a provider is recreated or
    # a checkpoint consumes the same history a second time.
    assert duplicate.inserted is False
    assert _LifecycleProvider.latest is first
    restarted.close("test")


def test_persistent_provider_worker_times_out_and_is_terminated(tmp_path: Path) -> None:
    module = tmp_path / "slow_provider.py"
    module.write_text(
        "import time\n"
        "class SlowProvider:\n"
        "    def initialize(self, context): pass\n"
        "    def evaluate(self, context): time.sleep(2)\n"
        "    def stop(self, reason): pass\n",
        encoding="utf-8",
    )
    worker = PersistentStrategyWorker(
        "slow_provider:SlowProvider", {}, base=tmp_path, timeout=0.05,
    )
    worker.initialize(None)
    with pytest.raises(TimeoutError, match="evaluate"):
        worker.evaluate(None)
    assert worker._process is None


def test_qlib_selection_snapshot_replay_outputs_only_bound_topk(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    model = tmp_path / "model.pkl"
    factors = tmp_path / "factors.csv"
    model.write_bytes(b"immutable-model")
    factors.write_text("factor_name,factor_expression\nf1,$close\n", encoding="utf-8")
    universe = ("600000.SSE", "000001.SZSE")
    binding = {
        "artifact_type": "qlib_selection",
        "model_path": str(model),
        "model_hash": hashlib.sha256(model.read_bytes()).hexdigest(),
        "factor_path": str(factors),
        "factor_hash": hashlib.sha256(factors.read_bytes()).hexdigest(),
        "universe": list(universe),
        "yaml_params": {},
        "use_local": True,
        "provider_uri": str(tmp_path / "qlib"),
        "market": "main_stock_pit",
        "factor_data_fingerprint": "training-factor-fingerprint",
    }

    def fake_scores(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return pd.Series({"SH600000": 0.9, "SZ000001": 0.1})

    monkeypatch.setattr(
        "alphapilot.systems.selection.predict.predict_scores", fake_scores,
    )
    registry = StrategyRegistry(local_root=tmp_path / "missing-strategies").discover(
        builtin_contributions=selection_definitions(),
    )
    policies = _policy_registry(tmp_path)
    definition = registry.get("qlib_selection")
    policy = policies.get("selection_topk_dropout_equal_weight")
    instance = StrategyInstanceConfig(
        instance_id="selection-golden",
        strategy_id="qlib_selection",
        strategy_version=definition.version,
        universe=universe,
        frequency="day",
        data_policy={"feature_adjustment": "backward", "data_version": "selection-v1"},
        portfolio_policy={
            "policy_id": policy.policy_id, "version": policy.version,
            "code_hash": policy.code_hash,
            "params": {"topk": 1, "n_drop": 0, "cash_buffer": 0.1, "max_position_weight": 0.5},
        },
        strategy_code_hash=definition.code_hash,
        model_hash=binding["model_hash"],
        artifact_binding=binding,
    )
    sessions = [f"2026-03-{day:02d}" for day in range(2, 8)]
    feature = _bars(
        universe, sessions, adjustment=PriceAdjustment.BACKWARD,
        frequency="day", data_version="selection-v1",
    )
    raw = _bars(
        universe, sessions, adjustment=PriceAdjustment.NONE,
        frequency="day", data_version="selection-raw-v1",
    )
    result = ReplayRuntime(
        strategy_registry=registry, policy_registry=policies,
        store=StrategyRuntimeStore(tmp_path / "selection-control.sqlite3"),
        output_root=tmp_path / "selection-runs",
    ).run("selection-run", instance, feature, raw)
    weights = pd.read_json(result.artifact_dir / "weights.json")
    assert all(set(value) == {"600000.SSE"} for value in weights["weights"])
    assert result.summary["fills"] > 0


def test_generic_instance_api_cannot_bind_arbitrary_qlib_model(
    engine,
    tmp_path: Path,
) -> None:
    model = tmp_path / "arbitrary.pkl"
    factors = tmp_path / "arbitrary.csv"
    model.write_bytes(b"arbitrary-model")
    factors.write_text("factor_name,factor_expression\nf1,$close\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the immutable snapshot root"):
        engine.get_system("trading").create_instance({
            "instance_id": "forged-qlib-binding",
            "strategy_id": "qlib_selection",
            "universe": ["600000.SSE"],
            "artifact_binding": {
                "artifact_type": "qlib_selection",
                "model_path": str(model),
                "model_hash": hashlib.sha256(model.read_bytes()).hexdigest(),
                "factor_path": str(factors),
                "factor_hash": hashlib.sha256(factors.read_bytes()).hexdigest(),
                "universe": ["600000.SSE"],
            },
        })


def test_qlib_snapshot_detects_template_content_changes(tmp_path: Path) -> None:
    from alphapilot.systems.trading.artifacts import (
        _tree_sha256,
        verify_artifact_binding,
    )

    root = tmp_path / "artifacts"
    destination = root / "selection-instance" / "fingerprint"
    model = destination / "model" / "model.pkl"
    factors = destination / "factors.csv"
    template = destination / "qlib_template"
    model.parent.mkdir(parents=True)
    template.mkdir(parents=True)
    model.write_bytes(b"trusted-model")
    factors.write_text("factor_name,factor_expression\nf1,$close\n", encoding="utf-8")
    template_file = template / "workflow.yaml"
    template_file.write_text("model: LightGBM\n", encoding="utf-8")
    binding = {
        "artifact_type": "qlib_selection",
        "model_path": str(model),
        "model_hash": hashlib.sha256(model.read_bytes()).hexdigest(),
        "factor_path": str(factors),
        "factor_hash": hashlib.sha256(factors.read_bytes()).hexdigest(),
        "qlib_template_dir": str(template),
        "qlib_template_hash": _tree_sha256(template),
        "universe": ["600000.SSE"],
    }
    (destination / "manifest.json").write_text(
        json.dumps(binding, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    verify_artifact_binding(
        binding,
        snapshot_root=root,
        expected_instance_id="selection-instance",
    )

    template_file.write_text("model: tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="template is missing or has changed"):
        verify_artifact_binding(
            binding,
            snapshot_root=root,
            expected_instance_id="selection-instance",
        )


class _AccountPort:
    def __init__(self, snapshot: AccountSnapshot) -> None:
        self.snapshot = snapshot

    def account_snapshot(self) -> AccountSnapshot:
        return self.snapshot


class _RoutePort:
    def __init__(self) -> None:
        self.statuses: dict[str, str] = {}
        self.submitted: list[str] = []

    def submit_child(self, child) -> str:  # noqa: ANN001
        self.submitted.append(child.reference)
        self.statuses[child.reference] = "nottraded"
        return f"order-{len(self.submitted)}"

    def child_statuses(self, references):  # noqa: ANN001, ANN201
        return {reference: self.statuses[reference] for reference in references if reference in self.statuses}

    def cancel_child(self, reference: str) -> bool:
        self.statuses[reference] = "cancelled"
        return True


def _execution_fixture(tmp_path: Path):  # noqa: ANN202
    store = StrategyRuntimeStore(tmp_path / "execution.sqlite3")
    instance = StrategyInstanceConfig(
        "execution-golden", "demo", "1.0.0", universe=("600000.SSE", "000001.SZSE"),
    )
    store.create_instance(instance)
    snapshot = AccountSnapshot(
        account_id="account-1", as_of="2026-07-13", balance=10_000, available=9_000,
        positions={"600000.SSE": 100}, sellable={"600000.SSE": 100},
    )
    target = TargetPortfolio(
        date="2026-07-13", holdings={"000001.SZSE": 100},
        prices={"600000.SSE": 10, "000001.SZSE": 10},
        decision_id="decision-1", instance_id=instance.instance_id,
        config_hash=instance.config_hash,
    )
    quotes = {
        key: TradableQuote(key, "2026-07-13T09:25:00+08:00", 10, open=10)
        for key in instance.universe
    }
    metadata = {key: InstrumentMetadata(key, lot_size=100) for key in instance.universe}
    planner = ExecutionPlanner(lot_size=100)
    plan = planner.plan(target, snapshot, quotes=quotes, instruments=metadata)
    return store, instance, snapshot, target, quotes, metadata, planner, plan


def test_execution_restart_ignores_never_routed_future_phase_children(tmp_path: Path) -> None:
    store, instance, snapshot, target, quotes, metadata, planner, plan = _execution_fixture(tmp_path)
    account = _AccountPort(snapshot)
    route = _RoutePort()
    first = ExecutionCoordinator(
        store=store, account_port=account, route_port=route, planner=planner,
        can_route=True, expected_account_id="account-1",
    )
    first.begin(plan, target, universe=instance.universe, quotes=quotes, instruments=metadata)
    first.advance(plan.plan_id)  # PLANNED -> SELLING
    waiting = first.advance(plan.plan_id)  # submit only sells
    assert waiting["phase"] == ExecutionPhase.WAITING_SELL_REPORTS.value
    assert len(route.submitted) == 1

    restarted = ExecutionCoordinator(
        store=store, account_port=account, route_port=route, planner=planner,
        can_route=True, expected_account_id="account-1",
    )
    recovered = restarted.recover(plan.plan_id)
    assert recovered["phase"] == ExecutionPhase.WAITING_SELL_REPORTS.value
    assert recovered["last_error"] == {}


def test_execution_rejection_pauses_instead_of_repeatedly_resubmitting(tmp_path: Path) -> None:
    store, instance, snapshot, target, quotes, metadata, planner, plan = _execution_fixture(tmp_path)
    route = _RoutePort()
    coordinator = ExecutionCoordinator(
        store=store, account_port=_AccountPort(snapshot), route_port=route,
        planner=planner, can_route=True, expected_account_id="account-1",
    )
    coordinator.begin(plan, target, universe=instance.universe, quotes=quotes, instruments=metadata)
    coordinator.advance(plan.plan_id)
    waiting = coordinator.advance(plan.plan_id)
    assert waiting["phase"] == ExecutionPhase.WAITING_SELL_REPORTS.value
    route.statuses[route.submitted[0]] = "rejected"
    paused = coordinator.advance(plan.plan_id)
    assert paused["phase"] == ExecutionPhase.PAUSED.value
    assert paused["last_error"]["rule"] == "child_order_not_filled"
    assert len(route.submitted) == 1


def test_authorization_rejection_pauses_without_generating_retry_children(tmp_path: Path) -> None:
    store, instance, snapshot, target, quotes, metadata, planner, plan = _execution_fixture(tmp_path)

    class RejectingRoute(_RoutePort):
        def submit_child(self, child):  # noqa: ANN001, ANN201
            self.submitted.append(child.reference)
            return None

    route = RejectingRoute()
    coordinator = ExecutionCoordinator(
        store=store, account_port=_AccountPort(snapshot), route_port=route,
        planner=planner, can_route=True, expected_account_id="account-1",
    )
    coordinator.begin(plan, target, universe=instance.universe, quotes=quotes, instruments=metadata)
    coordinator.advance(plan.plan_id)
    coordinator.advance(plan.plan_id)

    paused = coordinator.advance(plan.plan_id)

    assert paused["phase"] == ExecutionPhase.PAUSED.value
    assert paused["last_error"]["rule"] == "child_order_not_filled"
    assert len(route.submitted) == 1


def test_final_reconcile_refreshes_account_after_late_fill_report(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "late-fill.sqlite3")
    instance = StrategyInstanceConfig(
        "late-fill", "demo", "1.0.0", universe=("600000.SSE",),
    )
    store.create_instance(instance)
    stale = AccountSnapshot(
        account_id="account-1", as_of="2026-07-14", balance=10_000, available=10_000,
    )
    filled = AccountSnapshot(
        account_id="account-1", as_of="2026-07-14", balance=10_000, available=9_000,
        positions={"600000.SSE": 100}, sellable={"600000.SSE": 0},
    )
    account = _AccountPort(stale)
    target = TargetPortfolio(
        date="2026-07-14", holdings={"600000.SSE": 100},
        prices={"600000.SSE": 10}, decision_id="late-fill-decision",
        instance_id=instance.instance_id, config_hash=instance.config_hash,
    )
    quotes = {
        "600000.SSE": TradableQuote(
            "600000.SSE", "2026-07-14T09:25:00+08:00", 10, open=10,
        ),
    }
    metadata = {"600000.SSE": InstrumentMetadata("600000.SSE", lot_size=100)}
    planner = ExecutionPlanner(lot_size=100)
    plan = planner.plan(target, stale, quotes=quotes, instruments=metadata)
    route = _RoutePort()
    coordinator = ExecutionCoordinator(
        store=store, account_port=account, route_port=route, planner=planner,
        can_route=True, expected_account_id="account-1",
    )
    coordinator.begin(
        plan, target, universe=instance.universe, quotes=quotes, instruments=metadata,
    )
    coordinator.advance(plan.plan_id)  # PLANNED -> REFRESHING_ACCOUNT
    coordinator.advance(plan.plan_id)  # REFRESHING_ACCOUNT -> BUYING
    coordinator.advance(plan.plan_id)  # submit buy
    route.statuses[route.submitted[0]] = "alltraded"
    final = coordinator.advance(plan.plan_id)
    assert final["phase"] == ExecutionPhase.FINAL_RECONCILE.value

    class LateAccountPort:
        calls = 0

        def account_snapshot(self) -> AccountSnapshot:
            self.calls += 1
            return stale if self.calls == 1 else filled

    coordinator.account_port = LateAccountPort()
    completed = coordinator.advance(plan.plan_id)

    assert completed["phase"] == ExecutionPhase.COMPLETED.value
    assert len(route.submitted) == 1


def test_execution_plan_expiry_cancels_and_never_routes_stale_children(tmp_path: Path) -> None:
    store, instance, snapshot, target, quotes, metadata, planner, plan = _execution_fixture(tmp_path)
    target.valid_until = "2026-07-13T15:00:00+08:00"
    expired_snapshot = AccountSnapshot(
        **{**snapshot.__dict__, "as_of": "2026-07-14T09:15:00+08:00"},
    )
    route = _RoutePort()
    coordinator = ExecutionCoordinator(
        store=store, account_port=_AccountPort(expired_snapshot), route_port=route,
        planner=planner, can_route=True, expected_account_id="account-1",
    )
    coordinator.begin(plan, target, universe=instance.universe, quotes=quotes, instruments=metadata)

    expired = coordinator.advance(plan.plan_id)

    assert expired["phase"] == ExecutionPhase.PAUSED.value
    assert "expired" in expired["last_error"]["reason"]
    assert route.submitted == []


def test_daily_instance_runner_only_plans_in_opening_auction() -> None:
    from datetime import datetime

    from alphapilot.systems.live.fsm.session_fsm import SessionState
    from alphapilot.systems.live.instance_runner import StrategyInstanceRunner

    signal = SignalEnvelope(
        kind=SignalKind.INSTRUMENT_TIMING,
        source_instance_id="auction-instance",
        as_of="2026-07-13T15:00:00+08:00",
        frequency="day",
        data_version="features-v1",
        payload=TimingSignal(
            scores={"600000.SSE": 1.0},
            states={"600000.SSE": "long"},
        ),
    )
    decision = PortfolioDecision(
        decision_id="auction-decision",
        instance_id="auction-instance",
        config_hash="auction-config",
        as_of=signal.as_of,
        effective_session="2026-07-14",
        valid_until="2026-07-14T15:00:00+08:00",
        signal=signal,
        target_weights=TargetWeights(
            as_of=signal.as_of,
            weights={"600000.SSE": 0.2},
        ),
    )

    class Clock:
        now = datetime.fromisoformat("2026-07-14T10:00:00+08:00")
        state = SessionState.CONTINUOUS_AM

        def _now_fn(self):  # noqa: ANN202
            return self.now

        def can_submit(self) -> bool:
            return self.state in {
                SessionState.CALL_AUCTION_OPEN,
                SessionState.CONTINUOUS_AM,
                SessionState.CONTINUOUS_PM,
            }

    clock = Clock()
    engine = SimpleNamespace(session=clock, tick_session=lambda: clock.state)
    planned: list[str] = []
    store = SimpleNamespace(
        list_due_decisions=lambda *_args: [
            {"status": "pending", "decision": decision.to_dict()},
        ],
        list_unfinished_execution_plans=lambda *_args: [],
        update_decision_status=lambda *_args: None,
    )
    runner = StrategyInstanceRunner.__new__(StrategyInstanceRunner)
    runner._started = True
    runner._paused = False
    runner._stopped = False
    runner.instance = SimpleNamespace(
        frequency="day",
        instance_id="auction-instance",
        config_hash="auction-config",
    )
    runner.engine = engine
    runner.trading = SimpleNamespace(store=store)
    runner.store = store
    runner._heartbeat = lambda: None
    runner._stage_event = lambda *_args, **_kwargs: None
    runner._halt = lambda reason: (_ for _ in ()).throw(AssertionError(reason))
    runner._plan_decision = lambda item, session: planned.append(
        f"{item.decision_id}:{session}"
    )
    runner.status = lambda: {"instance_id": "auction-instance"}

    status = runner.step()
    assert status["session"] == SessionState.CONTINUOUS_AM.value
    assert planned == []

    clock.now = datetime.fromisoformat("2026-07-14T09:20:00+08:00")
    clock.state = SessionState.CALL_AUCTION_OPEN
    status = runner.step()
    assert status["session"] == SessionState.CALL_AUCTION_OPEN.value
    assert planned == ["auction-decision:2026-07-14"]


def test_live_route_adapter_exposes_stable_broker_fills() -> None:
    from alphapilot.systems.live.execution_adapter import LiveExecutionRouteAdapter
    from alphapilot.systems.live.oms import OMS
    from alphapilot.systems.live.types import Direction, Exchange, Order, OrderStatus, Trade

    oms = OMS()
    oms.on_order(Order(
        order_id="broker-order", code="600000", exchange=Exchange.SSE,
        direction=Direction.LONG, volume=100, traded=100, price=10,
        status=OrderStatus.ALLTRADED, reference="instance:hash:decision:600000.SSE:B:0",
    ))
    oms.on_trade(Trade(
        trade_id="broker-fill", order_id="broker-order", code="600000",
        exchange=Exchange.SSE, direction=Direction.LONG, volume=100, price=10.01,
    ))
    adapter = LiveExecutionRouteAdapter(
        SimpleNamespace(engine=SimpleNamespace(oms=oms)), automated_router=None,
    )
    fills = adapter.child_fills(["instance:hash:decision:600000.SSE:B:0"])
    assert fills == [{
        "fill_key": "broker-fill",
        "reference": "instance:hash:decision:600000.SSE:B:0",
        "order_id": "broker-order",
        "volume": 100.0,
        "price": 10.01,
        "payload": {
            "trade_id": "broker-fill",
            "instrument": "600000.SSE",
            "direction": "long",
        },
    }]


def test_live_route_adapter_cancels_through_runtime_api() -> None:
    from alphapilot.systems.live.execution_adapter import LiveExecutionRouteAdapter
    from alphapilot.systems.live.oms import OMS
    from alphapilot.systems.live.types import Direction, Exchange, Order, OrderStatus

    oms = OMS()
    reference = "instance:hash:decision:600000.SSE:B:0"
    oms.on_order(Order(
        order_id="working-order",
        code="600000",
        exchange=Exchange.SSE,
        direction=Direction.LONG,
        volume=100,
        price=10,
        status=OrderStatus.NOTTRADED,
        reference=reference,
    ))
    cancelled: list[str] = []
    runtime = SimpleNamespace(
        engine=SimpleNamespace(oms=oms),
        cancel_order=lambda order_id: (
            cancelled.append(order_id) or {"cancelled": True}
        ),
    )
    adapter = LiveExecutionRouteAdapter(runtime, automated_router=None)

    assert adapter.cancel_child(reference) is True
    assert cancelled == ["working-order"]


def test_shadow_advances_same_plan_without_touching_route_port(tmp_path: Path) -> None:
    store, instance, snapshot, target, quotes, metadata, planner, plan = _execution_fixture(tmp_path)
    route = _RoutePort()
    coordinator = ExecutionCoordinator(
        store=store, account_port=_AccountPort(snapshot), route_port=route, planner=planner,
        can_route=False, shadow=True, expected_account_id="account-1",
    )
    state = coordinator.begin(
        plan, target, universe=instance.universe, quotes=quotes, instruments=metadata,
    )
    for _ in range(6):
        state = coordinator.advance(plan.plan_id)
    assert state["phase"] == ExecutionPhase.COMPLETED.value
    assert route.submitted == []
    assert store.latest_execution_target(instance.instance_id, instance.config_hash) is None


def test_core_dependency_boundary_has_no_outer_system_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "alphapilot" / "systems"
    core = [
        root / "trading" / name
        for name in (
            "contracts.py", "domain.py", "ports.py", "application.py",
            "portfolio.py", "planning.py", "execution.py", "service.py",
        )
    ]
    forbidden = (
        "alphapilot.systems.live", "alphapilot.systems.timing",
        "alphapilot.systems.selection", "alphapilot.systems.backtest",
        "alphapilot.systems.strategy",
    )
    for path in core:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]
        assert not [name for name in imported if name.startswith(forbidden)], path

    for path in (root / "timing").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]
        assert not [name for name in imported if name.startswith("alphapilot.systems.live")], path

    for name in ("instance_runner.py", "execution_adapter.py"):
        path = root / "live" / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]
        assert not [
            value for value in imported
            if value.startswith(("alphapilot.systems.timing", "alphapilot.systems.backtest"))
        ], path


def test_formal_deployment_preflight_reads_policy_exposure_not_legacy_default(engine) -> None:
    trading = engine.get_system("trading")
    safe = trading.create_instance({
        "instance_id": "policy-safe",
        "strategy_id": "sma_filter",
        "params": {"window": 2},
        "universe": ["SH600000"],
        "frequency": "day",
        "data_policy": {"feature_adjustment": "backward", "data_version": "features-v1"},
    })
    checked = trading.validate_instance(safe["instance_id"])
    assert checked["ok"] is True
    assert checked["deployment_ready"] is True

    unsafe = trading.create_instance({
        "instance_id": "policy-unsafe",
        "strategy_id": "sma_filter",
        "params": {"window": 2},
        "universe": ["SH600000"],
        "frequency": "day",
        "data_policy": {"feature_adjustment": "backward", "data_version": "features-v1"},
        "portfolio_policy": {
            "policy_id": "timing_fixed_exposure",
            "version": "1.0.0",
            "code_hash": trading.policy_registry.get("timing_fixed_exposure").code_hash,
            "params": {
                "target_percent": 0.4,
                "cash_buffer": 0.1,
                "max_position_weight": 0.5,
            },
        },
    })
    checked = trading.validate_instance(unsafe["instance_id"])
    assert checked["ok"] is True
    assert checked["deployment_ready"] is False
    assert "set it to <=" in checked["deployment_errors"][0]


def test_instance_service_resolves_public_policy_binding(engine) -> None:
    trading = engine.get_system("trading")
    created = trading.create_instance({
        "instance_id": "policy-public-binding",
        "strategy_id": "sma_filter",
        "params": {"window": 2},
        "universe": ["SH600000"],
        "frequency": "day",
        "portfolio_policy": {
            "policy_id": "timing_fixed_exposure",
            "params": {"target_percent": 0.15},
        },
    })
    binding = created["config"]["portfolio_policy"]
    definition = trading.policy_registry.get("timing_fixed_exposure")
    assert binding == {
        "policy_id": definition.policy_id,
        "version": definition.version,
        "code_hash": definition.code_hash,
        "params": {
            "target_percent": 0.15,
            "cash_buffer": 0.1,
            "max_position_weight": 0.3,
        },
    }
    assert trading.validate_instance(created["instance_id"])["ok"] is True

    updated = trading.update_instance(created["instance_id"], {
        "portfolio_policy": {
            "policy_id": "timing_fixed_exposure",
            "params": {"target_percent": 0.1, "cash_buffer": 0.2},
        },
    })
    assert updated["config"]["portfolio_policy"]["params"] == {
        "target_percent": 0.1,
        "cash_buffer": 0.2,
        "max_position_weight": 0.3,
    }

    with pytest.raises(ValueError, match="code hash does not match"):
        trading.create_instance({
            "instance_id": "policy-stale-binding",
            "strategy_id": "sma_filter",
            "params": {"window": 2},
            "universe": ["SH600000"],
            "portfolio_policy": {
                "policy_id": "timing_fixed_exposure",
                "code_hash": "stale-policy-code",
            },
        })

    zero_buffer = trading.create_instance({
        "instance_id": "policy-zero-buffer",
        "strategy_id": "sma_filter",
        "params": {"window": 2, "target_percent": 0.1},
        "universe": ["SH600000"],
        "cash_buffer": 0,
    })
    assert zero_buffer["config"]["portfolio_policy"]["params"]["cash_buffer"] == 0


def test_running_or_paused_instance_cannot_be_reconfigured(engine) -> None:
    trading = engine.get_system("trading")
    created = trading.create_instance({
        "instance_id": "immutable-while-running",
        "strategy_id": "sma_filter",
        "params": {"window": 2},
        "universe": ["SH600000"],
    })
    trading.store.set_validation_state(created["instance_id"], "validated")
    trading.store.configure_deployment(DeploymentSpec(
        instance_id=created["instance_id"],
        config_hash=created["config_hash"],
        run_mode="paper",
    ))
    trading.store.transition_runtime(
        created["instance_id"],
        lifecycle="running",
        desired_state="running",
        observed_state="running",
        runtime_id="runtime-active",
    )

    with pytest.raises(ValueError, match="stop the strategy daemon"):
        trading.update_instance(created["instance_id"], {"params": {"window": 3}})

    trading.store.transition_runtime(
        created["instance_id"],
        lifecycle="paused",
        desired_state="paused",
        observed_state="paused",
    )
    with pytest.raises(ValueError, match="stop the strategy daemon"):
        trading.update_instance(created["instance_id"], {"params": {"window": 3}})

    trading.store.transition_runtime(
        created["instance_id"],
        lifecycle="stopped",
        desired_state="stopped",
        observed_state="stopped",
        runtime_id="",
        binding_active=False,
    )
    updated = trading.update_instance(
        created["instance_id"], {"params": {"window": 3}},
    )
    assert updated["validation_state"] == "created"
    assert trading.store.get_deployment_spec(created["instance_id"])["stale"] is True
    assert updated["config_hash"] != created["config_hash"]


def test_replay_config_preserves_zero_controls_and_rejects_invalid_values() -> None:
    config = ReplayConfig(
        open_cost=0,
        close_cost=0,
        min_cost=0,
        partial_fill_ratio=0,
    )
    assert config.open_cost == 0
    assert config.close_cost == 0
    assert config.min_cost == 0
    assert config.partial_fill_ratio == 0

    with pytest.raises(ValueError, match="partial_fill_ratio"):
        ReplayConfig(partial_fill_ratio=1.1)
    with pytest.raises(ValueError, match="lot_size"):
        ReplayConfig(lot_size=0)
    with pytest.raises(ValueError, match="price_tick"):
        InstrumentMetadata("600000.SSE", price_tick=0)


def test_account_sizer_reserves_fees_and_is_symbol_order_independent() -> None:
    instance = StrategyInstanceConfig(
        instance_id="fee-sizing",
        strategy_id="sma_filter",
        strategy_version="1.0.0",
        universe=("600000.SSE", "000001.SZSE"),
    )
    account = AccountSnapshot(
        account_id="paper",
        as_of="2026-07-14",
        balance=1_000,
        available=1_000,
    )
    quotes = {
        symbol: TradableQuote(symbol, "2026-07-14", 10, open=10)
        for symbol in instance.universe
    }
    metadata = {
        symbol: InstrumentMetadata(symbol, lot_size=1)
        for symbol in instance.universe
    }
    fees = FeeSchedule(buy_rate=0.01, sell_rate=0.01)
    sizer = AccountSizer(lot_size=1)

    forward = sizer.size(
        TargetWeights("2026-07-13", {
            "600000.SSE": 0.5,
            "000001.SZSE": 0.5,
        }),
        account,
        {},
        instance,
        quotes=quotes,
        instruments=metadata,
        fees=fees,
    )
    reversed_book = sizer.size(
        TargetWeights("2026-07-13", {
            "000001.SZSE": 0.5,
            "600000.SSE": 0.5,
        }),
        account,
        {},
        instance,
        quotes=quotes,
        instruments=metadata,
        fees=fees,
    )
    assert forward.holdings == reversed_book.holdings == {
        "000001.SZSE": 49.0,
        "600000.SSE": 49.0,
    }

    rotation_account = AccountSnapshot(
        account_id="paper",
        as_of="2026-07-14",
        balance=1_000,
        available=0,
        positions={"600000.SSE": 100},
        sellable={"600000.SSE": 100},
    )
    rotated = sizer.size(
        TargetWeights("2026-07-13", {"000001.SZSE": 1.0}),
        rotation_account,
        {},
        instance,
        quotes=quotes,
        instruments=metadata,
        fees=fees,
    )
    assert rotated.holdings == {"000001.SZSE": 98.0}

    minimum_fee_account = AccountSnapshot(
        account_id="paper",
        as_of="2026-07-14",
        balance=20,
        available=15,
    )
    one_yuan_quotes = {
        symbol: TradableQuote(symbol, "2026-07-14", 1, open=1)
        for symbol in instance.universe
    }
    minimum_fee_target = sizer.size(
        TargetWeights("2026-07-13", {
            "600000.SSE": 0.5,
            "000001.SZSE": 0.5,
        }),
        minimum_fee_account,
        {},
        instance,
        quotes=one_yuan_quotes,
        instruments=metadata,
        fees=FeeSchedule(min_fee=5),
    )
    assert minimum_fee_target.holdings == {
        "000001.SZSE": 2.0,
        "600000.SSE": 2.0,
    }

    split_fee_target = AccountSizer(lot_size=100).size(
        TargetWeights("2026-07-13", {"600000.SSE": 1.0}),
        AccountSnapshot(
            account_id="paper",
            as_of="2026-07-14",
            balance=10_005,
            available=10_005,
        ),
        {},
        instance,
        quotes=quotes,
        instruments={
            "600000.SSE": InstrumentMetadata("600000.SSE", lot_size=100),
        },
        fees=FeeSchedule(min_fee=5, max_order_value=5_000),
    )
    assert split_fee_target.holdings == {"600000.SSE": 900.0}


def test_decision_pipeline_requires_complete_universe_warmup_and_latest_watermark(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from alphapilot.systems.trading.application import WarmupRequired

    registry = StrategyRegistry(local_root=tmp_path / "missing-strategies").discover(
        builtin_contributions=timing_definitions(),
    )
    policies = _policy_registry(tmp_path)
    definition = registry.get("sma_filter")
    policy = policies.get("timing_fixed_exposure")
    instance = StrategyInstanceConfig(
        instance_id="complete-universe",
        strategy_id="sma_filter",
        strategy_version=definition.version,
        params={"window": 2, "target_percent": 0.1},
        universe=("600000.SSE", "000001.SZSE"),
        frequency="day",
        data_policy={"feature_adjustment": "backward", "data_version": "features"},
        portfolio_policy={
            "policy_id": policy.policy_id,
            "version": policy.version,
            "code_hash": policy.code_hash,
            "params": {"target_percent": 0.1, "cash_buffer": 0.1, "max_position_weight": 0.3},
        },
        strategy_code_hash=definition.code_hash,
    )
    calendar = SequenceCalendar([
        "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13", "2026-07-14",
    ])
    pipeline = DecisionPipeline(
        strategy_registry=registry,
        policy_registry=policies,
        store=StrategyRuntimeStore(tmp_path / "watermark.sqlite3"),
        calendar=calendar,
    )
    undersized_window = replace(
        instance,
        data_policy={**instance.data_policy, "history_window": 2},
        config_hash="",
    )
    with pytest.raises(ValueError, match="below required_history=3"):
        pipeline.evaluate(
            undersized_window,
            _bars(
                instance.universe,
                ["2026-07-08", "2026-07-09", "2026-07-10"],
                adjustment=PriceAdjustment.BACKWARD,
                frequency="day",
                data_version="features",
            ),
        )
    only_one = _bars(
        ("600000.SSE",),
        ["2026-07-08", "2026-07-09", "2026-07-10"],
        adjustment=PriceAdjustment.BACKWARD,
        frequency="day",
        data_version="features",
    )
    with pytest.raises(WarmupRequired, match="requires 3 completed bars.*have 0"):
        pipeline.evaluate(instance, only_one)

    misaligned = [
        *_bars(
            instance.universe,
            ["2026-07-08", "2026-07-09", "2026-07-10"],
            adjustment=PriceAdjustment.BACKWARD,
            frequency="day",
            data_version="features",
        ),
        *_bars(
            ("600000.SSE",),
            ["2026-07-13"],
            adjustment=PriceAdjustment.BACKWARD,
            frequency="day",
            data_version="features",
        ),
    ]
    with pytest.raises(ValueError, match="watermark does not cover"):
        pipeline.evaluate(instance, misaligned)

    mixed_versions = _bars(
        instance.universe,
        ["2026-07-08", "2026-07-09", "2026-07-10"],
        adjustment=PriceAdjustment.BACKWARD,
        frequency="day",
        data_version="features",
    )
    mixed_versions[-1] = CompletedBar.from_dict({
        **mixed_versions[-1].to_dict(),
        "data_version": "",
    })
    with pytest.raises(ValueError, match="multiple data versions"):
        pipeline.evaluate(instance, mixed_versions)
    pipeline.close("test_complete")


@pytest.mark.skip(reason="schema v10 intentionally removed LIVE approvals and manual baselines")
def test_live_approval_binds_confirmed_baseline_and_is_consumed_once(engine) -> None:
    trading = engine.get_system("trading")
    row = trading.create_instance({
        "instance_id": "approval-golden",
        "strategy_id": "sma_filter",
        "params": {"window": 2},
        "universe": ["SH600000"],
        "frequency": "day",
        "data_policy": {"feature_adjustment": "backward", "data_version": "features-v1"},
    })
    assert trading.validate_instance(row["instance_id"])["ok"]
    trading.store.record_stage(row["instance_id"], "replay", passed=True)
    trading.store.promote(row["instance_id"], "paper")
    paper_run = trading.store.start_stage_run(row["instance_id"], "paper")
    for day in range(20):
        trading.store.record_stage_session(
            row["instance_id"], config_hash=row["config_hash"], stage="paper",
            session=f"2026-05-{day + 1:02d}",
        )
    trading.store.finish_stage_run(paper_run["run_id"], trading_sessions=20)
    assert trading.store.evaluate_stage(
        row["instance_id"], "paper", minimum_sessions=20
    )["passed"]
    trading.store.promote(row["instance_id"], "shadow")
    run = trading.store.start_stage_run(row["instance_id"], "shadow")
    for day in range(5):
        session = f"2026-06-{day + 1:02d}"
        trading.store.record_stage_session(
            row["instance_id"], config_hash=row["config_hash"], stage="shadow",
            session=session,
        )
        observation = {
            "decision_id": f"decision-{day}",
            "instance_id": row["instance_id"],
            "config_hash": row["config_hash"],
            "as_of": session,
            "effective_session": session,
            "history_hash": f"history-{day}",
            "provider_state_before_hash": f"before-{day}",
            "provider_state_after_hash": f"after-{day}",
            "signal_hash": f"signal-{day}",
            "weights_hash": f"weights-{day}",
            "data_version": "features-v1",
            "model_version": "",
            "policy_version": "1.0.0",
        }
        trading.store.record_decision_observation({
            **observation,
            "observation_id": f"replay-observation-{day}",
            "mode": "replay",
            "run_id": "approval-replay",
        })
        trading.store.record_decision_observation({
            **observation,
            "observation_id": f"shadow-observation-{day}",
            "mode": "shadow",
            "run_id": run["run_id"],
        })
    trading.store.finish_stage_run(run["run_id"], trading_sessions=999)
    parity = DecisionParityService(trading.store).compare(
        row["instance_id"],
        replay_run_id="approval-replay",
        shadow_stage_run_id=run["run_id"],
    )
    assert parity["status"] == "passed"
    assert trading.store.evaluate_stage(
        row["instance_id"], "shadow", minimum_sessions=5
    )["passed"]
    operator = OperatorContext(
        operator_id="risk-operator", request_id="approval-request",
        reason="dedicated account baseline confirmed", auth_source="test",
    )
    issued = trading.authorize_live(row["instance_id"], {
        "account_id": "account-live-1",
        "broker": "paper",
        "reason": operator.reason,
        "baseline_confirmed": True,
        "baseline_positions": {"SH600000": 100},
    }, operator)
    baseline = trading.store.get_account_baseline(row["instance_id"], row["config_hash"])
    assert baseline is not None
    assert baseline["positions"] == {"600000.SSE": 100.0}
    promoted = trading.promote(row["instance_id"], {
        "to": "live", "account_id": "account-live-1", "broker": "paper",
        "approval": issued["approval"],
    })
    assert promoted["deployment_level"] == "live"
    try:
        trading.operator_auth.consume_live_approval(
            issued["approval"], instance_id=row["instance_id"],
            config_hash=row["config_hash"], account_id="account-live-1", broker="paper",
        )
    except ValueError as exc:
        assert "consumed" in str(exc)
    else:  # pragma: no cover - one-shot approvals must never be reusable
        raise AssertionError("LIVE approval was reusable")

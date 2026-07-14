from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from alphapilot.systems.live.brokers.paper import PaperBroker
from alphapilot.systems.live.config import LiveConfig, RunMode, uses_real_providers
from alphapilot.systems.live.control import DaemonRuntimeControl
from alphapilot.systems.live.runtime import LiveRuntime
from alphapilot.systems.trading.authorization import AutomatedRouteAuthorizer
from alphapilot.systems.trading.contracts import (
    CrossSectionalSignal,
    PortfolioInputs,
    SignalEnvelope,
    SignalKind,
    TimingSignal,
)
from alphapilot.systems.trading.deployment import DeploymentCoordinator
from alphapilot.systems.trading.domain import StrategyInstanceConfig
from alphapilot.systems.trading.ports import RouteContext, RouteOrigin, RuntimeCommandResult
from alphapilot.systems.trading.store import LATEST_SCHEMA_VERSION, StrategyRuntimeStore


def _create(store: StrategyRuntimeStore, instance_id: str = "alpha") -> dict:
    return store.create_instance(StrategyInstanceConfig(
        instance_id=instance_id,
        strategy_id="dual_ma",
        strategy_version="1.0.0",
        params={"short_window": 5, "long_window": 20, "target_percent": 0.2},
        universe=("SH600000",),
    ))


def _promote_live(store: StrategyRuntimeStore, instance_id: str = "alpha") -> dict:
    current = _create(store, instance_id)
    for source, target in (("replay", "paper"), ("paper", "shadow")):
        store.record_stage(instance_id, source, passed=True)
        store.promote(instance_id, target)
    store.record_stage(instance_id, "shadow", passed=True)
    store.promote(
        instance_id,
        "live",
        account_id="account-1",
        broker="paper",
        approval="operator-approval",
    )
    return current


def _running_live(store: StrategyRuntimeStore, now: datetime) -> RouteContext:
    current = _promote_live(store)
    store.transition_runtime(
        "alpha",
        lifecycle="running",
        desired_state="running",
        observed_state="running",
        runtime_id="runtime-1",
        runner_heartbeat_at=now.isoformat(),
        reconcile_required=False,
        binding_active=True,
    )
    return RouteContext(
        origin=RouteOrigin.AUTOMATED,
        instance_id="alpha",
        config_hash=current["config_hash"],
        account_id="account-1",
        broker="paper",
        deployment_level="live",
        runtime_id="runtime-1",
    )


def test_automated_route_authorization_matches_full_runtime_binding(tmp_path: Path) -> None:
    now = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    context = _running_live(store, now)
    authorizer = AutomatedRouteAuthorizer(store, now_fn=lambda: now)

    assert authorizer.authorize(context).allowed is True

    stale = RouteContext(**{**context.__dict__, "config_hash": "old-hash"})
    assert authorizer.authorize(stale).rule == "config_hash"
    wrong_account = RouteContext(**{**context.__dict__, "account_id": "other"})
    assert authorizer.authorize(wrong_account).rule == "account_binding"
    wrong_broker = RouteContext(**{**context.__dict__, "broker": "other"})
    assert authorizer.authorize(wrong_broker).rule == "broker_binding"
    wrong_runtime = RouteContext(**{**context.__dict__, "runtime_id": "old-runtime"})
    assert authorizer.authorize(wrong_runtime).rule == "runtime_binding"
    wrong_level = RouteContext(**{**context.__dict__, "deployment_level": "shadow"})
    assert authorizer.authorize(wrong_level).rule == "deployment_level"
    store.update_runtime_state("alpha", binding_active=False)
    assert authorizer.authorize(context).rule == "writer_revoked"
    store.update_runtime_state("alpha", binding_active=True, desired_state="paused")
    assert authorizer.authorize(context).rule == "lifecycle"
    store.update_runtime_state("alpha", desired_state="running")
    store.update_runtime_state("alpha", reconcile_required=True)
    assert authorizer.authorize(context).rule == "reconcile_required"


def test_daemon_control_validates_runtime_strategy_account_and_broker_binding() -> None:
    instance = {
        "instance_id": "alpha",
        "config_hash": "config-1",
        "deployment_level": "live",
        "runtime": {
            "runtime_id": "runtime-1",
            "account_id": "account-1",
            "broker": "xtp",
        },
    }
    result = RuntimeCommandResult(
        True,
        runtime_id="runtime-1",
        runner_status={"instance_id": "alpha", "config_hash": "config-1"},
        raw={
            "mode": "live",
            "trade_broker": "xtp",
            "state": {"account": {"account_id": "account-1"}},
        },
    )
    assert DaemonRuntimeControl._binding_error(result, instance) == ""

    stale = RuntimeCommandResult(
        True,
        runtime_id="runtime-1",
        runner_status={"instance_id": "alpha", "config_hash": "old-config"},
        raw=result.raw,
    )
    assert "config_hash" in DaemonRuntimeControl._binding_error(stale, instance)


@pytest.mark.parametrize(
    ("scope_type", "scope_id"),
    (("instance", "alpha"), ("account", "account-1"), ("global", "ignored")),
)
def test_three_level_kill_switches_fail_closed(
    tmp_path: Path,
    scope_type: str,
    scope_id: str,
) -> None:
    now = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
    store = StrategyRuntimeStore(tmp_path / f"{scope_type}.sqlite3")
    context = _running_live(store, now)
    authorizer = AutomatedRouteAuthorizer(store, now_fn=lambda: now)

    store.set_route_block(scope_type, scope_id, active=True, reason="test")
    denied = authorizer.authorize(context)
    assert denied.allowed is False and denied.rule == "kill_switch"
    store.set_route_block(scope_type, scope_id, active=False)
    assert authorizer.authorize(context).allowed is True


def test_stale_heartbeat_and_manual_origin_are_rejected_by_automated_authorizer(tmp_path: Path) -> None:
    now = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    context = _running_live(store, now - timedelta(seconds=30))
    authorizer = AutomatedRouteAuthorizer(store, heartbeat_ttl_seconds=10, now_fn=lambda: now)

    assert authorizer.authorize(context).rule == "heartbeat_stale"
    assert authorizer.authorize(RouteContext.manual()).rule == "invalid_origin"

    store.update_runtime_state("alpha", runner_heartbeat_at="2026-07-14T02:00:00")
    assert authorizer.authorize(context).rule == "heartbeat_missing"


class FakeRuntimeControl:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.results: dict[str, RuntimeCommandResult] = {}

    def _call(self, action: str) -> RuntimeCommandResult:
        self.calls.append(action)
        return self.results[action]

    def status(self, instance: dict) -> RuntimeCommandResult:
        del instance
        return self._call("status")

    def start(self, instance: dict) -> RuntimeCommandResult:
        del instance
        return self._call("start")

    def pause(self, instance: dict) -> RuntimeCommandResult:
        del instance
        return self._call("pause")

    def reconcile(self, instance: dict) -> RuntimeCommandResult:
        del instance
        return self._call("reconcile")

    def resume(self, instance: dict) -> RuntimeCommandResult:
        del instance
        return self._call("resume")

    def stop(self, instance: dict) -> RuntimeCommandResult:
        del instance
        return self._call("stop")


class InspectingRuntimeControl(FakeRuntimeControl):
    def __init__(self, store: StrategyRuntimeStore) -> None:
        super().__init__()
        self.store = store
        self.observed_before_command: dict[str, dict] = {}

    def _call(self, action: str) -> RuntimeCommandResult:
        self.observed_before_command[action] = {
            "instance": self.store.get_instance("alpha"),
            "runtime": self.store.get_runtime_state("alpha"),
        }
        return super()._call(action)


def _confirmed(lifecycle: str, *, recovery: dict | None = None) -> RuntimeCommandResult:
    return RuntimeCommandResult(
        True,
        command_id=f"command-{lifecycle}",
        runtime_id="runtime-1",
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
        runner_status={"lifecycle": lifecycle},
        recovery=recovery or {},
        raw={
            "trade_broker": "paper",
            "state": {"account": {"account_id": "account-1"}},
        },
    )


def test_live_deployment_requires_reconcile_then_explicit_resume(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    _promote_live(store)
    control = FakeRuntimeControl()
    control.results = {
        "start": _confirmed("paused_pending_reconcile"),
        "reconcile": _confirmed("paused", recovery={"warnings": []}),
        "resume": _confirmed("running"),
    }
    coordinator = DeploymentCoordinator(store, control)

    started = coordinator.start("alpha")
    assert started["ok"] is True
    assert started["runtime"]["reconcile_required"] is True
    assert started["runtime"]["observed_state"] == "paused_pending_reconcile"
    assert store.active_route_blocks(instance_id="alpha", account_id="account-1")
    with pytest.raises(ValueError, match="reconcile"):
        coordinator.resume("alpha")

    assert coordinator.reconcile("alpha")["runtime"]["reconcile_required"] is False
    resumed = coordinator.resume("alpha")
    assert resumed["runtime"]["observed_state"] == "running"
    assert store.active_route_blocks(instance_id="alpha", account_id="account-1") == []
    assert control.calls == ["start", "reconcile", "resume"]


def test_live_resume_can_warm_up_then_become_routable_from_runner_heartbeat(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    current = _promote_live(store)
    control = FakeRuntimeControl()
    control.results = {
        "start": _confirmed("paused_pending_reconcile"),
        "reconcile": _confirmed("paused", recovery={"warnings": []}),
        "resume": _confirmed("warming_up"),
    }
    coordinator = DeploymentCoordinator(store, control)
    coordinator.start("alpha")
    coordinator.reconcile("alpha")

    resumed = coordinator.resume("alpha")
    assert resumed["runtime"]["observed_state"] == "warming_up"
    assert store.active_route_blocks(instance_id="alpha", account_id="account-1") == []

    context = RouteContext(
        origin=RouteOrigin.AUTOMATED,
        instance_id="alpha",
        config_hash=current["config_hash"],
        account_id="account-1",
        broker="paper",
        deployment_level="live",
        runtime_id="runtime-1",
    )
    authorizer = AutomatedRouteAuthorizer(store, now_fn=lambda: now)
    assert authorizer.authorize(context).rule == "lifecycle"
    assert store.record_runtime_heartbeat(
        "alpha",
        config_hash=current["config_hash"],
        runtime_id="runtime-1",
        heartbeat_at=now.isoformat(),
        observed_state="running",
    ) is True
    assert authorizer.authorize(context).allowed is True


def test_runtime_command_timeout_never_becomes_observed_success(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    _promote_live(store)
    control = FakeRuntimeControl()
    control.results["start"] = RuntimeCommandResult(
        False,
        command_id="timed-out",
        error="daemon command timed out",
        timed_out=True,
    )

    result = DeploymentCoordinator(store, control).start("alpha")

    assert result["ok"] is False
    assert result["runtime"]["observed_state"] == "error"
    assert result["runtime"]["reconcile_required"] is True
    assert store.active_route_blocks(instance_id="alpha", account_id="account-1")


def test_coordinator_does_not_publish_observed_state_before_daemon_confirmation(
    tmp_path: Path,
) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    _promote_live(store)
    control = InspectingRuntimeControl(store)
    control.results["start"] = RuntimeCommandResult(False, error="not confirmed", timed_out=True)

    DeploymentCoordinator(store, control).start("alpha")

    before = control.observed_before_command["start"]
    assert before["runtime"]["desired_state"] == "paused"
    assert before["runtime"]["observed_state"] == "ready"
    assert before["instance"]["lifecycle"] == "ready"


def test_failed_stop_keeps_live_writer_binding_reserved(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    current = _promote_live(store)
    store.transition_runtime(
        "alpha",
        lifecycle="running",
        desired_state="running",
        observed_state="running",
        runtime_id="runtime-1",
        runner_heartbeat_at=datetime.now(timezone.utc).isoformat(),
        reconcile_required=False,
        binding_active=True,
    )
    control = InspectingRuntimeControl(store)
    control.results["stop"] = RuntimeCommandResult(False, error="cancel timeout", timed_out=True)

    coordinator = DeploymentCoordinator(store, control)
    result = coordinator.stop("alpha")

    assert control.observed_before_command["stop"]["runtime"]["binding_active"] is True
    assert result["runtime"]["binding_active"] is True
    assert result["runtime"]["observed_state"] == "error"

    # A still-running daemon may heartbeat after the timeout. Its observed
    # truth is recorded, but it cannot erase the coordinator's HALTED state.
    assert store.record_runtime_heartbeat(
        "alpha",
        config_hash=current["config_hash"],
        runtime_id="runtime-1",
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
        observed_state="running",
    ) is True
    assert store.get_runtime_state("alpha")["observed_state"] == "running"
    assert store.get_instance("alpha")["lifecycle"] == "halted"

    control.results["status"] = _confirmed("running")
    status = coordinator.status("alpha")
    assert status["ok"] is False
    assert status["runtime"]["desired_state"] == "paused"
    assert status["runtime"]["observed_state"] == "running"
    assert store.get_instance("alpha")["lifecycle"] == "halted"


def test_reconciliation_warning_keeps_live_deployment_paused(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    _promote_live(store)
    control = FakeRuntimeControl()
    control.results = {
        "start": _confirmed("paused_pending_reconcile"),
        "reconcile": _confirmed(
            "paused_pending_reconcile",
            recovery={"warnings": [{"kind": "external_broker_order", "order_ids": ["x"]}]},
        ),
    }
    coordinator = DeploymentCoordinator(store, control)
    coordinator.start("alpha")

    result = coordinator.reconcile("alpha")

    assert result["ok"] is False
    assert result["runtime"]["reconcile_required"] is True
    assert result["runtime"]["observed_state"] == "paused_pending_reconcile"
    assert store.active_route_blocks(instance_id="alpha", account_id="account-1")


def test_stage_evidence_uses_recorded_sessions_and_detects_duplicates(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    current = _create(store)
    store.record_stage("alpha", "replay", passed=True)
    store.promote("alpha", "paper")
    run = store.start_stage_run("alpha", "paper")
    for day in range(1, 21):
        store.record_stage_session(
            "alpha",
            config_hash=current["config_hash"],
            stage="paper",
            session=f"2026-06-{day:02d}",
        )
    store.record_stage_event(
        "alpha",
        config_hash=current["config_hash"],
        stage="paper",
        event_type="duplicate_routes",
    )
    finished = store.finish_stage_run(run["run_id"], trading_sessions=999)

    assert finished["trading_sessions"] == 20
    assert finished["metrics"]["declared_trading_sessions"] == 999
    evidence = store.evaluate_stage("alpha", "paper", minimum_sessions=20)
    assert evidence["passed"] is False
    assert evidence["failures"]["duplicate_routes"] == 1


def test_stage_evidence_counts_a_trading_date_only_once_across_restarts(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    current = _create(store)
    store.record_stage("alpha", "replay", passed=True)
    store.promote("alpha", "paper")

    first = store.start_stage_run("alpha", "paper")
    store.record_stage_session(
        "alpha", config_hash=current["config_hash"], stage="paper", session="2026-07-14"
    )
    store.finish_stage_run(first["run_id"], trading_sessions=1)
    second = store.start_stage_run("alpha", "paper")
    store.record_stage_session(
        "alpha", config_hash=current["config_hash"], stage="paper", session="2026-07-14"
    )
    store.finish_stage_run(second["run_id"], trading_sessions=1)

    evidence = store.evaluate_stage("alpha", "paper", minimum_sessions=2)
    assert evidence["trading_sessions"] == 1
    assert evidence["passed"] is False


def test_config_change_invalidates_running_stage_and_old_evidence(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    current = _create(store)
    store.record_stage("alpha", "replay", passed=True)
    store.promote("alpha", "paper")
    run = store.start_stage_run("alpha", "paper")
    store.record_stage_session(
        "alpha",
        config_hash=current["config_hash"],
        stage="paper",
        session="2026-07-14",
    )

    updated = store.update_instance(
        "alpha",
        {"params": {"short_window": 10, "long_window": 30, "target_percent": 0.2}},
    )

    assert updated["deployment_level"] == "replay"
    assert store.get_stage_run(run["run_id"])["status"] == "invalidated"
    assert store.deployment("alpha")["evidence"] == []
    with pytest.raises(RuntimeError, match="config changed"):
        store.record_stage(
            "alpha",
            "replay",
            passed=True,
            expected_config_hash=current["config_hash"],
        )


def test_shadow_connects_to_supplied_real_path_but_cannot_route(tmp_path: Path) -> None:
    cfg = LiveConfig(
        mode=RunMode.SHADOW,
        broker="paper",
        trade_broker="paper",
        quote_provider="paper",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
    )
    broker = PaperBroker(cash=100_000, prices={"600000.SSE": 10.0})
    runtime = LiveRuntime.create(cfg, broker=broker, quote_provider=broker)
    runtime.engine.connect({})

    result = runtime.submit_order("SH600000", side="buy", volume=100, price=10.0)

    assert uses_real_providers(RunMode.SHADOW) is True
    assert result["submitted"] is False
    assert result["routing_rule"] == "run_mode"
    assert "routing_disabled_in_shadow" in result["routing_reason"]
    assert runtime.engine.oms.get_position("600000.SSE") is None


def test_parallel_signal_contracts_round_trip_without_composition_algorithm() -> None:
    inputs = PortfolioInputs(
        selection=SignalEnvelope(
            SignalKind.CROSS_SECTIONAL_SELECTION,
            "selection-1",
            "2026-07-14",
            CrossSectionalSignal({"SH600000": 0.8}, {"SH600000": 1}),
        ),
        instrument_timing=(SignalEnvelope(
            SignalKind.INSTRUMENT_TIMING,
            "timing-1",
            "2026-07-14",
            TimingSignal({"SH600000": 0.4}, {"SH600000": "bullish"}),
        ),),
        market_timing=(SignalEnvelope(
            SignalKind.MARKET_TIMING,
            "market-1",
            "2026-07-14",
            TimingSignal({"SH000300": -0.2}, {"SH000300": "risk_off"}),
        ),),
    )

    encoded = json.loads(json.dumps(inputs.to_dict()))
    restored = PortfolioInputs.from_dict(encoded)

    assert restored == inputs
    assert restored.selection is not None
    assert restored.selection.kind == SignalKind.CROSS_SECTIONAL_SELECTION


def test_strategy_code_and_model_artifacts_participate_in_config_hash() -> None:
    base = dict(
        instance_id="alpha",
        strategy_id="dual_ma",
        strategy_version="1.0.0",
        params={"short_window": 5, "long_window": 20},
        universe=("SH600000",),
    )
    first = StrategyInstanceConfig(**base, strategy_code_hash="code-a", model_hash="model-a")
    changed_code = StrategyInstanceConfig(**base, strategy_code_hash="code-b", model_hash="model-a")
    changed_model = StrategyInstanceConfig(**base, strategy_code_hash="code-a", model_hash="model-b")

    assert len({first.config_hash, changed_code.config_hash, changed_model.config_hash}) == 3


def _make_v1_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE strategy_instances (
                instance_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL, config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL, lifecycle TEXT NOT NULL,
                deployment_level TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE stage_evidence (
                instance_id TEXT NOT NULL, stage TEXT NOT NULL, passed INTEGER NOT NULL,
                details_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (instance_id, stage)
            );
            CREATE TABLE deployment_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, instance_id TEXT NOT NULL,
                from_level TEXT NOT NULL, to_level TEXT NOT NULL, config_hash TEXT NOT NULL,
                account_id TEXT NOT NULL DEFAULT '', broker TEXT NOT NULL DEFAULT '',
                approval TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE decisions (
                decision_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE execution_plans (
                plan_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, instance_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE child_orders (
                reference TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                order_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )


def test_versionless_database_migrates_once_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    _make_v1_database(path)

    store = StrategyRuntimeStore(path)
    backups = list(tmp_path.glob("runtime.sqlite3.backup-v1-*"))

    assert store.schema_version == LATEST_SCHEMA_VERSION
    assert len(backups) == 1
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"deployment_runtime", "stage_runs", "stage_run_sessions", "route_blocks"} <= tables
    StrategyRuntimeStore(path)
    assert len(list(tmp_path.glob("runtime.sqlite3.backup-v1-*"))) == 1


def test_failed_migration_preserves_v1_database_and_backup(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "runtime.sqlite3"
    _make_v1_database(path)

    def fail(_db: sqlite3.Connection) -> None:
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(StrategyRuntimeStore, "_migrate_v2", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected"):
        StrategyRuntimeStore(path)

    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "strategy_instances" in tables
        assert "schema_version" not in tables
    assert list(tmp_path.glob("runtime.sqlite3.backup-v1-*"))

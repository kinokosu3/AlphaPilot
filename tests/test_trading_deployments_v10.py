from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3

import pytest

from alphapilot.modules.portal.api import create_app
from alphapilot.modules.trading.module import TradingModule
from alphapilot.systems.trading.account_identity import account_identity_hash
from alphapilot.systems.trading.authorization import AutomatedRouteAuthorizer
from alphapilot.systems.trading.domain import (
    DeploymentSpec,
    InstanceValidationState,
    StrategyInstanceConfig,
)
from alphapilot.systems.trading.comparison import DecisionComparisonService
from alphapilot.systems.trading.ports import RouteContext, RouteOrigin
from alphapilot.systems.trading.store import LATEST_SCHEMA_VERSION, StrategyRuntimeStore


REMOVED_COMMANDS = {
    "trading_promote",
    "trading_authorize_live",
    "trading_qualification",
    "trading_parity_start",
    "trading_parity_status",
    "trading_bind_execution",
    "trading_execution_binding",
}

REMOVED_PATHS = {
    "/api/trading/deployments/{instance_id}/promote",
    "/api/trading/deployments/{instance_id}/authorize-live",
    "/api/trading/deployments/{instance_id}/qualification",
    "/api/trading/deployments/{instance_id}/parity-runs",
    "/api/trading/parity-runs/{run_id}",
    "/api/trading/deployments/{instance_id}/execution-binding",
}


def _instance(instance_id: str = "alpha") -> StrategyInstanceConfig:
    return StrategyInstanceConfig(
        instance_id=instance_id,
        strategy_id="dual_ma",
        strategy_version="1.0.0",
        params={"short_window": 5, "long_window": 20},
        universe=("SH600000",),
        data_policy={
            "feature_adjustment": "backward",
            "history_window": 21,
            "data_version": "test-bars-v1",
        },
        portfolio_policy={
            "policy_id": "timing_fixed_exposure",
            "version": "1.0.0",
            "params": {"target_percent": 0.2},
            "code_hash": "test-policy",
        },
    )


def _validated(store: StrategyRuntimeStore, instance_id: str = "alpha") -> dict:
    row = store.create_instance(_instance(instance_id))
    return store.set_validation_state(
        instance_id, InstanceValidationState.VALIDATED.value,
    )


def _paper_spec(row: dict) -> DeploymentSpec:
    return DeploymentSpec(
        instance_id=row["instance_id"],
        config_hash=row["config_hash"],
        run_mode="paper",
    )


def _live_spec(row: dict, *, account_id: str = "account-1") -> DeploymentSpec:
    return DeploymentSpec(
        instance_id=row["instance_id"],
        config_hash=row["config_hash"],
        run_mode="live",
        execution_environment="live",
        trade_provider="xtp",
        quote_provider="xtp",
        account_id=account_id,
        quote_data_kind="realtime",
    )


def test_removed_promotion_surfaces_are_absent() -> None:
    commands = TradingModule().commands()
    assert REMOVED_COMMANDS.isdisjoint(commands)
    assert {
        "trading_deploy", "trading_deployments", "trading_diagnostics",
        "trading_deployment_subscribe",
        "trading_decision_compare", "trading_decision_comparisons",
    } <= set(commands)

    paths = set(create_app().openapi()["paths"])
    assert REMOVED_PATHS.isdisjoint(paths)
    assert {
        "/api/trading/deployments",
        "/api/trading/deployments/{instance_id}",
        "/api/trading/deployments/{instance_id}/diagnostics",
        "/api/trading/deployments/{instance_id}/observer-subscriptions",
        "/api/trading/deployments/{instance_id}/market/snapshot",
        "/api/trading/deployments/{instance_id}/market/bars",
        "/api/trading/deployments/{instance_id}/decision-comparisons",
        "/api/trading/decision-comparisons/{comparison_id}",
        "/api/live/daemon/subscribe",
    } <= paths


def test_fresh_store_is_v10_without_promotion_tables(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = StrategyRuntimeStore(path)
    assert store.schema_version == LATEST_SCHEMA_VERSION == 10

    with sqlite3.connect(path) as db:
        tables = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "strategy_instances", "deployment_specs", "deployment_runtime",
        "runtime_runs", "runtime_run_sessions", "runtime_run_events",
        "decision_comparisons", "decision_comparison_results",
    } <= tables
    assert {
        "deployment_events", "stage_evidence", "stage_runs",
        "qualification_projections", "parity_runs", "parity_results",
        "live_approvals", "account_baselines", "execution_bindings",
    }.isdisjoint(tables)


def test_v9_store_is_rejected_without_modifying_the_file(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE schema_version (singleton INTEGER PRIMARY KEY, "
            "version INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute("INSERT INTO schema_version VALUES (1, 9, 'legacy')")
    before = path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()

    with pytest.raises(RuntimeError, match="new ALPHAPILOT_STRATEGY_RUNTIME_STORE"):
        StrategyRuntimeStore(path)

    after = path.read_bytes()
    assert hashlib.sha256(after).hexdigest() == before_hash
    assert after == before


def test_deployment_put_is_idempotent_and_rebinds_a_stale_config(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    current = _validated(store)
    first = store.configure_deployment(_paper_spec(current))
    second = store.configure_deployment(_paper_spec(current))

    assert second == first
    assert first["configuration"]["run_mode"] == "paper"
    assert first["runtime"]["desired_state"] == "ready"

    changed = store.update_instance(
        "alpha", {"params": {"short_window": 10, "long_window": 30}},
    )
    assert changed["validation_state"] == "created"
    assert store.get_deployment_spec("alpha")["stale"] is True
    with pytest.raises(ValueError, match="validated"):
        store.configure_deployment(_paper_spec(changed))

    validated = store.set_validation_state("alpha", "validated")
    rebound = store.configure_deployment(_paper_spec(validated))
    assert rebound["configuration"]["stale"] is False
    assert rebound["configuration"]["config_hash"] == validated["config_hash"]
    assert rebound["configuration"]["version"] == first["configuration"]["version"] + 1


def test_live_needs_no_stage_uat_or_approval_but_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    current = _validated(store)
    configured = store.configure_deployment(_live_spec(current))
    spec = configured["configuration"]
    assert spec["account_id"] == account_identity_hash("account-1")
    store.transition_runtime(
        "alpha",
        lifecycle="running",
        desired_state="running",
        observed_state="running",
        runtime_id="runtime-1",
        runner_heartbeat_at=now.isoformat(),
        reconcile_required=False,
        reconciled=True,
        binding_active=True,
    )
    context = RouteContext(
        origin=RouteOrigin.AUTOMATED,
        instance_id="alpha",
        config_hash=current["config_hash"],
        account_id="account-1",
        broker="xtp",
        run_mode="live",
        runtime_id="runtime-1",
        execution_environment="live",
        trade_provider="xtp",
        quote_provider="xtp",
        quote_data_kind="realtime",
        binding_hash=spec["binding_hash"],
    )
    authorizer = AutomatedRouteAuthorizer(store, now_fn=lambda: now)

    monkeypatch.delenv("ALPHAPILOT_AUTOMATED_LIVE_ENABLED", raising=False)
    assert authorizer.authorize(context).rule == "live_disabled"
    monkeypatch.setenv("ALPHAPILOT_AUTOMATED_LIVE_ENABLED", "true")
    assert authorizer.authorize(context).allowed is True
    assert authorizer.authorize(
        RouteContext(**{**context.__dict__, "account_id": "other"})
    ).rule == "account_binding"
    store.update_runtime_state("alpha", account_id="other")
    assert authorizer.authorize(
        RouteContext(**{**context.__dict__, "account_id": "other"})
    ).rule == "deployment_account_binding"
    store.update_runtime_state("alpha", account_id="account-1")
    store.update_runtime_state("alpha", reconciled=False)
    assert authorizer.authorize(context).rule == "not_reconciled"


def test_shadow_never_routes_and_external_accounts_have_one_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    first = _validated(store, "alpha")
    second = _validated(store, "beta")
    live = store.configure_deployment(_live_spec(first))["configuration"]
    store.transition_runtime(
        "alpha", lifecycle="running", desired_state="running", observed_state="running",
        runtime_id="runtime-a", runner_heartbeat_at=now.isoformat(),
        reconcile_required=False, reconciled=True, binding_active=True,
    )
    store.configure_deployment(_live_spec(
        second, account_id=account_identity_hash("account-1"),
    ))
    with pytest.raises(ValueError, match="already has an active automated writer"):
        store.transition_runtime(
            "beta", lifecycle="running", desired_state="running", observed_state="running",
            runtime_id="runtime-b", runner_heartbeat_at=now.isoformat(),
            reconcile_required=False, reconciled=True, binding_active=True,
        )

    store.transition_runtime(
        "alpha", lifecycle="stopped", desired_state="stopped",
        observed_state="stopped", runtime_id="", binding_active=False,
    )
    shadow_spec = DeploymentSpec(
        instance_id="alpha",
        config_hash=first["config_hash"],
        run_mode="shadow",
        execution_environment="live",
        trade_provider="xtp",
        quote_provider="xtp",
        account_id="account-1",
        quote_data_kind="realtime",
    )
    shadow = store.configure_deployment(shadow_spec)["configuration"]
    store.transition_runtime(
        "alpha", lifecycle="running", desired_state="running", observed_state="running",
        runtime_id="runtime-shadow", runner_heartbeat_at=now.isoformat(),
        reconcile_required=False, reconciled=True, binding_active=False,
    )
    monkeypatch.setenv("ALPHAPILOT_AUTOMATED_LIVE_ENABLED", "true")
    denied = AutomatedRouteAuthorizer(store, now_fn=lambda: now).authorize(RouteContext(
        origin=RouteOrigin.AUTOMATED,
        instance_id="alpha",
        config_hash=first["config_hash"],
        account_id="account-1",
        broker="xtp",
        run_mode="shadow",
        runtime_id="runtime-shadow",
        execution_environment="live",
        trade_provider="xtp",
        quote_provider="xtp",
        quote_data_kind="realtime",
        binding_hash=shadow["binding_hash"],
    ))
    assert denied.rule == "shadow_no_route"
    assert live["binding_hash"] != shadow["binding_hash"]


def test_runtime_diagnostics_and_comparisons_do_not_change_deployment(
    tmp_path: Path,
) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    current = _validated(store)
    configured = store.configure_deployment(_paper_spec(current))["configuration"]
    active = store.start_runtime_run("alpha", "paper", run_id="active")
    with pytest.raises(ValueError, match="already has an active runtime run"):
        store.start_runtime_run("alpha", "paper", run_id="duplicate")
    store.finish_runtime_run(active["run_id"])
    observation = {
        "instance_id": "alpha",
        "config_hash": current["config_hash"],
        "mode": "paper",
        "as_of": "2026-07-21",
        "effective_session": "2026-07-22",
        "history_hash": "history",
        "provider_state_before_hash": "before",
        "provider_state_after_hash": "after",
        "signal_hash": "signal",
        "weights_hash": "weights",
        "data_version": "data-v1",
        "model_version": "model-v1",
        "policy_version": "policy-v1",
    }
    for run_id in ("left", "right"):
        store.start_runtime_run("alpha", "paper", run_id=run_id)
        store.record_runtime_session(
            "alpha", config_hash=current["config_hash"], run_mode="paper",
            session="2026-07-21",
        )
        store.record_decision_observation({
            **observation,
            "observation_id": f"observation-{run_id}",
            "decision_id": f"decision-{run_id}",
            "run_id": run_id,
        })
        store.finish_runtime_run(run_id)

    diagnostics = store.runtime_diagnostics("alpha")
    assert diagnostics["modes"]["paper"]["trading_sessions"] == 1
    comparison = DecisionComparisonService(store).compare(
        "alpha",
        left_mode="paper",
        left_run_id="left",
        right_mode="paper",
        right_run_id="right",
    )
    assert comparison["status"] == "completed"
    assert comparison["match_count"] == 1
    after = store.get_deployment_spec("alpha")
    assert after["binding_hash"] == configured["binding_hash"]
    assert after["stale"] is False

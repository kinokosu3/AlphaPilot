from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from pathlib import Path

import pandas as pd
import pytest

from alphapilot.systems.live import broker_uat as broker_uat_module
from alphapilot.systems.live.broker_uat import BrokerUATHarness, CONFIRMATION
from alphapilot.systems.live.redaction import redact_secrets
from alphapilot.systems.timing.base import TimingBacktestRequest
from alphapilot.systems.trading.domain import StrategyInstanceConfig
from alphapilot.systems.trading.parity import (
    DecisionParityService,
    DeploymentQualificationService,
)
from alphapilot.systems.trading.compatibility import (
    RemovalReadinessService,
    compatibility_environment_report_hash,
    validate_compatibility_environment_report,
)
from alphapilot.systems.trading.release_verification import (
    REMOVAL_BASE_COMMIT,
    REMOVAL_REPORT_RELATIVE_PATH,
    REPORT_RELATIVE_PATH,
    REPORT_SCHEMA_VERSION,
    REQUIRED_CHECKS,
    canonical_report_hash,
    required_checks_for,
    validate_release_verification,
)
from alphapilot.systems.trading.service import (
    TradingStrategySystem,
    _latest_cutoff,
    _utc_timestamp,
)
from alphapilot.systems.trading.store import StrategyRuntimeStore


def _instance(instance_id: str = "qualification-demo") -> StrategyInstanceConfig:
    return StrategyInstanceConfig(
        instance_id=instance_id,
        strategy_id="sma_filter",
        strategy_version="1.0.0",
        universe=("600000.SSE",),
        data_policy={"history_window": 2, "data_version": "features-v1"},
        portfolio_policy={
            "policy_id": "timing_fixed_exposure",
            "version": "1.0.0",
            "params": {"target_percent": 0.2},
        },
        strategy_code_hash="code-v1",
    )


def _make_v5_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        for version in range(1, 6):
            getattr(StrategyRuntimeStore, f"_migrate_v{version}")(connection)
        connection.commit()
    finally:
        connection.close()


def _observation(
    instance: StrategyInstanceConfig,
    *,
    mode: str,
    run_id: str,
    session: str,
    suffix: str,
    signal_hash: str | None = None,
    data_version: str = "features-v1",
) -> dict[str, str]:
    return {
        "observation_id": f"{mode}-{run_id}-{suffix}",
        "decision_id": f"decision-{suffix}",
        "instance_id": instance.instance_id,
        "config_hash": instance.config_hash,
        "mode": mode,
        "run_id": run_id,
        "as_of": session,
        "effective_session": session,
        "history_hash": f"history-{suffix}",
        "provider_state_before_hash": f"before-{suffix}",
        "provider_state_after_hash": f"after-{suffix}",
        "signal_hash": signal_hash or f"signal-{suffix}",
        "weights_hash": f"weights-{suffix}",
        "data_version": data_version,
        "model_version": "model-v1",
        "policy_version": "1.0.0",
    }


def _record_stage_days(
    store: StrategyRuntimeStore,
    instance: StrategyInstanceConfig,
    stage: str,
    sessions: list[str],
) -> dict[str, object]:
    run = store.start_stage_run(instance.instance_id, stage)
    for session in sessions:
        assert store.record_stage_session(
            instance.instance_id,
            config_hash=instance.config_hash,
            stage=stage,
            session=session,
        )
    return store.finish_stage_run(run["run_id"], trading_sessions=999)


def test_acceptance_stage_evidence_before_migration_cutoff_is_not_reused(
    tmp_path: Path,
) -> None:
    store = StrategyRuntimeStore(tmp_path / "cutoff-evidence.sqlite3")
    instance = _instance("cutoff-evidence")
    store.create_instance(instance)
    store.record_stage(instance.instance_id, "replay", passed=True, details={})
    store.promote(instance.instance_id, "paper")
    completed = _record_stage_days(
        store,
        instance,
        "paper",
        [f"2026-06-{day:02d}" for day in range(1, 21)],
    )

    assert store.evaluate_stage(
        instance.instance_id, "paper", minimum_sessions=20,
    )["passed"] is True
    filtered = store.evaluate_stage(
        instance.instance_id,
        "paper",
        minimum_sessions=20,
        started_after=(
            datetime.fromisoformat(completed["ended_at"]) + timedelta(seconds=1)
        ).isoformat(),
    )
    assert filtered["passed"] is False
    assert filtered["trading_sessions"] == 0


def test_parity_and_qualification_are_derived_from_daily_observations(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    instance = _instance()
    store.create_instance(instance)
    store.record_stage(instance.instance_id, "replay", passed=True)
    store.promote(instance.instance_id, "paper")
    _record_stage_days(
        store,
        instance,
        "paper",
        [f"2026-05-{day:02d}" for day in range(1, 21)],
    )
    assert store.evaluate_stage(instance.instance_id, "paper", minimum_sessions=20)["passed"]
    store.promote(instance.instance_id, "shadow")
    shadow = _record_stage_days(
        store,
        instance,
        "shadow",
        [f"2026-06-{day:02d}" for day in range(1, 6)],
    )
    for day in range(1, 6):
        session = f"2026-06-{day:02d}"
        store.record_decision_observation(_observation(
            instance, mode="replay", run_id="replay-golden", session=session,
            suffix=str(day),
        ))
        store.record_decision_observation(_observation(
            instance, mode="shadow", run_id=str(shadow["run_id"]), session=session,
            suffix=str(day),
        ))

    parity = DecisionParityService(store).compare(
        instance.instance_id,
        replay_run_id="replay-golden",
        shadow_stage_run_id=str(shadow["run_id"]),
    )
    assert parity["status"] == "passed"
    assert parity["pass_count"] == 5
    qualification = DeploymentQualificationService(store).evaluate(instance.instance_id)
    assert qualification["paper"]["trading_sessions"] == 20
    assert qualification["shadow"]["trading_sessions"] == 5
    assert qualification["parity"]["passed"] is True
    assert qualification["eligible_for_live_authorization"] is True
    projection = store.get_qualification_projection(instance.instance_id)
    assert projection is not None
    assert projection["eligible"] is True
    assert projection["config_hash"] == instance.config_hash

    # A later contradictory comparison for a counted SHADOW session must
    # invalidate qualification; an older PASS cannot mask it.
    store.record_decision_observation(_observation(
        instance, mode="replay", run_id="replay-revised", session="2026-06-01",
        suffix="revised",
    ))
    store.record_decision_observation(_observation(
        instance, mode="shadow", run_id="shadow-revised", session="2026-06-01",
        suffix="revised", signal_hash="different-signal",
    ))
    contradictory = DecisionParityService(store).compare(
        instance.instance_id,
        replay_run_id="replay-revised",
        shadow_stage_run_id="shadow-revised",
    )
    assert contradictory["status"] == "failed"
    revoked = DeploymentQualificationService(store).evaluate(instance.instance_id)
    assert revoked["parity"]["passed"] is False
    assert revoked["parity"]["invalid_sessions"]["2026-06-01"] == ["mismatch", "pass"]
    assert store.get_qualification_projection(instance.instance_id)["eligible"] is False

    store.update_instance(instance.instance_id, {"params": {"window": 10}})
    assert store.get_qualification_projection(instance.instance_id) is None


@pytest.mark.parametrize(
    ("shadow_overrides", "expected"),
    [
        ({"signal_hash": "different"}, "mismatch"),
        ({"data_version": "revised-v2"}, "not_comparable"),
    ],
)
def test_parity_distinguishes_output_mismatch_from_incomparable_input(
    tmp_path: Path,
    shadow_overrides: dict[str, str],
    expected: str,
) -> None:
    store = StrategyRuntimeStore(tmp_path / f"{expected}.sqlite3")
    instance = _instance(f"parity-{expected}")
    store.create_instance(instance)
    replay = _observation(
        instance, mode="replay", run_id="replay", session="2026-06-01", suffix="1",
    )
    shadow = _observation(
        instance, mode="shadow", run_id="shadow", session="2026-06-01", suffix="1",
        **shadow_overrides,
    )
    store.record_decision_observation(replay)
    store.record_decision_observation(shadow)
    result = DecisionParityService(store).compare(
        instance.instance_id,
        replay_run_id="replay",
        shadow_stage_run_id="shadow",
    )
    assert result["status"] == "failed"
    assert result["results"][0]["status"] == expected


def test_daily_parity_compares_session_even_when_feed_timestamps_differ(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "daily-parity.sqlite3")
    instance = _instance("daily-feed-times")
    store.create_instance(instance)
    replay = _observation(
        instance, mode="replay", run_id="replay", session="2026-06-01T00:00:00",
        suffix="same",
    )
    shadow = _observation(
        instance, mode="shadow", run_id="shadow", session="2026-06-01T15:00:00+08:00",
        suffix="same",
    )
    store.record_decision_observation(replay)
    store.record_decision_observation(shadow)

    result = DecisionParityService(store).compare(
        instance.instance_id,
        replay_run_id="replay",
        shadow_stage_run_id="shadow",
    )

    assert result["status"] == "passed"
    assert result["results"][0]["session"] == "2026-06-01"


def test_removal_check_derives_the_complete_post_cutoff_acceptance_cycle() -> None:
    cutoff = "2026-01-01T00:00:00+00:00"
    config_hash = "current-config"
    stage_runs = [
        {
            "stage": "paper", "config_hash": config_hash, "status": "completed",
            "started_at": "2026-01-02T00:00:00+00:00",
            "ended_at": "2026-02-01T00:00:00+00:00",
        },
        {
            "stage": "shadow", "config_hash": config_hash, "status": "completed",
            "started_at": "2026-02-02T00:00:00+00:00",
            "ended_at": "2026-02-10T00:00:00+00:00",
        },
    ]
    evidence = {
        "evidence_id": "uat-evidence", "evidence_hash": "uat-hash",
        "environment": "protected", "plugin_version": "1.2.3",
        "sdk_hash": "sdk-hash", "runtime_code_hash": "runtime-hash",
        "passed_at": "2026-02-11T00:00:00+00:00",
        "expires_at": "2026-05-01T00:00:00+00:00",
    }
    store = SimpleNamespace(
        schema_version=8,
        evaluate_stage=lambda instance_id, stage, **kwargs: {
            "passed": True,
            "stage": stage,
            "trading_sessions": 20 if stage == "paper" else 5,
            "started_after": kwargs["started_after"],
        },
        list_stage_sessions=lambda *_args, **_kwargs: ["2026-02-05"],
        list_parity_runs=lambda _instance_id: [{
            "config_hash": config_hash,
            "status": "passed",
            "created_at": "2026-02-06T00:00:00+00:00",
            "updated_at": "2026-02-10T01:00:00+00:00",
            "results": [{"session": "2026-02-05", "status": "pass"}],
        }],
        valid_broker_uat_evidence=lambda *_args, **_kwargs: dict(evidence),
        list_stage_runs=lambda _instance_id: list(stage_runs),
    )
    system = object.__new__(TradingStrategySystem)
    system.store = store
    system.compatibility_status = lambda: {"ready": True}
    system.removal_readiness_service = SimpleNamespace(evaluate=lambda _instance_id: {
        "checks": {"base_readiness": True},
        "environments": [{
            "environment_id": "env-a",
            "migration_cutoff": cutoff,
            "evidence_hash": "environment-hash",
            "evidence": {"generated_at": "2026-02-12T00:00:00+00:00"},
        }],
        "broker_uat": {},
        "code": {"commit": "a" * 40},
        "release_verification": {"report_hash": "release-hash"},
    })
    system._qualification = lambda _instance_id: {
        "eligible_for_live_authorization": True,
        "config_hash": config_hash,
    }
    system._timing_equivalence_status = lambda: {"passed": True}
    system.broker_uat_harness = SimpleNamespace(plugin_metadata=lambda broker: {
        "plugin_version": f"{broker}-version",
        "plugin_hash": f"{broker}-hash",
        "sdk_version": f"{broker}-sdk",
        "sdk_hash": "sdk-hash",
        "runtime_code_hash": "runtime-hash",
    })

    report = system.removal_check("acceptance-instance")

    assert report["ready"] is True
    assert report["removal_qualification"]["observation_cutoff"] == cutoff
    assert report["removal_qualification"]["live_waiting_period_required"] is False
    assert report["live_qualification"]["eligible_for_live_authorization"] is True
    assert report["checks"]["xtp_uat"] is True
    assert report["checks"]["emt_uat"] is True
    assert len(report["evidence_hash"]) == 64
    assert len(report["report_hash"]) == 64

    with pytest.raises(ValueError, match="acceptance_instance_id"):
        system.removal_check("")


def test_trading_service_time_helpers_and_local_uat_forwarding() -> None:
    assert _utc_timestamp("not-a-time") is None
    assert _utc_timestamp("2026-07-14T12:00:00").tzinfo is not None
    assert _latest_cutoff(["bad", "2026-07-14T12:00:00+00:00"]).startswith(
        "2026-07-14T12:00:00",
    )
    calls: list[tuple[str, object]] = []
    harness = SimpleNamespace(
        preflight=lambda **payload: calls.append(("preflight", payload))
        or {"query_only": True},
        start=lambda **payload: calls.append(("start", payload)) or {"status": "running"},
        resume=lambda run_id, **payload: calls.append(("resume", (run_id, payload)))
        or {"status": "passed"},
        abort=lambda run_id, **payload: calls.append(("abort", (run_id, payload)))
        or {"status": "aborted"},
    )
    system = object.__new__(TradingStrategySystem)
    system.broker_uat_harness = harness

    assert system.broker_uat_preflight({
        "broker": "xtp", "symbols": ["600000.SSE"], "max_notional": 20000,
    })["query_only"] is True
    assert system.start_broker_uat({
        "broker": "xtp", "symbol": "600000.SSE", "side": "buy",
        "volume": 100, "price": 10, "max_notional": 1500,
        "confirmation": CONFIRMATION,
    })["status"] == "running"
    assert system.resume_broker_uat("uat-1", {"confirmation": CONFIRMATION})["status"] == "passed"
    assert system.abort_broker_uat("uat-1", {
        "confirmation": CONFIRMATION, "reason": "operator stop",
    })["status"] == "aborted"
    assert [name for name, _payload in calls] == ["preflight", "start", "resume", "abort"]


class _FakeOMS:
    def __init__(self, state) -> None:  # noqa: ANN001
        self.state = state
        self.account = SimpleNamespace(account_id="uat-account")
        self.contracts = {
            "600000.SSE": SimpleNamespace(
                lot_size=100, price_tick=0.01, settlement_days=1,
                product=SimpleNamespace(value="equity"),
            ),
        }

    def get_positions(self):  # noqa: ANN201
        return []

    def get_contract(self, symbol: str):  # noqa: ANN201
        return self.contracts.get(symbol)

    def get_tick(self, symbol: str):  # noqa: ANN201
        if symbol not in self.contracts:
            return None
        return SimpleNamespace(
            key=symbol, last_price=10.0, bid_price_1=9.99, ask_price_1=10.0,
            bid_volume_1=1000, ask_volume_1=1000,
        )

    def get_active_orders(self):  # noqa: ANN201
        return [
            SimpleNamespace(**order)
            for order in self.state.orders.values() if order["active"]
        ]


class _FakeBrokerState:
    def __init__(self, *, fail_reconnect_once: bool = False) -> None:
        self.orders: dict[str, dict[str, object]] = {}
        self.subscriptions: list[str] = []
        self.fail_reconnect_once = fail_reconnect_once


class _FakeEngine:
    def __init__(self, state: _FakeBrokerState) -> None:
        self.state = state
        self.oms = _FakeOMS(state)
        self.connection = SimpleNamespace(state="logged_in")

    def subscribe_market_data(self, symbols) -> None:  # noqa: ANN001
        self.state.subscriptions.extend(str(item) for item in symbols)


class _FakeUATRuntime:
    def __init__(self, store: StrategyRuntimeStore, state: _FakeBrokerState) -> None:
        self.store = store
        self.state = state
        self.engine = _FakeEngine(state)

    def connect(self) -> None:
        return None

    def wait_ready(self, timeout: float) -> bool:
        return timeout > 0

    def close(self) -> None:
        return None

    def submit_order(self, symbol: str, **payload):  # noqa: ANN003, ANN201
        active_blocks = [row for row in self.store.list_route_blocks() if row["active"]]
        if active_blocks:
            return {"submitted": False, "routing_rule": "kill_switch"}
        reference = str(payload["reference"])
        if any(str(order["reference"]) == reference for order in self.state.orders.values()):
            return {"submitted": False, "routing_rule": "duplicate"}
        suffix = "FILL" if reference.endswith("/fill") else "REMAINDER"
        order_id = f"BROKER-ORDER-{suffix}"
        volume = float(payload["volume"])
        is_fill = reference.endswith("/fill")
        self.state.orders[order_id] = {
            "order_id": order_id,
            "symbol": symbol,
            "reference": reference,
            "volume": volume,
            "traded": volume if is_fill else 0.0,
            "active": not is_fill,
            "terminal": is_fill,
            "status": "alltraded" if is_fill else "nottraded",
        }
        return {"submitted": True, "order_id": order_id}

    def wait_for_order_ack(self, order_id: str, timeout: float):  # noqa: ANN201
        return {"acknowledged": bool(order_id in self.state.orders and timeout > 0), "order_id": order_id}

    def order_state(self, order_id: str):  # noqa: ANN201
        order = self.state.orders.get(order_id)
        if order is None:
            return {"found": False, "active": False, "terminal": False}
        return {
            "found": True,
            "active": bool(order["active"]),
            "terminal": bool(order["terminal"]),
            "order": dict(order),
        }

    def settle_broker_events(self, timeout: float) -> None:
        del timeout

    def refresh_broker_state(self, **_payload) -> None:  # noqa: ANN003
        return None

    def cancel_order(self, order_id: str):  # noqa: ANN201
        assert order_id in self.state.orders
        self.state.orders[order_id]["active"] = False
        self.state.orders[order_id]["terminal"] = True
        self.state.orders[order_id]["status"] = "cancelled"
        return {"cancelled": True, "order_id": order_id}

    def wait_for_order_terminal(self, order_id: str, timeout: float):  # noqa: ANN201
        current = self.order_state(order_id)
        return {"terminal": current["terminal"] and timeout > 0, "order_id": order_id}

    def reconnect(self, *, auto_resume: bool):  # noqa: ANN201
        assert auto_resume is False
        if self.state.fail_reconnect_once:
            self.state.fail_reconnect_once = False
            raise RuntimeError("injected reconnect failure")
        return {"recovery": {"warnings": []}}


def _uat_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_reconnect_once: bool = False,
) -> tuple[BrokerUATHarness, StrategyRuntimeStore]:
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_ENABLED", "true")
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_WHITELIST", "600000.SSE")
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_MAX_NOTIONAL", "2000")
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_ENVIRONMENT", "protected-test-account")
    metadata = {
        "broker": "xtp", "plugin_id": "xtp", "distribution": "fake-xtp",
        "plugin_version": "1.2.3", "gateway_path": "fake:gateway",
        "gateway_source_hash": "source-hash", "sdk_version": "9.9",
        "sdk_hash": "sdk-hash", "runtime_code_hash": "runtime-hash",
        "code_commit": "a" * 40, "plugin_hash": "plugin-hash",
    }
    monkeypatch.setattr(
        "alphapilot.systems.live.broker_uat._plugin_metadata",
        lambda broker: {**metadata, "broker": broker},
    )
    store = StrategyRuntimeStore(tmp_path / "uat.sqlite3")
    state = _FakeBrokerState(fail_reconnect_once=fail_reconnect_once)
    process_ids = iter(range(10_000, 10_100))
    harness = BrokerUATHarness(
        store,
        runtime_factory=lambda _broker: _FakeUATRuntime(store, state),
        sleep_fn=lambda _seconds: None,
        preflight_fn=lambda broker, timeout: {
            "ok": True,
            "broker": broker,
            "timeout": timeout,
            "architecture": {"machine": "test", "python_bits": 64},
            "channels": [
                {"role": "trade", "ok": True},
                {"role": "quote", "ok": True},
            ],
        },
        process_id_fn=lambda: next(process_ids),
    )
    return harness, store


def test_broker_uat_issues_callback_derived_expiring_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, store = _uat_harness(tmp_path, monkeypatch)
    checkpoint = harness.start(
        broker="xtp", symbol="600000.SSE", side="buy", volume=200, price=10,
        max_notional=2000, confirmation=CONFIRMATION, timeout=1,
    )
    assert checkpoint["status"] == "restart_required"
    assert checkpoint["evidence"] is None
    result = harness.resume(
        checkpoint["run_id"], confirmation=CONFIRMATION, timeout=1,
    )
    assert result["status"] == "passed"
    assert result["evidence"]["plugin_version"] == "1.2.3"
    assert result["evidence"]["plugin_hash"] == "plugin-hash"
    assert result["evidence"]["scenario_version"] == 2
    assert result["evidence"]["sdk_hash"] == "sdk-hash"
    assert result["evidence"]["runtime_code_hash"] == "runtime-hash"
    assert result["evidence"]["filled_notional"] > 0
    assert result["order_events"]
    assert result["evidence"]["environment"] == "protected-test-account"
    assert store.valid_broker_uat_evidence(
        "xtp",
        account_hash=result["account_hash"],
        environment="protected-test-account",
        plugin_version="1.2.3",
        plugin_hash="plugin-hash",
        sdk_version="9.9",
    ) is not None
    assert store.valid_broker_uat_evidence(
        "xtp",
        account_hash=result["account_hash"],
        environment="a-different-environment",
        plugin_version="1.2.3",
        plugin_hash="plugin-hash",
        sdk_version="9.9",
    ) is None
    required = {
        "preflight", "connected", "execution_plan", "marketable_order_acknowledged",
        "marketable_fill_observed", "remainder_order_acknowledged",
        "plan_partial_execution_observed", "process_restart_required",
        "restart_reconciled", "kill_switches_verified", "cancel_confirmed",
        "reconnect_reconciled",
    }
    assert required <= {step["step"] for step in result["steps"] if step["status"] == "passed"}
    assert not any(row["active"] for row in store.list_route_blocks())


def test_broker_uat_query_only_preflight_subscribes_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _store = _uat_harness(tmp_path, monkeypatch)

    result = harness.preflight(
        broker="xtp", symbols=["600000.SSE"], max_notional=2000, timeout=1,
    )

    assert result["account_hash"] == broker_uat_module._hash_text("uat-account")
    assert result["candidates"][0]["symbol"] == "600000.SSE"
    assert result["candidates"][0]["eligible"] is True
    assert result["active_orders"] == 0


def test_broker_uat_v2_uses_a_distinct_resting_price_and_actual_trade_amount() -> None:
    plan = broker_uat_module._build_execution_plan(
        {
            "symbol": "600000.SSE",
            "side": "buy",
            "volume": 200,
            "price": 10.0,
            "max_notional": 2000.0,
        },
        contract=SimpleNamespace(lot_size=100, price_tick=0.01),
        tick=SimpleNamespace(last_price=10.0, bid_price_1=9.99, ask_price_1=10.0),
    )

    assert plan["fill"] == {"volume": 100.0, "price": 10.0}
    assert plan["remainder"]["price"] <= 9.6
    assert plan["requested_notional"] <= 2000.0

    runtime = SimpleNamespace(
        engine=SimpleNamespace(
            oms=SimpleNamespace(
                get_trades=lambda: [
                    SimpleNamespace(order_id="fill", volume=100, price=9.98),
                ],
            ),
        ),
        order_state=lambda _order_id: {"order": {"traded": 100}},
    )
    assert broker_uat_module._actual_filled_notional(
        runtime,
        {"fill": 10.0, "remainder": 9.6},
    ) == pytest.approx(998.0)


def test_broker_uat_fingerprint_and_candidate_helpers_are_deterministic() -> None:
    assert len(broker_uat_module._runtime_code_hash()) == 64
    assert len(broker_uat_module._git_commit()) == 40
    assert broker_uat_module._distribution_hash("package-that-does-not-exist") == ""
    with pytest.raises(ValueError, match="unsupported Broker SDK"):
        broker_uat_module._native_sdk_hash("paper")

    contracts = {
        "600001.SSE": SimpleNamespace(
            product=SimpleNamespace(value="equity"),
            exchange=SimpleNamespace(value="SSE"),
        ),
        "510300.SSE": SimpleNamespace(
            product=SimpleNamespace(value="fund"),
            exchange=SimpleNamespace(value="SSE"),
        ),
        "IF0001.CFFEX": SimpleNamespace(
            product=SimpleNamespace(value="futures"),
            exchange=SimpleNamespace(value="CFFEX"),
        ),
    }
    assert broker_uat_module._default_candidate_symbols(contracts) == [
        "510300.SSE",
        "600001.SSE",
    ]


def test_broker_uat_execution_plan_rejects_unsafe_prices_and_supports_sell() -> None:
    contract = SimpleNamespace(lot_size=100, price_tick=0.01)
    tick = SimpleNamespace(last_price=10.0, bid_price_1=9.99, ask_price_1=10.0)
    sell = broker_uat_module._build_execution_plan(
        {
            "symbol": "600000.SSE",
            "side": "sell",
            "volume": 200,
            "price": 9.99,
            "max_notional": 3000.0,
        },
        contract=contract,
        tick=tick,
    )
    assert sell["remainder"]["price"] >= 10.4
    assert broker_uat_module._align_price(10.001, 0.01, direction="up") == 10.01
    with pytest.raises(ValueError, match="direction"):
        broker_uat_module._align_price(10, 0.01, direction="sideways")

    cases = (
        (
            {"side": "buy", "volume": 100, "price": 10, "max_notional": 2000},
            tick,
            "at least two trading lots",
        ),
        (
            {"side": "buy", "volume": 200, "price": 9.98, "max_notional": 2000},
            tick,
            "cross the current best ask",
        ),
        (
            {"side": "sell", "volume": 200, "price": 10.01, "max_notional": 3000},
            tick,
            "cross the current best bid",
        ),
        (
            {"side": "buy", "volume": 300, "price": 10, "max_notional": 1000},
            tick,
            "exceeds cap",
        ),
    )
    for request, quote, message in cases:
        with pytest.raises(ValueError, match=message):
            broker_uat_module._build_execution_plan(
                {"symbol": "600000.SSE", **request},
                contract=contract,
                tick=quote,
            )

    with pytest.raises(ValueError, match="must be positive"):
        broker_uat_module._build_execution_plan(
            {
                "symbol": "600000.SSE",
                "side": "buy",
                "volume": 200,
                "price": 0.001,
                "max_notional": 1000,
            },
            contract=contract,
            tick=SimpleNamespace(
                last_price=0.001,
                bid_price_1=0.001,
                ask_price_1=0.001,
            ),
        )


def test_broker_uat_quote_wait_and_legacy_order_helpers_cover_empty_state() -> None:
    runtime = SimpleNamespace(
        settle_broker_events=lambda _timeout: None,
        engine=SimpleNamespace(
            oms=SimpleNamespace(get_tick=lambda _symbol: None),
        ),
    )
    assert broker_uat_module._wait_for_quote(
        runtime,
        "600000.SSE",
        timeout=0.001,
    ) is None
    assert broker_uat_module._wait_for_any_quote(
        runtime,
        ["600000.SSE"],
        timeout=0.001,
    ) is None
    assert broker_uat_module._primary_order_id({
        "scenario_version": 2,
        "steps": [{
            "step": "remainder_order_acknowledged",
            "evidence": {"order_id": "remainder-id"},
        }],
    }) == "remainder-id"
    assert broker_uat_module._primary_order_id({
        "scenario_version": 1,
        "steps": [{
            "step": "order_acknowledged",
            "evidence": {"order_id": "primary-id"},
        }],
    }) == "primary-id"


def test_broker_uat_read_only_preflight_rejects_invalid_or_unready_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _store = _uat_harness(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="broker must be"):
        harness.preflight(broker="paper")
    with pytest.raises(ValueError, match="must be positive"):
        harness.preflight(broker="xtp", max_notional=0)

    harness.preflight_fn = lambda _broker, _timeout: {"ok": False, "password": "hidden"}
    with pytest.raises(RuntimeError, match="preflight failed"):
        harness.preflight(broker="xtp", max_notional=1000)

    class _UnreadyRuntime(_FakeUATRuntime):
        def wait_ready(self, timeout: float) -> bool:
            del timeout
            return False

    state = _FakeBrokerState()
    harness.preflight_fn = lambda _broker, _timeout: {"ok": True}
    harness.runtime_factory = lambda _broker: _UnreadyRuntime(_store, state)
    with pytest.raises(RuntimeError, match="did not become ready"):
        harness.preflight(broker="xtp", max_notional=1000, timeout=0.1)


def test_broker_uat_prefix_does_not_make_an_unpersisted_order_known(
    tmp_path: Path,
) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    state = _FakeBrokerState()
    state.orders["UNKNOWN-UAT-ORDER"] = {
        "order_id": "UNKNOWN-UAT-ORDER",
        "symbol": "600000.SSE",
        "reference": "broker-uat/run-a/remainder",
        "volume": 100.0,
        "traded": 0.0,
        "active": True,
        "terminal": False,
        "status": "nottraded",
    }
    runtime = _FakeUATRuntime(store, state)

    assert broker_uat_module._active_uat_order_ids(runtime, "run-a") == [
        "UNKNOWN-UAT-ORDER"
    ]
    with pytest.raises(RuntimeError, match="unknown external active orders"):
        broker_uat_module._assert_no_unknown_active_orders(runtime, "run-a", [])


def test_broker_uat_requires_a_new_process_and_can_abort_at_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, store = _uat_harness(tmp_path, monkeypatch)
    harness.process_id_fn = lambda: 42
    checkpoint = harness.start(
        broker="xtp", symbol="600000.SSE", side="buy", volume=200, price=10,
        max_notional=2000, confirmation=CONFIRMATION, timeout=1,
    )

    assert checkpoint["status"] == "restart_required"
    with pytest.raises(RuntimeError, match="newly started local CLI process"):
        harness.resume(checkpoint["run_id"], confirmation=CONFIRMATION, timeout=1)

    aborted = harness.abort(
        checkpoint["run_id"], confirmation=CONFIRMATION, reason="operator safety stop",
    )
    assert aborted["status"] == "aborted"
    assert store.get_broker_uat_run(checkpoint["run_id"])["status"] == "aborted"


def test_broker_uat_network_preflight_failure_is_persisted_without_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, store = _uat_harness(tmp_path, monkeypatch)
    harness.preflight_fn = lambda _broker, _timeout: {
        "ok": False,
        "channels": [{"role": "trade", "ok": False, "error_type": "TimeoutError"}],
    }

    with pytest.raises(RuntimeError, match="preflight failed"):
        harness.start(
            broker="xtp",
            symbol="600000.SSE",
            side="buy",
            volume=200,
            price=10,
            max_notional=2000,
            confirmation=CONFIRMATION,
            timeout=1,
        )

    [run] = store.list_broker_uat_runs("xtp")
    preflight = next(step for step in run["steps"] if step["step"] == "preflight")
    assert preflight["status"] == "failed"
    assert run["evidence"] is None
    assert not any(step["step"] == "order_acknowledged" for step in run["steps"])

    harness.preflight_fn = lambda _broker, _timeout: {
        "ok": True,
        "channels": [
            {"role": "trade", "ok": True},
            {"role": "quote", "ok": True},
        ],
    }
    restart = harness.resume(run["run_id"], confirmation=CONFIRMATION, timeout=1)
    assert restart["status"] == "restart_required"
    resumed = harness.resume(run["run_id"], confirmation=CONFIRMATION, timeout=1)
    assert resumed["status"] == "passed"


def test_broker_uat_resume_skips_passed_dangerous_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, store = _uat_harness(tmp_path, monkeypatch, fail_reconnect_once=True)
    checkpoint = harness.start(
        broker="xtp", symbol="600000.SSE", side="buy", volume=200, price=10,
        max_notional=2000, confirmation=CONFIRMATION, timeout=1,
    )
    assert checkpoint["status"] == "restart_required"
    with pytest.raises(RuntimeError, match="injected reconnect failure"):
        harness.resume(checkpoint["run_id"], confirmation=CONFIRMATION, timeout=1)
    failed = store.list_broker_uat_runs("xtp")[0]
    passed_before = {
        step["step"]: step["ended_at"]
        for step in failed["steps"] if step["status"] == "passed"
    }
    assert "cancel_confirmed" in passed_before
    resumed = harness.resume(failed["run_id"], confirmation=CONFIRMATION, timeout=1)
    assert resumed["status"] == "passed"
    passed_after = {step["step"]: step["ended_at"] for step in resumed["steps"]}
    assert passed_after["cancel_confirmed"] == passed_before["cancel_confirmed"]


def test_broker_uat_is_disabled_without_all_three_operator_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _store = _uat_harness(tmp_path, monkeypatch)
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_ENABLED", "false")
    with pytest.raises(PermissionError, match="disabled"):
        harness.start(
            broker="xtp", symbol="600000.SSE", side="buy", volume=100, price=10,
            max_notional=1500, confirmation=CONFIRMATION,
        )
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_ENABLED", "true")
    with pytest.raises(PermissionError, match="confirmation"):
        harness.start(
            broker="xtp", symbol="600000.SSE", side="buy", volume=100, price=10,
            max_notional=1500, confirmation="yes",
        )
    monkeypatch.delenv("ALPHAPILOT_BROKER_UAT_ENVIRONMENT")
    with pytest.raises(ValueError, match="ENVIRONMENT"):
        harness.start(
            broker="xtp", symbol="600000.SSE", side="buy", volume=100, price=10,
            max_notional=1500, confirmation=CONFIRMATION,
        )
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_ENVIRONMENT", "protected-test-account")
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_WHITELIST", "000001.SZSE")
    with pytest.raises(PermissionError, match="not in"):
        harness.start(
            broker="xtp", symbol="600000.SSE", side="buy", volume=100, price=10,
            max_notional=1500, confirmation=CONFIRMATION,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"broker": "paper"}, "broker must be xtp or emt"),
        ({"side": "hold"}, "side must be buy or sell"),
        ({"volume": 0}, "must be positive"),
        ({"volume": 300}, "exceeds UAT cap"),
    ],
)
def test_broker_uat_request_validation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    message: str,
) -> None:
    harness, _store = _uat_harness(tmp_path, monkeypatch)
    request = {
        "broker": "xtp", "symbol": "600000.SSE", "side": "buy",
        "volume": 100, "price": 10, "max_notional": 1500,
        "confirmation": CONFIRMATION,
    }
    request.update(overrides)

    with pytest.raises((ValueError, PermissionError), match=message):
        harness.start(**request)


def test_broker_uat_provider_preflight_checks_both_channels_without_endpoints_in_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alphapilot.systems.live.brokers import registry

    endpoint = lambda role, port: SimpleNamespace(  # noqa: E731
        name=role, host_key=f"{role}_host", port_key=f"{role}_port", port=port,
    )
    trade_spec = SimpleNamespace(endpoints=(endpoint("trade", 1001),))
    quote_spec = SimpleNamespace(endpoints=(endpoint("quote", 1002),))
    monkeypatch.setattr(registry, "get_broker", lambda _broker: trade_spec)
    monkeypatch.setattr(registry, "get_quote_provider", lambda _broker: quote_spec)
    monkeypatch.setattr(registry, "missing_setting_fields", lambda _broker: [])
    monkeypatch.setattr(registry, "missing_quote_setting_fields", lambda _broker: [])
    monkeypatch.setattr(
        registry, "provider_availability", lambda _broker, role: (True, f"{role} ready"),
    )
    monkeypatch.setattr(
        registry,
        "build_connect_setting",
        lambda _broker: {"trade_host": "secret-trade-host", "trade_port": 1001},
    )
    monkeypatch.setattr(
        registry,
        "build_quote_connect_setting",
        lambda _broker: {"quote_host": "secret-quote-host", "quote_port": 1002},
    )

    class _Connection:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *_args):  # noqa: ANN002, ANN204
            return False

    def connect(address, timeout):  # noqa: ANN001, ANN201
        assert timeout == 1
        if address[1] == 1002:
            raise TimeoutError("quote endpoint unavailable")
        return _Connection()

    monkeypatch.setattr(broker_uat_module.socket, "create_connection", connect)
    result = broker_uat_module._provider_preflight("xtp", 1)

    assert result["ok"] is False
    assert result["channels"][0]["endpoints"] == [{
        "name": "trade", "reachable": True, "error_type": "",
    }]
    assert result["channels"][1]["endpoints"][0]["error_type"] == "TimeoutError"
    assert "secret-trade-host" not in json.dumps(result)
    assert result["architecture"]["python_bits"] in {32, 64}

    monkeypatch.setattr(registry, "missing_quote_setting_fields", lambda _broker: ["password"])
    missing = broker_uat_module._provider_preflight("xtp", 1)
    assert missing["channels"][1]["missing_setting_fields"] == ["password"]
    assert missing["channels"][1]["endpoints"] == []


def test_broker_uat_plugin_artifact_fingerprints_and_safe_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "gateway.py"
    source.write_text("gateway = object()\n", encoding="utf-8")
    spec = SimpleNamespace(
        version="1.2.3", gateway_path="plugin.gateway:create", plugin_id="xtp",
        distribution="alphapilot-broker-xtp",
    )
    monkeypatch.setenv("ALPHAPILOT_XTP_SDK_VERSION", "9.9")
    monkeypatch.setattr(broker_uat_module, "get_broker", lambda _broker: spec)
    monkeypatch.setattr(
        broker_uat_module.importlib.util,
        "find_spec",
        lambda _module: SimpleNamespace(origin=str(source)),
    )
    monkeypatch.setattr(broker_uat_module, "_distribution_hash", lambda _name: "dist-hash")
    monkeypatch.setattr(broker_uat_module, "_native_sdk_hash", lambda _broker: "b" * 64)
    monkeypatch.setattr(broker_uat_module, "_runtime_code_hash", lambda: "c" * 64)
    monkeypatch.setattr(broker_uat_module, "_git_commit", lambda: "d" * 40)

    metadata = BrokerUATHarness.plugin_metadata("XTP")
    assert metadata["broker"] == "xtp"
    assert metadata["gateway_source_hash"]
    assert metadata["sdk_hash"] == "b" * 64
    assert metadata["sdk_version"].startswith("native-sha256:")
    assert len(metadata["plugin_hash"]) == 64

    redacted = broker_uat_module._safe_evidence({
        "password": "remove", "account_id": "account-1",
        "nested": [{"token": "remove", "value": "keep"}],
    })
    assert "password" not in redacted
    assert redacted["account_id_hash"] == broker_uat_module._hash_text("account-1")
    assert redacted["nested"] == [{"value": "keep"}]
    assert broker_uat_module._primary_order_id({"steps": []}) == ""
    with pytest.raises(RuntimeError, match="no persisted preflight request"):
        broker_uat_module._request_from_run({"steps": []})

    monkeypatch.setattr(broker_uat_module, "get_broker", lambda _broker: SimpleNamespace(
        **{**spec.__dict__, "version": ""},
    ))
    with pytest.raises(ValueError, match="version is unavailable"):
        broker_uat_module._plugin_metadata("xtp")
    monkeypatch.setattr(broker_uat_module, "get_broker", lambda _broker: spec)
    monkeypatch.delenv("ALPHAPILOT_XTP_SDK_VERSION")
    assert broker_uat_module._plugin_metadata("xtp")["sdk_declared_version"] == ""
    monkeypatch.setattr(broker_uat_module.importlib.util, "find_spec", lambda _module: None)
    monkeypatch.setattr(broker_uat_module, "_distribution_hash", lambda _name: "")
    with pytest.raises(ValueError, match="could not be hashed"):
        broker_uat_module._plugin_metadata("xtp")


def test_broker_uat_distribution_hash_handles_installed_and_missing_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"plugin")
    directory = tmp_path / "directory"
    directory.mkdir()
    package = SimpleNamespace(
        files=("artifact.bin", "directory"),
        locate_file=lambda item: {"artifact.bin": artifact, "directory": directory}[str(item)],
    )
    monkeypatch.setattr(broker_uat_module, "distribution", lambda _name: package)
    assert len(broker_uat_module._distribution_hash("installed")) == 64

    def missing(_name):  # noqa: ANN001, ANN202
        raise broker_uat_module.PackageNotFoundError

    monkeypatch.setattr(broker_uat_module, "distribution", missing)
    assert broker_uat_module._distribution_hash("missing") == ""


def test_broker_uat_native_sdk_fingerprint_comes_from_binary_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "api"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text("", encoding="utf-8")
    (package / "vendor.so").write_bytes(b"native-sdk-v1")
    monkeypatch.setattr(
        broker_uat_module.importlib.util,
        "find_spec",
        lambda _module: SimpleNamespace(origin=str(init)),
    )

    first = broker_uat_module._native_sdk_hash("xtp")
    (package / "vendor.so").write_bytes(b"native-sdk-v2")
    second = broker_uat_module._native_sdk_hash("xtp")

    assert len(first) == 64
    assert first != second


def test_broker_error_redaction_removes_environment_and_inline_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPHAPILOT_LIVE_XTP_PASSWORD", "do-not-persist")
    monkeypatch.setenv("ALPHAPILOT_LIVE_XTP_ACCOUNT", "private-account")
    redacted = redact_secrets(
        "login failed password=plain-text token:abc do-not-persist private-account"
    )
    assert "plain-text" not in redacted
    assert "abc" not in redacted
    assert "do-not-persist" not in redacted
    assert "private-account" not in redacted


def test_environment_scoped_legacy_usage_restarts_zero_call_window(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "compat.sqlite3")
    store.register_compatibility_environment("environment-a")
    store.register_compatibility_entrypoint(
        "CLI timing_signal",
        kind="cli",
        replacement="trading_preview",
        deprecated_since="0.1.x",
        sunset_at="Thu, 31 Dec 2026 00:00:00 GMT",
    )
    store.set_compatibility_cutoff("2026-07-01T00:00:00+00:00")
    before = store.compatibility_environment_status("environment-a")
    assert before["post_cutoff_count"] == 0
    store.record_legacy_usage("CLI timing_signal", client_kind="cli")
    after = store.compatibility_environment_status("environment-a")
    assert after["post_cutoff_count"] == 1


def test_controlled_environment_reports_are_hash_checked_and_aggregated(engine) -> None:
    trading = engine.get_system("trading")
    local = trading.set_compatibility_cutoff()["local_environment_report"]
    assert local["ready"] is True
    external = {key: value for key, value in local.items() if key != "ready"}
    external["environment_id"] = "controlled-shadow-host"
    malformed = {**external, "code_commit": "compatibility-build-commit"}
    malformed["evidence_hash"] = compatibility_environment_report_hash(malformed)
    with pytest.raises(ValueError, match="full Git commit"):
        trading.import_compatibility_environment_report(malformed)
    external["code_commit"] = local["code_commit"]
    external["evidence_hash"] = compatibility_environment_report_hash(external)

    imported = trading.import_compatibility_environment_report(external)

    assert imported["source"] == "imported"
    assert imported["post_cutoff_count"] == 0
    environments = trading.compatibility_status()["environments"]
    assert {row["environment_id"] for row in environments} == {
        trading.compatibility_environment_id,
        "controlled-shadow-host",
    }
    tampered = {**external, "post_cutoff_count": 1}
    with pytest.raises(ValueError, match="hash"):
        trading.import_compatibility_environment_report(tampered)


def test_compatibility_environment_report_rejects_every_incomplete_binding() -> None:
    base = {
        "schema_version": 1,
        "runtime_schema_version": 8,
        "environment_id": "environment-a",
        "migration_cutoff": "2026-07-01T00:00:00+00:00",
        "generated_at": "2026-07-14T00:00:00+00:00",
        "code_commit": "a" * 40,
        "post_cutoff_count": 0,
        "active_legacy_runtime_count": 0,
        "unmigrated_legacy_job_count": 0,
        "entrypoints": [],
    }

    def invalid(
        changes: dict[str, object],
        message: str,
        *,
        remove: str = "",
        rehash: bool = True,
    ) -> None:
        payload = {**base, **changes}
        if remove:
            payload.pop(remove, None)
        if rehash:
            payload["evidence_hash"] = compatibility_environment_report_hash(payload)
        with pytest.raises(ValueError, match=message):
            validate_compatibility_environment_report(payload)

    with pytest.raises(ValueError, match="JSON object"):
        validate_compatibility_environment_report([])
    invalid({"schema_version": 2}, "unsupported")
    invalid({"runtime_schema_version": 6}, "predates runtime")
    invalid({"environment_id": ""}, "environment_id")
    invalid({"migration_cutoff": ""}, "migration cutoff")
    invalid({"generated_at": ""}, "generated_at")
    invalid({"migration_cutoff": "not-a-date"}, "ISO-8601")
    invalid(
        {
            "migration_cutoff": "2026-07-02T00:00:00",
            "generated_at": "2026-07-01T00:00:00",
        },
        "predates its migration cutoff",
    )
    invalid({"code_commit": "short"}, "full Git commit")
    invalid({}, "hash is invalid", rehash=False)
    invalid({"entrypoints": {}}, "entrypoints are missing")
    invalid(
        {"entrypoints": [{"post_cutoff_count": 1}], "post_cutoff_count": 0},
        "totals are inconsistent",
    )
    invalid({}, "invalid active_legacy_runtime_count", remove="active_legacy_runtime_count")
    invalid({"unmigrated_legacy_job_count": -1}, "invalid unmigrated_legacy_job_count")


def test_unmigrated_legacy_job_scan_ignores_bad_and_already_imported_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "jobs"
    for job_id, payload in (
        ("missing", {"kind": "timing_backtest", "status": "succeeded"}),
        ("failed", {"kind": "timing_backtest", "status": "failed"}),
        ("other", {"kind": "market_download", "status": "succeeded"}),
        ("imported", {"kind": "timing_backtest", "status": "succeeded"}),
    ):
        directory = root / job_id
        directory.mkdir(parents=True)
        (directory / "job.json").write_text(json.dumps(payload), encoding="utf-8")
    bad = root / "bad"
    bad.mkdir()
    (bad / "job.json").write_text("{", encoding="utf-8")
    monkeypatch.setenv("ALPHAPILOT_PORTAL_JOB_ROOT", str(root))
    store = SimpleNamespace(list_legacy_job_imports=lambda: [{"legacy_job_id": "imported"}])

    missing = RemovalReadinessService(store, repository_root=tmp_path)._unmigrated_legacy_jobs()

    assert [item["job_id"] for item in missing] == ["missing"]


def test_removal_readiness_fails_closed_for_an_unknown_acceptance_instance(
    tmp_path: Path,
) -> None:
    store = StrategyRuntimeStore(tmp_path / "missing-instance.sqlite3")

    report = RemovalReadinessService(store, repository_root=tmp_path).evaluate("missing")

    assert report["ready"] is False
    assert report["live_qualification"]["eligible_for_live_authorization"] is False
    assert "unknown strategy instance" in report["live_qualification"]["error"]


def test_removal_gate_rejects_environment_report_from_another_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StrategyRuntimeStore(tmp_path / "commit-bound-environment.sqlite3")
    instance = _instance("commit-bound-environment")
    store.create_instance(instance)
    payload = {
        "schema_version": 1,
        "runtime_schema_version": 8,
        "environment_id": "controlled-paper-host",
        "migration_cutoff": "2026-07-01T00:00:00+00:00",
        "generated_at": "2026-07-14T00:00:00+00:00",
        "code_commit": "a" * 40,
        "post_cutoff_count": 0,
        "active_legacy_runtime_count": 0,
        "unmigrated_legacy_job_count": 0,
        "entrypoints": [],
    }
    payload["evidence_hash"] = compatibility_environment_report_hash(payload)
    store.save_compatibility_environment_report(
        payload,
        evidence_hash=payload["evidence_hash"],
        imported=True,
    )
    readiness = RemovalReadinessService(store, repository_root=tmp_path)
    monkeypatch.setattr(
        readiness,
        "_git_state",
        lambda: {"commit": "b" * 40, "clean": True},
    )

    report = readiness.evaluate(instance.instance_id)

    assert report["checks"]["environment_report_commits_match"] is False


def test_schema_v5_upgrades_through_v8_once_with_online_backup(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    _make_v5_database(path)

    store = StrategyRuntimeStore(path)

    assert store.schema_version == 8
    assert len(list(tmp_path.glob("runtime.sqlite3.backup-v5-*"))) == 1
    with sqlite3.connect(path) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        evidence_columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(broker_uat_evidence)"
            )
        }
    assert {
        "decision_observations", "parity_runs", "broker_uat_runs",
        "broker_uat_evidence", "broker_uat_route_claims", "qualification_projections",
        "broker_uat_order_events", "legacy_job_imports",
    } <= tables
    assert {
        "environment", "plugin_version", "plugin_hash", "sdk_version", "sdk_hash",
        "scenario_version", "code_commit", "runtime_code_hash", "requested_notional",
        "filled_notional",
    } <= evidence_columns
    StrategyRuntimeStore(path)
    assert len(list(tmp_path.glob("runtime.sqlite3.backup-v5-*"))) == 1


def test_schema_v6_rehash_refuses_active_runtime_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "active.sqlite3"
    _make_v5_database(path)
    instance = _instance("active-before-v6")
    now = "2026-07-14T12:00:00+00:00"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO strategy_instances "
            "(instance_id,strategy_id,strategy_version,config_json,config_hash,lifecycle,"
            "deployment_level,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                instance.instance_id, instance.strategy_id, instance.strategy_version,
                json.dumps(instance.to_dict()), instance.config_hash,
                "running", "paper", now,
            ),
        )
        connection.execute(
            "INSERT INTO deployment_runtime "
            "(instance_id,config_hash,deployment_level,desired_state,observed_state,"
            "runtime_id,binding_active,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                instance.instance_id, instance.config_hash, "paper", "running", "running",
                "runtime-active", 1, now,
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="stop active instance"):
        StrategyRuntimeStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_version WHERE singleton=1"
        ).fetchone()[0] == 5
        connection.execute(
            "UPDATE deployment_runtime SET binding_active=0, desired_state='stopped', "
            "observed_state='stopped'"
        )
        connection.execute(
            "UPDATE strategy_instances SET lifecycle='stopped'"
        )
        connection.commit()

    migrated = StrategyRuntimeStore(path)
    assert migrated.schema_version == 8
    assert migrated.get_instance(instance.instance_id)["deployment_level"] == "replay"


def test_schema_v7_failure_preserves_v5_database_and_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "interrupted.sqlite3"
    _make_v5_database(path)

    def fail(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("injected v7 interruption")

    monkeypatch.setattr(StrategyRuntimeStore, "_migrate_v7", staticmethod(fail))
    with pytest.raises(RuntimeError, match="v7 interruption"):
        StrategyRuntimeStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_version WHERE singleton=1"
        ).fetchone()[0] == 5
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "decision_observations" not in tables
    assert list(tmp_path.glob("interrupted.sqlite3.backup-v5-*"))


def test_completed_legacy_job_import_is_idempotent_and_read_only(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "legacy.sqlite3")
    instance = _instance("legacy-instance")
    store.create_instance(instance)
    artifact = tmp_path / "legacy-artifact"
    artifact.mkdir()
    first = store.import_legacy_backtest_job(
        "old-job-1",
        instance_id=instance.instance_id,
        request={"strategy_name": "sma_filter"},
        result={"total_return": 0.1},
        artifact_dir=str(artifact),
    )
    second = store.import_legacy_backtest_job(
        "old-job-1",
        instance_id=instance.instance_id,
        request={"strategy_name": "changed-should-not-overwrite"},
        result={"total_return": 999},
        artifact_dir=str(artifact),
    )
    assert first["run_id"] == second["run_id"]
    assert second["origin"] == "legacy_import"
    assert second["request"]["strategy_name"] == "sma_filter"
    assert len(store.list_legacy_job_imports()) == 1


def test_release_verification_is_commit_bound_complete_and_hash_checked(tmp_path: Path) -> None:
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "removal_release": "0.2.0",
        "build_kind": "compatibility",
        "base_commit": REMOVAL_BASE_COMMIT,
        "commit": "release-commit",
        "generated_at": "2026-07-14T12:00:00+00:00",
        "checks": {
            name: {"passed": True, "returncode": 0, "output_sha256": name}
            for name in REQUIRED_CHECKS
        },
    }
    report["report_hash"] = canonical_report_hash(report)
    destination = tmp_path / REPORT_RELATIVE_PATH
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(report), encoding="utf-8")

    valid = validate_release_verification(tmp_path, expected_commit="release-commit")
    assert valid["passed"] is True

    stale = validate_release_verification(tmp_path, expected_commit="new-commit")
    assert stale["passed"] is False
    assert any("current commit" in error for error in stale["errors"])

    report["checks"]["wheel_smoke"]["passed"] = False
    # Rehashing a failed report proves that the hash itself cannot turn a
    # failed build into a passing removal decision.
    report["report_hash"] = canonical_report_hash(report)
    destination.write_text(json.dumps(report), encoding="utf-8")
    failed = validate_release_verification(tmp_path, expected_commit="release-commit")
    assert failed["passed"] is False
    assert any("wheel_smoke" in error for error in failed["errors"])

    removal = {
        **report,
        "build_kind": "removal",
        "checks": {
            name: {"passed": True, "returncode": 0, "output_sha256": name}
            for name in required_checks_for("removal")
        },
    }
    removal["report_hash"] = canonical_report_hash(removal)
    removal_path = tmp_path / REMOVAL_REPORT_RELATIVE_PATH
    removal_path.write_text(json.dumps(removal), encoding="utf-8")
    assert validate_release_verification(
        tmp_path,
        expected_commit="release-commit",
        build_kind="removal",
    )["passed"] is True


def test_release_verification_rejects_missing_malformed_and_incomplete_reports(
    tmp_path: Path,
) -> None:
    destination = tmp_path / REPORT_RELATIVE_PATH
    assert validate_release_verification(tmp_path, expected_commit="commit")["passed"] is False
    destination.parent.mkdir(parents=True)
    destination.write_text("{", encoding="utf-8")
    assert validate_release_verification(tmp_path, expected_commit="commit")["passed"] is False
    destination.write_text("[]", encoding="utf-8")
    assert validate_release_verification(tmp_path, expected_commit="commit")["passed"] is False

    incomplete = {
        "schema_version": 99,
        "removal_release": "0.3.0",
        "base_commit": "wrong",
        "commit": "different",
        "generated_at": "",
        "checks": [],
        "report_hash": "tampered",
    }
    destination.write_text(json.dumps(incomplete), encoding="utf-8")
    result = validate_release_verification(tmp_path, expected_commit="commit")
    assert result["passed"] is False
    assert any("schema version" in error for error in result["errors"])
    assert any("different release" in error for error in result["errors"])
    assert any("migration baseline" in error for error in result["errors"])
    assert any("generation timestamp" in error for error in result["errors"])
    assert any("checks are missing" in error for error in result["errors"])


def test_removal_source_scan_covers_all_first_party_code_except_compatibility_boundary(
    tmp_path: Path,
) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    allowed = tmp_path / "alphapilot/modules/timing/module.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("def timing_signal(): pass\n", encoding="utf-8")
    removal_gate = tmp_path / "scripts/check_legacy_entrypoint_absence.py"
    removal_gate.parent.mkdir(parents=True)
    removal_gate.write_text('FORBIDDEN = "/api/timing/signal"\n', encoding="utf-8")
    caller = tmp_path / "alphapilot/modules/new_client.py"
    caller.write_text('PATH = "/api/timing/signal"\n', encoding="utf-8")
    script = tmp_path / "scripts/operator.py"
    script.write_text("command = 'timing_backtest'\n", encoding="utf-8")

    references = RemovalReadinessService(
        store, repository_root=tmp_path,
    )._production_legacy_references()

    assert {item["path"] for item in references} == {
        "alphapilot/modules/new_client.py", "scripts/operator.py",
    }


def test_all_registered_timing_strategies_pass_legacy_to_formal_equivalence_matrix(
    engine,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "raw"
    data_root.mkdir()
    dates = pd.date_range("2026-01-01", periods=90, freq="D")
    for stem, offset in (("sz000001", 0.0), ("sh600000", 2.0)):
        closes = [10.0 + offset + index * 0.03 for index in range(len(dates))]
        pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "code": [stem] * len(dates),
            "open": closes,
            "high": [value + 0.2 for value in closes],
            "low": [value - 0.2 for value in closes],
            "close": closes,
            "volume": [10_000] * len(dates),
            "amount": [100_000] * len(dates),
        }).to_csv(data_root / f"{stem}.csv", index=False)

    timing = engine.get_system("timing")
    trading = engine.get_system("trading")
    timing_ids = sorted(
        definition.strategy_id
        for definition in trading.registry.list()
        if definition.signal_kind.value == "instrument_timing"
    )
    for strategy_id in timing_ids:
        result = timing.run_backtest(TimingBacktestRequest(
            strategy_name=strategy_id,
            symbols=["sz.000001"],
            start_date="2026-01-05",
            end_date="2026-03-20",
            data_dir=data_root,
            adjust_mode="none",
            output_dir=tmp_path / "matrix" / strategy_id,
            target_percent=1.0,
        ))
        assert result.summary["compatibility_equivalence"]["status"] == "passed"

    for label, symbols, target in (
        ("multi", ["sz.000001", "sh.600000"], 0.2),
        ("zero", ["sz.000001"], 0.0),
    ):
        timing.run_backtest(TimingBacktestRequest(
            strategy_name="sma_filter",
            symbols=symbols,
            start_date="2026-01-05",
            end_date="2026-03-20",
            data_dir=data_root,
            adjust_mode="none",
            output_dir=tmp_path / "matrix" / label,
            strategy_params={"window": 5},
            target_percent=target,
        ))

    status = trading.compatibility_status()["timing_equivalence"]
    assert status["passed"] is True
    assert status["missing_strategies"] == []
    assert status["missing_cases"] == []


def test_formal_cli_preview_files_and_waiting_backtest_replace_legacy_quick_commands(
    engine,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "cli-data"
    data_root.mkdir()
    dates = pd.date_range("2026-01-01", periods=35, freq="D")
    closes = [10 + index * 0.02 for index in range(len(dates))]
    pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "code": ["sh600000"] * len(dates),
        "open": closes,
        "high": [value + 0.1 for value in closes],
        "low": [value - 0.1 for value in closes],
        "close": closes,
        "volume": [10_000] * len(dates),
        "amount": [100_000] * len(dates),
    }).to_csv(data_root / "sh600000.csv", index=False)
    module = engine.get_module("trading_cli")
    instance_id = "formal-cli-replacement"
    module.trading_instance_create(
        instance_id,
        "sma_filter",
        "sh.600000",
        params={"window": 5},
        data_policy={
            "feature_adjustment": "none",
            "history_window": 6,
            "data_version": "cli-fixture-v1",
        },
        portfolio_policy={
            "policy_id": "timing_fixed_exposure",
            "params": {"target_percent": 0.2},
        },
    )
    assert module.trading_instance_validate(instance_id)["ok"] is True

    json_path = tmp_path / "preview.json"
    csv_path = tmp_path / "preview.csv"
    options = {"data_dir": str(data_root), "adjust_mode": "none"}
    json_result = module.trading_preview(
        instance_id, options=options, output_path=str(json_path), output_format="json",
    )
    csv_result = module.trading_preview(
        instance_id, options=options, output_path=str(csv_path), output_format="csv",
    )
    assert json_result["output_path"] == str(json_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["instance_id"] == instance_id
    assert csv_result["output_format"] == "csv"
    assert "instrument" in csv_path.read_text(encoding="utf-8")

    output_root = tmp_path / "formal-replay"
    run = module.trading_backtest(
        instance_id,
        options={"data_dir": str(data_root), "adjust_mode": "none"},
        wait=True,
        output_dir=str(output_root),
    )
    assert run["status"] == "completed"
    assert Path(run["artifact_dir"]).parent == output_root
    assert (Path(run["artifact_dir"]) / "summary.json").is_file()

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from alphapilot.systems.live.brokers import registry
from alphapilot.systems.live.config import LiveConfig, MarketDataConfig, RunMode
from alphapilot.systems.live.control import DaemonRuntimeControl
from alphapilot.systems.live.fsm.runmode_fsm import RunModeMachine
from alphapilot.systems.live import broker_uat
from alphapilot.systems.live.plugin import (
    GatewayCapabilities,
    LivePluginSpec,
    ProviderSpec,
    QuoteChannelSpec,
    TradeChannelSpec,
)
from alphapilot.systems.live.service import LiveSystem
from alphapilot.systems.live.runtime import LiveRuntime
from alphapilot.modules.live.module import LiveModule
from alphapilot.systems.trading.account_identity import account_identity_hash
from alphapilot.systems.trading.authorization import AutomatedRouteAuthorizer
from alphapilot.systems.trading.domain import DeploymentSpec, StrategyInstanceConfig
from alphapilot.systems.trading.ports import RouteContext, RouteOrigin
from alphapilot.systems.trading.store import StrategyRuntimeStore


def _tts_contract_spec() -> LivePluginSpec:
    """Represent the separately installed TTS plugin without importing it."""

    native_assets = ("stock", "fund", "bond", "option", "futures")
    return LivePluginSpec(
        plugin_id="tts-contract-fixture",
        providers=(
            ProviderSpec(
                name="tts",
                factory_path="external_tts_plugin:create_gateway",
                gateway_name="TTS",
                trade=TradeChannelSpec(
                    account_kind="simulation",
                    capabilities=GatewayCapabilities(
                        asset_classes=native_assets,
                        native_asset_classes=native_assets,
                        routable_asset_classes=("stock", "fund"),
                        supports_order_query=True,
                        supports_trade_query=True,
                    ),
                ),
            ),
            ProviderSpec(
                name="tts_7x24",
                factory_path="external_tts_plugin:create_gateway",
                gateway_name="TTS_7X24",
                quote=QuoteChannelSpec(
                    data_kind="replay",
                    capabilities=GatewayCapabilities(
                        asset_classes=native_assets,
                        native_asset_classes=native_assets,
                        routable_asset_classes=(),
                        supports_cancel=False,
                    ),
                ),
            ),
        ),
    )


def _install_tts() -> None:
    try:
        registry.get_broker("tts")
    except ValueError:
        registry.register_plugin_spec(
            _tts_contract_spec(),
            distribution="external-alphapilot-tts",
            version="test",
        )


def _paper_instance(store: StrategyRuntimeStore, instance_id: str) -> dict:
    created = store.create_instance(StrategyInstanceConfig(
        instance_id=instance_id,
        strategy_id="dual_ma",
        strategy_version="1.0.0",
        params={"short_window": 5, "long_window": 20},
        universe=("600000.SSE",),
    ))
    validated = store.set_validation_state(instance_id, "validated")
    store.configure_deployment(DeploymentSpec(
        instance_id=instance_id,
        config_hash=created["config_hash"],
        run_mode="paper",
    ))
    return validated


def _bind_realtime(store: StrategyRuntimeStore, instance_id: str, profile: str = "tts-main") -> dict:
    _install_tts()
    current = store.get_instance(instance_id)
    return store.configure_deployment(DeploymentSpec(
        instance_id=instance_id,
        config_hash=current["config_hash"],
        run_mode="simulation",
        execution_environment="broker_simulation",
        trade_provider="tts",
        quote_provider="xtp",
        account_profile=profile,
        quote_data_kind="realtime",
    ))["configuration"]


def test_provider_catalog_filters_tts_to_simulation_and_replay_quote() -> None:
    _install_tts()
    trade, quote = registry.validate_provider_pair(RunMode.SIMULATION, "tts", "xtp")
    assert trade.account_kind == "simulation"
    assert quote.data_kind == "realtime"
    assert [row.name for row in registry.list_brokers(account_kind="simulation")] == ["tts"]
    assert "tts_7x24" in {
        row.name for row in registry.list_quote_providers(data_kind="replay")
    }
    with pytest.raises(ValueError, match="requires a live trade provider"):
        registry.validate_provider_pair(RunMode.LIVE, "tts", "xtp")


def test_binding_change_preserves_diagnostics_and_resets_runtime(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    current = _paper_instance(store, "alpha")
    old_hash = store.get_deployment_spec("alpha")["binding_hash"]
    run = store.start_runtime_run("alpha", "paper")
    store.record_runtime_session(
        "alpha", config_hash=current["config_hash"], run_mode="paper",
        session="2026-07-16",
    )
    store.finish_runtime_run(run["run_id"])

    binding = _bind_realtime(store, "alpha")
    deployment = store.deployment("alpha")

    assert binding["binding_hash"] != old_hash
    assert deployment["runtime"]["execution_environment"] == "broker_simulation"
    assert store.runtime_diagnostics("alpha")["modes"]["paper"]["trading_sessions"] == 1
    assert deployment["runtime"]["desired_state"] == "ready"
    assert deployment["runtime"]["reconcile_required"] is False
    assert deployment["runtime"]["reconciled"] is False
    assert deployment["runtime"]["observed_state"] == "ready"


def test_simulation_to_shadow_replaces_provider_binding_directly(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    _paper_instance(store, "alpha")
    simulation = _bind_realtime(store, "alpha")
    current = store.get_instance("alpha")
    shadow = store.configure_deployment(DeploymentSpec(
        instance_id="alpha",
        config_hash=current["config_hash"],
        run_mode="shadow",
        execution_environment="live",
        trade_provider="xtp",
        quote_provider="emt",
        account_id="live-account",
        quote_data_kind="realtime",
    ))["configuration"]
    deployment = store.deployment("alpha")

    assert shadow["run_mode"] == "shadow"
    assert deployment["configuration"] == {
        **deployment["configuration"],
        "execution_environment": "live",
        "trade_provider": "xtp",
        "quote_provider": "emt",
        "quote_data_kind": "realtime",
    }
    assert deployment["configuration"]["binding_hash"] != simulation["binding_hash"]
    assert deployment["runtime"]["execution_environment"] == "live"
    assert deployment["runtime"]["trade_provider"] == "xtp"
    assert deployment["runtime"]["quote_provider"] == "emt"


def test_replay_quote_binding_can_observe_but_runmode_cannot_route(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    _paper_instance(store, "alpha")
    _install_tts()
    current = store.get_instance("alpha")
    binding = store.configure_deployment(DeploymentSpec(
        instance_id="alpha",
        config_hash=current["config_hash"],
        run_mode="simulation",
        execution_environment="broker_simulation",
        trade_provider="tts",
        quote_provider="tts_7x24",
        account_profile="night-replay",
        quote_data_kind="replay",
    ))["configuration"]
    assert binding["quote_data_kind"] == "replay"
    machine = RunModeMachine(
        RunMode.SIMULATION,
        provider_routing_enabled=False,
        provider_block_reason="quote provider is not realtime",
    )
    assert machine.can_submit_orders() is False


def test_simulation_profile_enforces_one_automated_writer(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    for instance_id in ("alpha", "beta"):
        _paper_instance(store, instance_id)
        _bind_realtime(store, instance_id, profile="same-tts-account")
    store.update_runtime_state("alpha", binding_active=True)
    with pytest.raises(ValueError, match="active automated writer"):
        store.update_runtime_state("beta", binding_active=True)


def test_account_kill_switch_persists_only_hashed_identity(tmp_path: Path) -> None:
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")

    block = store.set_route_block(
        "account", "private-account", active=True, reason="test",
    )

    assert block["scope_id"] == account_identity_hash("private-account")
    assert "private-account" not in str(store.list_route_blocks())
    assert store.active_route_blocks(instance_id="alpha", account_id="private-account")
    store.set_route_block("account", "private-account", active=False, reason="done")
    assert not store.active_route_blocks(
        instance_id="alpha", account_id="private-account",
    )


def test_simulation_manual_safety_finds_external_automated_writer() -> None:
    calls: list[dict] = []

    class Journal:
        def active_external_writer(self, **kwargs):  # noqa: ANN003, ANN201
            calls.append(kwargs)
            return {"instance_id": "tts-writer"}

        def active_live_writer(self, account_id: str):  # noqa: ANN201, ARG002
            return None

    runtime = object.__new__(LiveRuntime)
    runtime.config = SimpleNamespace(
        execution_environment="broker_simulation",
        trade_broker="tts",
        broker="tts",
    )
    runtime.execution_journal = Journal()

    assert runtime._active_automated_writer("actual-account") == {
        "instance_id": "tts-writer",
    }
    assert calls == [{
        "execution_environment": "broker_simulation",
        "trade_provider": "tts",
        "account_id": "actual-account",
    }]


def test_simulation_authorizer_accepts_hashed_persisted_account_identity(tmp_path: Path) -> None:
    now = datetime(2026, 7, 17, 2, 0, tzinfo=timezone.utc)
    store = StrategyRuntimeStore(tmp_path / "runtime.sqlite3")
    instance = _paper_instance(store, "alpha")
    binding = _bind_realtime(store, "alpha")
    store.transition_runtime(
        "alpha",
        lifecycle="running",
        desired_state="running",
        observed_state="running",
        runtime_id="runtime-tts-1",
        runner_heartbeat_at=now.isoformat(),
        account_id=account_identity_hash("actual-tts-account"),
        reconcile_required=False,
        reconciled=True,
        binding_active=True,
    )
    context = RouteContext(
        origin=RouteOrigin.AUTOMATED,
        instance_id="alpha",
        config_hash=instance["config_hash"],
        account_id="actual-tts-account",
        broker="tts",
        run_mode="simulation",
        runtime_id="runtime-tts-1",
        execution_environment="broker_simulation",
        trade_provider="tts",
        quote_provider="xtp",
        quote_data_kind="realtime",
        binding_hash=binding["binding_hash"],
    )
    authorizer = AutomatedRouteAuthorizer(store, now_fn=lambda: now)
    assert authorizer.authorize(context).allowed is True

    replay = RouteContext(**{**context.__dict__, "quote_data_kind": "replay"})
    assert authorizer.authorize(replay).rule == "quote_data_kind_binding"
    wrong_account = RouteContext(**{**context.__dict__, "account_id": "other"})
    assert authorizer.authorize(wrong_account).rule == "account_binding"
    store.update_runtime_state("alpha", account_profile="other-profile")
    assert authorizer.authorize(context).rule == "runtime_account_profile"


def test_daemon_runtime_directories_are_namespaced_by_binding(tmp_path: Path) -> None:
    base = LiveConfig(
        state_dir=tmp_path / "state",
        ledger_dir=tmp_path / "ledger",
        market_data=MarketDataConfig(data_dir=tmp_path / "market"),
    )
    control = DaemonRuntimeControl(base)

    def instance(binding_hash: str, quote: str) -> dict:
        return {
            "instance_id": "alpha",
            "config_hash": "config-hash",
            "deployment": {"run_mode": "simulation"},
            "runtime": {
                "execution_environment": "broker_simulation",
                "trade_provider": "tts",
                "quote_provider": quote,
                "quote_data_kind": "realtime" if quote == "xtp" else "replay",
                "binding_hash": binding_hash,
                "broker": "tts",
            },
        }

    realtime = control._config_for(instance("a" * 64, "xtp"))
    replay = control._config_for(instance("b" * 64, "tts_7x24"))
    assert realtime.mode == RunMode.SIMULATION
    assert realtime.state_dir != replay.state_dir
    assert "broker_simulation/tts--xtp" in realtime.state_dir.as_posix()
    assert "broker_simulation/tts--tts_7x24" in replay.ledger_dir.as_posix()
    assert "broker_simulation/tts--xtp" in realtime.market_data.data_dir.as_posix()


def test_standalone_portal_runtimes_use_parallel_provider_namespaces(tmp_path: Path) -> None:
    base = LiveConfig(
        state_dir=tmp_path / "state",
        ledger_dir=tmp_path / "ledger",
        market_data=MarketDataConfig(data_dir=tmp_path / "market"),
    )
    system = SimpleNamespace(config=base)
    module = LiveModule()
    module.context = SimpleNamespace(system=lambda _name: system)

    simulation = module._standalone_config(
        mode="simulation", trade_broker="tts", quote_provider="xtp",
    )
    paper = module._standalone_config(
        mode="paper", trade_broker="paper", quote_provider="paper",
    )

    assert simulation.state_dir != paper.state_dir
    assert simulation.state_dir.as_posix().endswith(
        "runtimes/broker_simulation/tts--xtp/standalone"
    )
    assert paper.ledger_dir.as_posix().endswith(
        "runtimes/local_paper/paper--paper/standalone"
    )
    assert simulation.market_data.data_dir.as_posix().endswith(
        "runtimes/broker_simulation/tts--xtp/standalone"
    )
    # No explicit selector retains the historical paths for existing CLI users.
    assert module._standalone_config().state_dir == base.state_dir


def test_tts_uat_requires_an_independent_realtime_quote_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_tts()
    monkeypatch.delenv("ALPHAPILOT_BROKER_UAT_QUOTE_PROVIDER", raising=False)
    with pytest.raises(ValueError, match="QUOTE_PROVIDER is required"):
        broker_uat._uat_quote_provider("tts")

    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_QUOTE_PROVIDER", "tts_7x24")
    with pytest.raises(ValueError, match="realtime"):
        broker_uat._uat_quote_provider("tts")
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_QUOTE_PROVIDER", "xtp")
    assert broker_uat._uat_quote_provider("tts") == "xtp"

    endpoint = SimpleNamespace(host_key="交易服务器", port_key="")
    assert broker_uat._endpoint_host_port(
        {"交易服务器": "tcp://127.0.0.1:12345"}, endpoint,
    ) == ("127.0.0.1", 12345)

    store = StrategyRuntimeStore(tmp_path / "uat.sqlite3")
    run = store.create_broker_uat_run(
        broker="tts",
        account_hash="",
        environment="tts-simulation",
        plugin_version="test",
        plugin_hash="a" * 64,
        sdk_version="6.3.15",
        sdk_hash="b" * 64,
        runtime_code_hash="c" * 64,
        code_commit="d" * 40,
        symbol="600000.SSE",
        max_notional=20_000,
        scenario_version=2,
    )
    assert run["broker"] == "tts"


def test_live_system_builds_tts_uat_in_simulation_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_QUOTE_PROVIDER", "xtp")
    calls: dict = {}
    system = object.__new__(LiveSystem)
    system.config = SimpleNamespace(ledger_dir=tmp_path / "ledger")

    def create_runtime(**kwargs):  # noqa: ANN003, ANN202
        calls.update(kwargs)
        return "runtime"

    system.create_runtime = create_runtime  # type: ignore[method-assign]
    harness = system.broker_uat_harness(StrategyRuntimeStore(tmp_path / "runtime.sqlite3"))
    assert harness.runtime_factory("tts") == "runtime"
    assert calls["mode"] == RunMode.SIMULATION
    assert calls["trade_broker"] == "tts"
    assert calls["quote_provider"] == "xtp"

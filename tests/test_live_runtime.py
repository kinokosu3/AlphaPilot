from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path

import pytest

from alphapilot.systems.live.brokers.paper import PaperBroker
from alphapilot.systems.live.brokers.sim import SimBroker
from alphapilot.systems.live.clock import SimulatedClock
from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.runtime import LiveRuntime, require_live_confirmation
from alphapilot.systems.live.journal import InMemoryExecutionJournal
from alphapilot.systems.live.targets import TargetPortfolio
from alphapilot.systems.trading.domain import StrategyInstanceConfig
from alphapilot.systems.trading.ports import RouteContext, RouteOrigin
from alphapilot.systems.trading.store import StrategyRuntimeStore


class QueryTrackingPaper(PaperBroker):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.order_queries = 0
        self.trade_queries = 0

    def query_orders(self) -> bool:
        self.order_queries += 1
        return super().query_orders()

    def query_trades(self) -> bool:
        self.trade_queries += 1
        return super().query_trades()


class ActiveWriterJournal(InMemoryExecutionJournal):
    def active_live_writer(self, account_id: str):  # noqa: ANN201
        return {"instance_id": "live-writer", "account_id": account_id}


def _runtime(tmp_path: Path) -> LiveRuntime:
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
    )
    broker = PaperBroker(cash=100_000.0, prices={"600000.SSE": 10.0}, open_cost=0.0, min_cost=0.0)
    clock = SimulatedClock(datetime(2026, 7, 1, 10, 0))
    return LiveRuntime.create(cfg, broker=broker, now_fn=clock)


def test_runtime_connect_plan_route_and_persist(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.connect(paper_cash=100_000.0)
    assert runtime.wait_ready(timeout=1, require_contracts=False)

    target = TargetPortfolio(
        date="2026-07-01",
        holdings={"SH600000": 1000},
        prices={"SH600000": 10.0},
        source="test",
    )
    planned = runtime.submit_target(target, route=False)
    assert planned["planned"] == 1
    assert planned["routed"] == []
    assert planned["fully_routed"] is True
    assert runtime.engine.oms.get_position("600000.SSE") is None

    routed = runtime.submit_target(target, route=True)
    assert routed["planned"] == 1
    assert len(routed["routed"]) == 1
    assert routed["submitted"] == 1
    assert routed["unrouted"] == 0
    assert routed["fully_routed"] is True
    assert runtime.engine.oms.get_position("600000.SSE").volume == 1000
    assert runtime.state_path.exists()

    runtime.close()


def test_target_preflight_blocks_concentration_before_any_route(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.connect(paper_cash=100_000.0)
    target = TargetPortfolio(
        date="2026-07-01", holdings={"SH600000": 10_000},
        prices={"SH600000": 10.0}, source="unsafe-full-position",
    )

    result = runtime.submit_target(target, route=True)

    assert result["preflight_ok"] is False
    assert any(issue["rule"] == "max_position_pct" for issue in result["issues"])
    assert result["submitted"] == 0
    assert runtime.engine.oms.get_position("600000.SSE") is None
    runtime.close()


def test_runtime_reconnect_keeps_halted_until_manual_resume(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.connect(paper_cash=100_000.0)
    runtime.engine.handle_disconnect("socket drop")

    result = runtime.reconnect(auto_resume=False)

    assert result["reconnect"]["resumed"] is False
    assert result["state"]["engine"]["halted"] is True
    assert result["state"]["engine"]["halt_reason"] == "socket drop"
    assert result["state"]["recovery"]["risk_restored"] is True
    rejected = runtime.submit_order("SH600000", side="buy", volume=100, price=10.0)
    assert rejected["submitted"] is False
    assert rejected["routing_event"]["kind"] == "blocked"
    assert "halted" in rejected["routing_reason"]

    runtime.engine.resume()
    accepted = runtime.submit_order("SH600000", side="buy", volume=100, price=10.0)
    assert accepted["submitted"] is True
    runtime.close()


def test_recovery_queries_order_and_trade_snapshots(tmp_path: Path) -> None:
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
    )
    broker = QueryTrackingPaper(cash=100_000.0, prices={"600000.SSE": 10.0}, open_cost=0.0, min_cost=0.0)
    runtime = LiveRuntime.create(cfg, broker=broker)

    runtime.connect(paper_cash=100_000.0)

    recovery = runtime.snapshot()["recovery"]
    assert {"account", "position", "orders", "trades"} <= set(recovery["broker_refresh_kinds"])
    assert recovery["broker_refresh_unsupported"] == []
    assert broker.order_queries == 1
    assert broker.trade_queries == 1
    runtime.close()


def test_runtime_cancel_active_order(tmp_path: Path) -> None:
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
    )
    broker = SimBroker(
        cash=100_000.0,
        prices={"600000.SSE": 10.0},
        partial_ratio=0.5,
        open_cost=0.0,
        min_cost=0.0,
    )
    clock = SimulatedClock(datetime(2026, 7, 1, 10, 0))
    runtime = LiveRuntime.create(cfg, broker=broker, now_fn=clock)
    runtime.connect(paper_cash=100_000.0)
    submitted = runtime.submit_order("SH600000", side="buy", volume=1000, price=10.0)
    assert submitted["submitted"] is True
    ack = runtime.wait_for_order_ack(submitted["order_id"], timeout=0.5)
    assert ack["acknowledged"] is True
    assert ack["status"] == "parttraded"
    assert ack["active"] is True
    assert runtime.engine.oms.get_active_orders()

    cancelled = runtime.cancel_order(submitted["order_id"])
    confirmation = runtime.wait_for_order_terminal(submitted["order_id"], timeout=0.5)

    assert cancelled["cancelled"] is True
    assert confirmation["terminal"] is True
    assert confirmation["status"] == "cancelled"
    assert runtime.engine.oms.get_active_orders() == []
    assert runtime.engine.ledger.events(kind="cancel")[-1]["order_id"] == submitted["order_id"]
    runtime.close()


def test_runtime_live_requires_explicit_confirmation() -> None:
    cfg = LiveConfig(mode=RunMode.LIVE, broker="xtp")
    with pytest.raises(ValueError, match="confirm_live"):
        require_live_confirmation(cfg, confirm_live=False)
    require_live_confirmation(cfg, confirm_live=True)


def test_real_provider_runtime_projection_hashes_account_identifier(tmp_path: Path) -> None:
    cfg = LiveConfig(
        mode=RunMode.LIVE,
        broker="xtp",
        trade_broker="xtp",
        quote_provider="xtp",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
    )
    broker = PaperBroker(
        cash=100_000, prices={"600000.SSE": 10.0}, account_id="private-account",
    )
    runtime = LiveRuntime.create(cfg, broker=broker, is_trading_day_fn=lambda _dt: True)
    runtime.connect(setting={"cash": 100_000})

    account = runtime.snapshot()["account"]

    assert "account_id" not in account
    assert account["account_id_hash"] == hashlib.sha256(b"private-account").hexdigest()
    assert "private-account" not in runtime.state_path.read_text(encoding="utf-8")
    runtime.close()


def test_broker_uat_route_is_bound_to_durable_run_account_broker_and_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StrategyRuntimeStore(tmp_path / "uat-runtime.sqlite3")
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="xtp",
        trade_broker="xtp",
        quote_provider="xtp",
        ledger_dir=tmp_path / "uat-ledger",
        state_dir=tmp_path / "uat-state",
    )
    broker = PaperBroker(
        cash=100_000,
        prices={"600000.SSE": 10.0, "000001.SZSE": 10.0},
        account_id="uat-account",
        open_cost=0.0,
        min_cost=0.0,
    )
    runtime = LiveRuntime.create(cfg, broker=broker, execution_journal=store)
    runtime.connect(paper_cash=100_000)
    account_hash = hashlib.sha256(b"uat-account").hexdigest()
    run = store.create_broker_uat_run(
        broker="xtp",
        account_hash=account_hash,
        environment="test",
        plugin_version="1",
        plugin_hash="plugin",
        sdk_version="sdk",
        symbol="600000.SSE",
        max_notional=2_000,
    )
    context = RouteContext(
        origin=RouteOrigin.BROKER_UAT,
        uat_run_id=run["run_id"],
    )

    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_ENABLED", "false")
    disabled = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=10,
        reference=f"broker-uat/{run['run_id']}/disabled", route_context=context,
    )
    assert disabled["routing_rule"] == "uat_disabled"

    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_ENABLED", "true")
    missing = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=10,
        reference="broker-uat/missing/primary",
        route_context=RouteContext(origin=RouteOrigin.BROKER_UAT),
    )
    assert missing["routing_rule"] == "uat_binding"
    wrong_symbol = runtime.submit_order(
        "000001.SZSE", side="buy", volume=100, price=10,
        reference=f"broker-uat/{run['run_id']}/symbol", route_context=context,
    )
    assert wrong_symbol["routing_rule"] == "uat_whitelist"
    oversized = runtime.submit_order(
        "600000.SSE", side="buy", volume=300, price=10,
        reference=f"broker-uat/{run['run_id']}/large", route_context=context,
    )
    assert oversized["routing_rule"] == "uat_notional"
    unbound_reference = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=10,
        reference="operator/free-form", route_context=context,
    )
    assert unbound_reference["routing_rule"] == "uat_reference"

    wrong_account_run = store.create_broker_uat_run(
        broker="xtp", account_hash=hashlib.sha256(b"other").hexdigest(),
        environment="test", plugin_version="1", plugin_hash="plugin",
        sdk_version="sdk", symbol="600000.SSE", max_notional=2_000,
    )
    wrong_account = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=10,
        reference=f"broker-uat/{wrong_account_run['run_id']}/primary",
        route_context=RouteContext(
            origin=RouteOrigin.BROKER_UAT, uat_run_id=wrong_account_run["run_id"],
        ),
    )
    assert wrong_account["routing_rule"] == "uat_account"
    wrong_broker_run = store.create_broker_uat_run(
        broker="emt", account_hash=account_hash, environment="test",
        plugin_version="1", plugin_hash="plugin", sdk_version="sdk",
        symbol="600000.SSE", max_notional=2_000,
    )
    wrong_broker = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=10,
        reference=f"broker-uat/{wrong_broker_run['run_id']}/primary",
        route_context=RouteContext(
            origin=RouteOrigin.BROKER_UAT, uat_run_id=wrong_broker_run["run_id"],
        ),
    )
    assert wrong_broker["routing_rule"] == "uat_broker"

    store.set_route_block("global", "*", active=True, reason="test")
    killed = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=10,
        reference=f"broker-uat/{run['run_id']}/kill", route_context=context,
    )
    assert killed["routing_rule"] == "kill_switch"
    store.set_route_block("global", "*", active=False, reason="test complete")

    accepted = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=10,
        reference=f"broker-uat/{run['run_id']}/primary", route_context=context,
    )
    assert accepted["submitted"] is True
    duplicate = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=10,
        reference=f"broker-uat/{run['run_id']}/primary", route_context=context,
    )
    assert duplicate["submitted"] is False
    assert duplicate["routing_rule"] == "duplicate"

    writer = StrategyInstanceConfig(
        instance_id="writer",
        strategy_id="sma_filter",
        strategy_version="1.0.0",
        universe=("600000.SSE",),
        deployment_level="live",
    )
    store.create_instance(writer)
    store.transition_runtime(
        writer.instance_id,
        lifecycle="running",
        deployment_level="live",
        account_id="uat-account",
        broker="xtp",
        desired_state="running",
        observed_state="running",
        binding_active=True,
    )
    writer_locked = runtime.submit_order(
        "600000.SSE", side="sell", volume=100, price=10,
        reference=f"broker-uat/{run['run_id']}/writer-lock", route_context=context,
    )
    assert writer_locked["routing_rule"] == "automated_writer_lock"
    runtime.close()


def test_broker_uat_v2_allows_two_stable_children_with_one_cumulative_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StrategyRuntimeStore(tmp_path / "uat-v2.sqlite3")
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="xtp",
        trade_broker="xtp",
        quote_provider="xtp",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
    )
    broker = PaperBroker(
        cash=100_000, prices={"600000.SSE": 10.0}, account_id="uat-account",
        open_cost=0.0, min_cost=0.0,
    )
    runtime = LiveRuntime.create(cfg, broker=broker, execution_journal=store)
    runtime.connect(paper_cash=100_000)
    run = store.create_broker_uat_run(
        broker="xtp",
        account_hash=hashlib.sha256(b"uat-account").hexdigest(),
        environment="test",
        plugin_version="1",
        plugin_hash="plugin",
        sdk_version="sdk",
        sdk_hash="sdk-hash",
        scenario_version=2,
        code_commit="a" * 40,
        runtime_code_hash="runtime-hash",
        symbol="600000.SSE",
        max_notional=2_000,
    )
    context = RouteContext(origin=RouteOrigin.BROKER_UAT, uat_run_id=run["run_id"])
    monkeypatch.setenv("ALPHAPILOT_BROKER_UAT_ENABLED", "true")

    fill = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=10,
        reference=f"broker-uat/{run['run_id']}/fill", route_context=context,
    )
    remainder = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=9.99,
        reference=f"broker-uat/{run['run_id']}/remainder", route_context=context,
    )
    duplicate = runtime.submit_order(
        "600000.SSE", side="buy", volume=100, price=9.99,
        reference=f"broker-uat/{run['run_id']}/remainder", route_context=context,
    )

    assert fill["submitted"] is True
    assert remainder["submitted"] is True
    assert duplicate["submitted"] is False
    assert duplicate["routing_rule"] == "duplicate"
    persisted = store.get_broker_uat_run(run["run_id"])
    assert persisted["requested_notional"] == pytest.approx(1999.0)
    runtime.close()


def test_manual_buy_is_blocked_while_automated_live_writer_owns_account(tmp_path: Path) -> None:
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
    )
    broker = PaperBroker(
        cash=100_000.0, prices={"600000.SSE": 10.0}, open_cost=0.0, min_cost=0.0,
    )
    runtime = LiveRuntime.create(
        cfg,
        broker=broker,
        now_fn=SimulatedClock(datetime(2026, 7, 1, 10, 0)),
        execution_journal=ActiveWriterJournal(),
    )
    runtime.connect(paper_cash=100_000.0)

    result = runtime.submit_order("SH600000", side="buy", volume=100, price=10.0)

    assert result["submitted"] is False
    assert result["routing_event"]["payload"]["rule"] == "automated_writer_lock"
    runtime.close()

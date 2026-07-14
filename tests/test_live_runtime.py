from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from alphapilot.systems.live.brokers.paper import PaperBroker
from alphapilot.systems.live.brokers.sim import SimBroker
from alphapilot.systems.live.clock import SimulatedClock
from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.runtime import LiveRuntime, require_live_confirmation
from alphapilot.systems.live.journal import InMemoryExecutionJournal
from alphapilot.systems.live.targets import TargetPortfolio


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

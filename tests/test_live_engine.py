"""Phase 2 integration tests: LiveEngine end-to-end against Paper/Sim brokers.

All deterministic and offline — no vn.py, no broker SDK, no wall-clock waiting.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from alphapilot.systems.live.brokers.paper import PaperBroker
from alphapilot.systems.live.brokers.sim import SimBroker
from alphapilot.systems.live.clock import SimulatedClock
from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.engine import LiveEngine
from alphapilot.systems.live.fsm.session_fsm import SessionState
from alphapilot.systems.live.gateway import BrokerGateway
from alphapilot.systems.live.ledger import Ledger
from alphapilot.systems.live.types import Account, CancelRequest, Exchange, OrderRequest, OrderStatus, TickData

KEY = "600000.SSE"


class TrackingGateway(BrokerGateway):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.connected = 0
        self.closed = 0
        self.subscribed: list[list[str]] = []
        self.orders: list[OrderRequest] = []
        self.cancels: list[CancelRequest] = []
        self.account_queries = 0
        self.position_queries = 0
        self.settings: list[dict] = []

    def connect(self, setting: dict) -> None:
        self.connected += 1
        self.settings.append(setting)
        self._emit_account(Account(account_id=self.name, balance=100_000, available=100_000, gateway=self.name))

    def close(self) -> None:
        self.closed += 1

    def send_order(self, req: OrderRequest) -> str:
        self.orders.append(req)
        order_id = f"{self.name}-1"
        self._emit_order(req.create_order(order_id, self.name))
        return order_id

    def cancel_order(self, req: CancelRequest) -> None:
        self.cancels.append(req)

    def query_account(self) -> None:
        self.account_queries += 1

    def query_position(self) -> None:
        self.position_queries += 1

    def subscribe(self, codes: list[str]) -> None:
        self.subscribed.append(list(codes))
        self._emit_tick(TickData(code="600000", exchange=Exchange.SSE, last_price=10.0, gateway=self.name))


def _engine(tmp_path: Path, broker, mode: str = RunMode.PAPER, clock=None) -> LiveEngine:
    cfg = LiveConfig(mode=mode, ledger_dir=tmp_path / "ledger")
    return LiveEngine(cfg, broker, ledger=Ledger(tmp_path / "ledger"),
                      now_fn=clock or SimulatedClock(datetime(2026, 7, 1, 10, 0)))


def _kinds(engine: LiveEngine) -> set[str]:
    return {e["kind"] for e in engine.ledger.events()}


def test_paper_buy_end_to_end(tmp_path: Path) -> None:
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0}, open_cost=0.0, min_cost=0.0)
    engine = _engine(tmp_path, broker)
    engine.connect({})
    assert engine.oms.buying_power() == 100_000.0

    order_id = engine.submit(OrderRequest.buy("600000", Exchange.SSE, volume=1000, price=10.0))
    assert order_id is not None

    pos = engine.oms.get_position(KEY)
    assert pos.volume == 1000
    assert pos.yd_volume == 0                      # bought today -> not sellable (T+1)
    assert engine.oms.buying_power() == 90_000.0   # 100k - 1000*10
    assert engine.oms.get_order(order_id).status is OrderStatus.ALLTRADED
    assert {"connected", "submit", "order", "trade"} <= _kinds(engine)


def test_direct_real_account_engine_without_calendar_fails_closed(tmp_path: Path) -> None:
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0})
    cfg = LiveConfig(
        mode=RunMode.LIVE,
        broker="paper",
        ledger_dir=tmp_path / "ledger-real-calendar",
    )
    engine = LiveEngine(
        cfg,
        broker,
        now_fn=SimulatedClock(datetime(2026, 7, 1, 10, 0)),
    )

    assert engine.session.current_state() is SessionState.CLOSED


def test_engine_separates_trade_and_quote_gateways(tmp_path: Path) -> None:
    trade = TrackingGateway("trade")
    quote = TrackingGateway("quote")
    cfg = LiveConfig(mode=RunMode.PAPER, ledger_dir=tmp_path / "ledger")
    engine = LiveEngine(cfg, trade, quote_gateway=quote, ledger=Ledger(tmp_path / "ledger"))

    engine.connect({"trade": {"trade": True}, "quote": {"quote": True}})
    engine.subscribe_market_data(["SH600000"])
    engine.subscribe_market_data(["600000.SSE"])
    order_id = engine.submit(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0))
    engine.cancel(order_id, active_only=False)
    engine.reconcile_after_reconnect(auto_resume=False)

    assert trade.connected == 2
    assert quote.connected == 2
    # Explicit reconnect restores the desired quote subscriptions.
    assert quote.subscribed == [["600000.SSE"], ["600000.SSE"]]
    assert trade.subscribed == []
    assert len(trade.orders) == 1
    assert len(quote.orders) == 0
    assert len(trade.cancels) == 1
    assert trade.account_queries == 1
    assert trade.position_queries == 1


def test_market_subscriptions_are_classified_idempotent_and_replayed_in_order(
    tmp_path: Path,
) -> None:
    trade = TrackingGateway("trade")
    quote = TrackingGateway("quote")
    engine = LiveEngine(
        LiveConfig(mode=RunMode.PAPER, ledger_dir=tmp_path / "ledger"),
        trade,
        quote_gateway=quote,
        ledger=Ledger(tmp_path / "ledger"),
    )
    engine.connect({})

    strategy = engine.subscribe_market_data(
        ["SH600000"],
        subscription_type="strategy",
    )
    observer = engine.subscribe_market_data(["000001.SZ"])
    duplicate = engine.subscribe_market_data(["600000.SSE"])

    assert strategy["strategy_symbols"] == ["600000.SSE"]
    assert observer["observer_symbols"] == ["000001.SZSE"]
    assert duplicate["already_subscribed"] == ["600000.SSE"]
    assert duplicate["observer_symbols"] == ["000001.SZSE"]
    assert duplicate["subscription_sources"]["600000.SSE"] == "strategy"
    assert duplicate["awaiting_first_tick"] == []
    assert engine.snapshot()["subscribed_symbols"] == [
        "000001.SZSE",
        "600000.SSE",
    ]

    quote.subscribed.clear()
    engine.reconcile_after_reconnect()
    assert quote.subscribed == [["600000.SSE"], ["000001.SZSE"]]


def test_market_subscription_reports_partial_provider_failure(tmp_path: Path) -> None:
    class PartiallyFailingGateway(TrackingGateway):
        def subscribe(self, codes: list[str]) -> None:
            if codes == ["000001.SZSE"]:
                raise RuntimeError("provider rejected symbol")
            super().subscribe(codes)

    gateway = PartiallyFailingGateway("quote")
    engine = LiveEngine(
        LiveConfig(mode=RunMode.PAPER, ledger_dir=tmp_path / "ledger"),
        gateway,
        ledger=Ledger(tmp_path / "ledger"),
    )
    engine.connect({})

    result = engine.subscribe_market_data(["600000.SSE", "000001.SZ"])

    assert result["added"] == ["600000.SSE"]
    assert result["failed"][0]["symbol"] == "000001.SZSE"
    assert result["subscribed_symbols"] == ["600000.SSE"]
    assert engine.oms.get_tick(KEY).last_price == 10.0


def test_engine_reuses_same_gateway_for_trade_and_quote(tmp_path: Path) -> None:
    gateway = TrackingGateway("same")
    gateway.roles = frozenset({"trade", "quote"})
    cfg = LiveConfig(mode=RunMode.PAPER, ledger_dir=tmp_path / "ledger")
    engine = LiveEngine(cfg, gateway, quote_gateway=gateway, ledger=Ledger(tmp_path / "ledger"))
    settings = {"trade": {"cash": 1}, "quote": {"cash": 2}}

    engine.connect(settings)
    engine.reconcile_after_reconnect(setting=settings)
    engine.close()

    assert gateway.connected == 2
    assert gateway.closed == 2
    assert gateway.settings == [settings, settings]


def test_dry_run_submits_nothing(tmp_path: Path) -> None:
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0})
    engine = _engine(tmp_path, broker, mode=RunMode.DRY_RUN)
    engine.connect({})
    assert engine.submit(OrderRequest.buy("600000", Exchange.SSE, 1000, 10.0)) is None
    assert engine.oms.get_position(KEY) is None
    assert "dry_run_intent" in _kinds(engine)


def test_sim_reject(tmp_path: Path) -> None:
    broker = SimBroker(cash=100_000.0, prices={KEY: 10.0}, reject_codes={"600000"})
    engine = _engine(tmp_path, broker)
    engine.connect({})
    order_id = engine.submit(OrderRequest.buy("600000", Exchange.SSE, 1000, 10.0))
    assert engine.oms.get_order(order_id).status is OrderStatus.REJECTED
    assert engine.oms.get_position(KEY) is None
    assert engine.oms.get_active_orders() == []


def test_sim_partial_then_cancel(tmp_path: Path) -> None:
    broker = SimBroker(cash=100_000.0, prices={KEY: 10.0}, partial_ratio=0.5,
                       open_cost=0.0, min_cost=0.0)
    engine = _engine(tmp_path, broker)
    engine.connect({})
    order_id = engine.submit(OrderRequest.buy("600000", Exchange.SSE, 1000, 10.0))

    order = engine.oms.get_order(order_id)
    assert order.status is OrderStatus.PARTTRADED
    assert order.traded == 500 and order.is_active()
    assert engine.oms.get_position(KEY).volume == 500

    engine.cancel(order_id)
    assert engine.oms.get_order(order_id).status is OrderStatus.CANCELLED
    assert engine.oms.get_active_orders() == []


def test_cancel_inactive_order_is_skipped(tmp_path: Path) -> None:
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0}, open_cost=0.0, min_cost=0.0)
    engine = _engine(tmp_path, broker)
    engine.connect({})
    order_id = engine.submit(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0))
    assert engine.oms.get_order(order_id).status is OrderStatus.ALLTRADED

    result = engine.cancel(order_id)

    assert result["cancelled"] is False
    assert result["reason"] == "not_active"
    assert engine.ledger.events(kind="cancel_skipped")[-1]["payload"]["status"] == "alltraded"


def test_kill_switch_blocks_and_flattens(tmp_path: Path) -> None:
    broker = SimBroker(cash=100_000.0, prices={KEY: 10.0}, partial_ratio=0.5,
                       open_cost=0.0, min_cost=0.0)
    engine = _engine(tmp_path, broker)
    engine.connect({})
    order_id = engine.submit(OrderRequest.buy("600000", Exchange.SSE, 1000, 10.0))
    assert engine.oms.get_order(order_id).is_active()   # 500 still working

    engine.halt("panic")
    # working order cancelled by the kill-switch...
    assert engine.oms.get_order(order_id).status is OrderStatus.CANCELLED
    # ...and further submits are blocked
    assert engine.submit(OrderRequest.buy("000001", Exchange.SZSE, 100, 12.0)) is None
    assert "halt" in _kinds(engine)

    engine.resume()
    assert engine.runmode.can_submit_orders()


def test_disconnect_halts_then_reconcile_resumes(tmp_path: Path) -> None:
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0})
    engine = _engine(tmp_path, broker)
    engine.connect({})

    engine.handle_disconnect("socket drop")
    assert engine.runmode.halted
    assert engine.submit(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0)) is None

    engine.reconcile_and_resume()
    assert not engine.runmode.halted
    assert engine.connection.is_ready()
    assert {"disconnected", "reconciled"} <= _kinds(engine)


def test_reconnect_reconcile_keeps_halted_by_default(tmp_path: Path) -> None:
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0})
    engine = _engine(tmp_path, broker)
    engine.connect({})

    engine.handle_disconnect("socket drop")
    report = engine.reconcile_after_reconnect()

    assert report["resumed"] is False
    assert engine.runmode.halted
    assert engine.connection.is_ready()
    assert engine.submit(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0)) is None
    reconciled = engine.ledger.events(kind="reconciled")[-1]
    assert reconciled["payload"]["auto_resume"] is False
    assert reconciled["payload"]["resumed"] is False

    engine.resume()
    assert not engine.runmode.halted


def test_reconnect_from_logged_in_is_conservative(tmp_path: Path) -> None:
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0})
    engine = _engine(tmp_path, broker)
    engine.connect({})

    report = engine.reconcile_after_reconnect()

    assert report["resumed"] is False
    assert engine.runmode.halted
    assert engine.connection.is_ready()
    assert {"disconnected", "reconciled"} <= _kinds(engine)


def test_engine_applies_risk_gate(tmp_path: Path) -> None:
    from alphapilot.systems.live.config import RiskLimits
    from alphapilot.systems.live.risk import RiskGate

    broker = PaperBroker(cash=5_000.0, prices={KEY: 10.0}, open_cost=0.0, min_cost=0.0)
    cfg = LiveConfig(mode=RunMode.PAPER, ledger_dir=tmp_path / "ledger")
    gate = RiskGate(
        RiskLimits(max_order_value=1e12, max_daily_value=1e15, max_position_pct=1.0,
                   price_guard_pct=0.1, max_orders_per_day=1000, lot_size=100),
        enforce_session=False,
    )
    engine = LiveEngine(cfg, broker, ledger=Ledger(tmp_path / "ledger"),
                        now_fn=SimulatedClock(datetime(2026, 7, 1, 10, 0)), risk=gate)
    engine.connect({})
    # 1000 * 10 = 10000 > buying power 5000 -> risk gate rejects, order never routed
    assert engine.submit(OrderRequest.buy("600000", Exchange.SSE, 1000, 10.0)) is None
    assert engine.oms.get_position(KEY) is None
    assert "rejected" in _kinds(engine)


def test_account_loss_halt_is_immediate_latched_and_not_auto_resumed(
    tmp_path: Path,
) -> None:
    from alphapilot.systems.live.config import RiskLimits
    from alphapilot.systems.live.risk import RiskGate

    gateway = TrackingGateway("loss-gateway")
    limits = RiskLimits(
        max_order_value=10_000,
        max_daily_value=0,
        max_position_pct=0.02,
        price_guard_pct=0.02,
        max_orders_per_day=20,
        lot_size=100,
        max_daily_loss_pct=0.01,
        max_canary_loss_pct=0.03,
    )
    engine = LiveEngine(
        LiveConfig(mode=RunMode.PAPER, ledger_dir=tmp_path / "loss-ledger"),
        gateway,
        ledger=Ledger(tmp_path / "loss-ledger"),
        now_fn=SimulatedClock(datetime(2026, 7, 1, 10, 0)),
        risk=RiskGate(limits, enforce_session=False),
    )
    engine.connect({})

    engine.on_account(
        Account(
            account_id="loss-gateway",
            balance=98_900,
            available=98_900,
            gateway="loss-gateway",
        )
    )

    assert engine.runmode.halted
    assert engine.risk.snapshot()["loss_halt_rule"] == "daily_loss"
    report = engine.reconcile_after_reconnect(auto_resume=True)
    assert report["resumed"] is False
    assert engine.runmode.halted

    # Explicit operator recovery is required; the reconnect refreshed equity to 100k.
    engine.resume()
    assert engine.runmode.halted is False


def test_t_plus_one_roll_via_broker_snapshot(tmp_path: Path) -> None:
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0}, open_cost=0.0, min_cost=0.0)
    engine = _engine(tmp_path, broker)
    engine.connect({})
    engine.submit(OrderRequest.buy("600000", Exchange.SSE, 1000, 10.0))
    assert engine.oms.available_shares(KEY) == 0        # today's buy not sellable

    broker.roll_new_day()          # next trading day at the broker
    broker.query_position()        # broker re-publishes authoritative snapshot
    assert engine.oms.available_shares(KEY) == 1000     # now sellable

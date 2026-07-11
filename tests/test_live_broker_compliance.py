"""BrokerGateway compliance harness for offline gateways.

These tests encode the minimum behavior every broker adapter must preserve:
connect emits observable state, queries are repeatable, send_order returns an id
and reports status asynchronously through callbacks, cancel reports terminal
state for working orders, and close is observable. Real SDK brokers can reuse
the same harness in credential-gated tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alphapilot.systems.live.brokers.paper import PaperBroker
from alphapilot.systems.live.brokers.sim import SimBroker
from alphapilot.systems.live.gateway import BrokerGateway
from alphapilot.systems.live.types import (
    Account,
    Contract,
    Exchange,
    LogEvent,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
    TickData,
    Trade,
)


@dataclass
class GatewayCapture:
    orders: list[Order] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    contracts: list[Contract] = field(default_factory=list)
    ticks: list[TickData] = field(default_factory=list)
    logs: list[LogEvent] = field(default_factory=list)

    def on_order(self, order: Order) -> None:
        self.orders.append(order)

    def on_trade(self, trade: Trade) -> None:
        self.trades.append(trade)

    def on_position(self, position: Position) -> None:
        self.positions.append(position)

    def on_account(self, account: Account) -> None:
        self.accounts.append(account)

    def on_contract(self, contract: Contract) -> None:
        self.contracts.append(contract)

    def on_tick(self, tick: TickData) -> None:
        self.ticks.append(tick)

    def on_log(self, log: LogEvent) -> None:
        self.logs.append(log)


def run_basic_gateway_compliance(gateway: BrokerGateway, *, expect_working_cancel: bool) -> None:
    capture = GatewayCapture()
    gateway.register_callback(capture)

    if hasattr(gateway, "seed_position"):
        gateway.seed_position("SH600000", 200, 10.0, sellable=True)
    gateway.connect({"cash": 100_000.0})

    assert capture.logs
    assert capture.accounts
    assert capture.accounts[-1].available == 100_000.0
    assert any(position.key == "600000.SSE" for position in capture.positions)

    account_count = len(capture.accounts)
    position_count = len(capture.positions)
    gateway.query_account()
    gateway.query_position()
    assert len(capture.accounts) > account_count
    assert len(capture.positions) > position_count

    order_id = gateway.send_order(OrderRequest.buy("600000", Exchange.SSE, 200, 10.0, reference="compliance"))
    assert order_id
    order_updates = [order for order in capture.orders if order.order_id == order_id]
    assert order_updates
    assert order_updates[0].status is OrderStatus.SUBMITTING

    if expect_working_cancel:
        assert any(order.status is OrderStatus.PARTTRADED for order in order_updates)
        gateway.cancel_order(order_updates[-1].create_cancel())
        assert capture.orders[-1].order_id == order_id
        assert capture.orders[-1].status is OrderStatus.CANCELLED
    else:
        assert any(trade.order_id == order_id for trade in capture.trades)
        assert capture.orders[-1].order_id == order_id
        assert capture.orders[-1].status is OrderStatus.ALLTRADED

    gateway.close()
    assert "closed" in capture.logs[-1].msg


def test_paper_broker_compliance_full_fill() -> None:
    broker = PaperBroker(cash=100_000.0, prices={"600000.SSE": 10.0}, open_cost=0.0, min_cost=0.0)
    run_basic_gateway_compliance(broker, expect_working_cancel=False)


def test_sim_broker_compliance_partial_then_cancel() -> None:
    broker = SimBroker(
        cash=100_000.0,
        prices={"600000.SSE": 10.0},
        partial_ratio=0.5,
        open_cost=0.0,
        min_cost=0.0,
    )
    run_basic_gateway_compliance(broker, expect_working_cancel=True)

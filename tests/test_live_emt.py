"""Native EMT gateway: vendor-specific converters + shared-skeleton handlers.

The shared order/trade/tick/contract machinery is covered by
``test_live_xtp_pro.py`` (same ``AShareVendorGateway`` code path); this file
pins down what is EMT-specific: the ``order_emt_id`` field, ``market == 100``
position skip, ``sellable_qty``-as-yd semantics, and buying_power-as-balance.
"""

from __future__ import annotations

import pytest

from alphapilot.systems.live.brokers import emt as em
from alphapilot.systems.live.types import Exchange, OrderStatus


class RecordingCallback:
    def __init__(self) -> None:
        self.orders, self.trades, self.ticks = [], [], []
        self.positions, self.accounts, self.contracts, self.logs = [], [], [], []

    def on_order(self, o): self.orders.append(o)
    def on_trade(self, t): self.trades.append(t)
    def on_position(self, p): self.positions.append(p)
    def on_account(self, a): self.accounts.append(a)
    def on_contract(self, c): self.contracts.append(c)
    def on_tick(self, t): self.ticks.append(t)
    def on_log(self, e): self.logs.append(e)


@pytest.fixture()
def gateway() -> tuple[em.EmtGateway, RecordingCallback]:
    gw = em.EmtGateway()
    cb = RecordingCallback()
    gw.register_callback(cb)
    return gw, cb


def test_position_from_emt_uses_sellable_as_yd_and_skips_market_100() -> None:
    assert em.position_from_emt({"market": 100}, "emt") is None
    assert em.position_from_emt({}, "emt") is None
    pos = em.position_from_emt(
        {
            "ticker": "000001",
            "market": 1,
            "position_direction": 1,
            "total_qty": 1000,
            "sellable_qty": 600,
            "avg_price": 10.0,
            "unrealized_pnl": 12.0,
        },
        "emt",
    )
    assert pos is not None
    assert pos.exchange == Exchange.SZSE
    assert pos.frozen == 400
    assert pos.yd_volume == 600           # EMT: sellable_qty, not yesterday_position


def test_account_from_emt_buying_power_is_balance() -> None:
    acct = em.account_from_emt(
        {"buying_power": 50000.0, "withholding_amount": 100.0, "account_type": 0},
        "u1",
        "emt",
    )
    assert acct.balance == 50000.0 and acct.available == 50000.0 and acct.frozen == 100.0


def test_order_and_trade_events_use_order_emt_id(gateway) -> None:
    gw, cb = gateway
    order_data = {
        "ticker": "000001",
        "market": 1,
        "order_emt_id": 555,
        "side": 1,
        "price_type": 1,
        "price": 10.0,
        "quantity": 100,
        "qty_traded": 0,
        "order_status": 4,
        "insert_time": 20260706093001000,
    }
    gw.td_api.onOrderEvent(order_data, {"error_id": 0}, 1)
    gw.td_api.onTradeEvent(
        {
            "ticker": "000001", "market": 1, "order_emt_id": 555,
            "exec_id": 9, "side": 1, "price": 10.0, "quantity": 100,
            "trade_time": 20260706100000000,
        },
        1,
    )
    gw.dispatcher.run_pending()
    assert [o.status for o in cb.orders] == [OrderStatus.NOTTRADED, OrderStatus.ALLTRADED]
    assert cb.trades[0].order_id == "555"
    assert gw.orders["555"].traded == 100


def test_send_order_guards(gateway) -> None:
    gw, cb = gateway
    from alphapilot.systems.live.types import OrderRequest

    assert gw.send_order(OrderRequest.buy("830001", Exchange.BSE, 100, 5.0)) == ""
    gw.td_api.margin_trading = True
    assert gw.send_order(OrderRequest.buy("000001", Exchange.SZSE, 100, 10.0)) == ""
    gw.dispatcher.run_pending()
    assert all(log.level == "error" for log in cb.logs)

"""Native XTP Pro gateway: pure converters + dispatch-thread handlers.

No SDK session is opened: converter tests feed vendor-shaped dicts, handler
tests drive the gateway's dispatcher synchronously via ``run_pending()``. The
success path of ``send_order`` (which calls the C++ ``insertOrder``) is covered
by the connect smoke script against the broker test environment, not here.
"""

from __future__ import annotations

import pytest

from alphapilot.systems.live.brokers import xtp_pro as xp
from alphapilot.systems.live.types import (
    Direction,
    Exchange,
    Offset,
    OrderStatus,
    OrderType,
    Product,
)


# --------------------------------------------------------------------------- #
# fixtures: vendor-shaped dicts
# --------------------------------------------------------------------------- #
def make_tick_data(**overrides) -> dict:
    data = {
        "ticker": "600000",
        "exchange_id": 1,                      # EXCHANGE map: 1 == SSE
        "data_time": 20260706093000123,
        "qty": 1_234_500,
        "turnover": 1.5e7,
        "last_price": 12.3456,
        "upper_limit_price": 13.58,
        "lower_limit_price": 11.11,
        "open_price": 12.30,
        "high_price": 12.40,
        "low_price": 12.20,
        "pre_close_price": 12.34,
        "bid": [12.34, 12.33, 12.32, 12.31, 12.30, 0, 0, 0, 0, 0],
        "ask": [12.35, 12.36, 12.37, 12.38, 12.39, 0, 0, 0, 0, 0],
        "bid_qty": [100, 200, 300, 400, 500, 0, 0, 0, 0, 0],
        "ask_qty": [150, 250, 350, 450, 550, 0, 0, 0, 0, 0],
    }
    data.update(overrides)
    return data


def make_order_data(**overrides) -> dict:
    data = {
        "ticker": "600000",
        "market": 2,                           # MARKET map: 2 == SSE (reversed!)
        "order_xtp_id": 123456789,
        "side": 1,                             # LONG / NONE
        "price_type": 1,                       # LIMIT
        "price": 12.30,
        "quantity": 200,
        "qty_traded": 0,
        "order_status": 4,                     # NOTTRADED
        "insert_time": 20260706093001000,
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------- #
# converters
# --------------------------------------------------------------------------- #
def test_market_and_exchange_maps_are_opposite() -> None:
    # Inherited SDK quirk — porting must preserve both tables as-is.
    assert xp.MARKET_XTP2VT[1] == Exchange.SZSE
    assert xp.MARKET_XTP2VT[2] == Exchange.SSE
    assert xp.EXCHANGE_XTP2VT[1] == Exchange.SSE
    assert xp.EXCHANGE_XTP2VT[2] == Exchange.SZSE


def test_tick_from_xtp_without_contract() -> None:
    tick = xp.tick_from_xtp(make_tick_data(), None, "xtp")
    assert tick.code == "600000"
    assert tick.exchange == Exchange.SSE
    assert tick.volume == 1_234_500
    assert tick.turnover == 1.5e7
    assert tick.last_price == pytest.approx(12.3456)   # no rounding w/o contract
    assert tick.bid_price_1 == pytest.approx(12.34)
    assert tick.ask_volume_1 == 150
    assert tick.datetime is not None and tick.datetime.tzinfo is not None


def test_tick_from_xtp_rounds_to_price_tick_and_names() -> None:
    contract = xp.contract_from_xtp(
        {
            "ticker": "600000",
            "exchange_id": 1,
            "ticker_name": "浦发银行",
            "ticker_type": 0,
            "price_tick": 0.01,
            "buy_qty_unit": 100,
        },
        "xtp",
    )
    tick = xp.tick_from_xtp(make_tick_data(), contract, "xtp")
    assert tick.last_price == pytest.approx(12.35)     # 12.3456 -> nearest 0.01
    assert tick.name == "浦发银行"


def test_contract_from_xtp_maps_product_and_lot() -> None:
    contract = xp.contract_from_xtp(
        {
            "ticker": "510300",
            "exchange_id": 1,
            "ticker_name": "沪深300ETF",
            "ticker_type": 2,
            "price_tick": 0.001,
            "buy_qty_unit": 100,
        },
        "xtp",
    )
    assert contract.product == Product.FUND
    assert contract.price_tick == 0.001
    assert contract.lot_size == 100
    assert contract.key == "510300.SSE"


def test_order_from_xtp_stock_and_star_market() -> None:
    order = xp.order_from_xtp(make_order_data(), "xtp")
    assert order is not None
    assert order.order_id == "123456789"
    assert order.exchange == Exchange.SSE
    assert (order.direction, order.offset) == (Direction.LONG, Offset.NONE)
    assert order.type == OrderType.LIMIT
    assert order.status == OrderStatus.NOTTRADED
    assert order.datetime is not None

    star = xp.order_from_xtp(make_order_data(ticker="688001", price_type=7), "xtp")
    assert star is not None and star.type == OrderType.MARKET


def test_order_from_xtp_skips_options() -> None:
    assert xp.order_from_xtp(make_order_data(ticker="10004567"), "xtp") is None


def test_trade_from_xtp() -> None:
    trade = xp.trade_from_xtp(
        {
            "ticker": "000001",
            "market": 1,                       # SZSE
            "order_xtp_id": 42,
            "exec_id": 777,
            "side": 2,                         # SHORT / NONE
            "price": 10.5,
            "quantity": 100,
            "trade_time": 20260706100000500,
        },
        "xtp",
    )
    assert trade is not None
    assert trade.exchange == Exchange.SZSE
    assert trade.direction == Direction.SHORT
    assert trade.trade_id == "777" and trade.order_id == "42"


def test_position_from_xtp_and_market_zero_skip() -> None:
    assert xp.position_from_xtp({"market": 0}, "xtp") is None
    pos = xp.position_from_xtp(
        {
            "ticker": "600000",
            "market": 2,
            "position_direction": 1,
            "total_qty": 1000,
            "sellable_qty": 600,
            "avg_price": 12.0,
            "unrealized_pnl": 55.0,
            "yesterday_position": 800,
        },
        "xtp",
    )
    assert pos is not None
    assert pos.volume == 1000 and pos.frozen == 400 and pos.yd_volume == 800


def test_account_from_xtp_cash_and_option_accounts() -> None:
    base = {
        "total_asset": 100000.0,
        "withholding_amount": 500.0,
        "buying_power": 80000.0,
        "account_type": 0,
        "security_asset": 15000.0,
    }
    acct = xp.account_from_xtp(base, "u1", "xtp")
    assert acct.balance == 100000.0 and acct.available == 80000.0 and acct.frozen == 500.0

    opt = xp.account_from_xtp({**base, "account_type": 2}, "u1", "xtp")
    assert opt.frozen == pytest.approx(100000.0 - 80000.0 - 15000.0)


# --------------------------------------------------------------------------- #
# gateway handlers (synchronous dispatch via run_pending)
# --------------------------------------------------------------------------- #
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
def gateway() -> tuple[xp.XtpProGateway, RecordingCallback]:
    gw = xp.XtpProGateway()
    cb = RecordingCallback()
    gw.register_callback(cb)
    return gw, cb


def test_handle_order_event_creates_then_updates(gateway) -> None:
    gw, cb = gateway
    gw.td_api.onOrderEvent(make_order_data(), {"error_id": 0}, 1)
    gw.td_api.onOrderEvent(
        make_order_data(qty_traded=200, order_status=1), {"error_id": 0}, 1
    )
    gw.dispatcher.run_pending()
    assert [o.status for o in cb.orders] == [OrderStatus.NOTTRADED, OrderStatus.ALLTRADED]
    assert cb.orders[1].traded == 200
    # cache holds one order, updated in place
    assert gw.orders["123456789"].status == OrderStatus.ALLTRADED


def test_handle_trade_event_accumulates_fills(gateway) -> None:
    gw, cb = gateway
    gw.td_api.onOrderEvent(make_order_data(), {"error_id": 0}, 1)

    trade_data = {
        "ticker": "600000", "market": 2, "order_xtp_id": 123456789,
        "exec_id": 1, "side": 1, "price": 12.30, "quantity": 100,
        "trade_time": 20260706100000000,
    }
    gw.td_api.onTradeEvent(dict(trade_data), 1)
    gw.td_api.onTradeEvent({**trade_data, "exec_id": 2}, 1)
    gw.dispatcher.run_pending()

    assert len(cb.trades) == 2
    statuses = [o.status for o in cb.orders]
    assert statuses == [OrderStatus.NOTTRADED, OrderStatus.PARTTRADED, OrderStatus.ALLTRADED]
    assert cb.orders[-1].traded == 200


def test_handle_trade_without_order_logs_warning(gateway) -> None:
    gw, cb = gateway
    gw.td_api.onTradeEvent(
        {
            "ticker": "600000", "market": 2, "order_xtp_id": 999,
            "exec_id": 5, "side": 1, "price": 1.0, "quantity": 100,
            "trade_time": 20260706100000000,
        },
        1,
    )
    gw.dispatcher.run_pending()
    assert len(cb.trades) == 1 and not cb.orders
    assert any("999" in log.msg for log in cb.logs)


def test_handle_tick_uses_contract_cache(gateway) -> None:
    gw, cb = gateway
    gw.md_api.onQueryAllTickers(
        {
            "ticker": "600000", "exchange_id": 1, "ticker_name": "浦发银行",
            "ticker_type": 0, "price_tick": 0.01, "buy_qty_unit": 100,
        },
        {}, True,
    )
    gw.md_api.onDepthMarketData(make_tick_data())
    gw.dispatcher.run_pending()
    assert len(cb.contracts) == 1
    assert len(cb.ticks) == 1
    assert cb.ticks[0].last_price == pytest.approx(12.35)   # rounded via cache
    assert cb.ticks[0].name == "浦发银行"


def test_option_contract_cached_but_not_emitted(gateway) -> None:
    gw, cb = gateway
    gw.md_api.onQueryAllTickers(
        {
            "ticker": "10004567", "exchange_id": 1, "ticker_name": "某期权",
            "ticker_type": 4, "price_tick": 0.0001, "buy_qty_unit": 1,
        },
        {}, False,
    )
    gw.dispatcher.run_pending()
    assert not cb.contracts
    assert "10004567.SSE" in gw.contracts


def test_send_order_guards_reject_before_sdk(gateway) -> None:
    gw, cb = gateway
    from alphapilot.systems.live.types import OrderRequest

    # unsupported exchange
    assert gw.send_order(OrderRequest.buy("830001", Exchange.BSE, 100, 5.0)) == ""
    # margin account requires an explicit offset
    gw.td_api.margin_trading = True
    assert gw.send_order(OrderRequest.buy("600000", Exchange.SSE, 100, 12.0)) == ""
    gw.td_api.margin_trading = False
    # option symbols out of scope
    req = OrderRequest.buy("10004567", Exchange.SSE, 100, 5.0)
    req.code = "10004567"
    assert gw.send_order(req) == ""
    gw.dispatcher.run_pending()
    assert all(log.level == "error" for log in cb.logs)


def test_cancel_order_tolerates_prefixed_ids(gateway) -> None:
    gw, _ = gateway
    calls = []
    gw.td_api.cancelOrder = lambda oid, sid: calls.append((oid, sid))  # stub SDK call
    from alphapilot.systems.live.types import CancelRequest

    gw.cancel_order(CancelRequest(order_id="XTP.123", code="600000", exchange=Exchange.SSE))
    gw.cancel_order(CancelRequest(order_id="456", code="600000", exchange=Exchange.SSE))
    assert [c[0] for c in calls] == [123, 456]

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
        self.gateway_connected, self.gateway_disconnected = [], []

    def on_order(self, o): self.orders.append(o)
    def on_trade(self, t): self.trades.append(t)
    def on_position(self, p): self.positions.append(p)
    def on_account(self, a): self.accounts.append(a)
    def on_contract(self, c): self.contracts.append(c)
    def on_tick(self, t): self.ticks.append(t)
    def on_log(self, e): self.logs.append(e)
    def on_gateway_connected(self, gateway, channel, detail=""):
        self.gateway_connected.append({"gateway": gateway, "channel": channel, "detail": detail})
    def on_gateway_disconnected(self, gateway, channel, reason="", *, halt=True):
        self.gateway_disconnected.append({
            "gateway": gateway,
            "channel": channel,
            "reason": reason,
            "halt": halt,
        })


@pytest.fixture()
def gateway() -> tuple[em.EmtGateway, RecordingCallback]:
    gw = em.EmtGateway()
    cb = RecordingCallback()
    gw.register_callback(cb)
    return gw, cb


def test_connect_uses_separate_quote_credentials_when_present(gateway, monkeypatch) -> None:
    gw, _ = gateway
    calls = {}
    monkeypatch.setattr(em, "SDK_AVAILABLE", True)
    monkeypatch.setattr(gw, "start", lambda: None)
    monkeypatch.setattr(gw, "post_log", lambda *args, **kwargs: None)

    def md_connect(userid, password, client_id, server_ip, server_port, quote_protocol, log_level):
        calls["md"] = {
            "userid": userid,
            "password": password,
            "client_id": client_id,
            "server_ip": server_ip,
            "server_port": server_port,
            "quote_protocol": quote_protocol,
            "log_level": log_level,
        }

    def td_connect(userid, password, client_id, server_ip, server_port, log_level):
        calls["td"] = {
            "userid": userid,
            "password": password,
            "client_id": client_id,
            "server_ip": server_ip,
            "server_port": server_port,
            "log_level": log_level,
        }

    monkeypatch.setattr(gw.md_api, "connect", md_connect)
    monkeypatch.setattr(gw.td_api, "connect", td_connect)

    gw.connect({
        "账号": "trade-user",
        "密码": "trade-pass",
        "客户号": 9,
        "行情账号": "quote-user",
        "行情密码": "quote-pass",
        "行情地址": "quote-host",
        "行情端口": 1001,
        "交易地址": "trade-host",
        "交易端口": 1002,
        "行情协议": "TCP",
        "日志级别": "INFO",
    })

    assert calls["md"]["userid"] == "quote-user"
    assert calls["md"]["password"] == "quote-pass"
    assert calls["td"]["userid"] == "trade-user"
    assert calls["td"]["password"] == "trade-pass"


def test_position_from_emt_uses_sellable_as_yd_and_skips_market_100() -> None:
    assert em.position_from_emt({"market": 100}, "emt") is None
    assert em.position_from_emt({}, "emt") is None
    assert em.position_from_emt({"market": 3}, "emt") is None
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


def test_query_requests_and_callbacks_use_snapshots(gateway) -> None:
    gw, cb = gateway
    gw.td_api.connect_status = True
    gw.td_api.session_id = 77
    calls = []
    gw.td_api.queryOrders = lambda req, session, reqid: calls.append(("orders", req, session, reqid)) or 0
    gw.td_api.queryTrades = lambda req, session, reqid: calls.append(("trades", req, session, reqid)) or 0

    assert gw.query_orders() is True
    assert gw.query_trades() is True
    assert calls == [
        ("orders", {"ticker": "", "begin_time": 0, "end_time": 0}, 77, 1),
        ("trades", {"ticker": "", "begin_time": 0, "end_time": 0}, 77, 2),
    ]

    order_data = {
        "ticker": "000001",
        "market": 1,
        "order_emt_id": 555,
        "side": 1,
        "price_type": 1,
        "price": 10.0,
        "quantity": 100,
        "qty_traded": 100,
        "order_status": 1,
        "insert_time": 20260706093001000,
    }
    gw.td_api.onQueryOrder(order_data, {"error_id": 0}, 3, True, 77)
    gw.td_api.onQueryTrade(
        {
            "ticker": "000001", "market": 1, "order_emt_id": 555,
            "exec_id": 9, "side": 1, "price": 10.0, "quantity": 100,
            "trade_time": 20260706100000000,
        },
        {"error_id": 0},
        4,
        True,
        77,
    )
    gw.dispatcher.run_pending()

    assert cb.orders[-1].status is OrderStatus.ALLTRADED
    assert cb.orders[-1].traded == 100
    assert gw.orders["555"].traded == 100
    assert cb.trades[-1].order_id == "555"


def test_sdk_disconnect_callbacks_emit_gateway_events(gateway, monkeypatch) -> None:
    gw, cb = gateway

    class DummyThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(em.threading, "Thread", DummyThread)

    gw.md_api.onDisconnected(7)
    gw.td_api.onDisconnected(1, 8)
    gw.dispatcher.run_pending()

    assert cb.gateway_disconnected == [
        {"gateway": "emt", "channel": "quote", "reason": "7", "halt": False},
        {"gateway": "emt", "channel": "trade", "reason": "8", "halt": True},
    ]
    assert any("行情服务器连接断开" in log.msg for log in cb.logs)
    assert any("交易服务器连接断开" in log.msg for log in cb.logs)


def test_trade_disconnect_accepts_reason_only_callback(gateway, monkeypatch) -> None:
    gw, cb = gateway

    class DummyThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(em.threading, "Thread", DummyThread)

    gw.td_api.onDisconnected(9)
    gw.dispatcher.run_pending()

    assert cb.gateway_disconnected == [
        {"gateway": "emt", "channel": "trade", "reason": "9", "halt": True},
    ]


def test_trade_close_uses_logout_release_by_default(gateway, monkeypatch) -> None:
    gw, _ = gateway
    calls = []
    gw.td_api.connect_status = True
    gw.td_api.login_status = True
    gw.td_api.session_id = 42
    monkeypatch.delenv("ALPHAPILOT_LIVE_EMT_TD_EXIT", raising=False)
    monkeypatch.setattr(gw.td_api, "logout", lambda session: calls.append(("logout", session)) or 0, raising=False)
    monkeypatch.setattr(gw.td_api, "release", lambda: calls.append(("release",)), raising=False)
    monkeypatch.setattr(gw.td_api, "exit", lambda: calls.append(("exit",)), raising=False)

    gw.td_api.close()

    assert calls == [("logout", 42), ("release",)]
    assert gw.td_api.connect_status is False
    assert gw.td_api.login_status is False


def test_trade_close_native_exit_is_explicit_opt_in(gateway, monkeypatch) -> None:
    gw, _ = gateway
    calls = []
    gw.td_api.connect_status = True
    gw.td_api.login_status = True
    gw.td_api.session_id = 42
    monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_TD_EXIT", "1")
    monkeypatch.setattr(gw.td_api, "logout", lambda session: calls.append(("logout", session)) or 0, raising=False)
    monkeypatch.setattr(gw.td_api, "release", lambda: calls.append(("release",)), raising=False)
    monkeypatch.setattr(gw.td_api, "exit", lambda: calls.append(("exit",)), raising=False)

    gw.td_api.close()

    assert calls == [("exit",)]
    assert gw.td_api.connect_status is False
    assert gw.td_api.login_status is False


def test_disconnect_during_close_does_not_reconnect(gateway, monkeypatch) -> None:
    gw, cb = gateway

    def fail_thread(*args, **kwargs):
        raise AssertionError("close-time disconnect must not start reconnect")

    monkeypatch.setattr(em.threading, "Thread", fail_thread)

    gw.md_api._closing = True
    gw.td_api._closing = True
    gw.md_api.onDisconnected(7)
    gw.td_api.onDisconnected(8)
    gw.dispatcher.run_pending()

    assert cb.gateway_disconnected == []


def test_quote_close_soft_releases_by_default(gateway, monkeypatch) -> None:
    gw, _ = gateway
    calls = []
    gw.md_api.connect_status = True
    gw.md_api.login_status = True
    gw.md_api._contract_queries_pending = 0
    monkeypatch.delenv("ALPHAPILOT_LIVE_EMT_MD_LOGOUT", raising=False)
    monkeypatch.setattr(gw.md_api, "release", lambda: calls.append(("release",)), raising=False)
    monkeypatch.setattr(gw.md_api, "exit", lambda: calls.append(("exit",)), raising=False)

    gw.md_api.close()

    assert calls == [("release",)]
    assert gw.md_api.connect_status is False
    assert gw.md_api.login_status is False


def test_quote_close_native_logout_is_explicit_opt_in(gateway, monkeypatch) -> None:
    gw, _ = gateway
    calls = []
    gw.md_api.connect_status = True
    gw.md_api.login_status = True
    monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_MD_LOGOUT", "1")
    monkeypatch.setattr(gw.md_api, "release", lambda: calls.append(("release",)), raising=False)
    monkeypatch.setattr(gw.md_api, "exit", lambda: calls.append(("exit",)), raising=False)

    gw.md_api.close()

    assert calls == [("exit",)]
    assert gw.md_api.connect_status is False
    assert gw.md_api.login_status is False


def test_gateway_close_stops_dispatcher_without_draining(gateway, monkeypatch) -> None:
    gw, _ = gateway
    calls = []
    monkeypatch.setattr(gw, "shutdown", lambda: calls.append("shutdown"))
    monkeypatch.setattr(gw.dispatcher, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(gw.md_api, "close", lambda: calls.append("md"))
    monkeypatch.setattr(gw.td_api, "close", lambda: calls.append("td"))

    gw.close()

    assert calls == ["stop", "md", "td"]
    assert gw.md_api._closing is True
    assert gw.td_api._closing is True


def test_trade_login_failure_emits_halt_disconnect(gateway) -> None:
    gw, cb = gateway
    gw.td_api.login = lambda *args: 0
    gw.td_api.getApiLastError = lambda: {"error_id": 12130005, "error_msg": "Login xgw failed"}

    gw.td_api.login_server()
    gw.dispatcher.run_pending()

    assert cb.gateway_disconnected == [
        {"gateway": "emt", "channel": "trade", "reason": "Login xgw failed", "halt": True},
    ]
    assert cb.logs[-1].level == "error"
    assert "交易服务器登录失败" in cb.logs[-1].msg


def test_send_order_guards(gateway) -> None:
    gw, cb = gateway
    from alphapilot.systems.live.types import OrderRequest

    assert gw.send_order(OrderRequest.buy("830001", Exchange.BSE, 100, 5.0)) == ""
    gw.td_api.margin_trading = True
    assert gw.send_order(OrderRequest.buy("000001", Exchange.SZSE, 100, 10.0)) == ""
    gw.dispatcher.run_pending()
    assert all(log.level == "error" for log in cb.logs)

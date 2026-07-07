"""EmtGateway — native 东方财富 EMT gateway, no vn.py required.

Ported from the vendored ``vnpy_emt.gateway.emt_gateway`` onto the shared
:class:`~alphapilot.systems.live.brokers.vendor_common.AShareVendorGateway`
skeleton (EMT's counter is XTP-derived, so all int tables and the order/trade
state machine are common). What is genuinely EMT and lives here:

* quote API is the new EMQ build: ``createQuoteApi(path_str, file_log_level,
  console_log_level)`` (plain str path, 3 args) and a 4-arg ``login`` — both
  different from XTP;
* no software key;
* the vendor order-id field is ``order_emt_id``;
* ``queryPosition`` takes a trailing flag argument;
* position rows use ``sellable_qty`` as the T+1 sellable amount and flag
  non-rows with ``market == 100``;
* the asset snapshot has no total-asset field — ``buying_power`` doubles as
  the balance (upstream vn.py behavior, kept as-is).

Options and the credit-debt (两融) position query were dropped — out of scope
for the A-share cash-equity live stack (margin direction mapping is kept).
"""

from __future__ import annotations

import os
import threading

from alphapilot.systems.live.brokers.base import sdk_log_path
from alphapilot.systems.live.brokers.vendor_common import (
    AShareVendorGateway,
    DIRECTION_STOCK_VT2VENDOR,
    EQUITY_ORDERTYPE_VT2VENDOR,
    EXCHANGE_VENDOR2VT,
    EXCHANGE_VT2VENDOR,
    LOGLEVEL_VT2VENDOR,
    MARKET_VENDOR2VT,
    MARKET_VT2VENDOR,
    POSITION_DIRECTION_VENDOR2VT,
    PROTOCOL_VT2VENDOR,
    STAR_ORDERTYPE_VT2VENDOR,
)
from alphapilot.systems.live.types import (
    Account,
    CancelRequest,
    Direction,
    Exchange,
    Offset,
    OrderRequest,
    OrderStatus,
    Position,
    normalize_symbol,
)

try:  # compiled pybind bindings — present in the live env, absent on dev boxes
    from vnpy_emt.api import MdApi as _SdkMdApi
    from vnpy_emt.api import TdApi as _SdkTdApi

    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the SDK
    _SdkMdApi = object
    _SdkTdApi = object
    SDK_AVAILABLE = False


def _td_native_exit_enabled() -> bool:
    return os.environ.get("ALPHAPILOT_LIVE_EMT_TD_EXIT", "").lower() in {"1", "true", "yes", "on"}


# ---- vendor-specific converters (EMT field semantics) ------------------------ #
def position_from_emt(data: dict, gateway: str) -> Position | None:
    if not data or data.get("market") == 100:
        return None
    exchange = MARKET_VENDOR2VT.get(data.get("market"))
    if exchange is None:
        return None
    return Position(
        code=data["ticker"],
        exchange=exchange,
        direction=POSITION_DIRECTION_VENDOR2VT.get(data["position_direction"], Direction.LONG),
        volume=data["total_qty"],
        frozen=data["total_qty"] - data["sellable_qty"],
        price=data["avg_price"],
        pnl=data["unrealized_pnl"],
        yd_volume=data["sellable_qty"],
        gateway=gateway,
    )


def account_from_emt(data: dict, account_id: str, gateway: str) -> Account:
    # EMT's asset snapshot exposes no total-asset figure; upstream vn.py maps
    # buying_power to both balance and available. Preserved as-is.
    account = Account(
        account_id=account_id,
        balance=round(data["buying_power"], 2),
        frozen=round(data["withholding_amount"], 2),
        available=round(data["buying_power"], 2),
        gateway=gateway,
    )
    if data.get("account_type") == 2:  # option account: frozen derived differently
        account.frozen = round(account.balance - account.available - data["security_asset"], 2)
    return account


# ---- SDK subclasses (callbacks hop onto the dispatcher immediately) --------- #
class _EmtMdApi(_SdkMdApi):
    """Quote API wrapper (EMQ build). Callbacks arrive on the SDK's MD thread."""

    def __init__(self, gateway: "EmtGateway") -> None:
        super().__init__()
        self.gateway = gateway

        self.userid = ""
        self.password = ""
        self.client_id = 0
        self.server_ip = ""
        self.server_port = 0
        self.protocol = 0

        self.connect_status = False
        self.login_status = False
        self._contract_queries_pending = 0

    # -- SDK callbacks ------------------------------------------------------ #
    def onDisconnected(self, reason: int) -> None:
        self.connect_status = False
        self.login_status = False
        self.gateway.post_log(f"行情服务器连接断开, 原因{reason}", "warning")
        self.gateway.post_gateway_disconnected("quote", str(reason), halt=False)
        threading.Thread(target=self.login_server, daemon=True).start()

    def onError(self, error: dict) -> None:
        self.gateway.post_error("行情接口报错", error)

    def onSubMarketData(self, data: dict, error: dict, last: bool) -> None:
        if error and error.get("error_id"):
            self.gateway.post_error("行情订阅失败", error)

    def onDepthMarketData(self, data: dict) -> None:
        self.gateway.post(self.gateway._handle_tick, data)

    def onQueryAllTickers(self, data: dict, error: dict, last: bool) -> None:
        if last and self._contract_queries_pending:
            self._contract_queries_pending -= 1
        self.gateway.post(self.gateway._handle_contract, data, last)

    # -- driver methods ----------------------------------------------------- #
    def connect(
        self,
        userid: str,
        password: str,
        client_id: int,
        server_ip: str,
        server_port: int,
        quote_protocol: str,
        log_level: int,
    ) -> None:
        self.userid = userid
        self.password = password
        self.client_id = client_id
        self.server_ip = server_ip
        self.server_port = server_port
        self.protocol = PROTOCOL_VT2VENDOR[quote_protocol]

        if not self.connect_status:
            # New EMQ quote SDK: CreateQuoteApi(log_path, file_level, console_level)
            path = str(sdk_log_path(self.gateway.name))
            self.createQuoteApi(path, log_level, log_level)
            self.login_server()
        else:
            self.gateway.post_log("行情接口已登录，请勿重复操作")

    def login_server(self) -> None:
        # New EMQ quote SDK: 4-arg login (no protocol / local ip); 0 == success
        n: int = self.login(self.server_ip, self.server_port, self.userid, self.password)
        if not n:
            self.connect_status = True
            self.login_status = True
            msg = "行情服务器登录成功"
            level = "info"
            self.gateway.post_gateway_connected("quote", "login_success")
            self.query_contract()
            self.init()
        else:
            error: dict = self.getApiLastError()
            msg = f"行情服务器登录失败，原因：{error['error_msg']}"
            level = "error"
            self.gateway.post_gateway_disconnected("quote", str(error.get("error_msg") or error), halt=False)
        self.gateway.post_log(msg, level)

    def close(self) -> None:
        if self.connect_status:
            self.connect_status = False
            self.login_status = False
            if self._contract_queries_pending:
                self.release()
            else:
                self.exit()

    def subscribe_symbol(self, code: str, exchange: Exchange) -> None:
        if self.login_status:
            self.subscribeMarketData(code, 1, EXCHANGE_VT2VENDOR.get(exchange, 0))

    def query_contract(self) -> None:
        self._contract_queries_pending = len(EXCHANGE_VENDOR2VT)
        for exchange_id in EXCHANGE_VENDOR2VT:
            self.queryAllTickers(exchange_id)


class _EmtTdApi(_SdkTdApi):
    """Trader API wrapper. Callbacks arrive on the SDK's TD thread."""

    def __init__(self, gateway: "EmtGateway") -> None:
        super().__init__()
        self.gateway = gateway

        self.userid = ""
        self.password = ""
        self.client_id = 0
        self.server_ip = ""
        self.server_port = 0
        self.protocol = PROTOCOL_VT2VENDOR["TCP"]

        self.session_id = 0
        self.reqid = 0
        self.margin_trading = False

        self.connect_status = False
        self.login_status = False

    # -- SDK callbacks ------------------------------------------------------ #
    def onDisconnected(self, session: int = 0, reason: int | None = None) -> None:
        if reason is None:
            reason = session
            session = self.session_id
        self.connect_status = False
        self.login_status = False
        self.gateway.post_log(f"交易服务器连接断开, 原因{reason}", "warning")
        self.gateway.post_gateway_disconnected("trade", str(reason), halt=True)
        threading.Thread(target=self.login_server, daemon=True).start()

    def onError(self, error: dict) -> None:
        self.gateway.post_error("交易接口报错", error)

    def onOrderEvent(self, data: dict, error: dict, session: int) -> None:
        if error and error.get("error_id"):
            self.gateway.post_error("交易委托失败", error)
        self.gateway.post(self.gateway._handle_order_event, data)

    def onTradeEvent(self, data: dict, session: int) -> None:
        self.gateway.post(self.gateway._handle_trade_event, data)

    def onCancelOrderError(self, data: dict, error: dict, session: int) -> None:
        if error and error.get("error_id"):
            self.gateway.post_error("撤单失败", error)

    def onQueryOrder(self, data: dict, error: dict, request: int, last: bool, session: int) -> None:
        if error and error.get("error_id"):
            self.gateway.post_error("委托查询失败", error)
        if data:
            self.gateway.post(self.gateway._handle_order_event, data)

    def onQueryOrderByPage(
        self,
        data: dict,
        req_count: int,
        order_sequence: int,
        query_reference: int,
        request: int,
        last: bool,
        session: int,
    ) -> None:
        if data:
            self.gateway.post(self.gateway._handle_order_event, data)

    def onQueryTrade(self, data: dict, error: dict, request: int, last: bool, session: int) -> None:
        if error and error.get("error_id"):
            self.gateway.post_error("成交查询失败", error)
        if data:
            self.gateway.post(self.gateway._handle_trade_snapshot, data)

    def onQueryTradeByPage(
        self,
        data: dict,
        req_count: int,
        trade_sequence: int,
        query_reference: int,
        request: int,
        last: bool,
        session: int,
    ) -> None:
        if data:
            self.gateway.post(self.gateway._handle_trade_snapshot, data)

    def onQueryPosition(self, data: dict, error: dict, request: int, last: bool, session: int) -> None:
        self.gateway.post(self.gateway._handle_position, data)

    def onQueryAsset(self, data: dict, error: dict, request: int, last: bool, session: int) -> None:
        self.gateway.post(self.gateway._handle_asset, data)

    # -- driver methods ----------------------------------------------------- #
    def connect(
        self,
        userid: str,
        password: str,
        client_id: int,
        server_ip: str,
        server_port: int,
        log_level: int,
    ) -> None:
        self.userid = userid
        self.password = password
        self.client_id = client_id
        self.server_ip = server_ip
        self.server_port = server_port

        if not self.connect_status:
            path = str(sdk_log_path(self.gateway.name)).encode("GBK")
            self.createTraderApi(self.client_id, path, log_level)
            self.subscribePublicTopic(0)
            self.login_server()
        else:
            self.gateway.post_log("交易接口已登录，请勿重复操作")

    def login_server(self) -> None:
        n: int = self.login(
            self.server_ip, self.server_port, self.userid, self.password, self.protocol
        )
        if n:  # TD: non-zero == success, and n IS the session id
            self.session_id = n
            self.connect_status = True
            self.login_status = True
            msg = f"交易服务器登录成功, 会话编号：{self.session_id}"
            level = "info"
            self.gateway.post_gateway_connected("trade", str(self.session_id))
            self.init()
        else:
            error: dict = self.getApiLastError()
            msg = f"交易服务器登录失败，原因：{error['error_msg']}"
            level = "error"
            self.gateway.post_gateway_disconnected("trade", str(error.get("error_msg") or error), halt=True)
        self.gateway.post_log(msg, level)

    def close(self) -> None:
        if self.connect_status:
            self.connect_status = False
            self.login_status = False
            if _td_native_exit_enabled():
                self.exit()
                return
            if self.session_id:
                self.logout(self.session_id)
            self.release()

    def send_order(self, req: OrderRequest) -> str:
        if req.exchange not in MARKET_VT2VENDOR:
            self.gateway.post_log(f"委托失败，不支持的交易所{req.exchange.value}", "error")
            return ""
        if self.margin_trading and req.offset == Offset.NONE:
            self.gateway.post_log("委托失败，两融交易需要选择开平方向", "error")
            return ""
        if len(req.code) == 8:
            self.gateway.post_log("委托失败，期权交易不在支持范围", "error")
            return ""

        type_map = (
            STAR_ORDERTYPE_VT2VENDOR if req.code.startswith("688") else EQUITY_ORDERTYPE_VT2VENDOR
        )
        if req.type not in type_map:
            self.gateway.post_log(f"委托失败，不支持的委托类型{req.type.value}", "error")
            return ""

        emt_req: dict = {
            "ticker": req.code,
            "market": MARKET_VT2VENDOR[req.exchange],
            "price": req.price,
            "quantity": int(req.volume),
            "price_type": type_map[req.type],
        }
        if self.margin_trading:
            emt_req["side"] = DIRECTION_STOCK_VT2VENDOR.get((req.direction, req.offset), "")
            emt_req["business_type"] = 4
        else:
            emt_req["side"] = DIRECTION_STOCK_VT2VENDOR.get((req.direction, Offset.NONE), "")
            emt_req["business_type"] = 0

        emt_id: int = self.insertOrder(emt_req, self.session_id)
        if not emt_id:
            error: dict = self.getApiLastError()
            self.gateway.post_error("委托下单失败", error)
            return ""

        order_id = str(emt_id)
        order = req.create_order(order_id, self.gateway.name, status=OrderStatus.SUBMITTING)
        self.gateway.post(self.gateway._handle_local_order, order)
        return order_id

    def cancel_order(self, req: CancelRequest) -> None:
        order_id = req.order_id.split(".", 1)[-1]  # tolerate "GATEWAY.<id>" ids
        self.cancelOrder(int(order_id), self.session_id)

    def query_account(self) -> None:
        if not self.connect_status:
            return
        self.reqid += 1
        self.queryAsset(self.session_id, self.reqid)

    def query_position(self) -> None:
        if not self.connect_status:
            return
        self.reqid += 1
        self.queryPosition("", self.session_id, self.reqid, 0)

    def query_orders(self) -> bool:
        if not self.connect_status:
            return False
        fn = getattr(self, "queryOrders", None)
        if fn is None:
            self.gateway.post_log("委托查询不支持当前 EMT 绑定", "warning")
            return False
        self.reqid += 1
        n = fn({"ticker": "", "begin_time": 0, "end_time": 0}, self.session_id, self.reqid)
        if n:
            self.gateway.post_error("委托查询请求失败", self.getApiLastError())
            return False
        return True

    def query_trades(self) -> bool:
        if not self.connect_status:
            return False
        fn = getattr(self, "queryTrades", None)
        if fn is None:
            self.gateway.post_log("成交查询不支持当前 EMT 绑定", "warning")
            return False
        self.reqid += 1
        n = fn({"ticker": "", "begin_time": 0, "end_time": 0}, self.session_id, self.reqid)
        if n:
            self.gateway.post_error("成交查询请求失败", self.getApiLastError())
            return False
        return True


# ---- the gateway ------------------------------------------------------------ #
class EmtGateway(AShareVendorGateway):
    """AlphaPilot-native EMT gateway (stocks/funds/bonds, long-only focus)."""

    name = "emt"
    order_id_field = "order_emt_id"
    default_setting: dict[str, object] = {
        "账号": "",
        "密码": "",
        "客户号": 1,
        "行情地址": "",
        "行情端口": 0,
        "交易地址": "",
        "交易端口": 0,
        "行情账号": "",
        "行情密码": "",
        "行情协议": "TCP",
        "日志级别": "INFO",
    }
    exchanges = [Exchange.SSE, Exchange.SZSE]

    def __init__(self, name: str | None = None, **kwargs) -> None:
        super().__init__(name, **kwargs)
        self.md_api = _EmtMdApi(self)
        self.td_api = _EmtTdApi(self)

    # ---- BrokerGateway ----------------------------------------------------- #
    def connect(self, setting: dict) -> None:
        if not SDK_AVAILABLE:
            raise ImportError(
                "broker 'emt' needs the compiled 'vnpy_emt.api' bindings; install the "
                "vendored vnpy_emt package in the live environment (see Dockerfile.live)"
            )
        if not setting:
            from alphapilot.systems.live.brokers.registry import build_connect_setting

            setting = build_connect_setting(self.name)

        userid = setting["账号"]
        password = setting["密码"]
        quote_userid = str(setting.get("行情账号") or userid)
        quote_password = str(setting.get("行情密码") or password)
        client_id = int(setting["客户号"])
        log_level = LOGLEVEL_VT2VENDOR.get(str(setting.get("日志级别", "INFO")), 3)

        self.start()  # dispatcher + account/position polling
        self.md_api.connect(
            quote_userid, quote_password, client_id,
            setting["行情地址"], int(setting["行情端口"]),
            str(setting.get("行情协议", "TCP")), log_level,
        )
        self.td_api.connect(
            userid, password, client_id,
            setting["交易地址"], int(setting["交易端口"]), log_level,
        )
        self.post_log("emt gateway connect requested")

    def close(self) -> None:
        self.shutdown()
        self.md_api.close()
        self.td_api.close()

    def send_order(self, req: OrderRequest) -> str:
        return self.td_api.send_order(req)

    def cancel_order(self, req: CancelRequest) -> None:
        self.td_api.cancel_order(req)

    def query_account(self) -> None:
        self.td_api.query_account()

    def query_position(self) -> None:
        self.td_api.query_position()

    def query_orders(self) -> bool:
        return self.td_api.query_orders()

    def query_trades(self) -> bool:
        return self.td_api.query_trades()

    def subscribe(self, codes: list[str]) -> None:
        for raw in codes:
            code, exchange = normalize_symbol(raw)
            self.md_api.subscribe_symbol(code, exchange)

    # ---- vendor conversion hooks -------------------------------------------- #
    def _convert_position(self, data: dict) -> Position | None:
        return position_from_emt(data, self.name)

    def _convert_account(self, data: dict) -> Account | None:
        if data.get("account_type") == 1:
            self.td_api.margin_trading = True
        return account_from_emt(data, self.td_api.userid, self.name)

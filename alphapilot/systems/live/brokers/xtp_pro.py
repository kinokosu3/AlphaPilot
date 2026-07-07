"""XtpProGateway — native XTP Pro (XTPX) gateway, no vn.py required.

Ported from the vendored ``vnpy_xtp.gateway.xtp_gateway`` (itself migrated to
the XTP Pro / XTPX 1.2.1 SDK). Everything an XTP-family SDK shares — int
mapping tables, dict→dataclass converters, the dispatch-thread order/contract
state machine — lives in :mod:`.vendor_common`; this module keeps only what is
genuinely XTP: login/create signatures, the software key, and the
account/position field semantics.

Only ``vnpy_xtp.api`` (compiled pybind bindings) is imported — no vn.py. On
machines without the compiled SDK this module still imports (converters stay
unit-testable); only ``connect()`` raises.

Two SDK quirks inherited from upstream — do not "fix" them:
* MD ``login`` returns 0 on success; TD ``login`` returns the session id
  (non-zero) on success.
* ``MARKET_*`` (orders/trades/positions) and ``EXCHANGE_*`` (ticks/contracts)
  use *opposite* int→exchange assignments.
"""

from __future__ import annotations

import threading

from alphapilot.systems.live.brokers.base import sdk_log_path
from alphapilot.systems.live.brokers.vendor_common import (
    AShareVendorGateway,
    contract_from_vendor,
    order_fields_from_vendor,
    order_from_vendor,
    tick_from_vendor,
    trade_from_vendor,
)
from alphapilot.systems.live.brokers.vendor_common import (
    DIRECTION_STOCK_VENDOR2VT as DIRECTION_STOCK_XTP2VT,
)
from alphapilot.systems.live.brokers.vendor_common import (
    DIRECTION_STOCK_VT2VENDOR as DIRECTION_STOCK_VT2XTP,
)
from alphapilot.systems.live.brokers.vendor_common import (
    EQUITY_ORDERTYPE_VT2VENDOR as EQUITY_ORDERTYPE_VT2XTP,
)
from alphapilot.systems.live.brokers.vendor_common import (
    EQUITY_ORDERTYPE_VENDOR2VT as EQUITY_ORDERTYPE_XTP2VT,
)
from alphapilot.systems.live.brokers.vendor_common import (
    EXCHANGE_VENDOR2VT as EXCHANGE_XTP2VT,
)
from alphapilot.systems.live.brokers.vendor_common import (
    EXCHANGE_VT2VENDOR as EXCHANGE_VT2XTP,
)
from alphapilot.systems.live.brokers.vendor_common import (
    LOGLEVEL_VT2VENDOR as LOGLEVEL_VT2XTP,
)
from alphapilot.systems.live.brokers.vendor_common import (
    MARKET_VENDOR2VT as MARKET_XTP2VT,
)
from alphapilot.systems.live.brokers.vendor_common import (
    MARKET_VT2VENDOR as MARKET_VT2XTP,
)
from alphapilot.systems.live.brokers.vendor_common import (
    PROTOCOL_VT2VENDOR as PROTOCOL_VT2XTP,
)
from alphapilot.systems.live.brokers.vendor_common import (
    STAR_ORDERTYPE_VT2VENDOR as STAR_ORDERTYPE_VT2XTP,
)
from alphapilot.systems.live.brokers.vendor_common import (
    STAR_ORDERTYPE_VENDOR2VT as STAR_ORDERTYPE_XTP2VT,
)
from alphapilot.systems.live.brokers.vendor_common import (
    STATUS_VENDOR2VT as STATUS_XTP2VT,
)
from alphapilot.systems.live.brokers.vendor_common import (
    POSITION_DIRECTION_VENDOR2VT as POSITION_DIRECTION_XTP2VT,
)
from alphapilot.systems.live.types import (
    Account,
    CancelRequest,
    Contract,
    Direction,
    Exchange,
    Offset,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
    Trade,
    normalize_symbol,
)

try:  # compiled pybind bindings — present in the live env, absent on dev boxes
    from vnpy_xtp.api import MdApi as _SdkMdApi
    from vnpy_xtp.api import TdApi as _SdkTdApi

    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the SDK
    _SdkMdApi = object
    _SdkTdApi = object
    SDK_AVAILABLE = False


# ---- vendor-specific converters (XTP field semantics) ------------------------ #
def order_fields_from_xtp(data: dict):
    return order_fields_from_vendor(data)


def contract_from_xtp(data: dict, gateway: str) -> Contract:
    return contract_from_vendor(data, gateway)


def tick_from_xtp(data: dict, contract: Contract | None, gateway: str):
    return tick_from_vendor(data, contract, gateway)


def order_from_xtp(data: dict, gateway: str) -> Order | None:
    return order_from_vendor(data, "order_xtp_id", gateway)


def trade_from_xtp(data: dict, gateway: str) -> Trade | None:
    return trade_from_vendor(data, "order_xtp_id", gateway)


def position_from_xtp(data: dict, gateway: str) -> Position | None:
    if data["market"] == 0:
        return None
    return Position(
        code=data["ticker"],
        exchange=MARKET_XTP2VT[data["market"]],
        direction=POSITION_DIRECTION_XTP2VT.get(data["position_direction"], Direction.LONG),
        volume=data["total_qty"],
        frozen=data["total_qty"] - data["sellable_qty"],
        price=data["avg_price"],
        pnl=data["unrealized_pnl"],
        yd_volume=data["yesterday_position"],
        gateway=gateway,
    )


def account_from_xtp(data: dict, account_id: str, gateway: str) -> Account:
    account = Account(
        account_id=account_id,
        balance=round(data["total_asset"], 2),
        frozen=round(data["withholding_amount"], 2),
        available=round(data["buying_power"], 2),
        gateway=gateway,
    )
    if data.get("account_type") == 2:  # option account: frozen derived differently
        account.frozen = round(account.balance - account.available - data["security_asset"], 2)
    return account


# ---- SDK subclasses (callbacks hop onto the dispatcher immediately) --------- #
class _XtpMdApi(_SdkMdApi):
    """Quote API wrapper. All ``on*`` callbacks arrive on the SDK's MD thread."""

    def __init__(self, gateway: "XtpProGateway") -> None:
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

    # -- SDK callbacks ------------------------------------------------------ #
    def onDisconnected(self, reason: int) -> None:
        self.connect_status = False
        self.login_status = False
        self.gateway.post_log(f"行情服务器连接断开, 原因{reason}", "warning")
        self.gateway.post_gateway_disconnected("quote", str(reason), halt=False)
        # Re-login on a fresh thread: login blocks for seconds and must not
        # stall either the SDK callback thread or the dispatch thread.
        threading.Thread(target=self.login_server, daemon=True).start()

    def onError(self, error: dict) -> None:
        self.gateway.post_error("行情接口报错", error)

    def onSubMarketData(self, data: dict, error: dict, last: bool) -> None:
        if error and error.get("error_id"):
            self.gateway.post_error("行情订阅失败", error)

    def onDepthMarketData(self, data: dict) -> None:
        self.gateway.post(self.gateway._handle_tick, data)

    def onQueryAllTickers(self, data: dict, error: dict, last: bool) -> None:
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
        self.protocol = PROTOCOL_VT2XTP[quote_protocol]

        if not self.connect_status:
            path = sdk_log_path(self.gateway.name)
            self.createQuoteApi(self.client_id, str(path).encode("GBK"), log_level)
            self.login_server()
        else:
            self.gateway.post_log("行情接口已登录，请勿重复操作")

    def login_server(self) -> None:
        n: int = self.login(
            self.server_ip, self.server_port, self.userid, self.password, self.protocol, ""
        )
        if not n:  # MD: 0 == success
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
            self.exit()

    def subscribe_symbol(self, code: str, exchange: Exchange) -> None:
        if self.login_status:
            self.subscribeMarketData(code, 1, EXCHANGE_VT2XTP.get(exchange, 0))

    def query_contract(self) -> None:
        for exchange_id in EXCHANGE_XTP2VT:
            self.queryAllTickers(exchange_id)


class _XtpTdApi(_SdkTdApi):
    """Trader API wrapper. All ``on*`` callbacks arrive on the SDK's TD thread."""

    def __init__(self, gateway: "XtpProGateway") -> None:
        super().__init__()
        self.gateway = gateway

        self.userid = ""
        self.password = ""
        self.client_id = 0
        self.server_ip = ""
        self.server_port = 0
        self.software_key = ""
        self.protocol = PROTOCOL_VT2XTP["TCP"]

        self.session_id = 0
        self.reqid = 0
        self.margin_trading = False

        self.connect_status = False
        self.login_status = False

    # -- SDK callbacks ------------------------------------------------------ #
    def onDisconnected(self, session: int, reason: int) -> None:
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

    def onQueryOrderEx(self, data: dict, error: dict, request: int, last: bool, session: int) -> None:
        if error and error.get("error_id"):
            self.gateway.post_error("委托查询失败", error)
        if data:
            self.gateway.post(self.gateway._handle_order_event, data)

    def onQueryOrderByPageEx(
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
        software_key: str,
        log_level: int,
    ) -> None:
        self.userid = userid
        self.password = password
        self.client_id = client_id
        self.server_ip = server_ip
        self.server_port = server_port
        self.software_key = software_key

        if not self.connect_status:
            path = sdk_log_path(self.gateway.name)
            self.createTraderApi(self.client_id, str(path).encode("GBK"), log_level)
            self.setSoftwareKey(self.software_key)
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
            self.exit()

    def send_order(self, req: OrderRequest) -> str:
        if req.exchange not in MARKET_VT2XTP:
            self.gateway.post_log(f"委托失败，不支持的交易所{req.exchange.value}", "error")
            return ""
        if self.margin_trading and req.offset == Offset.NONE:
            self.gateway.post_log("委托失败，两融交易需要选择开平方向", "error")
            return ""
        if len(req.code) == 8:
            self.gateway.post_log("委托失败，期权交易不在支持范围", "error")
            return ""

        type_map = STAR_ORDERTYPE_VT2XTP if req.code.startswith("688") else EQUITY_ORDERTYPE_VT2XTP
        if req.type not in type_map:
            self.gateway.post_log(f"委托失败，不支持的委托类型{req.type.value}", "error")
            return ""

        xtp_req: dict = {
            "ticker": req.code,
            "market": MARKET_VT2XTP[req.exchange],
            "price": req.price,
            "quantity": int(req.volume),
            "price_type": type_map[req.type],
        }
        if self.margin_trading:
            xtp_req["side"] = DIRECTION_STOCK_VT2XTP.get((req.direction, req.offset), "")
            xtp_req["business_type"] = 4
        else:
            xtp_req["side"] = DIRECTION_STOCK_VT2XTP.get((req.direction, Offset.NONE), "")
            xtp_req["business_type"] = 0

        xtp_id: int = self.insertOrder(xtp_req, self.session_id)
        if not xtp_id:
            error: dict = self.getApiLastError()
            self.gateway.post_error("委托下单失败", error)
            return ""

        order_id = str(xtp_id)
        order = req.create_order(order_id, self.gateway.name, status=OrderStatus.SUBMITTING)
        # Register + emit on the dispatch thread so the order cache stays single-writer.
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
        self.queryPosition("", self.session_id, self.reqid)

    def query_orders(self) -> bool:
        if not self.connect_status:
            return False

        req = {"ticker": "", "begin_time": 0, "end_time": 0}
        for name in ("queryOrders", "queryOrdersEx"):
            fn = getattr(self, name, None)
            if fn is None:
                continue
            self.reqid += 1
            n = fn(req, self.session_id, self.reqid)
            if n:
                self.gateway.post_error("委托查询请求失败", self.getApiLastError())
                return False
            return True

        fn = getattr(self, "queryUnfinishedOrders", None)
        if fn is not None:
            self.reqid += 1
            n = fn(self.session_id, self.reqid)
            if n:
                self.gateway.post_error("未完成委托查询请求失败", self.getApiLastError())
                return False
            return True

        self.gateway.post_log("委托查询不支持当前 XTP 绑定", "warning")
        return False

    def query_trades(self) -> bool:
        if not self.connect_status:
            return False
        fn = getattr(self, "queryTrades", None)
        if fn is None:
            self.gateway.post_log("成交查询不支持当前 XTP 绑定", "warning")
            return False
        self.reqid += 1
        n = fn({"ticker": "", "begin_time": 0, "end_time": 0}, self.session_id, self.reqid)
        if n:
            self.gateway.post_error("成交查询请求失败", self.getApiLastError())
            return False
        return True


# ---- the gateway ------------------------------------------------------------ #
class XtpProGateway(AShareVendorGateway):
    """AlphaPilot-native XTP Pro gateway (stocks/funds/bonds, long-only focus)."""

    name = "xtp"
    order_id_field = "order_xtp_id"
    default_setting: dict[str, object] = {
        "账号": "",
        "密码": "",
        "客户号": 1,
        "行情地址": "",
        "行情端口": 0,
        "交易地址": "",
        "交易端口": 0,
        "行情协议": "TCP",
        "日志级别": "INFO",
        "授权码": "",
    }
    exchanges = [Exchange.SSE, Exchange.SZSE]

    def __init__(self, name: str | None = None, **kwargs) -> None:
        super().__init__(name, **kwargs)
        self.md_api = _XtpMdApi(self)
        self.td_api = _XtpTdApi(self)

    # ---- BrokerGateway ----------------------------------------------------- #
    def connect(self, setting: dict) -> None:
        if not SDK_AVAILABLE:
            raise ImportError(
                "broker 'xtp' needs the compiled 'vnpy_xtp.api' bindings; install the "
                "vendored vnpy_xtp package in the live environment (see docs/live-xtp.md)"
            )
        if not setting:
            from alphapilot.systems.live.brokers.registry import build_connect_setting

            setting = build_connect_setting(self.name)

        userid = setting["账号"]
        password = setting["密码"]
        client_id = int(setting["客户号"])
        log_level = LOGLEVEL_VT2XTP.get(str(setting.get("日志级别", "INFO")), 3)

        self.start()  # dispatcher + account/position polling
        self.md_api.connect(
            userid, password, client_id,
            setting["行情地址"], int(setting["行情端口"]),
            str(setting.get("行情协议", "TCP")), log_level,
        )
        self.td_api.connect(
            userid, password, client_id,
            setting["交易地址"], int(setting["交易端口"]),
            str(setting.get("授权码", "")), log_level,
        )
        self.post_log("xtp pro gateway connect requested")

    def close(self) -> None:
        self.md_api.close()
        self.td_api.close()
        self.shutdown()

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
        return position_from_xtp(data, self.name)

    def _convert_account(self, data: dict) -> Account | None:
        if data.get("account_type") == 1:
            self.td_api.margin_trading = True
        return account_from_xtp(data, self.td_api.userid, self.name)

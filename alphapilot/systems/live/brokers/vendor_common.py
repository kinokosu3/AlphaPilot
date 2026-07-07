"""Shared tables + converters for XTP-family A-share broker SDKs.

XTP Pro (中泰) and EMT (东方财富) expose near-identical dict-based pybind APIs —
EMT's counter is XTP-derived — so the int mapping tables and most converters are
literally the same. This module owns that common ground; each vendor gateway
keeps only what genuinely differs (login signatures, vendor id field, the
account/position field semantics).

``AShareVendorGateway`` also hosts the shared dispatch-thread handlers (order
cache, fill accumulation, contract cache, tick rounding) so a new XTP-family
broker only implements: api construction/login, ``send_order`` field mapping,
and the two ``_convert_*`` hooks.
"""

from __future__ import annotations

from copy import copy
from datetime import datetime
from zoneinfo import ZoneInfo

from alphapilot.systems.live.brokers.base import SdkBrokerGateway, round_to
from alphapilot.systems.live.types import (
    Account,
    Contract,
    Direction,
    Exchange,
    Offset,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Product,
    TickData,
    Trade,
    symbol_key,
)

# ---- int tables shared by the XTP-family SDKs ------------------------------- #
# NOTE the two exchange tables are intentionally OPPOSITE — an SDK quirk:
# MARKET_* is used by orders/trades/positions, EXCHANGE_* by ticks/contracts.
MARKET_VENDOR2VT: dict[int, Exchange] = {
    1: Exchange.SZSE,
    2: Exchange.SSE,
}
MARKET_VT2VENDOR: dict[Exchange, int] = {v: k for k, v in MARKET_VENDOR2VT.items()}

EXCHANGE_VENDOR2VT: dict[int, Exchange] = {
    1: Exchange.SSE,
    2: Exchange.SZSE,
}
EXCHANGE_VT2VENDOR: dict[Exchange, int] = {v: k for k, v in EXCHANGE_VENDOR2VT.items()}

DIRECTION_STOCK_VENDOR2VT: dict[int, tuple[Direction, Offset]] = {
    1: (Direction.LONG, Offset.NONE),
    2: (Direction.SHORT, Offset.NONE),
    21: (Direction.LONG, Offset.OPEN),
    22: (Direction.SHORT, Offset.OPEN),
    24: (Direction.LONG, Offset.CLOSE),
    23: (Direction.SHORT, Offset.CLOSE),
}
DIRECTION_STOCK_VT2VENDOR: dict[tuple[Direction, Offset], int] = {
    v: k for k, v in DIRECTION_STOCK_VENDOR2VT.items()
}

POSITION_DIRECTION_VENDOR2VT: dict[int, Direction] = {
    0: Direction.NET,
    1: Direction.LONG,
    2: Direction.SHORT,
    3: Direction.SHORT,
}

EQUITY_ORDERTYPE_VENDOR2VT: dict[int, OrderType] = {
    1: OrderType.LIMIT,
    4: OrderType.MARKET,
}
EQUITY_ORDERTYPE_VT2VENDOR: dict[OrderType, int] = {
    v: k for k, v in EQUITY_ORDERTYPE_VENDOR2VT.items()
}

STAR_ORDERTYPE_VENDOR2VT: dict[int, OrderType] = {
    1: OrderType.LIMIT,
    7: OrderType.MARKET,
}
STAR_ORDERTYPE_VT2VENDOR: dict[OrderType, int] = {
    v: k for k, v in STAR_ORDERTYPE_VENDOR2VT.items()
}

PROTOCOL_VT2VENDOR: dict[str, int] = {
    "TCP": 1,
    "UDP": 2,
}

STATUS_VENDOR2VT: dict[int, OrderStatus] = {
    0: OrderStatus.SUBMITTING,
    1: OrderStatus.ALLTRADED,
    2: OrderStatus.PARTTRADED,
    3: OrderStatus.CANCELLED,
    4: OrderStatus.NOTTRADED,
    5: OrderStatus.CANCELLED,
    6: OrderStatus.REJECTED,
    7: OrderStatus.SUBMITTING,
}

PRODUCT_VENDOR2VT: dict[int, Product] = {
    0: Product.EQUITY,
    1: Product.INDEX,
    2: Product.FUND,
    3: Product.BOND,
    4: Product.OPTION,
    5: Product.EQUITY,
    6: Product.FUND,
}

LOGLEVEL_VT2VENDOR: dict[str, int] = {
    "FATAL": 0,
    "ERROR": 1,
    "WARNING": 2,
    "INFO": 3,
    "DEBUG": 4,
    "TRACE": 5,
}

CHINA_TZ = ZoneInfo("Asia/Shanghai")


def parse_vendor_dt(timestamp: object) -> datetime:
    """Parse the SDKs' ``YYYYMMDDHHMMSSfff`` int/str timestamps (Asia/Shanghai)."""
    return datetime.strptime(str(timestamp), "%Y%m%d%H%M%S%f").replace(tzinfo=CHINA_TZ)


# ---- shared pure converters -------------------------------------------------- #
def order_fields_from_vendor(data: dict) -> tuple[Direction, Offset, OrderType] | None:
    """Resolve direction/offset/type for a stock order; ``None`` for options."""
    symbol: str = data["ticker"]
    if len(symbol) == 8:  # option contract — out of scope
        return None
    pair = DIRECTION_STOCK_VENDOR2VT.get(data["side"])
    if pair is None:
        return None
    direction, offset = pair
    # Trade events carry no price_type; default to MARKET like the vn.py original.
    type_map = STAR_ORDERTYPE_VENDOR2VT if symbol.startswith("688") else EQUITY_ORDERTYPE_VENDOR2VT
    order_type = type_map.get(data.get("price_type"), OrderType.MARKET)
    return direction, offset, order_type


def contract_from_vendor(data: dict, gateway: str) -> Contract:
    return Contract(
        code=data["ticker"],
        exchange=EXCHANGE_VENDOR2VT[data["exchange_id"]],
        name=data["ticker_name"],
        product=PRODUCT_VENDOR2VT.get(data["ticker_type"], Product.EQUITY),
        size=1.0,
        price_tick=data["price_tick"],
        lot_size=int(data.get("buy_qty_unit") or 100),
        gateway=gateway,
    )


def tick_from_vendor(data: dict, contract: Contract | None, gateway: str) -> TickData:
    tick = TickData(
        code=data["ticker"],
        exchange=EXCHANGE_VENDOR2VT[data["exchange_id"]],
        datetime=parse_vendor_dt(data["data_time"]),
        volume=data["qty"],
        turnover=data["turnover"],
        last_price=data["last_price"],
        limit_up=data["upper_limit_price"],
        limit_down=data["lower_limit_price"],
        open_price=data["open_price"],
        high_price=data["high_price"],
        low_price=data["low_price"],
        pre_close=data["pre_close_price"],
        bid_price_1=data["bid"][0],
        ask_price_1=data["ask"][0],
        bid_volume_1=data["bid_qty"][0],
        ask_volume_1=data["ask_qty"][0],
        gateway=gateway,
    )
    if contract is not None:
        pt = contract.price_tick
        tick.name = contract.name
        for field_name in (
            "last_price", "limit_up", "limit_down", "open_price",
            "high_price", "low_price", "pre_close", "bid_price_1", "ask_price_1",
        ):
            setattr(tick, field_name, round_to(getattr(tick, field_name), pt))
    return tick


def order_from_vendor(data: dict, id_field: str, gateway: str) -> Order | None:
    fields = order_fields_from_vendor(data)
    if fields is None:
        return None
    direction, offset, order_type = fields
    order = Order(
        order_id=str(data[id_field]),
        code=data["ticker"],
        exchange=MARKET_VENDOR2VT[data["market"]],
        direction=direction,
        offset=offset,
        type=order_type,
        price=data["price"],
        volume=data["quantity"],
        traded=data["qty_traded"],
        status=STATUS_VENDOR2VT[data["order_status"]],
        gateway=gateway,
    )
    if data.get("insert_time"):
        order.datetime = parse_vendor_dt(data["insert_time"])
    return order


def trade_from_vendor(data: dict, id_field: str, gateway: str) -> Trade | None:
    fields = order_fields_from_vendor(data)
    if fields is None:
        return None
    direction, offset, _ = fields
    return Trade(
        trade_id=str(data["exec_id"]),
        order_id=str(data[id_field]),
        code=data["ticker"],
        exchange=MARKET_VENDOR2VT[data["market"]],
        direction=direction,
        offset=offset,
        price=data["price"],
        volume=data["quantity"],
        datetime=parse_vendor_dt(data["trade_time"]),
        gateway=gateway,
    )


# ---- shared gateway skeleton -------------------------------------------------- #
class AShareVendorGateway(SdkBrokerGateway):
    """Common dispatch-thread state machine for XTP-family gateways.

    Subclasses set :attr:`order_id_field` and implement the abstract SDK-facing
    methods of :class:`BrokerGateway` plus the two ``_convert_*`` hooks; the
    handlers below own the caches and the fill-accumulation logic, and run
    exclusively on the dispatch thread.
    """

    #: vendor's order-id key in order/trade event dicts (e.g. ``order_xtp_id``).
    order_id_field: str = "order_xtp_id"

    def __init__(self, name: str | None = None, **kwargs) -> None:
        super().__init__(name, **kwargs)
        # Caches below are touched ONLY on the dispatch thread.
        self.contracts: dict[str, Contract] = {}
        self.orders: dict[str, Order] = {}

    # ---- vendor-specific conversion hooks ---------------------------------- #
    def _convert_position(self, data: dict) -> Position | None:
        raise NotImplementedError

    def _convert_account(self, data: dict) -> Account | None:
        raise NotImplementedError

    # ---- dispatch-thread handlers ------------------------------------------ #
    def _handle_tick(self, data: dict) -> None:
        key = symbol_key(data["ticker"], EXCHANGE_VENDOR2VT[data["exchange_id"]])
        tick = tick_from_vendor(data, self.contracts.get(key), self.name)
        self._emit_tick(tick)

    def _handle_contract(self, data: dict, last: bool) -> None:
        contract = contract_from_vendor(data, self.name)
        self.contracts[contract.key] = contract
        if contract.product != Product.OPTION:
            self._emit_contract(contract)
        if last:
            self._emit_log(f"{contract.exchange.value}合约信息查询成功")

    def _handle_local_order(self, order: Order) -> None:
        self.orders[order.order_id] = order
        self._emit_order(copy(order))

    def _handle_order_event(self, data: dict) -> None:
        if not data:
            return
        incoming = order_from_vendor(data, self.order_id_field, self.name)
        if incoming is None:
            return
        cached = self.orders.get(incoming.order_id)
        if cached is None:
            self.orders[incoming.order_id] = incoming
            cached = incoming
        else:
            cached.traded = incoming.traded
            cached.status = incoming.status
            if cached.datetime is None:
                cached.datetime = incoming.datetime
        self._emit_order(copy(cached))

    def _handle_trade_event(self, data: dict) -> None:
        if not data:
            return
        trade = trade_from_vendor(data, self.order_id_field, self.name)
        if trade is None:
            return
        order = self.orders.get(trade.order_id)
        if order is not None:
            order.traded += trade.volume
            order.status = (
                OrderStatus.PARTTRADED if order.traded < order.volume else OrderStatus.ALLTRADED
            )
            self._emit_order(copy(order))
        else:
            self._emit_log(f"成交找不到对应委托{trade.order_id}", level="warning")
        self._emit_trade(trade)

    def _handle_trade_snapshot(self, data: dict) -> None:
        """Replay queried trades without mutating cached order fill totals."""
        if not data:
            return
        trade = trade_from_vendor(data, self.order_id_field, self.name)
        if trade is not None:
            self._emit_trade(trade)

    def _handle_position(self, data: dict) -> None:
        position = self._convert_position(data)
        if position is not None:
            self._emit_position(position)

    def _handle_asset(self, data: dict) -> None:
        account = self._convert_account(data)
        if account is not None:
            self._emit_account(account)

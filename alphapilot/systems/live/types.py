"""Normalized, broker-agnostic domain objects for the live-trading subsystem.

This module is the *single source of truth* for the objects that flow between the
broker gateway, the OMS, the risk gate and the executor. It is deliberately
dependency-light (stdlib + dataclasses only) so it can be imported anywhere —
including from ``systems/timing`` and the kernel — **without pulling vn.py, qlib
or any broker SDK**. Concrete gateways (paper / sim / vn.py) translate their
native structures into these types at the boundary, mirroring vn.py's approach of
normalizing every gateway into ``OrderData/TradeData/PositionData/...`` keyed by a
uniform ``vt_symbol`` / ``vt_orderid``.

The A-share domain is the first production route (SSE / SZSE / BSE, long-only,
T+1, board lots), while the normalized objects already carry enough futures
metadata for later gateway implementations to plug in without changing the OMS
contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    """Order / position direction."""

    LONG = "long"
    SHORT = "short"
    NET = "net"


class Offset(str, Enum):
    """Open/close offset. A-shares use ``NONE``; futures use the rest."""

    NONE = "none"
    OPEN = "open"
    CLOSE = "close"
    CLOSETODAY = "close_today"
    CLOSEYESTERDAY = "close_yesterday"


class OrderType(str, Enum):
    """Supported order price types."""

    LIMIT = "limit"
    MARKET = "market"
    FAK = "fak"
    FOK = "fok"


class Exchange(str, Enum):
    """Normalized exchange codes."""

    SSE = "SSE"       # 上交所
    SZSE = "SZSE"     # 深交所
    BSE = "BSE"       # 北交所
    CFFEX = "CFFEX"   # 中金所
    SHFE = "SHFE"     # 上期所
    DCE = "DCE"       # 大商所
    CZCE = "CZCE"     # 郑商所
    INE = "INE"       # 上海国际能源交易中心
    GFEX = "GFEX"     # 广期所
    UNKNOWN = "UNKNOWN"


class Product(str, Enum):
    EQUITY = "equity"
    FUND = "fund"      # ETF / LOF
    BOND = "bond"
    INDEX = "index"
    OPTION = "option"
    FUTURES = "futures"


class OrderStatus(str, Enum):
    """Order lifecycle states — aligned with vn.py's ``Status`` (6 states).

    ``SUBMITTED`` / ``FILLED`` are kept as **back-compat aliases** of
    ``SUBMITTING`` / ``ALLTRADED`` so the pre-existing ``systems/timing`` code
    that referenced the old 4-state enum keeps working unchanged.
    """

    SUBMITTING = "submitting"   # local, not yet acknowledged by the broker
    NOTTRADED = "nottraded"     # accepted by the exchange, no fill yet
    PARTTRADED = "parttraded"   # partially filled
    ALLTRADED = "alltraded"     # fully filled
    CANCELLED = "cancelled"     # cancelled (any remaining volume withdrawn)
    REJECTED = "rejected"       # rejected by risk gate / broker / exchange

    # --- back-compat aliases (old timing names) ---
    SUBMITTED = "submitting"
    FILLED = "alltraded"


# Orders in these states are still "working" and may still fill or be cancelled.
ACTIVE_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.SUBMITTING, OrderStatus.NOTTRADED, OrderStatus.PARTTRADED}
)


def is_active(status: OrderStatus) -> bool:
    """True if an order in ``status`` is still working (mirrors vn.py's is_active)."""
    return status in ACTIVE_STATUSES


# --------------------------------------------------------------------------- #
# Symbol helpers
# --------------------------------------------------------------------------- #
def infer_exchange(code: str) -> Exchange:
    """Infer the A-share exchange from a 6-digit board code.

    SSE: 60xxxx / 68xxxx (科创板) / 5xxxxx (基金) / 11xxxx (债).
    SZSE: 00xxxx / 30xxxx (创业板) / 15xxxx / 16xxxx / 12xxxx.
    BSE: 8xxxxx / 4xxxxx / 920xxx.
    """
    c = code.strip()
    if not c[:1].isdigit():
        return Exchange.UNKNOWN
    if c[0] == "6" or c[0] == "5" or c.startswith("11"):
        return Exchange.SSE
    if c[0] in ("0", "3", "1", "2"):
        return Exchange.SZSE
    if c[0] in ("8", "4") or c.startswith("920"):
        return Exchange.BSE
    return Exchange.UNKNOWN


def normalize_symbol(symbol: str) -> tuple[str, Exchange]:
    """Parse a symbol in any common form into ``(code, exchange)``.

    Accepts ``600000``, ``SH600000`` / ``sh600000``, ``sh.600000``,
    ``600000.SH``, ``SSE.600000`` etc. Futures-style symbols must carry an
    explicit exchange (``rb2410.SHFE`` / ``SHFE.rb2410`` / ``IF2409.CFFEX``).
    Alphanumeric symbols without an exchange are preserved and marked UNKNOWN.
    """
    raw = symbol.strip().upper().replace(" ", "")
    prefix_map = {"SH": Exchange.SSE, "SZ": Exchange.SZSE, "BJ": Exchange.BSE}

    for sep in (".", "-", "_"):
        if sep in raw:
            a, b = raw.split(sep, 1)
            a_ex = _exchange_from_tag(a)
            b_ex = _exchange_from_tag(b)
            if a_ex is not None and b_ex is None:      # SH.600000 / SHFE.RB2410
                code, ex = b, a_ex
            elif b_ex is not None and a_ex is None:    # 600000.SH / RB2410.SHFE
                code, ex = a, b_ex
            elif a.isdigit():                          # legacy fallback: 600000.foo
                code, ex = a, _exchange_from_tag(b) or infer_exchange(a)
            else:                                      # legacy fallback: foo.600000
                code = b
                ex = _exchange_from_tag(a) or infer_exchange(_stock_code(code))
            return _normalize_code(code, ex), ex

    for pfx, ex in prefix_map.items():
        if raw.startswith(pfx) and raw[len(pfx):].isdigit():
            return raw[len(pfx):], ex

    if raw.isdigit():
        return raw, infer_exchange(raw)
    return raw, Exchange.UNKNOWN


def _exchange_from_tag(tag: str) -> Optional[Exchange]:
    aliases = {"SH": Exchange.SSE, "SZ": Exchange.SZSE, "BJ": Exchange.BSE}
    if tag in aliases:
        return aliases[tag]
    try:
        return Exchange(tag)
    except ValueError:
        return None


def _stock_code(code: str) -> str:
    return "".join(ch for ch in code if ch.isdigit())


def _normalize_code(code: str, exchange: Exchange) -> str:
    cleaned = code.strip().upper()
    if exchange in {Exchange.SSE, Exchange.SZSE, Exchange.BSE}:
        digits = _stock_code(cleaned)
        return digits or cleaned
    return "".join(ch for ch in cleaned if ch.isalnum())


def symbol_key(code: str, exchange: Exchange) -> str:
    """Uniform instrument key (vn.py's ``vt_symbol`` analogue): ``600000.SSE``."""
    return f"{code}.{exchange.value}"


# --------------------------------------------------------------------------- #
# Contracts / requests / stateful objects
# --------------------------------------------------------------------------- #
@dataclass
class Contract:
    """Static instrument metadata (board lot, price tick) — needed for rounding."""

    code: str
    exchange: Exchange
    name: str = ""
    product: Product = Product.EQUITY
    size: float = 1.0
    price_tick: float = 0.01
    lot_size: int = 100          # A-share board lot
    margin_rate: float = 0.0
    gateway: str = ""

    @property
    def key(self) -> str:
        return symbol_key(self.code, self.exchange)


@dataclass
class OrderRequest:
    """An intent to trade, before it is accepted by any broker.

    ``reference`` carries the caller's idempotency key (client order id) so the
    risk gate can dedup and the ledger can trace an intent end-to-end.
    """

    code: str
    exchange: Exchange
    direction: Direction
    volume: float
    price: float = 0.0
    type: OrderType = OrderType.LIMIT
    offset: Offset = Offset.NONE
    reference: str = ""

    @property
    def key(self) -> str:
        return symbol_key(self.code, self.exchange)

    @property
    def is_buy(self) -> bool:
        return self.direction == Direction.LONG

    @property
    def is_sell(self) -> bool:
        return self.direction == Direction.SHORT

    @classmethod
    def buy(cls, code: str, exchange: Exchange, volume: float, price: float = 0.0,
            type: OrderType = OrderType.LIMIT, reference: str = "") -> "OrderRequest":
        """A-share buy = (Direction.LONG, Offset.NONE), matching the EMT/XTP side map."""
        return cls(code=code, exchange=exchange, direction=Direction.LONG,
                   volume=volume, price=price, type=type, reference=reference)

    @classmethod
    def sell(cls, code: str, exchange: Exchange, volume: float, price: float = 0.0,
             type: OrderType = OrderType.LIMIT, reference: str = "") -> "OrderRequest":
        """A-share sell = (Direction.SHORT, Offset.NONE)."""
        return cls(code=code, exchange=exchange, direction=Direction.SHORT,
                   volume=volume, price=price, type=type, reference=reference)

    def create_order(self, order_id: str, gateway: str, status: OrderStatus = OrderStatus.SUBMITTING) -> "Order":
        return Order(
            order_id=order_id,
            code=self.code,
            exchange=self.exchange,
            direction=self.direction,
            offset=self.offset,
            type=self.type,
            price=self.price,
            volume=self.volume,
            traded=0.0,
            status=status,
            gateway=gateway,
            reference=self.reference,
            datetime=datetime.now(),
        )


@dataclass
class CancelRequest:
    order_id: str
    code: str
    exchange: Exchange


@dataclass
class Order:
    """A live order tracked through its lifecycle (see :class:`OrderStatus`)."""

    order_id: str
    code: str
    exchange: Exchange
    direction: Direction
    volume: float
    price: float = 0.0
    type: OrderType = OrderType.LIMIT
    offset: Offset = Offset.NONE
    traded: float = 0.0
    status: OrderStatus = OrderStatus.SUBMITTING
    gateway: str = ""
    reference: str = ""
    datetime: Optional[datetime] = None
    message: str = ""

    @property
    def key(self) -> str:
        return symbol_key(self.code, self.exchange)

    @property
    def remaining(self) -> float:
        return max(self.volume - self.traded, 0.0)

    def is_active(self) -> bool:
        return is_active(self.status)

    def create_cancel(self) -> CancelRequest:
        return CancelRequest(order_id=self.order_id, code=self.code, exchange=self.exchange)


@dataclass
class Trade:
    """A single fill of an order."""

    trade_id: str
    order_id: str
    code: str
    exchange: Exchange
    direction: Direction
    volume: float
    price: float
    offset: Offset = Offset.NONE
    gateway: str = ""
    datetime: Optional[datetime] = None

    @property
    def key(self) -> str:
        return symbol_key(self.code, self.exchange)


@dataclass
class Position:
    """Holding of one instrument, with A-share T+1 accounting.

    ``yd_volume`` is the sellable (yesterday) portion; ``volume - yd_volume`` is
    today's non-sellable portion; ``frozen`` is the amount locked by working sell
    orders. Mirrors vn.py's ``PositionData`` + ``PositionHolding`` split.
    """

    code: str
    exchange: Exchange
    direction: Direction = Direction.LONG
    volume: float = 0.0
    yd_volume: float = 0.0
    today_volume: float = 0.0
    frozen: float = 0.0
    price: float = 0.0          # average cost
    settlement_price: float = 0.0
    margin: float = 0.0
    pnl: float = 0.0
    gateway: str = ""

    @property
    def key(self) -> str:
        return symbol_key(self.code, self.exchange)

    @property
    def available(self) -> float:
        """Sellable now = yesterday's holding not already frozen by sell orders."""
        return max(self.yd_volume - self.frozen, 0.0)


@dataclass
class Account:
    account_id: str
    balance: float = 0.0        # total assets snapshot (cash + optional securities)
    frozen: float = 0.0
    available: float = 0.0      # buying power
    margin: float = 0.0
    commission: float = 0.0
    close_profit: float = 0.0
    position_profit: float = 0.0
    risk_ratio: float = 0.0
    gateway: str = ""


@dataclass
class TickData:
    """L1 real-time quote (extend with full depth later).

    ``volume`` / ``turnover`` are the *cumulative* session totals as pushed by
    A-share feeds — bar aggregation diffs consecutive ticks to get per-bar
    volume, so they must be carried here.
    """

    code: str
    exchange: Exchange
    datetime: Optional[datetime] = None
    name: str = ""
    volume: float = 0.0          # cumulative traded volume (shares)
    turnover: float = 0.0        # cumulative traded amount (CNY)
    last_price: float = 0.0
    pre_close: float = 0.0
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    limit_up: float = 0.0
    limit_down: float = 0.0
    bid_price_1: float = 0.0
    ask_price_1: float = 0.0
    bid_volume_1: float = 0.0
    ask_volume_1: float = 0.0
    gateway: str = ""
    received_at: Optional[datetime] = None
    trading_day: str = ""

    @property
    def key(self) -> str:
        return symbol_key(self.code, self.exchange)


@dataclass
class LogEvent:
    """A gateway log line surfaced to the engine (connection msgs, errors)."""

    msg: str
    level: str = "info"
    gateway: str = ""
    datetime: Optional[datetime] = field(default_factory=datetime.now)

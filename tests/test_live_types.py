"""Phase 0 unit tests: normalized live-trading domain + config (no engine, no vn.py)."""

from __future__ import annotations

import pytest

from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.types import (
    ACTIVE_STATUSES,
    Account,
    Contract,
    Direction,
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Product,
    infer_exchange,
    is_active,
    normalize_symbol,
    symbol_key,
)


# --------------------------------------------------------------------------- #
# OrderStatus (6-state, vn.py-aligned) + back-compat aliases
# --------------------------------------------------------------------------- #
def test_order_status_has_six_states_and_active_set() -> None:
    assert {s.name for s in OrderStatus} >= {
        "SUBMITTING",
        "NOTTRADED",
        "PARTTRADED",
        "ALLTRADED",
        "CANCELLED",
        "REJECTED",
    }
    assert ACTIVE_STATUSES == frozenset(
        {OrderStatus.SUBMITTING, OrderStatus.NOTTRADED, OrderStatus.PARTTRADED}
    )
    assert is_active(OrderStatus.PARTTRADED)
    assert not is_active(OrderStatus.ALLTRADED)
    assert not is_active(OrderStatus.CANCELLED)


def test_order_status_backcompat_aliases_point_at_new_states() -> None:
    # Old timing code used SUBMITTED / FILLED; these must still resolve.
    assert OrderStatus.SUBMITTED is OrderStatus.SUBMITTING
    assert OrderStatus.FILLED is OrderStatus.ALLTRADED


# --------------------------------------------------------------------------- #
# Symbol normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,code,exchange",
    [
        ("600000", "600000", Exchange.SSE),
        ("SH600000", "600000", Exchange.SSE),
        ("sh.600000", "600000", Exchange.SSE),
        ("600000.SH", "600000", Exchange.SSE),
        ("600000.SSE", "600000", Exchange.SSE),
        ("000001", "000001", Exchange.SZSE),
        ("sz.000001", "000001", Exchange.SZSE),
        ("300750", "300750", Exchange.SZSE),
        ("688981", "688981", Exchange.SSE),
        ("830799", "830799", Exchange.BSE),
        ("BJ830799", "830799", Exchange.BSE),
        ("rb2410.SHFE", "RB2410", Exchange.SHFE),
        ("SHFE.rb2410", "RB2410", Exchange.SHFE),
        ("IF2409.CFFEX", "IF2409", Exchange.CFFEX),
        ("ag2408", "AG2408", Exchange.UNKNOWN),
    ],
)
def test_normalize_symbol(raw: str, code: str, exchange: Exchange) -> None:
    assert normalize_symbol(raw) == (code, exchange)


def test_symbol_key_and_infer() -> None:
    assert symbol_key("600000", Exchange.SSE) == "600000.SSE"
    assert infer_exchange("000001") is Exchange.SZSE
    assert infer_exchange("abc") is Exchange.UNKNOWN


# --------------------------------------------------------------------------- #
# Order / Position value objects
# --------------------------------------------------------------------------- #
def test_order_request_creates_working_order() -> None:
    req = OrderRequest(
        code="600000", exchange=Exchange.SSE, direction=Direction.LONG,
        volume=200, price=10.0, reference="cid-1",
    )
    order = req.create_order(order_id="o1", gateway="paper")
    assert order.is_active()
    assert order.remaining == 200
    assert order.key == "600000.SSE"
    assert order.reference == "cid-1"
    assert order.create_cancel().order_id == "o1"


def test_order_remaining_after_partial_fill() -> None:
    order = Order(
        order_id="o2", code="000001", exchange=Exchange.SZSE,
        direction=Direction.LONG, volume=1000, traded=300, status=OrderStatus.PARTTRADED,
    )
    assert order.remaining == 700
    assert order.is_active()


def test_position_t_plus_one_available() -> None:
    # 1000 held, only 600 sellable (yesterday), 100 frozen by a working sell.
    pos = Position(
        code="600000", exchange=Exchange.SSE,
        volume=1000, yd_volume=600, frozen=100,
    )
    assert pos.available == 500          # 600 sellable - 100 frozen
    # Bought today (yd_volume 0) => nothing sellable today.
    fresh = Position(code="600000", exchange=Exchange.SSE, volume=500, yd_volume=0)
    assert fresh.available == 0


def test_futures_ready_type_defaults() -> None:
    assert OrderType.FAK.value == "fak"
    assert Product.FUTURES.value == "futures"
    contract = Contract(code="RB2410", exchange=Exchange.SHFE, product=Product.FUTURES, size=10, margin_rate=0.12)
    assert contract.key == "RB2410.SHFE"
    assert contract.margin_rate == 0.12
    account = Account(account_id="futures", balance=100_000, margin=12_000, risk_ratio=0.12)
    assert account.margin == 12_000
    pos = Position(code="IF2409", exchange=Exchange.CFFEX, today_volume=1, margin=15_000, settlement_price=3500)
    assert pos.today_volume == 1
    assert pos.settlement_price == 3500


# --------------------------------------------------------------------------- #
# LiveConfig
# --------------------------------------------------------------------------- #
def test_live_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALPHAPILOT_LIVE_MODE",
        "ALPHAPILOT_LIVE_BROKER",
        "ALPHAPILOT_LIVE_TRADE_BROKER",
        "ALPHAPILOT_LIVE_QUOTE_PROVIDER",
        "ALPHAPILOT_LIVE_MARKET_ENABLED",
        "ALPHAPILOT_LIVE_MARKET_RETENTION_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = LiveConfig.load()
    assert cfg.mode == RunMode.DRY_RUN
    assert cfg.broker == "paper"
    assert cfg.trade_broker == "paper"
    assert cfg.quote_provider == "paper"
    assert cfg.risk.lot_size == 100
    assert cfg.market_data.enabled is True
    assert cfg.market_data.retention_days == 30
    assert 0 < cfg.risk.max_position_pct <= 1
    assert "mode=" in cfg.summary()


def test_live_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHAPILOT_LIVE_MODE", RunMode.PAPER)
    monkeypatch.setenv("ALPHAPILOT_LIVE_BROKER", "xtp")
    monkeypatch.setenv("ALPHAPILOT_LIVE_TRADE_BROKER", "emt")
    monkeypatch.setenv("ALPHAPILOT_LIVE_QUOTE_PROVIDER", "xtp")
    monkeypatch.setenv("ALPHAPILOT_LIVE_MAX_ORDER_VALUE", "50000")
    monkeypatch.setenv("ALPHAPILOT_LIVE_LOT_SIZE", "200")
    monkeypatch.setenv("ALPHAPILOT_LIVE_MARKET_ENABLED", "false")
    monkeypatch.setenv("ALPHAPILOT_LIVE_MARKET_RETENTION_DAYS", "7")
    cfg = LiveConfig.load()
    assert cfg.mode == RunMode.PAPER
    assert cfg.broker == "emt"
    assert cfg.trade_broker == "emt"
    assert cfg.quote_provider == "xtp"
    assert cfg.risk.max_order_value == 50000.0
    assert cfg.risk.lot_size == 200
    assert cfg.market_data.enabled is False
    assert cfg.market_data.retention_days == 7

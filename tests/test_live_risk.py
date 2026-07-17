"""Phase 3 unit tests: the pre-trade risk gate, rule by rule."""

from __future__ import annotations

from datetime import datetime

from alphapilot.systems.live.config import RiskLimits, RunMode
from alphapilot.systems.live.fsm.runmode_fsm import RunModeMachine
from alphapilot.systems.live.fsm.session_fsm import SessionClock
from alphapilot.systems.live.oms import OMS
from alphapilot.systems.live.risk import RiskGate
from alphapilot.systems.live.types import Account, Contract, Exchange, OrderRequest, Position, Product, TickData


def _permissive() -> RiskLimits:
    return RiskLimits(
        max_order_value=1e12, max_daily_value=1e15, max_position_pct=1.0,
        price_guard_pct=0.1, max_orders_per_day=1000, lot_size=100,
    )


def _oms(cash: float = 1_000_000.0, ticks=None, positions=None, contracts=None) -> OMS:
    oms = OMS()
    oms.on_account(Account(account_id="acc", balance=cash, available=cash))
    for code, kwargs in (contracts or {}).items():
        c, ex = _split(code)
        oms.on_contract(Contract(code=c, exchange=ex, **kwargs))
    for code, px in (ticks or {}).items():
        c, ex = _split(code)
        if isinstance(px, dict):
            oms.on_tick(TickData(code=c, exchange=ex, **px))
        else:
            oms.on_tick(TickData(code=c, exchange=ex, last_price=px))
    for code, (vol, yd) in (positions or {}).items():
        c, ex = _split(code)
        oms.on_position(Position(code=c, exchange=ex, volume=vol, yd_volume=yd))
    return oms


def _split(code: str):
    from alphapilot.systems.live.types import normalize_symbol
    return normalize_symbol(code)


def _ctx():
    return SessionClock(now_fn=lambda: datetime(2026, 7, 1, 10, 0)), RunModeMachine(RunMode.PAPER)


def test_valid_buy_passes() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    oms = _oms(ticks={"600000": 10.0})
    session, mode = _ctx()
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 1000, 10.0), oms, session, mode)
    assert v.ok


def test_dynamic_notional_caps_use_minimum_of_cash_and_equity_limits() -> None:
    limits = _permissive()
    limits.max_order_value = 10_000
    limits.max_order_equity_pct = 0.02
    limits.max_daily_value = 0
    limits.max_daily_equity_pct = 0.10
    gate = RiskGate(limits, enforce_session=False)
    oms = _oms(cash=300_000, ticks={"600000": 10.0, "600001": 10.0})

    # 2% of 300k = 6k, which is tighter than the absolute 10k limit.
    too_large = gate.check(
        OrderRequest.buy("600000", Exchange.SSE, 700, 10.0), oms, *_ctx()
    )
    assert not too_large.ok and too_large.rule == "max_order_value"
    assert gate.check(
        OrderRequest.buy("600000", Exchange.SSE, 600, 10.0), oms, *_ctx()
    ).ok

    # Accepted turnover is retained; the dynamic daily cap is 30k.
    for index in range(4):
        verdict = gate.check(
            OrderRequest.buy(
                "600001",
                Exchange.SSE,
                600,
                10.0,
                reference=f"daily-{index}",
            ),
            oms,
            *_ctx(),
        )
        assert verdict.ok
    rejected = gate.check(
        OrderRequest.buy(
            "600001", Exchange.SSE, 600, 10.0, reference="daily-over"
        ),
        oms,
        *_ctx(),
    )
    assert not rejected.ok and rejected.rule == "max_daily_value"


def test_loss_limits_survive_recovery_and_require_manual_halt_recovery() -> None:
    limits = _permissive()
    limits.max_daily_loss_pct = 0.01
    limits.max_canary_loss_pct = 0.03
    gate = RiskGate(limits, enforce_session=False)
    oms = _oms(cash=1_000_000, ticks={"600000": 10.0})
    assert gate.check(
        OrderRequest.buy("600000", Exchange.SSE, 100, 10.0), oms, *_ctx()
    ).ok

    state = gate.snapshot()
    recovered = RiskGate(limits, enforce_session=False)
    recovered.restore(state)
    oms.account.balance = 989_000
    verdict = recovered.check(
        OrderRequest.buy("600000", Exchange.SSE, 100, 10.0), oms, *_ctx()
    )

    assert not verdict.ok and verdict.rule == "daily_loss"
    assert recovered.snapshot()["canary_start_equity"] == 1_000_000
    assert recovered.snapshot()["loss_halt_rule"] == "daily_loss"
    recovered.reset_day()
    assert recovered.check_equity(oms).rule == "daily_loss"


def test_lot_size_rejected() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 150, 10.0), _oms(), *_ctx())
    assert not v.ok and v.rule == "lot_size"


def test_contract_lot_size_overrides_global_limit() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    oms = _oms(ticks={"600000": 10.0}, contracts={"600000": {"lot_size": 10, "price_tick": 0.01}})
    assert gate.check(OrderRequest.buy("600000", Exchange.SSE, 50, 10.0), oms, *_ctx()).ok
    rejected = gate.check(OrderRequest.buy("600000", Exchange.SSE, 55, 10.0), oms, *_ctx())
    assert not rejected.ok and rejected.rule == "lot_size"


def test_contract_price_tick_rejected() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    oms = _oms(ticks={"600000": 10.0}, contracts={"600000": {"price_tick": 0.01}})
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 100, 10.005), oms, *_ctx())
    assert not v.ok and v.rule == "price_tick"


def test_tick_price_limits_rejected() -> None:
    limits = _permissive()
    limits.price_guard_pct = 1.0
    gate = RiskGate(limits, enforce_session=False)
    oms = _oms(
        ticks={"600000": {"last_price": 10.0, "limit_up": 11.0, "limit_down": 9.0}},
        contracts={"600000": {"price_tick": 0.01}},
    )
    high = gate.check(OrderRequest.buy("600000", Exchange.SSE, 100, 11.01), oms, *_ctx())
    low = gate.check(OrderRequest.sell("600000", Exchange.SSE, 100, 8.99), oms, *_ctx())
    assert not high.ok and high.rule == "price_limit"
    assert not low.ok and low.rule == "price_limit"


def test_live_mode_requires_contract_metadata() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    session = SessionClock(now_fn=lambda: datetime(2026, 7, 1, 10, 0))
    live_mode = RunModeMachine(RunMode.LIVE)
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0), _oms(ticks={"600000": 10.0}), session, live_mode)
    assert not v.ok and v.rule == "unknown_contract"


def test_live_mode_rejects_futures_contracts_until_supported() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    session = SessionClock(now_fn=lambda: datetime(2026, 7, 1, 10, 0))
    live_mode = RunModeMachine(RunMode.LIVE)
    oms = _oms(
        ticks={"RB2410.SHFE": 3500.0},
        contracts={"RB2410.SHFE": {"product": Product.FUTURES, "lot_size": 1, "price_tick": 1.0}},
    )
    verdict = gate.check(OrderRequest.buy("RB2410", Exchange.SHFE, 1, 3500.0), oms, session, live_mode)
    assert not verdict.ok
    assert verdict.rule == "unsupported_product"


def test_live_mode_requires_fresh_timestamped_quote() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    session = SessionClock(now_fn=lambda: datetime(2026, 7, 1, 10, 0, 10))
    live_mode = RunModeMachine(RunMode.LIVE)
    oms = _oms(
        ticks={"600000": {"last_price": 10.0, "datetime": datetime(2026, 7, 1, 10, 0, 0)}},
        contracts={"600000": {"lot_size": 100, "price_tick": 0.01}},
    )

    verdict = gate.check(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0), oms, session, live_mode)

    assert not verdict.ok and verdict.rule == "stale_quote"


def test_insufficient_cash_rejected() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    oms = _oms(cash=5_000.0, ticks={"600000": 10.0})
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 1000, 10.0), oms, *_ctx())
    assert not v.ok and v.rule == "insufficient_cash"


def test_insufficient_position_rejected_t_plus_one() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    oms = _oms(positions={"600000": (1000, 300)})   # 1000 held, only 300 sellable
    v = gate.check(OrderRequest.sell("600000", Exchange.SSE, 500, 10.0), oms, *_ctx())
    assert not v.ok and v.rule == "insufficient_position"
    # selling within the sellable amount is fine
    ok = gate.check(OrderRequest.sell("600000", Exchange.SSE, 300, 10.0), oms, *_ctx())
    assert ok.ok


def test_price_guard_rejects_fat_finger() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    oms = _oms(ticks={"600000": 10.0})
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 100, 12.0), oms, *_ctx())  # +20% vs ref
    assert not v.ok and v.rule == "price_guard"


def test_max_order_value_rejected() -> None:
    limits = _permissive()
    limits.max_order_value = 5_000.0
    gate = RiskGate(limits, enforce_session=False)
    oms = _oms(ticks={"600000": 10.0})
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 1000, 10.0), oms, *_ctx())
    assert not v.ok and v.rule == "max_order_value"


def test_max_position_pct_rejected() -> None:
    limits = _permissive()
    limits.max_position_pct = 0.3
    gate = RiskGate(limits, enforce_session=False)
    oms = _oms(cash=100_000.0, ticks={"600000": 10.0})
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 4000, 10.0), oms, *_ctx())  # 40k / 100k
    assert not v.ok and v.rule == "max_position_pct"


def test_canary_total_exposure_and_position_count_are_hard_caps() -> None:
    limits = _permissive()
    limits.max_total_position_pct = 0.10
    limits.max_position_count = 5
    symbols = [f"{600000 + index}" for index in range(6)]
    ticks = {symbol: 10.0 for symbol in symbols}
    positions = {symbol: (100, 100) for symbol in symbols[:5]}
    gate = RiskGate(limits, enforce_session=False)
    oms = _oms(cash=100_000, ticks=ticks, positions=positions)

    count_breach = gate.check(
        OrderRequest.buy(symbols[5], Exchange.SSE, 100, 10.0), oms, *_ctx()
    )
    assert not count_breach.ok and count_breach.rule == "max_position_count"

    exposure_limits = _permissive()
    exposure_limits.max_total_position_pct = 0.05
    exposure_limits.max_position_count = 5
    exposure_gate = RiskGate(exposure_limits, enforce_session=False)
    exposure_oms = _oms(
        cash=100_000,
        ticks={"600000": 10.0, "600001": 10.0},
        positions={"600000": (400, 400)},
    )
    exposure_breach = exposure_gate.check(
        OrderRequest.buy("600001", Exchange.SSE, 200, 10.0),
        exposure_oms,
        *_ctx(),
    )
    assert not exposure_breach.ok
    assert exposure_breach.rule == "max_total_position_pct"


def test_duplicate_reference_rejected() -> None:
    gate = RiskGate(_permissive(), enforce_session=False)
    oms = _oms(ticks={"600000": 10.0})
    req = OrderRequest.buy("600000", Exchange.SSE, 100, 10.0, reference="cid-1")
    assert gate.check(req, oms, *_ctx()).ok
    dup = gate.check(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0, reference="cid-1"), oms, *_ctx())
    assert not dup.ok and dup.rule == "duplicate"
    snap = gate.snapshot()
    restored = RiskGate(_permissive(), enforce_session=False)
    restored.restore(snap)
    assert restored.snapshot()["seen_refs"] == ["cid-1"]
    dup_after_restore = restored.check(
        OrderRequest.buy("600000", Exchange.SSE, 100, 10.0, reference="cid-1"), oms, *_ctx()
    )
    assert not dup_after_restore.ok and dup_after_restore.rule == "duplicate"


def test_max_orders_per_day_rejected() -> None:
    limits = _permissive()
    limits.max_orders_per_day = 1
    gate = RiskGate(limits, enforce_session=False)
    oms = _oms(ticks={"600000": 10.0})
    assert gate.check(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0), oms, *_ctx()).ok
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0), oms, *_ctx())
    assert not v.ok and v.rule == "max_orders_per_day"


def test_session_gate_when_enforced() -> None:
    gate = RiskGate(_permissive(), enforce_session=True)
    oms = _oms(ticks={"600000": 10.0})
    lunch = SessionClock(now_fn=lambda: datetime(2026, 7, 1, 12, 0))  # LUNCH_BREAK
    v = gate.check(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0), oms, lunch, RunModeMachine(RunMode.PAPER))
    assert not v.ok and v.rule == "session"

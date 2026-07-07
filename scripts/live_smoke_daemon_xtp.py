"""Gated XTP daemon smoke through the AlphaPilot live control plane.

This is the stronger end-to-end check for real trading: it starts the live
daemon, waits for account + tick state, submits one tiny far-off limit order via
``live_daemon_order``, waits for the OMS/broker ack, cancels it via
``live_daemon_cancel``, waits for cancel confirmation, then stops the daemon.

Credentials are read from environment variables (or .env when python-dotenv is
installed). Secrets and account balances are never printed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:  # best effort, environment variables still win
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
except Exception:  # noqa: BLE001
    pass

from alphapilot.kernel import build_engine  # noqa: E402
from alphapilot.systems.live.brokers.registry import missing_setting_fields  # noqa: E402
from alphapilot.systems.live.fsm.session_fsm import SessionState, can_submit, session_state_at  # noqa: E402
from alphapilot.systems.live.types import normalize_symbol, symbol_key  # noqa: E402


def wait_for(fn, timeout: float, label: str) -> tuple[bool, Any]:
    deadline = time.time() + float(timeout)
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            print(f"  [ok] {label}")
            return True, last
        time.sleep(0.5)
    print(f"  [TIMEOUT] {label}")
    return False, last


def _state_dir(args) -> Path:
    if args.state_dir:
        return Path(args.state_dir).expanduser()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "git_ignore_folder" / "live_smoke" / f"xtp_daemon_{stamp}"


def _ledger_dir(args, state_dir: Path) -> Path:
    if args.ledger_dir:
        return Path(args.ledger_dir).expanduser()
    return state_dir / "ledger"


def _tick_from_status(status: dict[str, Any], key: str) -> dict[str, Any] | None:
    state = status.get("state") if isinstance(status.get("state"), dict) else {}
    for tick in state.get("ticks") or []:
        if tick.get("key") == key:
            return tick
    return None


def _reference_price(tick: dict[str, Any]) -> float:
    for field in ("bid_price_1", "last_price", "ask_price_1", "pre_close"):
        value = float(tick.get(field) or 0.0)
        if value > 0:
            return value
    return 0.0


def _smoke_price(tick: dict[str, Any], *, factor: float) -> float:
    ref = _reference_price(tick)
    if ref <= 0:
        raise ValueError("tick has no usable reference price")
    price = round(ref * float(factor), 2)
    limit_down = float(tick.get("limit_down") or 0.0)
    if limit_down > 0:
        price = max(price, limit_down)
    return round(price, 2)


def _last_command(result: dict[str, Any]) -> dict[str, Any]:
    daemon = result.get("daemon") if isinstance(result.get("daemon"), dict) else {}
    command = daemon.get("last_command") if isinstance(daemon.get("last_command"), dict) else {}
    return command


def wait_for_session_window(*, timeout: float, continuous: bool) -> bool:
    deadline = time.time() + float(timeout)
    last_state = None
    while time.time() < deadline:
        now = datetime.now()
        state = session_state_at(now, is_trading_day=True)
        allowed = (
            state in {SessionState.CONTINUOUS_AM, SessionState.CONTINUOUS_PM}
            if continuous else can_submit(state)
        )
        if allowed:
            print(f"  [ok] session window {state.value}")
            return True
        if state != last_state:
            target = "continuous session" if continuous else "submit window"
            print(f"  waiting for {target}; current={state.value}")
            last_state = state
        time.sleep(5.0)
    print("  [TIMEOUT] session window")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="512880", help="low-notional test symbol, e.g. 512880 or 600000")
    parser.add_argument("--volume", type=float, default=100.0, help="A-share board-lot volume")
    parser.add_argument("--price", type=float, default=0.0, help="explicit limit price; overrides tick-derived price")
    parser.add_argument("--price-factor", type=float, default=0.96, help="tick reference multiplier when price is omitted")
    parser.add_argument("--timeout", type=float, default=40.0, help="daemon connect/readiness timeout")
    parser.add_argument("--event-timeout", type=float, default=15.0, help="broker order/cancel callback wait")
    parser.add_argument("--interval", type=float, default=0.2, help="daemon heartbeat interval")
    parser.add_argument("--wait-submit-window", action="store_true", help="wait until the A-share session accepts orders")
    parser.add_argument("--wait-continuous", action="store_true", help="wait until continuous trading, safer for cancel smoke")
    parser.add_argument("--session-timeout", type=float, default=3600.0, help="max seconds to wait for the requested session")
    parser.add_argument("--client-id", type=int, help="temporary ALPHAPILOT_LIVE_XTP_CLIENT_ID override")
    parser.add_argument("--state-dir", help="state directory; defaults under git_ignore_folder/live_smoke")
    parser.add_argument("--ledger-dir", help="ledger directory; defaults to <state-dir>/ledger")
    parser.add_argument("--confirm-live", action="store_true", help="required to submit/cancel through LIVE mode")
    parser.add_argument("--dump-status", action="store_true", help="print compact daemon command summaries")
    args = parser.parse_args()

    if not args.confirm_live:
        print("Refusing to route a LIVE daemon order without --confirm-live.")
        return 2
    if args.client_id is not None:
        os.environ["ALPHAPILOT_LIVE_XTP_CLIENT_ID"] = str(int(args.client_id))

    missing = missing_setting_fields("xtp")
    if missing:
        print("Missing XTP env fields:")
        for name in missing:
            print(f"  {name}")
        return 2

    code, exchange = normalize_symbol(args.symbol)
    key = symbol_key(code, exchange)
    state_dir = _state_dir(args)
    ledger_dir = _ledger_dir(args, state_dir)
    print(f"Using state_dir={state_dir}")

    if args.wait_submit_window or args.wait_continuous:
        if not wait_for_session_window(timeout=args.session_timeout, continuous=args.wait_continuous):
            return 1

    engine = build_engine()
    live = engine.get_module("live")
    ok = True
    started = False
    try:
        start = live.live_daemon_start(
            mode="live",
            broker="xtp",
            symbols=args.symbol,
            interval=args.interval,
            timeout=args.timeout,
            state_dir=str(state_dir),
            ledger_dir=str(ledger_dir),
        )
        started = bool(start.get("started") or start.get("running"))
        if not started:
            print(f"  [FAIL] daemon did not start: {start.get('error') or start.get('status') or start}")
            return 1

        def ready_status() -> dict[str, Any] | None:
            status = live.live_daemon_status(mode="live", broker="xtp", state_dir=str(state_dir))
            state = status.get("state") if isinstance(status.get("state"), dict) else {}
            if status.get("running") and state.get("account") and _tick_from_status(status, key):
                return status
            return None

        ready, status = wait_for(ready_status, args.timeout, f"daemon ready with tick for {key}")
        if not ready or status is None:
            ok = False
            return 1

        tick = _tick_from_status(status, key)
        assert tick is not None
        price = float(args.price) if args.price > 0 else _smoke_price(tick, factor=args.price_factor)
        ref = _reference_price(tick)
        print(f"  ref_price={ref:.3f} smoke_price={price:.3f} volume={args.volume:.0f}")

        reference = f"daemon-smoke-{int(time.time())}"
        order_result = live.live_daemon_order(
            args.symbol,
            "buy",
            args.volume,
            price=price,
            state_dir=str(state_dir),
            wait=True,
            timeout=args.event_timeout + 10.0,
            event_timeout=args.event_timeout,
            confirm_live=True,
            reference=reference,
        )
        order_cmd = _last_command(order_result)
        if args.dump_status:
            print("  order:", {
                "ok": order_cmd.get("ok"),
                "message": order_cmd.get("message"),
                "status": order_cmd.get("order_status"),
                "acknowledged": order_cmd.get("order_acknowledged"),
                "active": order_cmd.get("order_active"),
                "routing_rule": order_cmd.get("routing_rule"),
                "routing_reason": order_cmd.get("routing_reason"),
            })
        if not (order_result.get("accepted") and order_cmd.get("submitted") and order_cmd.get("order_acknowledged")):
            if order_cmd.get("routing_reason"):
                print(f"  [FAIL] order was not routed: {order_cmd.get('routing_rule')}: {order_cmd.get('routing_reason')}")
            else:
                print(f"  [FAIL] order was not acknowledged: {order_cmd}")
            return 1
        if order_cmd.get("order_active") is not True:
            print(f"  [FAIL] order is not working; status={order_cmd.get('order_status')}")
            return 1
        order_id = str(order_cmd.get("order_id") or "")
        print(f"  [ok] order acknowledged and active ({order_id})")

        cancel_result = live.live_daemon_cancel(
            order_id,
            symbol=args.symbol,
            state_dir=str(state_dir),
            wait=True,
            timeout=args.event_timeout + 10.0,
            event_timeout=args.event_timeout,
        )
        cancel_cmd = _last_command(cancel_result)
        if args.dump_status:
            print("  cancel:", {
                "ok": cancel_cmd.get("ok"),
                "message": cancel_cmd.get("message"),
                "cancelled": cancel_cmd.get("cancelled"),
                "confirmed": cancel_cmd.get("cancel_confirmed"),
                "terminal": cancel_cmd.get("cancel_terminal"),
            })
        if not (cancel_result.get("accepted") and cancel_cmd.get("cancelled") and cancel_cmd.get("cancel_confirmed")):
            print(f"  [FAIL] cancel was not confirmed: {cancel_cmd}")
            return 1
        print("  [ok] cancel confirmed")
    finally:
        if started:
            stop = live.live_daemon_stop(state_dir=str(state_dir), timeout=10.0)
            print(f"  daemon stopped={not stop.get('running', False)}")
        try:
            engine.shutdown()
        except Exception:
            pass

    print("XTP DAEMON SMOKE:", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

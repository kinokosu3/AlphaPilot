"""Gated XTP real-connect smoke — run inside the live image against the XTP
公网测试环境 (simulation servers). Query-only by default; pass --order to also
place + cancel one tiny far-from-market limit order.

Usage (credentials via env, never files):

    docker compose --profile live run --rm \
      -e ALPHAPILOT_LIVE_XTP_ACCOUNT=... \
      -e ALPHAPILOT_LIVE_XTP_PASSWORD=... \
      -e ALPHAPILOT_LIVE_XTP_CLIENT_ID=1 \
      -e ALPHAPILOT_LIVE_XTP_SOFTWARE_KEY=... \
      -e ALPHAPILOT_LIVE_XTP_QUOTE_HOST=... -e ALPHAPILOT_LIVE_XTP_QUOTE_PORT=... \
      -e ALPHAPILOT_LIVE_XTP_TRADE_HOST=... -e ALPHAPILOT_LIVE_XTP_TRADE_PORT=... \
      live python scripts/live_smoke_connect_xtp.py [--order] [--symbol 600000]

For XTP public simulation accounts, if the account email did not include
endpoints, pass --use-public-test-endpoints to fill the commonly published
test hosts. Broker-provided values in env always win.

Checks: TD+MD login, account/positions arrive, contracts load, one tick after
subscribing; with --order: submit a limit buy ~10% below last price (1 lot),
wait for the ack, cancel it, and verify the cancel.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alphapilot.systems.live.brokers.registry import (
    build_connect_setting,
    missing_setting_fields,
)
from alphapilot.systems.live.brokers.vnpy_adapter import VnpyBrokerAdapter
from alphapilot.systems.live.oms import OMS
from alphapilot.systems.live.types import OrderRequest, OrderType, normalize_symbol, symbol_key
from scripts.live_xtp_common import env_with_public_test_endpoints


def wait_for(predicate, timeout: float, what: str) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            print(f"  [ok] {what}")
            return True
        time.sleep(0.5)
    print(f"  [TIMEOUT] {what}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--order", action="store_true", help="also place+cancel a tiny far-off limit order")
    parser.add_argument("--symbol", default="600000", help="test symbol, e.g. 600000 or 000001.SZ")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--tick-timeout", type=float, default=None, help="override tick wait timeout")
    parser.add_argument(
        "--use-public-test-endpoints",
        action="store_true",
        help="fill missing quote/trade hosts and ports with public XTP simulation defaults",
    )
    parser.add_argument("--skip-tick", action="store_true", help="skip market-data subscription/tick check")
    parser.add_argument("--dump-logs", action="store_true", help="print gateway logs collected by OMS")
    args = parser.parse_args()

    env = env_with_public_test_endpoints(os.environ) if args.use_public_test_endpoints else os.environ

    missing = missing_setting_fields("xtp", env)
    if missing:
        print("Missing XTP credentials in env:")
        for name in missing:
            print(f"  {name}")
        print("Tip: pass --use-public-test-endpoints to fill the common public simulation hosts/ports.")
        return 2

    setting = build_connect_setting("xtp", env)
    print(f"Connecting XTP (trade {setting['交易地址']}:{setting['交易端口']}, "
          f"quote {setting['行情地址']}:{setting['行情端口']}) ...")
    code, exchange = normalize_symbol(args.symbol)

    adapter = VnpyBrokerAdapter("XTP")
    oms = OMS()
    adapter.register_callback(oms)
    adapter.connect(setting)

    ok = True
    ok &= wait_for(lambda: oms.account is not None, args.timeout, "account snapshot received")
    ok &= wait_for(lambda: len(oms.contracts) > 0, args.timeout, "contracts received")
    if oms.account:
        print(f"  buying_power={oms.account.available:.2f} balance={oms.account.balance:.2f}")
    print(f"  positions={len(oms.get_positions())} contracts={len(oms.contracts)}")

    tick = None
    if not args.skip_tick:
        adapter.subscribe([args.symbol])
        key = symbol_key(code, exchange)
        tick_wait = args.tick_timeout if args.tick_timeout is not None else args.timeout
        ok &= wait_for(lambda: oms.get_tick(key) is not None, tick_wait, f"tick for {key}")
        tick = oms.get_tick(key)
        if tick:
            print(f"  last_price={tick.last_price} bid1={tick.bid_price_1} ask1={tick.ask_price_1}")

    if args.order:
        if tick is None or tick.last_price <= 0:
            print("  [skip] no tick -> not placing an order")
        else:
            price = round(tick.last_price * 0.9, 2)   # far below market: won't fill
            req = OrderRequest.buy(code, exchange, 100, price, type=OrderType.LIMIT,
                                   reference="smoke")
            print(f"Placing 1-lot limit buy {key} @ {price} (far off market) ...")
            order_id = adapter.send_order(req)
            ok &= wait_for(lambda: oms.get_order(order_id) is not None, args.timeout, "order ack")
            order = oms.get_order(order_id)
            if order and order.is_active():
                adapter.cancel_order(order.create_cancel())
                ok &= wait_for(
                    lambda: not oms.get_order(order_id).is_active(), args.timeout, "cancel confirmed"
                )

    adapter.close()
    if args.dump_logs and oms.logs:
        print("Gateway logs:")
        for log in list(oms.logs):
            print(f"  [{log.level}] {log.msg}")
    print("XTP CONNECT SMOKE:", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

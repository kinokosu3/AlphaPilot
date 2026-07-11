"""Offline x86 readiness check for the compiled broker gateways (no credentials).

Goes one level deeper than live_smoke_import.py: besides importing, it
*executes* each compiled binding on this machine — constructs the C++ API
objects (createQuoteApi / createTraderApi), registers callbacks, and attempts a
login against an unreachable local address, expecting a clean, fast failure
(nonzero code / empty session) instead of a crash. That exercises dlopen, the
vendor factory symbols, SDK worker threads, GIL callback plumbing and our new
EMT MD binding's code paths — everything except a real broker connection.

Run it inside the live image (or on the future x86 box after pip-installing the
gateways):

    docker compose --profile live run --rm live python scripts/live_x86_check.py
"""

from __future__ import annotations

import platform
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UNREACHABLE = ("127.0.0.1", 1)  # connection refused -> immediate clean failure

results: dict[str, str] = {}
failed = False


def record(name: str, fn) -> None:
    global failed
    try:
        results[name] = str(fn())
    except Exception:
        results[name] = "FAIL\n" + traceback.format_exc()
        failed = True


def check_platform() -> str:
    return f"{platform.system()} {platform.machine()} py{platform.python_version()}"


def check_xtp_md() -> str:
    from alphapilot_xtpx.api import MdApi

    api = MdApi()
    api.createQuoteApi(1, tempfile.mkdtemp(prefix="xtpmd"), 3)  # client_id, path, log_level
    ret = api.login(UNREACHABLE[0], UNREACHABLE[1], "smoke", "smoke", 1, "")
    err = api.getApiLastError()
    if ret == 0:
        raise RuntimeError("login to unreachable host unexpectedly succeeded")
    return f"created, login refused as expected (ret={ret}, err_id={err.get('error_id')})"


def check_xtp_td() -> str:
    from alphapilot_xtpx.api import TdApi

    api = TdApi()
    api.createTraderApi(1, tempfile.mkdtemp(prefix="xtptd"), 3)
    api.setSoftwareKey("smoke-key")
    api.subscribePublicTopic(0)
    session = api.login(UNREACHABLE[0], UNREACHABLE[1], "smoke", "smoke", 1)
    err = api.getApiLastError()
    if session != 0:
        raise RuntimeError(f"login unexpectedly returned session {session}")
    return f"created, login refused as expected (session=0, err_id={err.get('error_id')})"


def check_emt_md() -> str:
    # Exercises the NEW minimal EMQ::API binding end to end (factory, RegisterSpi,
    # Login signature, synthesized getApiLastError).
    from alphapilot_emt.api import MdApi

    api = MdApi()
    api.createQuoteApi(tempfile.mkdtemp(prefix="emtmd"), 3, 4)  # log_path, file_lvl, console_lvl
    ret = api.login(UNREACHABLE[0], UNREACHABLE[1], "smoke", "smoke")
    err = api.getApiLastError()
    if ret == 0:
        raise RuntimeError("login to unreachable host unexpectedly succeeded")
    return f"created, login refused as expected (ret={ret}, err={err})"


def check_emt_td() -> str:
    from alphapilot_emt.api import TdApi

    api = TdApi()
    api.createTraderApi(1, tempfile.mkdtemp(prefix="emttd"), 4)
    api.subscribePublicTopic(0)
    session = api.login(UNREACHABLE[0], UNREACHABLE[1], "smoke", "smoke", 1)
    err = api.getApiLastError()
    if session != 0:
        raise RuntimeError(f"login unexpectedly returned session {session}")
    return f"created, login refused as expected (session=0, err_id={err.get('error_id')})"


def check_gateways_wire() -> str:
    # Native AlphaPilot gateways: registry resolution + construction (which
    # instantiates the C++ API wrapper objects) + callback registration.
    from alphapilot.systems.live.brokers.registry import create_gateway_pair
    from alphapilot.systems.live.oms import OMS

    names = []
    for broker in ("xtp", "emt"):
        gateway, quote_gateway = create_gateway_pair(broker, broker)
        assert quote_gateway is gateway
        gateway.register_callback(OMS())
        names.append(f"{broker}:{type(gateway).__name__}")
    return f"native gateways: {names}"


def main() -> int:
    record("platform", check_platform)
    record("xtp MdApi create+login", check_xtp_md)
    record("xtp TdApi create+login", check_xtp_td)
    record("emt MdApi create+login (new EMQ binding)", check_emt_md)
    record("emt TdApi create+login", check_emt_td)
    record("native gateway wiring", check_gateways_wire)

    print("=" * 64)
    for key, value in results.items():
        print(f"{key}: {value}")
    print("=" * 64)
    print("X86 CHECK", "FAILED" if failed else "PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

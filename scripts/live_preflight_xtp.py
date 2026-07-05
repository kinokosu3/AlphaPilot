"""XTP live preflight.

This script does not log in, submit orders, or send credentials to XTP. It
checks whether the local runtime can plausibly run the normal XTP gateway and
whether the configured quote/trade endpoints are reachable by TCP.
"""

from __future__ import annotations

import argparse
import os
import platform
import socket
import struct
import sys
from collections.abc import Mapping
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alphapilot.systems.live.brokers.registry import (  # noqa: E402
    ENV_PREFIX,
    build_connect_setting,
    get_broker,
    missing_setting_fields,
    resolve_gateway_class,
)
from scripts.live_xtp_common import env_with_public_test_endpoints  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
XTP_SDK_LIBS = (
    REPO_ROOT / "vnpy_xtp/vnpy_xtp/api/libxtpxquoteapi.so",
    REPO_ROOT / "vnpy_xtp/vnpy_xtp/api/libxtpxtraderapi.so",
)
ELF_MACHINE_NAMES = {
    62: "x86_64",
    183: "aarch64",
}
HOST_MACHINE_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}


def normalized_host_machine() -> str:
    """Return a normalized host machine string for common Linux platforms."""
    return HOST_MACHINE_ALIASES.get(platform.machine().lower(), platform.machine().lower())


def read_elf_machine(path: Path) -> str:
    """Read the ELF e_machine value without shelling out to readelf/file."""
    data = path.read_bytes()[:20]
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return "unknown"
    endian = "<" if data[5] == 1 else ">"
    machine_id = struct.unpack_from(f"{endian}H", data, 18)[0]
    return ELF_MACHINE_NAMES.get(machine_id, f"machine-{machine_id}")


def tcp_probe(host: str, port: int, timeout: float) -> tuple[bool, str]:
    """Try a TCP connect without sending any application data."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "reachable"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_line(ok: bool, label: str, detail: str = "") -> None:
    status = "ok" if ok else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"[{status}] {label}{suffix}")


def missing_endpoint_fields(env: Mapping[str, str]) -> list[str]:
    """Return missing XTP endpoint env vars; credentials are intentionally ignored."""
    spec = get_broker("xtp")
    prefix = f"{ENV_PREFIX}{spec.name.upper()}_"
    if env.get(f"{prefix}SETTING_JSON"):
        return []
    required_suffixes = ("QUOTE_HOST", "QUOTE_PORT", "TRADE_HOST", "TRADE_PORT")
    return [prefix + suffix for suffix in required_suffixes if not env.get(prefix + suffix)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--use-public-test-endpoints",
        action="store_true",
        help="fill missing quote/trade hosts and ports with public XTP simulation defaults",
    )
    parser.add_argument("--skip-network", action="store_true", help="skip TCP endpoint probes")
    parser.add_argument("--timeout", type=float, default=3.0, help="TCP connect timeout in seconds")
    args = parser.parse_args()

    env = env_with_public_test_endpoints(os.environ) if args.use_public_test_endpoints else os.environ

    failed = False

    missing = missing_setting_fields("xtp", env)
    check_line(not missing, "required XTP env fields", ", ".join(missing) if missing else "present")
    failed |= bool(missing)
    if missing and not args.use_public_test_endpoints:
        print("      hint: pass --use-public-test-endpoints to fill public simulation host/port defaults")

    host_machine = normalized_host_machine()
    check_line(sys.maxsize > 2**32, "64-bit Python", platform.python_version())

    sdk_machines: set[str] = set()
    for lib_path in XTP_SDK_LIBS:
        exists = lib_path.exists()
        machine = read_elf_machine(lib_path) if exists else "missing"
        sdk_machines.add(machine)
        compatible = exists and machine == host_machine
        check_line(compatible, lib_path.name, f"sdk={machine}, host={host_machine}")
        failed |= not compatible

    if len(sdk_machines) == 1 and next(iter(sdk_machines)) != host_machine:
        print("      hint: normal XTP Linux SDK is x86_64; run the live image on linux/amd64")

    try:
        gateway_class = resolve_gateway_class("xtp")
        from alphapilot.systems.live.brokers.xtp_pro import SDK_AVAILABLE

        detail = f"{gateway_class.__name__}, sdk_bindings={'ok' if SDK_AVAILABLE else 'MISSING'}"
        check_line(SDK_AVAILABLE, "native xtp gateway + compiled bindings", detail)
        failed |= not SDK_AVAILABLE
    except Exception as exc:  # noqa: BLE001 - preflight should report any import/link issue
        check_line(False, "native xtp gateway + compiled bindings", f"{type(exc).__name__}: {exc}")
        failed = True

    endpoint_missing = missing_endpoint_fields(env)
    if not args.skip_network and not endpoint_missing:
        try:
            setting = build_connect_setting("xtp", env)
            endpoints = (
                ("quote", setting["行情地址"], int(setting["行情端口"])),
                ("trade", setting["交易地址"], int(setting["交易端口"])),
            )
        except Exception as exc:  # noqa: BLE001 - report malformed JSON/native setting
            check_line(False, "endpoint setting", f"{type(exc).__name__}: {exc}")
            failed = True
            endpoints = ()
        for name, host, port in endpoints:
            ok, detail = tcp_probe(host, port, args.timeout)
            check_line(ok, f"{name} endpoint {host}:{port}", detail)
            failed |= not ok
    elif endpoint_missing and not args.skip_network:
        check_line(False, "endpoint env fields", ", ".join(endpoint_missing))
        failed = True

    print("XTP PREFLIGHT:", "PASSED" if not failed else "FAILED")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

"""XTP preflight helpers."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from scripts.live_preflight_xtp import missing_endpoint_fields, read_elf_machine, tcp_probe


def write_minimal_elf(path: Path, machine: int) -> None:
    data = bytearray(20)
    data[:4] = b"\x7fELF"
    data[4] = 2   # ELF64
    data[5] = 1   # little endian
    data[18] = machine & 0xFF
    data[19] = machine >> 8
    path.write_bytes(bytes(data))


def test_read_elf_machine_x86_64(tmp_path: Path) -> None:
    lib = tmp_path / "lib.so"
    write_minimal_elf(lib, 62)
    assert read_elf_machine(lib) == "x86_64"


def test_read_elf_machine_aarch64(tmp_path: Path) -> None:
    lib = tmp_path / "lib.so"
    write_minimal_elf(lib, 183)
    assert read_elf_machine(lib) == "aarch64"


def test_read_elf_machine_unknown_for_non_elf(tmp_path: Path) -> None:
    lib = tmp_path / "lib.so"
    lib.write_bytes(b"not an elf")
    assert read_elf_machine(lib) == "unknown"


def test_missing_endpoint_fields_ignores_credentials() -> None:
    env = {
        "ALPHAPILOT_LIVE_XTP_QUOTE_HOST": "120.27.164.138",
        "ALPHAPILOT_LIVE_XTP_QUOTE_PORT": "6002",
        "ALPHAPILOT_LIVE_XTP_TRADE_HOST": "120.27.164.69",
        "ALPHAPILOT_LIVE_XTP_TRADE_PORT": "6002",
    }
    assert missing_endpoint_fields(env) == []


def test_missing_endpoint_fields_accepts_setting_json() -> None:
    env = {"ALPHAPILOT_LIVE_XTP_SETTING_JSON": '{"行情地址": "127.0.0.1"}'}
    assert missing_endpoint_fields(env) == []


def test_tcp_probe_reachable_local_port() -> None:
    ready = threading.Event()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()

        def accept_once() -> None:
            ready.set()
            conn, _addr = server.accept()
            conn.close()

        thread = threading.Thread(target=accept_once)
        thread.start()
        ready.wait(timeout=1)

        ok, detail = tcp_probe(host, port, 1)
        thread.join(timeout=1)

    assert ok
    assert detail == "reachable"

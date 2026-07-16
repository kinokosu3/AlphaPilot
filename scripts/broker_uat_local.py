#!/usr/bin/env python3
"""Local-only XTP/EMT UAT launcher with in-memory secret normalization.

The script intentionally supports only the fixed Broker UAT commands. It does
not expose an arbitrary child-command escape hatch and never prints credential
values. Secret files must be private to the current user (mode 0600).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_UAT_NOTIONAL = 20_000.0
CONFIRMATION = "I_UNDERSTAND_REAL_ORDERS"


def _require_private(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"secret file {path} must use mode 0600")


def _load_xtp(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    _require_private(path)
    values = dotenv_values(path)
    prefix = "ALPHAPILOT_LIVE_XTP_"
    return {
        str(key): str(value)
        for key, value in values.items()
        if str(key).startswith(prefix) and value not in {None, ""}
    }


def _labeled_section(path: Path, heading: str) -> dict[str, str]:
    _require_private(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, value in enumerate(lines)
        if heading.lower() in value.strip().lower()
    )
    end = next(
        (
            index for index in range(start + 1, len(lines))
            if lines[index].strip()
            and index > start + 1
            and any(marker in lines[index].strip().lower() for marker in ("fens", "sudo"))
        ),
        len(lines),
    )
    result: dict[str, str] = {}
    index = start + 1
    while index < end:
        raw = lines[index].strip()
        if not raw or "：" not in raw:
            index += 1
            continue
        key, value = (part.strip() for part in raw.split("：", 1))
        if not value:
            cursor = index + 1
            while cursor < end and not lines[cursor].strip():
                cursor += 1
            if cursor < end:
                value = lines[cursor].strip()
                index = cursor
        if key and value:
            result[key] = value
        index += 1
    return result


def _endpoint(raw: str) -> tuple[str, int]:
    value = str(raw).split("://", 1)[-1].strip()
    host, port = value.rsplit(":", 1)
    return host.strip(), int(port.strip())


def _load_emt(path: Path, *, client_id: int) -> dict[str, str]:
    values = _labeled_section(path, "emt")
    password = values.get("登入密码") or values.get("登录密码") or ""
    quote_key = next(
        (key for key in values if key.upper().startswith("L1") and "行情地址" in key),
        "",
    )
    required = {
        "测试账户": values.get("测试账户") or "",
        "登录密码": password,
        "交易地址": values.get("交易地址") or "",
        "行情账户": values.get("行情账户") or "",
        "行情密码": values.get("行情密码") or "",
        "L1行情地址": values.get(quote_key) or "",
    }
    missing = sorted(key for key, value in required.items() if not value)
    if missing:
        raise ValueError(f"EMT secret section is missing fields: {missing}")
    trade_host, trade_port = _endpoint(required["交易地址"])
    quote_host, quote_port = _endpoint(required["L1行情地址"])
    prefix = "ALPHAPILOT_LIVE_EMT_"
    return {
        f"{prefix}ACCOUNT": required["测试账户"],
        f"{prefix}PASSWORD": required["登录密码"],
        f"{prefix}CLIENT_ID": str(int(client_id)),
        f"{prefix}TRADE_HOST": trade_host,
        f"{prefix}TRADE_PORT": str(trade_port),
        f"{prefix}QUOTE_ACCOUNT": required["行情账户"],
        f"{prefix}QUOTE_PASSWORD": required["行情密码"],
        f"{prefix}QUOTE_HOST": quote_host,
        f"{prefix}QUOTE_PORT": str(quote_port),
        f"{prefix}QUOTE_PROTOCOL": "TCP",
        f"{prefix}LOG_LEVEL": "INFO",
    }


def _configure(args: argparse.Namespace) -> None:
    broker = str(args.broker).lower()
    secret_path = Path(args.secret_file).expanduser().resolve()
    values = (
        _load_xtp(secret_path)
        if broker == "xtp"
        else _load_emt(secret_path, client_id=args.client_id)
    )
    for key, value in values.items():
        os.environ[key] = value
    state = Path(args.state_dir).expanduser().resolve()
    os.environ["ALPHAPILOT_LIVE_STATE_DIR"] = str(state)
    os.environ["ALPHAPILOT_LIVE_LEDGER_DIR"] = str(state / "ledger")
    os.environ["ALPHAPILOT_LIVE_MARKET_DATA_DIR"] = str(state / "market")
    # The UAT environment is also the compatibility-observation environment.
    # Keeping both bindings identical prevents a local UAT process from
    # registering an unintended second environment in the shared runtime DB.
    os.environ["ALPHAPILOT_ENVIRONMENT_ID"] = str(args.environment)
    os.environ["ALPHAPILOT_BROKER_UAT_ENVIRONMENT"] = str(args.environment)
    os.environ["ALPHAPILOT_BROKER_UAT_MAX_NOTIONAL"] = str(args.max_notional)
    if getattr(args, "symbol", ""):
        os.environ["ALPHAPILOT_BROKER_UAT_WHITELIST"] = str(args.symbol)
    if args.command != "preflight":
        os.environ["ALPHAPILOT_BROKER_UAT_ENABLED"] = "true"


def _module() -> Any:
    from alphapilot.kernel import build_engine

    return build_engine().get_module("trading_cli")


def _bind_persisted_run_symbol(module: Any, args: argparse.Namespace) -> None:
    """Restore the immutable UAT whitelist binding for recovery commands."""

    if args.command not in {"resume", "abort"}:
        return
    run = module._system().get_broker_uat_run(str(args.run_id))
    persisted_broker = str(run.get("broker") or "").strip().lower()
    requested_broker = str(args.broker).strip().lower()
    if persisted_broker != requested_broker:
        raise ValueError(
            f"UAT run {args.run_id} belongs to broker {persisted_broker}, "
            f"not {requested_broker}"
        )
    symbol = str(run.get("symbol") or "").strip()
    if not symbol:
        raise ValueError(f"UAT run {args.run_id} has no persisted symbol binding")
    if args.symbol and str(args.symbol).strip() != symbol:
        raise ValueError("--symbol does not match the persisted UAT run symbol")
    os.environ["ALPHAPILOT_BROKER_UAT_WHITELIST"] = symbol


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "start", "resume", "abort", "status"))
    parser.add_argument("--broker", required=True, choices=("xtp", "emt"))
    parser.add_argument("--secret-file", required=True)
    parser.add_argument(
        "--state-dir",
        default=str(ROOT / "git_ignore_folder" / "acceptance" / "0.2.0"),
    )
    parser.add_argument("--environment", default="simulation-uat")
    parser.add_argument("--client-id", type=int, default=1)
    parser.add_argument("--max-notional", type=float, default=MAX_UAT_NOTIONAL)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--symbol", default="")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--volume", type=float, default=0.0)
    parser.add_argument("--price", type=float, default=0.0)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--reason", default="operator requested Broker UAT abort")
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    if not 0 < float(args.max_notional) <= MAX_UAT_NOTIONAL:
        parser.error(f"--max-notional must be in (0, {MAX_UAT_NOTIONAL:.0f}]")
    if args.command in {"start", "resume", "abort"} and args.confirmation != CONFIRMATION:
        parser.error(f"--confirmation must equal {CONFIRMATION}")
    if args.command == "start" and (
        not args.symbol or min(args.volume, args.price) <= 0
    ):
        parser.error("start requires --symbol, positive --volume and --price")
    if args.command in {"resume", "abort", "status"} and not args.run_id:
        parser.error(f"{args.command} requires --run-id")
    try:
        _configure(args)
        module = _module()
        _bind_persisted_run_symbol(module, args)
        if args.command == "preflight":
            module.trading_broker_uat_preflight(
                args.broker, args.symbols, args.max_notional, args.timeout,
            )
        elif args.command == "start":
            module.trading_broker_uat_start(
                args.broker, args.symbol, args.side, args.volume, args.price,
                args.max_notional, args.confirmation, args.timeout,
            )
        elif args.command == "resume":
            module.trading_broker_uat_resume(
                args.run_id, args.confirmation, args.timeout,
            )
        elif args.command == "abort":
            module.trading_broker_uat_abort(
                args.run_id, args.confirmation, args.reason,
            )
        else:
            module.trading_broker_uat_status(run_id=args.run_id)
    except Exception as exc:  # noqa: BLE001 - local CLI returns redacted diagnostics
        from alphapilot.systems.live.redaction import redact_secrets

        print(f"Broker UAT failed: {type(exc).__name__}: {redact_secrets(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

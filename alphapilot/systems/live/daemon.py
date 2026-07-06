"""Process helpers for a long-lived AlphaPilot live runtime daemon."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.runtime import clone_config, require_live_confirmation
from alphapilot.systems.live.targets import TargetPortfolio


def daemon_path(state_dir: str | Path | None = None) -> Path:
    root = Path(state_dir).expanduser() if state_dir else LiveConfig.load().state_dir
    return root / "runtime_daemon.json"


def commands_path(state_dir: str | Path | None = None) -> Path:
    root = Path(state_dir).expanduser() if state_dir else LiveConfig.load().state_dir
    return root / "runtime_commands.jsonl"


def load_daemon(state_dir: str | Path | None = None) -> dict[str, Any]:
    path = daemon_path(state_dir)
    if not path.exists():
        return {"exists": False, "path": str(path), "running": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - status should be best-effort
        return {"exists": True, "path": str(path), "running": False, "error": str(exc)}
    alive = pid_running(data.get("pid"))
    status = data.get("status")
    data["exists"] = True
    data["path"] = str(path)
    data["alive"] = alive
    data["starting"] = alive and status in {"starting", "connecting"}
    data["running"] = alive and status == "running"
    return data


def pid_running(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def write_daemon(state_dir: str | Path, payload: dict[str, Any]) -> Path:
    path = daemon_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**payload, "updated_at": datetime.now().isoformat(timespec="seconds")}
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def send_daemon_command(
    config: LiveConfig,
    action: str,
    *,
    payload: dict[str, Any] | None = None,
    state_dir: str | Path | None = None,
    wait: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Append one safe command for the running daemon to pick up."""
    cfg = clone_config(config, state_dir=state_dir)
    status = load_daemon(cfg.state_dir)
    if not status.get("running"):
        return {"accepted": False, "reason": "daemon is not running", "daemon": status}

    command = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": str(action),
        "payload": payload or {},
    }
    path = commands_path(cfg.state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(command, ensure_ascii=False, default=str) + "\n")

    result: dict[str, Any] = {
        "accepted": True,
        "command": command,
        "commands_path": str(path),
        "daemon": status,
    }
    if wait:
        result["daemon"] = wait_for_command(cfg, command["id"], timeout=timeout)
    return result


def wait_for_command(config: LiveConfig, command_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        status = load_daemon(config.state_dir)
        last = status.get("last_command") or {}
        if last.get("id") == command_id:
            return status
        time.sleep(0.1)
    status = load_daemon(config.state_dir)
    status["wait_timeout"] = True
    status["waited_command_id"] = command_id
    return status


def daemon_status(config: LiveConfig, *, state_dir: str | Path | None = None) -> dict[str, Any]:
    cfg = clone_config(config, state_dir=state_dir)
    status = load_daemon(cfg.state_dir)
    runtime_state = cfg.state_dir / "runtime_state.json"
    status["state_path"] = str(runtime_state)
    if runtime_state.exists():
        try:
            status["state"] = json.loads(runtime_state.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - keep daemon status available
            status["state_error"] = str(exc)
    return status


def start_daemon(
    config: LiveConfig,
    *,
    mode: str | None = None,
    broker: str | None = None,
    symbols: list[str] | None = None,
    cash: float | None = None,
    interval: float = 2.0,
    timeout: float = 20.0,
    ledger_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    duration: float | None = None,
    timing_strategy: str | None = None,
    timing_params: dict[str, Any] | None = None,
    timing_freq: str = "day",
    bar_seconds: int = 60,
    min_bars: int = 30,
    window: int = 250,
) -> dict[str, Any]:
    selected_mode = mode or config.mode
    selected_broker = broker or ("paper" if selected_mode != RunMode.LIVE else config.broker)
    cfg = clone_config(config, mode=selected_mode, broker=selected_broker, ledger_dir=ledger_dir, state_dir=state_dir)

    if timing_strategy and not symbols:
        raise ValueError("symbols are required when timing_strategy is enabled")

    current = load_daemon(cfg.state_dir)
    if current.get("running"):
        return {"started": False, **current}

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.ledger_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.state_dir / "runtime_daemon.log"
    args = [
        sys.executable,
        "-m",
        "alphapilot.systems.live.daemon",
        "run",
        "--mode",
        cfg.mode,
        "--broker",
        cfg.broker,
        "--interval",
        str(float(interval)),
        "--timeout",
        str(float(timeout)),
        "--ledger-dir",
        str(cfg.ledger_dir),
        "--state-dir",
        str(cfg.state_dir),
    ]
    if symbols:
        args.extend(["--symbols", ",".join(symbols)])
    if cash is not None:
        args.extend(["--cash", str(float(cash))])
    if duration is not None:
        args.extend(["--duration", str(float(duration))])
    if timing_strategy:
        args.extend(["--timing-strategy", timing_strategy])
        args.extend(["--timing-freq", timing_freq])
        args.extend(["--bar-seconds", str(int(bar_seconds))])
        args.extend(["--min-bars", str(int(min_bars))])
        args.extend(["--window", str(int(window))])
        if timing_params:
            args.extend(["--timing-params", json.dumps(timing_params, ensure_ascii=False)])

    with log_path.open("ab") as log:
        proc = subprocess.Popen(  # noqa: S603 - args are constructed, not shell-expanded
            args,
            cwd=Path.cwd(),
            env=os.environ.copy(),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    meta = {
        "pid": proc.pid,
        "status": "starting",
        "alive": True,
        "starting": True,
        "running": False,
        "mode": cfg.mode,
        "broker": cfg.broker,
        "symbols": symbols or [],
        "interval": float(interval),
        "timeout": float(timeout),
        "runner": _runner_config(
            timing_strategy=timing_strategy,
            timing_params=timing_params,
            timing_freq=timing_freq,
            bar_seconds=bar_seconds,
            min_bars=min_bars,
            window=window,
        ),
        "ledger_dir": str(cfg.ledger_dir),
        "state_dir": str(cfg.state_dir),
        "log_path": str(log_path),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_daemon(cfg.state_dir, meta)
    return {"started": True, "path": str(daemon_path(cfg.state_dir)), **meta}


def stop_daemon(
    config: LiveConfig,
    *,
    state_dir: str | Path | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    cfg = clone_config(config, state_dir=state_dir)
    status = load_daemon(cfg.state_dir)
    pid = status.get("pid")
    if not status.get("alive"):
        return {"stopped": False, **status}

    os.kill(int(pid), signal.SIGTERM)
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        latest = load_daemon(cfg.state_dir)
        if latest.get("status") in {"stopped", "error"} or not latest.get("alive"):
            latest["running"] = False
            latest["stopped"] = True
            return latest
        if not pid_running(pid):
            status = load_daemon(cfg.state_dir)
            status["running"] = False
            status["stopped"] = True
            return status
        time.sleep(0.1)

    return {"stopped": False, "running": True, **load_daemon(cfg.state_dir)}


def _read_new_commands(path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    if not path.exists():
        return offset, []
    size = path.stat().st_size
    if offset > size:
        offset = 0
    commands: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
                if isinstance(command, dict):
                    commands.append(command)
            except json.JSONDecodeError as exc:
                commands.append({"id": uuid.uuid4().hex, "action": "invalid", "payload": {"error": str(exc)}})
        return fh.tell(), commands


def _apply_command(runtime: Any, command: dict[str, Any]) -> dict[str, Any]:
    action = str(command.get("action") or "").lower()
    payload = command.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    result = {
        "id": command.get("id"),
        "action": action,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "ok": True,
    }
    try:
        if action == "halt":
            runtime.engine.halt(str(payload.get("reason") or "daemon command"))
            result["message"] = "halted"
        elif action == "resume":
            runtime.engine.resume()
            result["message"] = "resumed"
        elif action == "refresh":
            runtime.refresh_broker_state()
            result["message"] = "refreshed"
        elif action == "order":
            require_live_confirmation(runtime.config, confirm_live=bool(payload.get("confirm_live", False)))
            order = runtime.submit_order(
                str(payload.get("symbol") or payload.get("code") or "").strip(),
                side=str(payload.get("side") or "buy"),
                volume=float(payload["volume"]),
                price=float(payload.get("price") or 0.0),
                order_type=str(payload.get("order_type") or "limit"),
                reference=str(payload.get("reference") or "daemon"),
            )
            result.update({
                "message": "order_submitted" if order.get("submitted") else "order_not_routed",
                "submitted": order.get("submitted"),
                "order_id": order.get("order_id"),
                "request": order.get("request"),
            })
            if not order.get("submitted"):
                result["ok"] = False
        elif action == "target":
            route = bool(payload.get("route", False))
            if route:
                require_live_confirmation(runtime.config, confirm_live=bool(payload.get("confirm_live", False)))
            target = TargetPortfolio(
                date=str(payload.get("date") or "daemon"),
                holdings={str(k): float(v) for k, v in (payload.get("holdings") or {}).items()},
                prices={str(k): float(v) for k, v in (payload.get("prices") or {}).items()},
                cash=payload.get("cash"),
                source=str(payload.get("source") or "daemon"),
                market=payload.get("market"),
            )
            routed = runtime.submit_target(target, route=route)
            fully_routed = bool(routed.get("fully_routed", True))
            result.update({
                "message": (
                    "target_submitted" if route and fully_routed
                    else "target_partially_routed" if route
                    else "target_planned"
                ),
                "planned": routed.get("planned"),
                "routed": routed.get("routed"),
                "submitted": routed.get("submitted"),
                "unrouted": routed.get("unrouted"),
                "unrouted_requests": routed.get("unrouted_requests"),
                "fully_routed": routed.get("fully_routed"),
                "requests": routed.get("requests"),
            })
            if route and not fully_routed:
                result["ok"] = False
        elif action == "snapshot":
            result["message"] = "snapshotted"
        else:
            raise ValueError(f"unsupported daemon command: {action!r}")
        runtime.engine.ledger.record("daemon_command", result)
        runtime.write_state()
    except Exception as exc:  # noqa: BLE001 - command failures should not kill the daemon
        result.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        runtime.engine.ledger.record("daemon_command_error", result)
        runtime.write_state()
    return result


def _runner_config(
    *,
    timing_strategy: str | None,
    timing_params: dict[str, Any] | None,
    timing_freq: str,
    bar_seconds: int,
    min_bars: int,
    window: int,
) -> dict[str, Any]:
    return {
        "enabled": bool(timing_strategy),
        "strategy": timing_strategy,
        "params": timing_params or {},
        "freq": timing_freq,
        "bar_seconds": int(bar_seconds),
        "min_bars": int(min_bars),
        "window": int(window),
    }


def _build_timing_runner(
    engine: Any,
    symbols: list[str],
    *,
    timing_strategy: str | None,
    timing_params: dict[str, Any] | None,
    timing_freq: str,
    bar_seconds: int,
    min_bars: int,
    window: int,
) -> Any | None:
    if not timing_strategy:
        return None
    if not symbols:
        raise ValueError("symbols are required when timing_strategy is enabled")
    from alphapilot.systems.live.strategy_runner import LiveTimingRunner
    from alphapilot.systems.timing.live_adapter import BatchStrategyAdapter
    from alphapilot.systems.timing.strategies import create_strategy

    strategy = create_strategy(timing_strategy, timing_params or {})
    adapter = BatchStrategyAdapter(strategy, min_bars=min_bars, window=window)
    runner = LiveTimingRunner(
        engine,
        adapter,
        symbols,
        freq=timing_freq,
        bar_seconds=bar_seconds,
        lot_size=engine.config.risk.lot_size,
    )
    runner.start()
    return runner


def run_daemon(
    *,
    mode: str | None = None,
    broker: str | None = None,
    symbols: list[str] | None = None,
    cash: float | None = None,
    interval: float = 2.0,
    timeout: float = 20.0,
    ledger_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    duration: float | None = None,
    timing_strategy: str | None = None,
    timing_params: dict[str, Any] | None = None,
    timing_freq: str = "day",
    bar_seconds: int = 60,
    min_bars: int = 30,
    window: int = 250,
) -> int:
    from alphapilot.kernel import build_engine

    base = LiveConfig.load()
    cfg = clone_config(
        base,
        mode=mode,
        broker=broker or ("paper" if (mode or base.mode) != RunMode.LIVE else base.broker),
        ledger_dir=ledger_dir,
        state_dir=state_dir,
    )
    stop = False

    def _handle_stop(signum, frame) -> None:  # noqa: ANN001, ARG001
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    engine = build_engine()
    runtime = engine.get_system("live").create_runtime(
        mode=cfg.mode,
        broker=cfg.broker,
        ledger_dir=str(cfg.ledger_dir),
        state_dir=str(cfg.state_dir),
    )
    meta = {
        "pid": os.getpid(),
        "status": "connecting",
        "mode": cfg.mode,
        "broker": cfg.broker,
        "symbols": symbols or [],
        "commands_processed": 0,
        "runner": _runner_config(
            timing_strategy=timing_strategy,
            timing_params=timing_params,
            timing_freq=timing_freq,
            bar_seconds=bar_seconds,
            min_bars=min_bars,
            window=window,
        ),
    }

    def _write_status(**changes: Any) -> None:
        meta.update(changes)
        write_daemon(cfg.state_dir, meta)

    try:
        _write_status(status="connecting")
        runtime.connect(paper_cash=cash)
        ready = runtime.wait_ready(timeout=timeout)
        runner = _build_timing_runner(
            runtime.engine,
            symbols or [],
            timing_strategy=timing_strategy,
            timing_params=timing_params,
            timing_freq=timing_freq,
            bar_seconds=bar_seconds,
            min_bars=min_bars,
            window=window,
        )
        if symbols and runner is None:
            runtime.engine.gateway.subscribe(symbols)
        _write_status(status="running", ready=ready, started_at=datetime.now().isoformat(timespec="seconds"))
        started = time.time()
        command_path = commands_path(cfg.state_dir)
        command_offset = command_path.stat().st_size if command_path.exists() else 0
        while not stop:
            command_offset, commands = _read_new_commands(command_path, command_offset)
            for command in commands:
                result = _apply_command(runtime, command)
                meta["commands_processed"] = int(meta.get("commands_processed") or 0) + 1
                _write_status(last_command=result)
            if runner is not None:
                runner_status = runner.step()
            else:
                runtime.engine.tick_session()
                runner_status = None
            runtime.write_state()
            _write_status(
                heartbeat_at=datetime.now().isoformat(timespec="seconds"),
                runner_status=runner_status,
                halted=runtime.engine.runmode.halted,
            )
            if duration is not None and time.time() - started >= float(duration):
                break
            time.sleep(max(float(interval), 0.05))
        return 0
    except Exception as exc:  # noqa: BLE001 - preserve error in daemon status
        _write_status(status="error", error=str(exc))
        raise
    finally:
        try:
            runtime.close()
        finally:
            _write_status(status="stopped", stopped_at=datetime.now().isoformat(timespec="seconds"))
            try:
                engine.shutdown()
            except Exception:
                pass


def _split_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_json_obj(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("JSON value must be an object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AlphaPilot live runtime daemon.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode")
    run.add_argument("--broker")
    run.add_argument("--symbols")
    run.add_argument("--cash", type=float)
    run.add_argument("--interval", type=float, default=2.0)
    run.add_argument("--timeout", type=float, default=20.0)
    run.add_argument("--ledger-dir")
    run.add_argument("--state-dir")
    run.add_argument("--duration", type=float)
    run.add_argument("--timing-strategy")
    run.add_argument("--timing-params")
    run.add_argument("--timing-freq", default="day")
    run.add_argument("--bar-seconds", type=int, default=60)
    run.add_argument("--min-bars", type=int, default=30)
    run.add_argument("--window", type=int, default=250)
    ns = parser.parse_args(argv)
    if ns.command == "run":
        return run_daemon(
            mode=ns.mode,
            broker=ns.broker,
            symbols=_split_symbols(ns.symbols),
            cash=ns.cash,
            interval=ns.interval,
            timeout=ns.timeout,
            ledger_dir=ns.ledger_dir,
            state_dir=ns.state_dir,
            duration=ns.duration,
            timing_strategy=ns.timing_strategy,
            timing_params=_parse_json_obj(ns.timing_params),
            timing_freq=ns.timing_freq,
            bar_seconds=ns.bar_seconds,
            min_bars=ns.min_bars,
            window=ns.window,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

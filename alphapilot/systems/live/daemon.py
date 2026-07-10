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
from alphapilot.systems.live.targets import TargetPortfolio, parse_target_positions


def daemon_path(state_dir: str | Path | None = None) -> Path:
    root = Path(state_dir).expanduser() if state_dir else LiveConfig.load().state_dir
    return root / "runtime_daemon.json"


def commands_path(state_dir: str | Path | None = None) -> Path:
    root = Path(state_dir).expanduser() if state_dir else LiveConfig.load().state_dir
    return root / "runtime_commands.jsonl"


def command_status_path(state_dir: str | Path | None = None) -> Path:
    root = Path(state_dir).expanduser() if state_dir else LiveConfig.load().state_dir
    return root / "runtime_command_status.jsonl"


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


def record_command_status(
    state_dir: str | Path,
    command: dict[str, Any],
    *,
    stage: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one command lifecycle event for Portal/CLI/recovery inspection."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "id": command.get("id"),
        "action": command.get("action"),
        "stage": stage,
        "ok": None if result is None else bool(result.get("ok", False)),
    }
    if result is not None:
        entry["result"] = result
    path = command_status_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


def command_status_tail(state_dir: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    path = command_status_path(state_dir)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries[-int(limit):]


def command_status_entry(state_dir: str | Path, command_id: str) -> dict[str, Any] | None:
    for entry in reversed(command_status_tail(state_dir, limit=500)):
        if entry.get("id") == command_id and entry.get("stage") in {"done", "failed"}:
            return entry
    return None


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
    record_command_status(cfg.state_dir, command, stage="accepted")

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
            return daemon_status(config)
        completed = command_status_entry(config.state_dir, command_id)
        if completed is not None:
            resolved = daemon_status(config)
            resolved_last = resolved.get("last_command") if isinstance(resolved.get("last_command"), dict) else {}
            if resolved_last.get("id") != command_id and isinstance(completed.get("result"), dict):
                resolved["last_command"] = completed["result"]
            return resolved
        time.sleep(0.1)
    status = daemon_status(config)
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
    status["command_status_path"] = str(command_status_path(cfg.state_dir))
    status["command_status_tail"] = command_status_tail(cfg.state_dir)
    return status


def start_daemon(
    config: LiveConfig,
    *,
    mode: str | None = None,
    broker: str | None = None,
    trade_broker: str | None = None,
    quote_provider: str | None = None,
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
    trade_override = trade_broker or broker
    selected_broker = trade_override or ("paper" if selected_mode != RunMode.LIVE else config.trade_broker)
    selected_quote = quote_provider or (
        "paper" if selected_mode != RunMode.LIVE
        else selected_broker if trade_override
        else config.quote_provider or selected_broker
    )
    cfg = clone_config(
        config,
        mode=selected_mode,
        broker=selected_broker,
        trade_broker=selected_broker,
        quote_provider=selected_quote,
        ledger_dir=ledger_dir,
        state_dir=state_dir,
    )
    plugin_selection = None
    if cfg.mode == RunMode.LIVE:
        from alphapilot.systems.live.brokers.registry import (
            missing_quote_setting_fields,
            missing_setting_fields,
            provider_pair_metadata,
        )

        plugin_selection = provider_pair_metadata(cfg.trade_broker, cfg.quote_provider)
        unavailable = [
            row
            for row in plugin_selection.values()
            if isinstance(row, dict) and not row.get("available")
        ]
        if unavailable:
            details = "; ".join(
                f"{row.get('role')}:{row.get('name')} {row.get('availability_detail')}" for row in unavailable
            )
            raise RuntimeError(f"live provider unavailable: {details}")
        missing = [
            *missing_setting_fields(cfg.trade_broker),
            *missing_quote_setting_fields(cfg.quote_provider),
        ]
        if missing:
            raise ValueError("missing live provider env fields: " + ", ".join(sorted(set(missing))))

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
        "--trade-broker",
        cfg.trade_broker,
        "--quote-provider",
        cfg.quote_provider,
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
        "trade_broker": cfg.trade_broker,
        "quote_provider": cfg.quote_provider,
        "plugins": plugin_selection,
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


def _apply_command(
    runtime: Any,
    command: dict[str, Any],
    *,
    runner_holder: dict[str, Any] | None = None,
    default_symbols: list[str] | None = None,
) -> dict[str, Any]:
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
    record_command_status(runtime.config.state_dir, command, stage="processing")
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
        elif action == "reconnect":
            auto_resume = bool(payload.get("auto_resume", False))
            if auto_resume:
                require_live_confirmation(runtime.config, confirm_live=bool(payload.get("confirm_live", False)))
            reconnect = runtime.reconnect(auto_resume=auto_resume)
            result.update({
                "message": "reconnected",
                "auto_resume": auto_resume,
                "reconnect": reconnect.get("reconnect"),
                "recovery": reconnect.get("recovery"),
                "state": reconnect.get("state"),
            })
        elif action == "cancel":
            event_timeout = _event_timeout(payload)
            cancel = runtime.cancel_order(
                str(payload.get("order_id") or "").strip(),
                symbol=payload.get("symbol") or payload.get("code"),
                force=bool(payload.get("force", False)),
            )
            confirmation = (
                runtime.wait_for_order_terminal(str(cancel.get("order_id") or payload.get("order_id")), timeout=event_timeout)
                if cancel.get("cancelled")
                else runtime.order_state(str(cancel.get("order_id") or payload.get("order_id") or ""))
            )
            result.update({
                "message": (
                    "cancel_confirmed" if confirmation.get("status") == "cancelled"
                    else "cancel_requested" if cancel.get("cancelled")
                    else "cancel_not_sent"
                ),
                "cancelled": cancel.get("cancelled"),
                "order_id": cancel.get("order_id"),
                "cancel": cancel.get("result"),
                "cancel_confirmation": confirmation,
                "cancel_confirmed": confirmation.get("status") == "cancelled",
                "cancel_terminal": bool(confirmation.get("terminal")),
            })
            if not cancel.get("cancelled"):
                result["ok"] = False
        elif action == "order":
            event_timeout = _event_timeout(payload)
            require_live_confirmation(runtime.config, confirm_live=bool(payload.get("confirm_live", False)))
            order = runtime.submit_order(
                str(payload.get("symbol") or payload.get("code") or "").strip(),
                side=str(payload.get("side") or "buy"),
                volume=float(payload["volume"]),
                price=float(payload.get("price") or 0.0),
                order_type=str(payload.get("order_type") or "limit"),
                exchange=payload.get("exchange"),
                offset=str(payload.get("offset") or "none"),
                product=str(payload.get("product") or "equity"),
                reference=str(payload.get("reference") or "daemon"),
            )
            ack = (
                runtime.wait_for_order_ack(str(order.get("order_id")), timeout=event_timeout)
                if order.get("submitted")
                else runtime.order_state(str(order.get("order_id") or ""))
            )
            result.update({
                "message": (
                    "order_acknowledged" if ack.get("acknowledged")
                    else "order_submitted" if order.get("submitted")
                    else "order_not_routed"
                ),
                "submitted": order.get("submitted"),
                "order_id": order.get("order_id"),
                "request": order.get("request"),
                "routing_event": order.get("routing_event"),
                "routing_rule": order.get("routing_rule"),
                "routing_reason": order.get("routing_reason"),
                "order_ack": ack,
                "order_acknowledged": bool(ack.get("acknowledged")),
                "order_status": ack.get("status"),
                "order_active": ack.get("active"),
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
                positions=parse_target_positions(payload.get("positions")),
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
        elif action == "strategy_status":
            result["message"] = "strategy_status"
            result["runner_status"] = _runner_status(runner_holder)
        elif action == "strategy_pause":
            runner = _require_runner(runner_holder)
            result["message"] = "strategy_paused"
            result["runner_status"] = runner.pause()
        elif action == "strategy_resume":
            runner = _require_runner(runner_holder)
            result["message"] = "strategy_resumed"
            result["runner_status"] = runner.resume()
        elif action == "strategy_stop":
            runner = _require_runner(runner_holder)
            result["message"] = "strategy_stopped"
            result["runner_status"] = runner.stop()
            if runner_holder is not None:
                runner_holder["config"] = {**(runner_holder.get("config") or {}), "enabled": False}
        elif action == "strategy_start":
            require_live_confirmation(runtime.config, confirm_live=bool(payload.get("confirm_live", False)))
            current = None if runner_holder is None else runner_holder.get("runner")
            if current is not None and current.status().get("active"):
                raise ValueError("strategy runner is already active")
            strategy_name = str(payload.get("timing_strategy") or payload.get("strategy") or "").strip()
            if not strategy_name:
                raise ValueError("timing_strategy is required")
            symbols = _split_symbols_payload(payload.get("symbols")) or list(default_symbols or [])
            if not symbols:
                raise ValueError("symbols are required when timing_strategy is enabled")
            params = _parse_runner_params(payload.get("timing_params") or payload.get("params"))
            freq = str(payload.get("timing_freq") or payload.get("freq") or "day")
            bar_seconds = int(payload.get("bar_seconds") or 60)
            min_bars = int(payload.get("min_bars") or 30)
            window = int(payload.get("window") or 250)
            runner = _build_timing_runner(
                runtime.engine,
                symbols,
                timing_strategy=strategy_name,
                timing_params=params,
                timing_freq=freq,
                bar_seconds=bar_seconds,
                min_bars=min_bars,
                window=window,
            )
            if runner_holder is not None:
                runner_holder["runner"] = runner
                runner_holder["config"] = _runner_config(
                    timing_strategy=strategy_name,
                    timing_params=params,
                    timing_freq=freq,
                    bar_seconds=bar_seconds,
                    min_bars=min_bars,
                    window=window,
                )
            result["message"] = "strategy_started"
            result["runner_status"] = runner.status() if runner is not None else None
            result["runner_config"] = None if runner_holder is None else runner_holder.get("config")
        else:
            raise ValueError(f"unsupported daemon command: {action!r}")
        runtime.engine.ledger.record("daemon_command", result)
        runtime.write_state()
    except Exception as exc:  # noqa: BLE001 - command failures should not kill the daemon
        result.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        runtime.engine.ledger.record("daemon_command_error", result)
        runtime.write_state()
    record_command_status(
        runtime.config.state_dir,
        command,
        stage="done" if result.get("ok") else "failed",
        result=result,
    )
    return result


def _require_runner(runner_holder: dict[str, Any] | None):
    runner = None if runner_holder is None else runner_holder.get("runner")
    if runner is None:
        raise ValueError("strategy runner is not enabled")
    return runner


def _runner_status(runner_holder: dict[str, Any] | None) -> dict[str, Any]:
    runner = None if runner_holder is None else runner_holder.get("runner")
    config = {} if runner_holder is None else dict(runner_holder.get("config") or {})
    if runner is None:
        return {"enabled": False, "config": config}
    return {"enabled": True, "config": config, **runner.status()}


def _parse_runner_params(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    parsed = json.loads(str(raw))
    if not isinstance(parsed, dict):
        raise ValueError("timing_params must be a JSON object")
    return parsed


def _split_symbols_payload(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).replace("，", ",").split(",") if part.strip()]


def _event_timeout(payload: dict[str, Any], default: float = 3.0) -> float:
    raw = payload.get("event_timeout", payload.get("ack_timeout", default))
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return float(default)


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
    trade_broker: str | None = None,
    quote_provider: str | None = None,
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
    selected_mode = mode or base.mode
    trade_override = trade_broker or broker
    selected_trade = trade_override or ("paper" if selected_mode != RunMode.LIVE else base.trade_broker)
    selected_quote = quote_provider or (
        "paper" if selected_mode != RunMode.LIVE
        else selected_trade if trade_override
        else base.quote_provider or selected_trade
    )
    cfg = clone_config(
        base,
        mode=selected_mode,
        broker=selected_trade,
        trade_broker=selected_trade,
        quote_provider=selected_quote,
        ledger_dir=ledger_dir,
        state_dir=state_dir,
    )
    plugin_selection = None
    if cfg.mode == RunMode.LIVE:
        from alphapilot.systems.live.brokers.registry import provider_pair_metadata

        plugin_selection = provider_pair_metadata(cfg.trade_broker, cfg.quote_provider)
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
        trade_broker=cfg.trade_broker,
        quote_provider=cfg.quote_provider,
        ledger_dir=str(cfg.ledger_dir),
        state_dir=str(cfg.state_dir),
    )
    meta = {
        "pid": os.getpid(),
        "status": "connecting",
        "mode": cfg.mode,
        "broker": cfg.broker,
        "trade_broker": cfg.trade_broker,
        "quote_provider": cfg.quote_provider,
        "plugins": plugin_selection,
        "symbols": symbols or [],
        "commands_processed": 0,
        "recovery": None,
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
            runtime.engine.subscribe_market_data(symbols)
        runner_holder = {
            "runner": runner,
            "config": meta["runner"],
        }
        _write_status(
            status="running",
            ready=ready,
            recovery=runtime.recovery,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        started = time.time()
        command_path = commands_path(cfg.state_dir)
        command_offset = command_path.stat().st_size if command_path.exists() else 0
        while not stop:
            command_offset, commands = _read_new_commands(command_path, command_offset)
            for command in commands:
                result = _apply_command(
                    runtime,
                    command,
                    runner_holder=runner_holder,
                    default_symbols=symbols or [],
                )
                runner = runner_holder.get("runner")
                meta["runner"] = runner_holder.get("config") or meta.get("runner")
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
    run.add_argument("--trade-broker")
    run.add_argument("--quote-provider")
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
            trade_broker=ns.trade_broker,
            quote_provider=ns.quote_provider,
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

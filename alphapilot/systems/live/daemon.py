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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alphapilot.systems.live.config import LiveConfig, RunMode, uses_real_providers
from alphapilot.systems.live.market_data import market_snapshot_path
from alphapilot.systems.live.runtime import clone_config, require_live_confirmation
from alphapilot.systems.live.state_io import atomic_write_json
from alphapilot.systems.live.targets import TargetPortfolio, parse_target_positions
from alphapilot.systems.trading.ports import RouteContext, RouteOrigin


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
        stat = Path(f"/proc/{value}/stat").read_text(encoding="utf-8")
        marker = stat.rfind(")")
        if marker >= 0 and stat[marker + 2 : marker + 3] == "Z":
            return False
    except OSError:
        pass
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
    atomic_write_json(path, merged)
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
    status["market_snapshot_path"] = str(market_snapshot_path(cfg.state_dir))
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
    interval: float = 1.0,
    timeout: float = 20.0,
    ledger_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    duration: float | None = None,
    timing_strategy: str | None = None,
    strategy_instance_id: str | None = None,
    timing_params: dict[str, Any] | None = None,
    timing_freq: str = "day",
    bar_seconds: int = 60,
    min_bars: int = 30,
    window: int = 250,
    record_market_data: bool | None = None,
    runtime_id: str | None = None,
) -> dict[str, Any]:
    selected_mode = mode or config.mode
    trade_override = trade_broker or broker
    real_providers = uses_real_providers(selected_mode)
    selected_broker = trade_override or (config.trade_broker if real_providers else "paper")
    selected_quote = quote_provider or (
        "paper" if not real_providers
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
    if uses_real_providers(cfg.mode):
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

    symbols = _normalize_symbols(symbols or [])
    if (timing_strategy or strategy_instance_id) and not symbols:
        raise ValueError("symbols are required when a strategy runner is enabled")

    current = load_daemon(cfg.state_dir)
    if current.get("alive") and current.get("status") in {"starting", "connecting", "running"}:
        return {"started": False, **current}

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.ledger_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.state_dir / "runtime_daemon.log"
    runtime_identifier = str(runtime_id or uuid.uuid4().hex)
    deprecation_warning = (
        "timing_strategy is deprecated for daemon deployment; use strategy_instance_id"
        if timing_strategy and not strategy_instance_id else ""
    )
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
        "--runtime-id",
        runtime_identifier,
    ]
    if symbols:
        args.extend(["--symbols", ",".join(symbols)])
    if record_market_data is not None:
        args.append("--record-market-data" if record_market_data else "--no-record-market-data")
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
    if strategy_instance_id:
        args.extend(["--strategy-instance-id", str(strategy_instance_id)])

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
        "runtime_id": runtime_identifier,
        "deprecation_warning": deprecation_warning,
        "alive": True,
        "starting": True,
        "running": False,
        "mode": cfg.mode,
        "broker": cfg.broker,
        "trade_broker": cfg.trade_broker,
        "quote_provider": cfg.quote_provider,
        "plugins": plugin_selection,
        "symbols": symbols or [],
        "record_market_data": cfg.market_data.enabled if record_market_data is None else bool(record_market_data),
        "interval": float(interval),
        "timeout": float(timeout),
        "runner": _runner_config(
            timing_strategy=timing_strategy,
            timing_params=timing_params,
            timing_freq=timing_freq,
            bar_seconds=bar_seconds,
            min_bars=min_bars,
            window=window,
            instance_id=strategy_instance_id,
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
        return {**status, "running": False, "stopped": True}

    os.kill(int(pid), signal.SIGTERM)
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        latest = load_daemon(cfg.state_dir)
        if not latest.get("alive"):
            return {**latest, "running": False, "stopped": True}
        time.sleep(0.1)

    latest = load_daemon(cfg.state_dir)
    return {**latest, "stopped": not bool(latest.get("alive"))}


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
                route_context=RouteContext.manual(),
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
                decision_id=str(payload.get("decision_id") or ""),
                instance_id=str(payload.get("instance_id") or "legacy"),
                as_of=payload.get("as_of"),
                effective_session=payload.get("effective_session"),
                valid_until=payload.get("valid_until"),
                config_hash=str(payload.get("config_hash") or ""),
                data_version=str(payload.get("data_version") or ""),
                model_version=str(payload.get("model_version") or ""),
                target_weights={str(k): float(v) for k, v in (payload.get("target_weights") or {}).items()},
                price_source=str(payload.get("price_source") or ""),
            )
            routed = runtime.submit_target(
                target,
                route=route,
                route_context=RouteContext(
                    origin=RouteOrigin.MANUAL,
                    instance_id=target.instance_id,
                ),
            )
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
        elif action == "model_target":
            route = bool(payload.get("route", False))
            if route:
                require_live_confirmation(runtime.config, confirm_live=bool(payload.get("confirm_live", False)))
            kernel_engine = None if runner_holder is None else runner_holder.get("kernel_engine")
            if kernel_engine is None:
                raise RuntimeError("kernel context is required for model target planning")
            from alphapilot.modules.daily_trade.module import _parse_yaml_params
            from alphapilot.systems.backtest.live import DailySignalRequest, generate_daily_signal
            from alphapilot.systems.backtest.live.service import to_target_portfolio
            from alphapilot.systems.backtest.live.types import PortfolioState
            if payload.get("model_pickle_path"):
                from alphapilot.systems.trading.security import verify_trusted_model

                verify_trusted_model(payload["model_pickle_path"], extra_roots=[runtime.config.state_dir])

            account = runtime.engine.oms.account
            if account is None:
                raise RuntimeError("account snapshot is required before model target planning")
            seed = PortfolioState(
                date=str(payload.get("date") or ""),
                cash=float(account.available),
                positions={
                    position.key: float(position.volume)
                    for position in runtime.engine.oms.get_positions() if position.volume > 0
                },
                metadata={"source": "live_daemon_oms", "account_id": str(account.account_id)},
            )
            daily = generate_daily_signal(
                kernel_engine.context,
                DailySignalRequest(
                    strategy_name=payload.get("strategy_name"),
                    session=payload.get("session"),
                    factor_path=payload.get("factor_path"),
                    model_pickle_path=payload.get("model_pickle_path"),
                    yaml_params=_parse_yaml_params(payload.get("yaml_params")),
                    date=payload.get("date"),
                    refresh_data=bool(payload.get("refresh_data", False)),
                    use_local=kernel_engine.context.config.backtest.use_local,
                ),
                seed_state=seed,
                persist_state=False,
            )
            target = to_target_portfolio(daily, source=payload.get("source"))
            routed = runtime.submit_target(
                target,
                route=route,
                route_context=RouteContext(
                    origin=RouteOrigin.MANUAL,
                    instance_id=target.instance_id,
                ),
            )
            result.update({
                "message": "model_target_submitted" if route else "model_target_planned",
                "target": routed.get("target"),
                "planned": routed.get("planned"),
                "routed": routed.get("routed"),
                "submitted": routed.get("submitted"),
                "unrouted": routed.get("unrouted"),
                "fully_routed": routed.get("fully_routed"),
                "issues": routed.get("issues"),
                "requests": routed.get("requests"),
            })
            if route and not routed.get("fully_routed", False):
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
            confirmations, cancel_errors = _confirm_runner_cancellations(
                runtime,
                result["runner_status"],
                timeout=_event_timeout(payload),
            )
            result["cancel_confirmations"] = confirmations
            if cancel_errors:
                result.update({
                    "ok": False,
                    "error": "one or more strategy orders could not be cancelled",
                    "cancel_errors": cancel_errors,
                })
        elif action == "strategy_reconcile":
            runner = _require_runner(runner_holder)
            runner.pause()
            recovery = runtime.recover()
            result["message"] = "strategy_reconciled"
            result["recovery"] = recovery
            result["runner_status"] = runner.mark_reconciled(recovery)
        elif action == "strategy_resume":
            require_live_confirmation(runtime.config, confirm_live=bool(payload.get("confirm_live", False)))
            runner = _require_runner(runner_holder)
            result["message"] = "strategy_resumed"
            result["runner_status"] = runner.resume()
            if runtime.engine.runmode.halted:
                runtime.engine.resume()
        elif action == "strategy_stop":
            runner = _require_runner(runner_holder)
            result["message"] = "strategy_stopped"
            result["runner_status"] = runner.stop()
            confirmations, cancel_errors = _confirm_runner_cancellations(
                runtime,
                result["runner_status"],
                timeout=_event_timeout(payload),
            )
            result["cancel_confirmations"] = confirmations
            if cancel_errors:
                result.update({
                    "ok": False,
                    "error": "one or more strategy orders could not be cancelled",
                    "cancel_errors": cancel_errors,
                })
            if runner_holder is not None:
                runner_holder["config"] = {**(runner_holder.get("config") or {}), "enabled": False}
        elif action == "strategy_start":
            require_live_confirmation(runtime.config, confirm_live=bool(payload.get("confirm_live", False)))
            current = None if runner_holder is None else runner_holder.get("runner")
            if current is not None and current.status().get("active"):
                raise ValueError("strategy runner is already active")
            strategy_instance_id = str(payload.get("instance_id") or "").strip() or None
            strategy_name = str(payload.get("timing_strategy") or payload.get("strategy") or "").strip()
            if not strategy_name and not strategy_instance_id:
                raise ValueError("timing_strategy or instance_id is required")
            symbols = _normalize_symbols(
                _split_symbols_payload(payload.get("symbols")) or list(default_symbols or [])
            )
            if not symbols:
                raise ValueError("symbols are required when timing_strategy is enabled")
            allowed_symbols = set(_normalize_symbols(default_symbols or []))
            unexpected = sorted(set(symbols) - allowed_symbols)
            if unexpected:
                raise ValueError(
                    "strategy symbols must be subscribed when daemon starts: "
                    + ", ".join(unexpected)
                )
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
                kernel_engine=None if runner_holder is None else runner_holder.get("kernel_engine"),
                state_dir=runtime.config.state_dir,
                strategy_instance_id=strategy_instance_id,
                bar_source=None if runner_holder is None else runner_holder.get("bar_source"),
                runtime=runtime,
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
                    instance_id=strategy_instance_id,
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


def _confirm_runner_cancellations(
    runtime: Any,
    runner_status: dict[str, Any],
    *,
    timeout: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cancel = runner_status.get("cancel_report") if isinstance(runner_status, dict) else {}
    errors = list((cancel or {}).get("errors") or [])
    confirmations: list[dict[str, Any]] = []
    for order_id in (cancel or {}).get("attempted") or []:
        confirmation = runtime.wait_for_order_terminal(str(order_id), timeout=timeout)
        confirmations.append(confirmation)
        if not confirmation.get("terminal"):
            errors.append({
                "order_id": str(order_id),
                "reason": "cancel was not confirmed terminal before timeout",
            })
    return confirmations, errors


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
    instance_id: str | None = None,
) -> dict[str, Any]:
    return {
        "enabled": bool(timing_strategy or instance_id),
        "strategy": timing_strategy,
        "params": timing_params or {},
        "freq": timing_freq,
        "bar_seconds": int(bar_seconds),
        "min_bars": int(min_bars),
        "window": int(window),
        "instance_id": instance_id,
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
    kernel_engine: Any | None = None,
    state_dir: str | Path | None = None,
    strategy_instance_id: str | None = None,
    bar_source: Any | None = None,
    runtime: Any | None = None,
) -> Any | None:
    if not timing_strategy and not strategy_instance_id:
        return None
    from alphapilot.systems.live.strategy_runner import LiveTimingRunner
    from alphapilot.systems.timing.live_adapter import BatchStrategyAdapter
    from alphapilot.systems.trading.domain import StrategyInstanceConfig

    definition = None
    stored_instance = None
    trading = None
    if strategy_instance_id:
        if kernel_engine is None or not kernel_engine.has_system("trading"):
            raise RuntimeError("trading strategy system is required for instance deployment")
        trading = kernel_engine.get_system("trading")
        if runtime is not None and runtime.execution_journal is not trading.store:
            raise RuntimeError(
                "formal strategy instances must use the configured deployment state directory"
            )
        stored_instance = trading.store.get_instance(strategy_instance_id)
        validation = trading.validate_instance(strategy_instance_id)
        if not validation.get("ok"):
            raise ValueError("; ".join(validation.get("errors") or []))
        expected_level = {
            RunMode.PAPER: "paper",
            RunMode.SHADOW: "shadow",
            RunMode.LIVE: "live",
        }.get(engine.config.mode)
        if expected_level and stored_instance["deployment_level"] != expected_level:
            raise ValueError(
                f"strategy instance must be promoted to {expected_level.upper()} "
                f"before running in {engine.config.mode}"
            )
        config = stored_instance["config"]
        timing_strategy = str(config["strategy_id"])
        timing_params = dict(config.get("params") or {})
        timing_freq = str(config.get("frequency") or timing_freq)
        symbols = list(config.get("universe") or symbols)
        definition = trading.registry.get(timing_strategy)
        if runtime is not None and bar_source is not None:
            from alphapilot.systems.live.instance_runner import StrategyInstanceRunner
            from alphapilot.systems.trading.data_adapters import TimingHistoricalDataAdapter

            instance = StrategyInstanceConfig.from_dict(config)
            runner = StrategyInstanceRunner(
                runtime=runtime,
                trading=trading,
                instance=instance,
                historical_data=TimingHistoricalDataAdapter(
                    kernel_engine.get_system("timing"),
                    data_dir=str(instance.data_policy.get("data_dir") or "") or None,
                ),
                bar_source=bar_source,
                bar_seconds=bar_seconds,
            )
            runner.start()
            return runner
        strategy = trading.registry.create(timing_strategy, timing_params)
    elif kernel_engine is not None and kernel_engine.has_system("trading"):
        if engine.config.mode in {RunMode.LIVE, RunMode.SHADOW}:
            raise ValueError("legacy strategy_name routing is disabled in LIVE; use a promoted instance_id")
        trading = kernel_engine.get_system("trading")
        trading.store.record_legacy_usage(
            "daemon --timing-strategy",
            {
                "strategy": timing_strategy,
                "frequency": timing_freq,
                "mode": str(engine.config.mode),
            },
        )
        definition = trading.registry.get(timing_strategy)
        strategy = trading.registry.create(timing_strategy, timing_params or {})
    else:
        from alphapilot.systems.timing.strategies import create_strategy

        strategy = create_strategy(timing_strategy, timing_params or {})
    from alphapilot.systems.trading.registry import resolve_required_history

    required_history = resolve_required_history(
        definition,
        dict(timing_params or {}),
        fallback=int(min_bars or 1),
    )
    if not symbols:
        raise ValueError("symbols are required when timing strategy is enabled")
    instance = (
        StrategyInstanceConfig.from_dict(stored_instance["config"])
        if stored_instance is not None
        else StrategyInstanceConfig(
            instance_id=f"legacy-{timing_strategy}-{timing_freq}",
            strategy_id=str(timing_strategy),
            strategy_version=str(getattr(definition, "version", "legacy")),
            params=dict(timing_params or {}),
            universe=tuple(symbols),
            frequency=timing_freq,
            strategy_code_hash=str(getattr(definition, "code_hash", "") or ""),
        )
    )
    target_pct = float(instance.params.get("target_percent", 1.0) or 0.0)
    if target_pct > float(engine.config.risk.max_position_pct) + 1e-9:
        raise ValueError(
            f"target_percent {target_pct:.1%} exceeds automated max_position_pct "
            f"{engine.config.risk.max_position_pct:.1%}"
        )
    legacy_paper_adapter = False
    if (
        stored_instance is None
        and trading is not None
        and runtime is not None
        and engine.config.mode == RunMode.PAPER
    ):
        stored_instance = _prepare_legacy_paper_instance(trading, instance, runtime)
        instance = StrategyInstanceConfig.from_dict(stored_instance["config"])
        legacy_paper_adapter = True
    route_port = (
        runtime.automated_order_router(
            instance_id=instance.instance_id,
            config_hash=instance.config_hash,
            deployment_level=(
                stored_instance["deployment_level"]
                if stored_instance is not None else engine.config.mode
            ),
        )
        if runtime is not None else None
    )
    adapter = BatchStrategyAdapter(strategy, min_bars=required_history, window=max(window, required_history))
    checkpoint = (
        Path(state_dir).expanduser() / "strategy_instances" / instance.instance_id / "runner.json"
        if state_dir is not None else None
    )
    restored = False
    if checkpoint is not None and checkpoint.is_file():
        try:
            import json

            checkpoint_state = json.loads(checkpoint.read_text(encoding="utf-8"))
            if checkpoint_state.get("config_hash") == instance.config_hash:
                restored = True
        except (OSError, ValueError, TypeError):
            checkpoint_state = None
    if not restored:
        _warm_timing_adapter(
            kernel_engine, adapter, symbols, freq=timing_freq,
            bar_seconds=bar_seconds, window=max(window, required_history), engine=engine,
        )
    runner = LiveTimingRunner(
        engine,
        adapter,
        symbols,
        freq=timing_freq,
        bar_seconds=bar_seconds,
        lot_size=engine.config.risk.lot_size,
        instance_id=instance.instance_id,
        config_hash=instance.config_hash,
        state_path=checkpoint,
        bar_source=bar_source,
        execution_journal=(
            getattr(runtime, "execution_journal", None)
            if runtime is not None else
            getattr(trading, "store", None) if trading is not None else None
        ),
        route_port=route_port,
        runtime_id=(getattr(runtime, "runtime_id", "") if runtime is not None else ""),
    )
    if restored and checkpoint_state is not None:
        runner.restore(checkpoint_state, require_reconcile=engine.config.mode == RunMode.LIVE)
    runner.start()
    if legacy_paper_adapter and runtime is not None:
        journal = runtime.execution_journal
        if journal.get_active_stage_run(instance.instance_id, stage="paper") is None:
            journal.start_stage_run(instance.instance_id, "paper")
        journal.set_route_block("instance", instance.instance_id, active=False)
    if engine.config.mode == RunMode.LIVE:
        runner.mark_reconcile_required()
    return runner


def _prepare_legacy_paper_instance(trading: Any, config: Any, runtime: Any) -> dict[str, Any]:
    """Adapt the deprecated strategy-name daemon path to a persisted PAPER instance."""

    if runtime.route_authorizer is None:
        raise RuntimeError("legacy PAPER automation requires a bound route authorizer")
    store = runtime.execution_journal
    required_store_methods = {
        "get_instance", "create_instance", "update_instance", "record_stage",
        "promote", "transition_runtime", "set_route_block", "start_stage_run",
    }
    if any(not callable(getattr(store, name, None)) for name in required_store_methods):
        raise RuntimeError("legacy PAPER automation requires a persistent execution journal")
    definition = trading.registry.get(config.strategy_id)
    if config.strategy_version != definition.version:
        raise ValueError("legacy strategy version does not match the registered definition")
    if not definition.code_hash or config.strategy_code_hash != definition.code_hash:
        raise ValueError("legacy strategy code hash does not match the trusted definition")
    try:
        current = store.get_instance(config.instance_id)
    except KeyError:
        current = store.create_instance(config)
    else:
        if current["strategy_id"] != config.strategy_id:
            raise ValueError(f"reserved legacy instance id {config.instance_id!r} is already in use")
        if current["config_hash"] != config.config_hash:
            current = store.update_instance(
                config.instance_id,
                {
                    "strategy_version": config.strategy_version,
                    "params": config.params,
                    "universe": list(config.universe),
                    "frequency": config.frequency,
                    "data_policy": config.data_policy,
                    "portfolio_policy": config.portfolio_policy,
                    "strategy_code_hash": config.strategy_code_hash,
                    "model_hash": config.model_hash,
                },
            )
    current = store.get_instance(config.instance_id)
    if current["deployment_level"] == "replay":
        store.record_stage(
            config.instance_id,
            "replay",
            passed=True,
            details={"source": "deprecated_paper_strategy_adapter"},
        )
        current = store.promote(config.instance_id, "paper")
    elif current["deployment_level"] != "paper":
        raise ValueError("legacy strategy-name automation is restricted to PAPER deployment")

    account = runtime.engine.oms.account
    if account is None or not str(account.account_id):
        raise RuntimeError("PAPER account snapshot is not ready")
    store.transition_runtime(
        config.instance_id,
        lifecycle="warming_up",
        desired_state="running",
        observed_state="warming_up",
        account_id=str(account.account_id),
        broker=str(runtime.config.trade_broker or runtime.config.broker or "paper"),
        runtime_id=runtime.runtime_id,
        runner_heartbeat_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        reconcile_required=False,
        binding_active=False,
        last_error={},
    )
    store.set_route_block(
        "instance",
        config.instance_id,
        active=True,
        reason="legacy PAPER runner is awaiting start confirmation",
    )
    return store.get_instance(config.instance_id)


def _warm_timing_adapter(
    kernel_engine: Any | None,
    adapter: Any,
    symbols: list[str],
    *,
    freq: str,
    bar_seconds: int,
    window: int,
    engine: Any,
) -> None:
    """Best-effort preload from the canonical local history stores."""
    try:
        import pandas as pd

        live_daily = None
        if freq == "day":
            from alphapilot.systems.live.bars import DAY_INTERVAL
            from alphapilot.systems.live.market_data import load_market_bars

            rows: list[dict[str, Any]] = []
            provider = engine.config.quote_provider or engine.config.trade_broker or "quote"
            for symbol in symbols:
                rows.extend(load_market_bars(
                    engine.config.market_data.data_dir, provider, symbol, DAY_INTERVAL, limit=window
                ))
            if rows:
                live_daily = pd.DataFrame(rows).rename(columns={"date": "datetime"})
        if freq == "day" and kernel_engine is not None:
            bars = kernel_engine.get_system("timing").load_bars(
                symbols=symbols, freq="day", adjust_mode="backward"
            )
            if live_daily is not None:
                bars = pd.concat([bars, live_daily], ignore_index=True)
                bars = bars.drop_duplicates(["datetime", "instrument"], keep="last")
            adapter.warm_up(bars.groupby("instrument", group_keys=False).tail(window))
            return
        if freq == "day" and live_daily is not None:
            adapter.warm_up(live_daily)
            return
        if freq == "min" and int(bar_seconds) in (60, 300):
            from alphapilot.systems.live.market_data import load_market_bars

            rows: list[dict[str, Any]] = []
            provider = engine.config.quote_provider or engine.config.trade_broker or "quote"
            for symbol in symbols:
                rows.extend(load_market_bars(
                    engine.config.market_data.data_dir, provider, symbol, int(bar_seconds), limit=window
                ))
            if rows:
                frame = pd.DataFrame(rows).rename(columns={"date": "datetime"})
                adapter.warm_up(frame)
    except Exception:  # noqa: BLE001 - not ready is represented as WARMING_UP
        return


def run_daemon(
    *,
    mode: str | None = None,
    broker: str | None = None,
    trade_broker: str | None = None,
    quote_provider: str | None = None,
    symbols: list[str] | None = None,
    cash: float | None = None,
    interval: float = 1.0,
    timeout: float = 20.0,
    ledger_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    duration: float | None = None,
    timing_strategy: str | None = None,
    strategy_instance_id: str | None = None,
    timing_params: dict[str, Any] | None = None,
    timing_freq: str = "day",
    bar_seconds: int = 60,
    min_bars: int = 30,
    window: int = 250,
    record_market_data: bool | None = None,
    runtime_id: str | None = None,
) -> int:
    from alphapilot.kernel import build_engine

    base = LiveConfig.load()
    selected_mode = mode or base.mode
    trade_override = trade_broker or broker
    real_providers = uses_real_providers(selected_mode)
    selected_trade = trade_override or (base.trade_broker if real_providers else "paper")
    selected_quote = quote_provider or (
        "paper" if not real_providers
        else selected_trade if trade_override
        else base.quote_provider or selected_trade
    )
    symbols = _normalize_symbols(symbols or [])
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
    if uses_real_providers(cfg.mode):
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
        runtime_id=runtime_id,
        require_exchange_calendar=bool(strategy_instance_id),
    )
    meta = {
        "pid": os.getpid(),
        "status": "connecting",
        "runtime_id": runtime.runtime_id,
        "deprecation_warning": (
            "timing_strategy is deprecated for daemon deployment; use strategy_instance_id"
            if timing_strategy and not strategy_instance_id else ""
        ),
        "mode": cfg.mode,
        "broker": cfg.broker,
        "trade_broker": cfg.trade_broker,
        "quote_provider": cfg.quote_provider,
        "plugins": plugin_selection,
        "symbols": symbols or [],
        "record_market_data": cfg.market_data.enabled if record_market_data is None else bool(record_market_data),
        "commands_processed": 0,
        "recovery": None,
        "runner": _runner_config(
            timing_strategy=timing_strategy,
            timing_params=timing_params,
            timing_freq=timing_freq,
            bar_seconds=bar_seconds,
            min_bars=min_bars,
            window=window,
            instance_id=strategy_instance_id,
        ),
    }

    def _write_status(**changes: Any) -> None:
        meta.update(changes)
        write_daemon(cfg.state_dir, meta)

    try:
        _write_status(status="connecting")
        if symbols:
            runtime.enable_market_data(symbols, recording=record_market_data)
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
            kernel_engine=engine,
            state_dir=cfg.state_dir,
            strategy_instance_id=strategy_instance_id,
            bar_source=runtime.market_data,
            runtime=runtime,
        )
        if symbols and runner is None:
            runtime.engine.subscribe_market_data(symbols)
        runner_holder = {
            "runner": runner,
            "config": meta["runner"],
            "kernel_engine": engine,
            "bar_source": runtime.market_data,
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
            if runtime.market_data is not None:
                runtime.market_data.step(runtime.engine.session.state)
                runtime.market_data.write_snapshot()
            runtime.write_state()
            _write_status(
                heartbeat_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                runner_status=runner_status,
                halted=runtime.engine.runmode.halted,
            )
            if duration is not None and time.time() - started >= float(duration):
                break
            cadence = float(interval)
            if runtime.market_data is not None:
                cadence = min(cadence, float(cfg.market_data.snapshot_interval))
            time.sleep(max(cadence, 0.05))
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


def _normalize_symbols(symbols: list[str]) -> list[str]:
    from alphapilot.systems.live.types import normalize_symbol

    output: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        code, exchange = normalize_symbol(raw)
        key = f"{code}.{exchange.value}"
        if key not in seen:
            seen.add(key)
            output.append(key)
    return output


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
    run.add_argument("--interval", type=float, default=1.0)
    run.add_argument("--timeout", type=float, default=20.0)
    run.add_argument("--ledger-dir")
    run.add_argument("--state-dir")
    run.add_argument("--runtime-id")
    run.add_argument("--duration", type=float)
    run.add_argument("--timing-strategy")
    run.add_argument("--strategy-instance-id")
    run.add_argument("--timing-params")
    run.add_argument("--timing-freq", default="day")
    run.add_argument("--bar-seconds", type=int, default=60)
    run.add_argument("--min-bars", type=int, default=30)
    run.add_argument("--window", type=int, default=250)
    run.add_argument("--record-market-data", action=argparse.BooleanOptionalAction, default=None)
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
            runtime_id=ns.runtime_id,
            duration=ns.duration,
            timing_strategy=ns.timing_strategy,
            strategy_instance_id=ns.strategy_instance_id,
            timing_params=_parse_json_obj(ns.timing_params),
            timing_freq=ns.timing_freq,
            bar_seconds=ns.bar_seconds,
            min_bars=ns.min_bars,
            window=ns.window,
            record_market_data=ns.record_market_data,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

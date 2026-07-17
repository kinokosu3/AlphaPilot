"""CLI module for the live-trading subsystem.

The commands are a thin control plane over ``LiveRuntime``: preflight, one-shot
connect/run, explicit order/target routing, and long-lived daemon controls.
Paper/dry-run is the default mode.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphapilot.kernel.base import BaseModule

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context


class LiveModule(BaseModule):
    """CLI commands for inspecting the live-trading configuration."""

    name = "live"

    def setup(self, context: "Context") -> None:
        self.context = context

    def _system(self):
        return self.context.system("live")

    def _runtime(
        self,
        *,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        ledger_dir: str | None = None,
        state_dir: str | None = None,
    ):
        return self._system().create_runtime(
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            ledger_dir=ledger_dir,
            state_dir=state_dir,
        )

    def live_status(self) -> dict[str, Any]:
        """Show the resolved live config (mode, broker, risk limits) — no secrets."""
        return self._system().snapshot()

    def live_modes(self) -> list[str]:
        """List modes (SHADOW uses real data/account but cannot route)."""
        return self._system().modes()

    def live_brokers(self) -> list[dict[str, Any]]:
        """List registered brokers: gateway, env fields for credentials, availability."""
        from alphapilot.systems.live.brokers.registry import (
            ENV_PREFIX,
            list_brokers,
            missing_setting_fields,
            provider_availability,
        )

        rows = []
        for spec in list_brokers():
            prefix = f"{ENV_PREFIX}{spec.name.upper()}_"
            available, detail = provider_availability(spec.name, role="trade")
            rows.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "gateway": spec.gateway_path,
                    "gateway_importable": available,
                    "availability_detail": detail,
                    "env_fields": [prefix + f.env_suffix for f in spec.setting_fields],
                    "missing_env": missing_setting_fields(spec.name),
                    "plugin_id": spec.plugin_id,
                    "distribution": spec.distribution,
                    "version": spec.version,
                    "roles": sorted(spec.roles),
                    "capabilities": _capabilities_dict(spec.capabilities),
                }
            )
        return rows

    def live_quote_providers(self) -> list[dict[str, Any]]:
        """List registered quote providers without exposing secret values."""
        from alphapilot.systems.live.brokers.registry import (
            ENV_PREFIX,
            list_quote_providers,
            missing_quote_setting_fields,
            provider_availability,
        )

        rows = []
        for spec in list_quote_providers():
            prefix = f"{ENV_PREFIX}{spec.name.upper()}_"
            available, detail = provider_availability(spec.name, role="quote")
            rows.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "gateway": spec.gateway_path,
                    "gateway_importable": available,
                    "availability_detail": detail,
                    "env_fields": [prefix + f.env_suffix for f in spec.setting_fields],
                    "missing_env": missing_quote_setting_fields(spec.name),
                    "plugin_id": spec.plugin_id,
                    "distribution": spec.distribution,
                    "version": spec.version,
                    "roles": sorted(spec.roles),
                    "capabilities": _capabilities_dict(spec.capabilities),
                }
            )
        return rows

    def live_plugins(self) -> dict[str, Any]:
        """Show installed live plugin versions and isolated discovery failures."""
        from alphapilot.systems.live.brokers.registry import plugin_diagnostics

        return plugin_diagnostics()

    def live_risk_status(
        self,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        state_dir: str | None = None,
        ledger_dir: str | None = None,
        tail: int = 20,
    ) -> dict[str, Any]:
        """Show persisted risk/recovery status without connecting to a broker."""
        runtime = self._runtime(
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            state_dir=state_dir,
            ledger_dir=ledger_dir,
        )
        status = self.live_state(
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            state_dir=state_dir,
        )
        state = status.get("state") if status.get("exists") else runtime.snapshot()
        engine = state.get("engine") if isinstance(state, dict) else {}
        return {
            "exists": bool(status.get("exists")),
            "state_path": str(runtime.state_path),
            "ledger_dir": str(runtime.config.ledger_dir),
            "config": state.get("config") if isinstance(state, dict) else runtime.snapshot()["config"],
            "risk": (engine or {}).get("risk"),
            "recovery": state.get("recovery") if isinstance(state, dict) else None,
            "recent_rejections": runtime.engine.ledger.tail(int(tail), kind="rejected"),
        }

    def live_ledger_events(
        self,
        kind: str | None = None,
        command_id: str | None = None,
        order_id: str | None = None,
        reference: str | None = None,
        day: str | None = None,
        limit: int = 50,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        ledger_dir: str | None = None,
        state_dir: str | None = None,
    ) -> dict[str, Any]:
        """Query live audit ledger events by common correlation fields."""
        runtime = self._runtime(
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            ledger_dir=ledger_dir,
            state_dir=state_dir,
        )
        events = runtime.engine.ledger.events(
            kind=kind,
            command_id=command_id,
            order_id=order_id,
            reference=reference,
            day=day,
            limit=int(limit),
        )
        return {
            "ledger_dir": str(runtime.config.ledger_dir),
            "count": len(events),
            "events": events,
        }

    def live_state(
        self,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        state_dir: str | None = None,
    ) -> dict[str, Any]:
        """Read the last persisted live runtime state without connecting."""
        runtime = self._runtime(mode=mode, broker=broker, trade_broker=trade_broker,
                                quote_provider=quote_provider, state_dir=state_dir)
        path = runtime.state_path
        if not path.exists():
            return {"exists": False, "state_path": str(path), "config": runtime.snapshot()["config"]}
        return {"exists": True, "state_path": str(path), "state": json.loads(path.read_text(encoding="utf-8"))}

    def live_connect(
        self,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        cash: float | None = None,
        timeout: float = 20.0,
        ledger_dir: str | None = None,
        state_dir: str | None = None,
    ) -> dict[str, Any]:
        """Connect once, wait for readiness, persist state, then close.

        Use this as the AlphaPilot-native smoke path. In LIVE mode credentials
        are still read only from environment variables.
        """
        runtime = self._runtime(
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            ledger_dir=ledger_dir,
            state_dir=state_dir,
        )
        try:
            state = runtime.connect(paper_cash=cash)
            ready = runtime.wait_ready(timeout=timeout)
            return {"ready": ready, "state": state if not ready else runtime.snapshot()}
        finally:
            runtime.close()

    def live_preflight(
        self,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        network: bool = False,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        """Check broker registry, env fields and optional TCP endpoint reachability."""
        cfg = self._system().config
        trade_override = trade_broker or broker
        selected_trade = trade_override or cfg.trade_broker or cfg.broker
        selected_quote = quote_provider or (selected_trade if trade_override else cfg.quote_provider or selected_trade)
        trade = _preflight_provider(selected_trade, kind="trade", network=network, timeout=timeout)
        quote = _preflight_provider(selected_quote, kind="quote", network=network, timeout=timeout)
        endpoints = [*trade["endpoints"], *quote["endpoints"]]
        result: dict[str, Any] = {
            "broker": selected_trade,
            "trade_broker": selected_trade,
            "quote_provider": selected_quote,
            "description": trade.get("description"),
            "gateway": trade.get("gateway"),
            "gateway_importable": trade.get("gateway_importable"),
            "missing_env": trade.get("missing_env", []),
            "network_checked": bool(network),
            "endpoints": endpoints,
            "trade": trade,
            "quote": quote,
        }
        result["ok"] = bool(trade.get("ok")) and bool(quote.get("ok"))
        return result

    def live_run(
        self,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        symbols: str | list[str] | None = None,
        cash: float | None = None,
        interval: float = 2.0,
        duration: float | None = None,
        timeout: float = 20.0,
        ledger_dir: str | None = None,
        state_dir: str | None = None,
    ) -> dict[str, Any]:
        """Run a connected live runtime loop and keep writing state snapshots.

        Omit ``duration`` for a foreground daemon. This command does not route
        orders by itself; strategy/target routing is done by explicit commands.
        """
        runtime = self._runtime(
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            ledger_dir=ledger_dir,
            state_dir=state_dir,
        )
        try:
            runtime.connect(paper_cash=cash)
            ready = runtime.wait_ready(timeout=timeout)
            loop = runtime.run_loop(
                symbols=_split_symbols(symbols),
                interval=interval,
                duration=duration,
            )
            return {"ready": ready, **loop}
        finally:
            runtime.close()

    def live_daemon_status(
        self,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        state_dir: str | None = None,
    ) -> dict[str, Any]:
        """Show the long-lived live runtime daemon status and latest state."""
        from alphapilot.systems.live.daemon import daemon_status
        from alphapilot.systems.live.runtime import clone_config

        cfg = clone_config(
            self._system().config,
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            state_dir=state_dir,
        )
        return daemon_status(cfg, state_dir=state_dir)

    def live_daemon_start(
        self,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        symbols: str | list[str] | None = None,
        cash: float | None = None,
        interval: float = 1.0,
        timeout: float = 20.0,
        ledger_dir: str | None = None,
        state_dir: str | None = None,
        duration: float | None = None,
        record_market_data: bool | None = None,
    ) -> dict[str, Any]:
        """Start a detached runtime without attaching an automated strategy.

        Formal strategy instances are started exclusively through the trading
        deployment coordinator. ``duration`` is mainly for smoke tests; omit it
        for a persistent operations daemon.
        """
        from alphapilot.systems.live.daemon import start_daemon

        return start_daemon(
            self._system().config,
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            symbols=_split_symbols(symbols),
            cash=cash,
            interval=interval,
            timeout=timeout,
            ledger_dir=ledger_dir,
            state_dir=state_dir,
            duration=duration,
            record_market_data=record_market_data,
        )

    def live_market_snapshot(
        self,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        state_dir: str | None = None,
        symbols: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Read the daemon's latest quote projection without touching a gateway."""
        from alphapilot.systems.live.daemon import load_daemon
        from alphapilot.systems.live.market_data import read_market_snapshot, refresh_snapshot_ages
        from alphapilot.systems.live.runtime import clone_config

        cfg = clone_config(
            self._system().config,
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            state_dir=state_dir,
        )
        daemon = load_daemon(cfg.state_dir)
        snapshot = refresh_snapshot_ages(
            read_market_snapshot(cfg.state_dir),
            symbols=_split_symbols(symbols),
            stale_after_seconds=cfg.market_data.stale_after_seconds,
        )
        daemon_running = bool(daemon.get("running"))
        daemon_status = str(daemon.get("status") or "stopped")
        if not daemon_running and daemon_status in {"running", "starting"}:
            daemon_status = "stopped"
        snapshot["daemon_running"] = daemon_running
        snapshot["daemon_status"] = daemon_status
        if not daemon.get("exists") and not snapshot.get("exists"):
            snapshot["exists"] = False
        return snapshot

    def live_market_bars(
        self,
        symbol: str,
        interval: int = 60,
        limit: int = 300,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        state_dir: str | None = None,
    ) -> dict[str, Any]:
        """Read recent persisted and current live bars for one subscribed symbol."""
        from alphapilot.systems.live.market_data import load_market_bars, read_market_snapshot
        from alphapilot.systems.live.runtime import clone_config
        from alphapilot.systems.live.types import normalize_symbol

        cfg = clone_config(
            self._system().config,
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            state_dir=state_dir,
        )
        code, exchange = normalize_symbol(symbol)
        key = f"{code}.{exchange.value}"
        snapshot = read_market_snapshot(cfg.state_dir)
        provider = str(snapshot.get("quote_provider") or cfg.quote_provider or "quote")
        rows = load_market_bars(
            cfg.market_data.data_dir,
            provider,
            key,
            int(interval),
            limit=max(1, min(int(limit), 2000)),
            current=snapshot.get("current_bars") if isinstance(snapshot, dict) else None,
        )
        return {
            "symbol": key,
            "label": key,
            "interval": int(interval),
            "date_range": [rows[0]["date"], rows[-1]["date"]] if rows else [],
            "rows": rows,
        }

    def live_daemon_stop(
        self,
        state_dir: str | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Stop the detached live runtime daemon if one is running."""
        from alphapilot.systems.live.daemon import stop_daemon

        return stop_daemon(self._system().config, state_dir=state_dir, timeout=timeout)

    def live_daemon_halt(
        self,
        reason: str = "manual",
        state_dir: str | None = None,
        wait: bool = False,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Ask the running daemon to engage the in-process kill-switch."""
        from alphapilot.systems.live.daemon import send_daemon_command

        return send_daemon_command(
            self._system().config,
            "halt",
            payload={"reason": reason},
            state_dir=state_dir,
            wait=wait,
            timeout=timeout,
        )

    def live_daemon_resume(
        self,
        state_dir: str | None = None,
        wait: bool = False,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Ask the running daemon to resume after a kill-switch halt."""
        from alphapilot.systems.live.daemon import send_daemon_command

        return send_daemon_command(
            self._system().config,
            "resume",
            state_dir=state_dir,
            wait=wait,
            timeout=timeout,
        )

    def live_daemon_refresh(
        self,
        state_dir: str | None = None,
        wait: bool = False,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Ask the running daemon to re-query broker account and positions."""
        from alphapilot.systems.live.daemon import send_daemon_command

        return send_daemon_command(
            self._system().config,
            "refresh",
            state_dir=state_dir,
            wait=wait,
            timeout=timeout,
        )

    def live_daemon_reconnect(
        self,
        state_dir: str | None = None,
        wait: bool = False,
        timeout: float = 20.0,
        auto_resume: bool = False,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Ask the running daemon to reconnect and reconcile broker state.

        Conservative by default: reconnect keeps the kill-switch engaged. Only
        pass ``auto_resume=True`` after explicit operator confirmation.
        """
        from alphapilot.systems.live.daemon import send_daemon_command

        if auto_resume:
            _require_daemon_live_confirmation(self.live_daemon_status(state_dir=state_dir), confirm_live=confirm_live)
        return send_daemon_command(
            self._system().config,
            "reconnect",
            payload={
                "auto_resume": bool(auto_resume),
                "confirm_live": bool(confirm_live),
            },
            state_dir=state_dir,
            wait=wait,
            timeout=timeout,
        )

    def live_daemon_cancel(
        self,
        order_id: str,
        symbol: str | None = None,
        force: bool = False,
        state_dir: str | None = None,
        wait: bool = False,
        timeout: float = 5.0,
        event_timeout: float = 3.0,
    ) -> dict[str, Any]:
        """Ask the running daemon to cancel one active order.

        By default only active OMS orders are cancellable. ``force=True`` sends a
        raw broker cancel and requires ``symbol`` when the OMS does not know the
        order.
        """
        from alphapilot.systems.live.daemon import send_daemon_command

        return send_daemon_command(
            self._system().config,
            "cancel",
            payload={
                "order_id": order_id,
                "symbol": symbol,
                "force": bool(force),
                "event_timeout": float(event_timeout),
            },
            state_dir=state_dir,
            wait=wait,
            timeout=timeout,
        )

    def live_daemon_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        price: float = 0.0,
        order_type: str = "limit",
        exchange: str | None = None,
        offset: str = "none",
        product: str = "equity",
        state_dir: str | None = None,
        wait: bool = False,
        timeout: float = 5.0,
        event_timeout: float = 3.0,
        confirm_live: bool = False,
        reference: str = "daemon_manual",
    ) -> dict[str, Any]:
        """Ask the running daemon to submit one manual/debug order."""
        from alphapilot.systems.live.daemon import send_daemon_command

        _require_daemon_live_confirmation(self.live_daemon_status(state_dir=state_dir), confirm_live=confirm_live)
        return send_daemon_command(
            self._system().config,
            "order",
            payload={
                "symbol": symbol,
                "side": side,
                "volume": float(volume),
                "price": float(price),
                "order_type": order_type,
                "exchange": exchange,
                "offset": offset,
                "product": product,
                "reference": reference,
                "confirm_live": confirm_live,
                "event_timeout": float(event_timeout),
            },
            state_dir=state_dir,
            wait=wait,
            timeout=timeout,
        )

    def live_daemon_submit_target(
        self,
        target_path: str | None = None,
        holdings: str | dict | None = None,
        prices: str | dict | None = None,
        positions: list[dict[str, Any]] | None = None,
        date: str | None = None,
        source: str | None = None,
        session: str | None = None,
        strategy_name: str | None = None,
        factor_path: str | None = None,
        model_pickle_path: str | None = None,
        yaml_params: str | None = None,
        refresh_data: bool = False,
        route: bool = False,
        state_dir: str | None = None,
        wait: bool = False,
        timeout: float = 5.0,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Ask the running daemon to plan or route a target portfolio."""
        from alphapilot.systems.live.daemon import send_daemon_command
        from alphapilot.systems.live.runtime import target_to_dict

        if route:
            _require_daemon_live_confirmation(self.live_daemon_status(state_dir=state_dir), confirm_live=confirm_live)
        model_source = bool(session or strategy_name or factor_path or model_pickle_path)
        if model_source and target_path is None and holdings is None and positions is None:
            action = "model_target"
            payload = {
                "date": date,
                "source": source,
                "session": session,
                "strategy_name": strategy_name,
                "factor_path": factor_path,
                "model_pickle_path": model_pickle_path,
                "yaml_params": yaml_params,
                "refresh_data": bool(refresh_data),
                "route": bool(route),
                "confirm_live": bool(confirm_live),
            }
        else:
            action = "target"
            target = self._load_target(
                target_path=target_path,
                holdings=holdings,
                prices=prices,
                positions=positions,
                date=date,
                source=source,
                session=session,
                strategy_name=strategy_name,
                factor_path=factor_path,
                model_pickle_path=model_pickle_path,
                yaml_params=yaml_params,
                refresh_data=refresh_data,
            )
            payload = {
                **target_to_dict(target),
                "route": bool(route),
                "confirm_live": bool(confirm_live),
            }
        return send_daemon_command(
            self._system().config,
            action,
            payload=payload,
            state_dir=state_dir,
            wait=wait,
            timeout=timeout,
        )

    def live_order(
        self,
        symbol: str,
        side: str,
        volume: float,
        price: float = 0.0,
        order_type: str = "limit",
        exchange: str | None = None,
        offset: str = "none",
        product: str = "equity",
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        cash: float | None = None,
        timeout: float = 20.0,
        event_timeout: float = 3.0,
        ledger_dir: str | None = None,
        state_dir: str | None = None,
        confirm_live: bool = False,
        reference: str = "manual",
    ) -> dict[str, Any]:
        """Submit one manual/debug order through LiveEngine and RiskGate.

        Defaults to the configured mode (normally dry_run). LIVE mode requires
        ``confirm_live=True``.
        """
        from alphapilot.systems.live.runtime import require_live_confirmation

        runtime = self._runtime(
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            ledger_dir=ledger_dir,
            state_dir=state_dir,
        )
        require_live_confirmation(runtime.config, confirm_live=confirm_live)
        try:
            runtime.connect(paper_cash=cash)
            runtime.wait_ready(timeout=timeout)
            result = runtime.submit_order(
                symbol,
                side=side,
                volume=volume,
                price=price,
                order_type=order_type,
                exchange=exchange,
                offset=offset,
                product=product,
                reference=reference,
            )
            if result.get("submitted"):
                ack = runtime.wait_for_order_ack(str(result.get("order_id")), timeout=event_timeout)
                result.update({
                    "order_ack": ack,
                    "order_acknowledged": bool(ack.get("acknowledged")),
                    "order_status": ack.get("status"),
                    "order_active": ack.get("active"),
                })
            return result
        finally:
            runtime.close()

    def live_cancel(
        self,
        order_id: str,
        symbol: str | None = None,
        force: bool = False,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        cash: float | None = None,
        timeout: float = 20.0,
        event_timeout: float = 3.0,
        ledger_dir: str | None = None,
        state_dir: str | None = None,
    ) -> dict[str, Any]:
        """Connect once and cancel one order.

        One-shot cancel can only cancel OMS-known active orders unless
        ``force=True`` and ``symbol`` are supplied for a raw broker cancel.
        """
        runtime = self._runtime(
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            ledger_dir=ledger_dir,
            state_dir=state_dir,
        )
        try:
            runtime.connect(paper_cash=cash)
            runtime.wait_ready(timeout=timeout)
            result = runtime.cancel_order(order_id, symbol=symbol, force=force)
            confirmation = (
                runtime.wait_for_order_terminal(str(result.get("order_id")), timeout=event_timeout)
                if result.get("cancelled")
                else runtime.order_state(str(result.get("order_id") or order_id))
            )
            result.update({
                "cancel_confirmation": confirmation,
                "cancel_confirmed": confirmation.get("status") == "cancelled",
                "cancel_terminal": bool(confirmation.get("terminal")),
            })
            return result
        finally:
            runtime.close()

    def live_submit_target(
        self,
        target_path: str | None = None,
        holdings: str | dict | None = None,
        prices: str | dict | None = None,
        positions: list[dict[str, Any]] | None = None,
        date: str | None = None,
        source: str | None = None,
        session: str | None = None,
        strategy_name: str | None = None,
        factor_path: str | None = None,
        model_pickle_path: str | None = None,
        yaml_params: str | None = None,
        refresh_data: bool = False,
        route: bool = False,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        cash: float | None = None,
        timeout: float = 20.0,
        ledger_dir: str | None = None,
        state_dir: str | None = None,
        confirm_live: bool = False,
    ) -> dict[str, Any]:
        """Plan or route a target portfolio through the live executor.

        Target source precedence: ``target_path`` JSON > inline ``holdings`` >
        ``daily_signals`` generated from ``session``/``strategy_name``. By default
        this only plans orders; pass ``route=True`` to submit them.
        """
        from alphapilot.systems.live.runtime import require_live_confirmation

        runtime = self._runtime(
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            ledger_dir=ledger_dir,
            state_dir=state_dir,
        )
        if route:
            require_live_confirmation(runtime.config, confirm_live=confirm_live)
        try:
            runtime.connect(paper_cash=cash)
            if not runtime.wait_ready(timeout=timeout):
                raise RuntimeError("live runtime did not become ready before target planning")
            target = self._load_target(
                target_path=target_path,
                holdings=holdings,
                prices=prices,
                positions=positions,
                date=date,
                source=source,
                session=session,
                strategy_name=strategy_name,
                factor_path=factor_path,
                model_pickle_path=model_pickle_path,
                yaml_params=yaml_params,
                refresh_data=refresh_data,
                seed_state=_portfolio_seed_from_oms(runtime.engine.oms, date=date),
            )
            return runtime.submit_target(target, route=route)
        finally:
            runtime.close()

    def _load_target(
        self,
        *,
        target_path: str | None,
        holdings: str | dict | None,
        prices: str | dict | None,
        positions: list[dict[str, Any]] | None,
        date: str | None,
        source: str | None,
        session: str | None,
        strategy_name: str | None,
        factor_path: str | None,
        model_pickle_path: str | None,
        yaml_params: str | None,
        refresh_data: bool,
        seed_state: Any | None = None,
    ):
        from alphapilot.systems.live.targets import TargetPortfolio, parse_target_positions

        if target_path:
            data = json.loads(Path(target_path).expanduser().read_text(encoding="utf-8"))
            return TargetPortfolio(
                date=str(data.get("date") or date or "target"),
                holdings={str(k): float(v) for k, v in (data.get("holdings") or {}).items()},
                prices={str(k): float(v) for k, v in (data.get("prices") or {}).items()},
                cash=data.get("cash"),
                source=str(data.get("source") or source or "target_file"),
                market=data.get("market"),
                positions=parse_target_positions(data.get("positions")),
                decision_id=str(data.get("decision_id") or ""),
                instance_id=str(data.get("instance_id") or "legacy"),
                as_of=data.get("as_of"),
                effective_session=data.get("effective_session"),
                valid_until=data.get("valid_until"),
                config_hash=str(data.get("config_hash") or ""),
                data_version=str(data.get("data_version") or ""),
                model_version=str(data.get("model_version") or ""),
                target_weights={str(k): float(v) for k, v in (data.get("target_weights") or {}).items()},
                price_source=str(data.get("price_source") or ""),
            )
        if holdings is not None or positions is not None:
            return TargetPortfolio(
                date=str(date or "inline"),
                holdings={str(k): float(v) for k, v in _parse_mapping(holdings).items()},
                prices={str(k): float(v) for k, v in _parse_mapping(prices).items()},
                source=source or "inline",
                positions=parse_target_positions(positions),
            )

        from alphapilot.modules.daily_trade.module import _parse_yaml_params
        from alphapilot.systems.backtest.live import DailySignalRequest, generate_daily_signal
        from alphapilot.systems.backtest.live.service import to_target_portfolio

        if not (session or strategy_name or factor_path or model_pickle_path):
            raise ValueError("provide target_path, holdings, session, strategy_name, factor_path or model_pickle_path")
        if model_pickle_path:
            from alphapilot.systems.trading.security import verify_trusted_model

            verify_trusted_model(model_pickle_path)
        result = generate_daily_signal(
            self.context,
            DailySignalRequest(
                strategy_name=strategy_name,
                session=session,
                factor_path=factor_path,
                model_pickle_path=model_pickle_path,
                yaml_params=_parse_yaml_params(yaml_params),
                date=date,
                refresh_data=refresh_data,
                use_local=self.context.config.backtest.use_local,
            ),
            seed_state=seed_state,
            persist_state=False,
        )
        return to_target_portfolio(result, source=source)

    def commands(self) -> dict[str, Callable[..., Any]]:
        return {
            "live_status": self.live_status,
            "live_modes": self.live_modes,
            "live_brokers": self.live_brokers,
            "live_quote_providers": self.live_quote_providers,
            "live_plugins": self.live_plugins,
            "live_risk_status": self.live_risk_status,
            "live_ledger_events": self.live_ledger_events,
            "live_state": self.live_state,
            "live_connect": self.live_connect,
            "live_preflight": self.live_preflight,
            "live_run": self.live_run,
            "live_daemon_status": self.live_daemon_status,
            "live_daemon_start": self.live_daemon_start,
            "live_market_snapshot": self.live_market_snapshot,
            "live_market_bars": self.live_market_bars,
            "live_daemon_stop": self.live_daemon_stop,
            "live_daemon_halt": self.live_daemon_halt,
            "live_daemon_resume": self.live_daemon_resume,
            "live_daemon_refresh": self.live_daemon_refresh,
            "live_daemon_reconnect": self.live_daemon_reconnect,
            "live_daemon_cancel": self.live_daemon_cancel,
            "live_daemon_order": self.live_daemon_order,
            "live_daemon_submit_target": self.live_daemon_submit_target,
            "live_order": self.live_order,
            "live_cancel": self.live_cancel,
            "live_submit_target": self.live_submit_target,
        }


def _parse_mapping(raw: str | dict | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    path = Path(text).expanduser()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(text)


def _portfolio_seed_from_oms(oms: Any, *, date: str | None = None):
    """Build the qlib daily-planner seed from broker/OMS truth."""
    from alphapilot.systems.backtest.live.types import PortfolioState

    account = oms.account
    if account is None:
        raise RuntimeError("account snapshot is required before model target planning")
    positions = {
        str(position.key): float(position.volume)
        for position in oms.get_positions()
        if float(position.volume) > 0
    }
    return PortfolioState(
        date=str(date or ""),
        cash=float(account.available),
        positions=positions,
        metadata={"source": "live_oms", "account_id": str(account.account_id)},
    )


def _split_symbols(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _tcp_probe(host: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, int(port)), timeout=float(timeout)):
            return True, "reachable"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _capabilities_dict(capabilities: Any) -> dict[str, Any]:
    return {
        "asset_classes": list(capabilities.asset_classes),
        "exchanges": list(capabilities.exchanges),
        "supports_tick": capabilities.supports_tick,
        "supports_depth": capabilities.supports_depth,
        "supports_contract_query": capabilities.supports_contract_query,
        "supports_account_query": capabilities.supports_account_query,
        "supports_position_query": capabilities.supports_position_query,
        "supports_order_query": capabilities.supports_order_query,
        "supports_trade_query": capabilities.supports_trade_query,
        "supports_cancel": capabilities.supports_cancel,
        "supports_margin": capabilities.supports_margin,
        "supports_history": capabilities.supports_history,
    }


def _preflight_provider(name: str, *, kind: str, network: bool, timeout: float) -> dict[str, Any]:
    if name == "paper":
        return {
            "name": "paper",
            "broker": "paper",
            "description": "In-process PaperBroker sandbox",
            "gateway": "alphapilot.systems.live.brokers.paper:PaperBroker",
            "gateway_importable": True,
            "missing_env": [],
            "network_checked": False,
            "endpoints": [],
            "ok": True,
        }

    from alphapilot.systems.live.brokers.registry import (
        build_connect_setting,
        build_quote_connect_setting,
        get_broker,
        get_quote_provider,
        missing_quote_setting_fields,
        missing_setting_fields,
        provider_availability,
    )

    if kind == "quote":
        spec = get_quote_provider(name)
        missing = missing_quote_setting_fields(name)
        importable, availability_detail = provider_availability(name, role="quote")
        build_setting = build_quote_connect_setting
    else:
        spec = get_broker(name)
        missing = missing_setting_fields(name)
        importable, availability_detail = provider_availability(name, role="trade")
        build_setting = build_connect_setting

    result: dict[str, Any] = {
        "name": name,
        "broker": name,
        "description": spec.description,
        "gateway": spec.gateway_path,
        "gateway_importable": importable,
        "availability_detail": availability_detail,
        "plugin_id": spec.plugin_id,
        "distribution": spec.distribution,
        "version": spec.version,
        "missing_env": missing,
        "network_checked": False,
        "endpoints": [],
    }
    if network and not missing:
        setting = build_setting(name)
        endpoints = []
        for endpoint in spec.endpoints:
            host = setting.get(endpoint.host_key)
            port = setting.get(endpoint.port_key)
            if host and port:
                ok, detail = _tcp_probe(str(host), int(port), timeout)
                endpoints.append({"name": endpoint.name, "host": host, "port": int(port), "ok": ok, "detail": detail})
        result["network_checked"] = True
        result["endpoints"] = endpoints
    result["ok"] = importable and not missing and all(item.get("ok", True) for item in result["endpoints"])
    return result


def _require_daemon_live_confirmation(status: dict[str, Any], *, confirm_live: bool) -> None:
    mode = status.get("mode") or (status.get("state") or {}).get("config", {}).get("mode")
    if mode == "live" and not confirm_live:
        raise ValueError("LIVE daemon route requires confirm_live=True")

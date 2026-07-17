"""LiveEngine — the event-loop hub of the live-trading subsystem.

Ownership:
* installs itself as the gateway's callback sink, fanning every normalized event
  into the :class:`OMS` (state) and the :class:`Ledger` (audit);
* owns the safety FSMs (:class:`RunModeMachine`, :class:`ConnectionMachine`,
  :class:`SessionClock`);
* exposes the single guarded path for acting on the market — :meth:`submit` /
  :meth:`cancel` / :meth:`halt` / :meth:`reconcile_after_reconnect`.

The engine is broker-agnostic: it talks only to the :class:`BrokerGateway` port,
so PAPER (PaperBroker), SIM (SimBroker) and LIVE (native XTP Pro / EMT gateways)
all run the same logic. The clock is injected so a whole trading day can be
simulated in tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from alphapilot.systems.live.config import LiveConfig, RunMode, requires_live_market_safety
from alphapilot.systems.live.events import LiveEvent, LiveEventBus
from alphapilot.systems.live.fsm.connection_fsm import ConnectionMachine, ConnectionState
from alphapilot.systems.live.fsm.runmode_fsm import RunModeMachine
from alphapilot.systems.live.fsm.session_fsm import SessionClock
from alphapilot.systems.live.gateway import BrokerGateway, QuoteGateway
from alphapilot.systems.live.ledger import Ledger
from alphapilot.systems.live.redaction import redact_secrets
from alphapilot.systems.live.oms import OMS
from alphapilot.systems.live.types import (
    Account,
    CancelRequest,
    Contract,
    LogEvent,
    Order,
    OrderRequest,
    Position,
    TickData,
    Trade,
    normalize_symbol,
)


class LiveEngine:
    """Coordinates gateway, OMS, audit ledger and the safety state machines."""

    def __init__(
        self,
        config: LiveConfig,
        gateway: BrokerGateway,
        *,
        quote_gateway: QuoteGateway | None = None,
        now_fn: Callable[[], Any] | None = None,
        is_trading_day_fn: Callable[[Any], bool] | None = None,
        ledger: Ledger | None = None,
        event_bus: LiveEventBus | None = None,
        risk: Any = None,
    ) -> None:
        self.config = config
        self.trade_gateway = gateway
        self.quote_gateway = quote_gateway or gateway
        # Backward-compatible alias used by older runtime/daemon/tests.
        self.gateway = self.trade_gateway
        self.oms = OMS()
        quote_kind = str(getattr(config, "quote_data_kind", "") or "realtime")
        route_enabled = quote_kind == "realtime" or config.mode == RunMode.PAPER
        self.runmode = RunModeMachine(
            config.mode,
            provider_routing_enabled=route_enabled,
            provider_block_reason=(
                "quote provider is not realtime" if not route_enabled else ""
            ),
        )
        self.connection = ConnectionMachine()
        calendar = is_trading_day_fn
        if calendar is None and requires_live_market_safety(config.mode):
            # Direct engine/runtime construction must not restore the historical
            # "every date is tradable" fallback for a real account.
            calendar = lambda _dt: False
        self.session = SessionClock(now_fn or datetime.now, calendar)
        self.ledger = ledger or Ledger(config.ledger_dir)
        self.events = event_bus or LiveEventBus()
        self._event_now = getattr(self.ledger, "_now_fn", datetime.now)
        self.risk = risk  # installed in Phase 3; None => no pre-trade checks
        self._tick_listeners: list[Callable[[TickData], None]] = []
        self._subscribed_symbols: set[str] = set()
        self.trade_gateway.register_callback(self)
        if self.quote_gateway is not self.trade_gateway:
            self.quote_gateway.register_callback(self)

    def add_tick_listener(self, listener: Callable[[TickData], None]) -> None:
        """Attach a tick consumer (e.g. a strategy runner's bar aggregator).

        Listeners run synchronously after the OMS update, on the same thread that
        delivers gateway events — keep them fast and non-blocking.
        """
        self._tick_listeners.append(listener)

    def remove_tick_listener(self, listener: Callable[[TickData], None]) -> None:
        """Detach a previously registered tick consumer."""
        if listener in self._tick_listeners:
            self._tick_listeners.remove(listener)

    # ---- GatewayCallback (fan-out to OMS + ledger) ----------------------- #
    def on_order(self, order: Order) -> None:
        self.oms.on_order(order)
        self._audit("order", order, order_id=order.order_id, reference=order.reference, source=_source(order, self))

    def on_trade(self, trade: Trade) -> None:
        self.oms.on_trade(trade)
        self._audit("trade", trade, order_id=trade.order_id, source=_source(trade, self))

    def on_position(self, position: Position) -> None:
        self.oms.on_position(position)
        self._publish("position", position, source=_source(position, self))

    def on_account(self, account: Account) -> None:
        self.oms.on_account(account)
        self._publish("account", account, source=_source(account, self))
        if self.risk is not None and hasattr(self.risk, "check_equity"):
            verdict = self.risk.check_equity(self.oms)
            if not verdict.ok and verdict.rule in {"daily_loss", "canary_loss"}:
                if not self.runmode.halted:
                    self.halt(f"risk:{verdict.rule}:{verdict.reason}")

    def on_contract(self, contract: Contract) -> None:
        self.oms.on_contract(contract)
        self._publish("contract", contract, source=_source(contract, self))

    def on_tick(self, tick: TickData) -> None:
        if tick.received_at is None:
            tick.received_at = self._event_now()
        self.oms.on_tick(tick)
        self._publish("tick", tick, source=_source(tick, self))
        for listener in self._tick_listeners:
            listener(tick)

    def on_log(self, log: LogEvent) -> None:
        self.oms.on_log(log)
        self._publish("log", log, source=_source(log, self))

    def on_gateway_connected(self, gateway: str, channel: str, detail: str = "") -> None:
        self._audit(
            "gateway_connected",
            {"gateway": gateway, "channel": channel, "detail": detail},
            source=gateway or self.gateway.name,
        )

    def on_gateway_disconnected(
        self,
        gateway: str,
        channel: str,
        reason: str = "",
        *,
        halt: bool = True,
    ) -> None:
        if halt:
            self.handle_disconnect(f"{channel}:{reason}" if reason else channel)
        self._audit(
            "gateway_disconnected",
            {
                "gateway": gateway,
                "channel": channel,
                "reason": reason,
                "halt": bool(halt),
            },
            source=gateway or self.gateway.name,
        )

    # ---- lifecycle ------------------------------------------------------- #
    def _connect_gateways(self, setting: dict | None) -> None:
        trade_setting, quote_setting = _split_gateway_settings(setting)
        if self.quote_gateway is self.trade_gateway:
            roles = getattr(self.trade_gateway, "roles", frozenset())
            shared_setting = (
                {"trade": trade_setting, "quote": quote_setting}
                if {"trade", "quote"} <= set(roles)
                and isinstance(setting, dict)
                and ("trade" in setting or "quote" in setting)
                else trade_setting
            )
            self.trade_gateway.connect(shared_setting)
        else:
            self.trade_gateway.connect(trade_setting)
            self.quote_gateway.connect(quote_setting)

    def connect(self, setting: dict | None = None) -> None:
        self.connection.transition(ConnectionState.CONNECTING)
        try:
            self._connect_gateways(setting)
        except Exception as exc:  # noqa: BLE001 - surface as connection error
            self.connection.transition(ConnectionState.ERROR)
            self._audit(
                "connect_error", {"error": redact_secrets(str(exc))},
                source=self.trade_gateway.name,
            )
            raise
        self.connection.transition(ConnectionState.CONNECTED)
        self.connection.transition(ConnectionState.LOGGED_IN)
        self._audit(
            "connected",
            {
                "broker": self.trade_gateway.name,
                "trade_broker": self.trade_gateway.name,
                "quote_provider": self.quote_gateway.name,
                "mode": self.config.mode,
            },
            source=self.trade_gateway.name,
        )

    def close(self) -> None:
        errors: list[str] = []
        for gateway in _unique_gateways(self.trade_gateway, self.quote_gateway):
            try:
                gateway.close()
            except Exception as exc:  # noqa: BLE001 - close every channel best-effort
                errors.append(redact_secrets(
                    f"{getattr(gateway, 'name', 'gateway')}: {exc}"
                ))
        if self.connection.state != ConnectionState.DISCONNECTED:
            self.connection.transition(ConnectionState.DISCONNECTED)
        payload: dict[str, Any] = {
            "broker": self.trade_gateway.name,
            "trade_broker": self.trade_gateway.name,
            "quote_provider": self.quote_gateway.name,
        }
        if errors:
            payload["errors"] = errors
        self._audit("closed", payload, source=self.trade_gateway.name)

    # ---- guarded actions ------------------------------------------------- #
    def submit(self, req: OrderRequest, *, origin: str = "manual") -> str | None:
        """The single guarded submission path.

        Returns the broker order id, or ``None`` when the order was not routed
        (dry-run, halted, or rejected by the risk gate — all audited).
        """
        if self.runmode.is_dry_run():
            self._audit("dry_run_intent", {"origin": origin, "req": _req(req)}, reference=req.reference)
            return None
        if not self.runmode.can_submit_orders():
            reason = (
                f"halted:{self.runmode.halt_reason}"
                if self.runmode.halted
                else f"routing_disabled_in_{self.runmode.mode}"
                if self.runmode.mode == RunMode.SHADOW
                else self.runmode.provider_block_reason
                if not self.runmode.provider_routing_enabled
                else f"routing_disabled_in_{self.runmode.mode}"
            )
            self._audit("blocked", {"origin": origin, "rule": "run_mode", "reason": reason, "req": _req(req)}, reference=req.reference)
            return None
        if self.risk is not None:
            verdict = self.risk.check(req, self.oms, self.session, self.runmode)
            if not verdict.ok:
                self._audit(
                    "rejected",
                    {"origin": origin, "rule": verdict.rule, "reason": verdict.reason, "req": _req(req)},
                    reference=req.reference,
                )
                if verdict.rule in {"daily_loss", "canary_loss"}:
                    self.halt(verdict.reason)
                return None
        order_id = self.trade_gateway.send_order(req)
        self._audit(
            "submit",
            {"origin": origin, "order_id": order_id, "req": _req(req)},
            order_id=order_id,
            reference=req.reference,
        )
        return order_id

    def cancel(self, order: Order | str, *, active_only: bool = True) -> dict[str, Any]:
        if isinstance(order, str):
            found = self.oms.get_order(order)
            if found is None:
                self._audit("cancel_miss", {"order_id": order}, order_id=order)
                return {"cancelled": False, "order_id": order, "reason": "not_found"}
            order = found
        if active_only and not order.is_active():
            self._audit(
                "cancel_skipped",
                {"order_id": order.order_id, "reason": "not_active", "status": order.status.value},
                order_id=order.order_id,
                reference=order.reference,
            )
            return {"cancelled": False, "order_id": order.order_id, "reason": "not_active"}
        self.trade_gateway.cancel_order(order.create_cancel())
        self._audit("cancel", {"order_id": order.order_id}, order_id=order.order_id, reference=order.reference)
        return {"cancelled": True, "order_id": order.order_id, "reference": order.reference}

    def cancel_request(self, req: CancelRequest, *, force: bool = False) -> dict[str, Any]:
        """Send a raw cancel request when the order is not in the OMS.

        This is intended for operator recovery only; the normal path is
        :meth:`cancel`, which verifies the order is known and active.
        """
        self.trade_gateway.cancel_order(req)
        payload = {
            "order_id": req.order_id,
            "code": req.code,
            "exchange": req.exchange.value,
            "force": bool(force),
        }
        self._audit("cancel", payload, order_id=req.order_id)
        return {"cancelled": True, **payload}

    # ---- safety controls ------------------------------------------------- #
    def halt(self, reason: str = "manual") -> None:
        """Kill-switch: stop new orders and cancel every working order."""
        self.runmode.halt(reason)
        self._audit("halt", {"reason": reason})
        for order in self.oms.get_active_orders():
            try:
                self.trade_gateway.cancel_order(order.create_cancel())
            except Exception as exc:  # noqa: BLE001 - best-effort flatten of working orders
                self._audit("halt_cancel_error", {"order_id": order.order_id, "error": str(exc)}, order_id=order.order_id)

    def resume(self) -> None:
        if self.risk is not None and hasattr(self.risk, "acknowledge_loss_halt"):
            self.risk.acknowledge_loss_halt()
            verdict = self.risk.check_equity(self.oms)
            if not verdict.ok:
                self.runmode.halt(f"risk:{verdict.rule}:{verdict.reason}")
                self._audit(
                    "resume_blocked",
                    {"rule": verdict.rule, "reason": verdict.reason},
                )
                raise ValueError("loss halt remains active; account equity has not recovered")
        self.runmode.resume()
        self._audit("resume", {})

    def handle_disconnect(self, reason: str = "disconnected") -> None:
        """Gateway dropped: halt immediately (no new orders until reconciled)."""
        if self.connection.state != ConnectionState.DISCONNECTED:
            self.connection.transition(ConnectionState.DISCONNECTED)
        self.runmode.halt(reason)
        self._audit("disconnected", {"reason": reason}, source=self.trade_gateway.name)

    def reconcile_after_reconnect(
        self,
        *,
        setting: dict | None = None,
        auto_resume: bool = False,
    ) -> dict[str, Any]:
        """After reconnect, re-query broker state before trading can resume.

        The conservative default keeps the kill-switch engaged. Operators or
        higher-level runtimes can pass ``auto_resume=True`` only after they have
        decided the broker/ledger state is safe.
        """
        if self.connection.state != ConnectionState.DISCONNECTED:
            try:
                for gateway in _unique_gateways(self.trade_gateway, self.quote_gateway):
                    gateway.close()
            except Exception as exc:  # noqa: BLE001 - reconnect still records the safety halt
                self._audit("reconnect_close_error", {"error": str(exc)}, source=self.trade_gateway.name)
            self.handle_disconnect("reconnect")
        elif not auto_resume and not self.runmode.halted:
            self.runmode.halt("reconnect")
            self._audit("halt", {"reason": "reconnect"})
        self.connection.transition(ConnectionState.CONNECTING)
        self._connect_gateways(setting)
        self.connection.transition(ConnectionState.CONNECTED)
        self.connection.transition(ConnectionState.LOGGED_IN)
        self.trade_gateway.query_account()
        self.trade_gateway.query_position()
        if auto_resume:
            verdict = (
                self.risk.check_equity(self.oms)
                if self.risk is not None and hasattr(self.risk, "check_equity")
                else None
            )
            if verdict is None or verdict.ok:
                self.runmode.resume()
            else:
                self.runmode.halt(f"risk:{verdict.rule}:{verdict.reason}")
        report = {
            "buying_power": self.oms.buying_power(),
            "auto_resume": bool(auto_resume),
            "resumed": not self.runmode.halted,
            "active_orders": len(self.oms.get_active_orders()),
            "positions": len(self.oms.get_positions()),
        }
        self._audit("reconciled", report, source=self.trade_gateway.name)
        return report

    def subscribe_market_data(self, symbols: list[str]) -> None:
        """Subscribe through the quote channel, not the trade channel."""
        pending: list[str] = []
        for raw in symbols:
            code, exchange = normalize_symbol(raw)
            key = f"{code}.{exchange.value}"
            if key not in self._subscribed_symbols:
                self._subscribed_symbols.add(key)
                pending.append(key)
        if pending:
            self.quote_gateway.subscribe(pending)

    def reconcile_and_resume(self) -> dict[str, Any]:
        """Backward-compatible reconnect helper that resumes after reconciliation."""
        return self.reconcile_after_reconnect(auto_resume=True)

    # ---- introspection --------------------------------------------------- #
    def tick_session(self):
        return self.session.tick()

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.runmode.mode,
            "halted": self.runmode.halted,
            "halt_reason": self.runmode.halt_reason,
            "connection": self.connection.state.value,
            "session": self.session.state.value,
            "buying_power": self.oms.buying_power(),
            "active_orders": len(self.oms.get_active_orders()),
            "positions": len(self.oms.get_positions()),
            "contracts": len(self.oms.contracts),
            "ticks": len(self.oms.ticks),
            "subscribed_symbols": sorted(self._subscribed_symbols),
            "risk": self.risk.snapshot() if self.risk is not None and hasattr(self.risk, "snapshot") else None,
        }

    def _publish(
        self,
        kind: str,
        payload: Any = None,
        *,
        order_id: str | None = None,
        reference: str | None = None,
        source: str = "live",
    ) -> LiveEvent:
        return self.events.publish(
            kind,
            payload,
            order_id=order_id,
            reference=reference,
            source=source,
            now_fn=self._event_now,
        )

    def _audit(
        self,
        kind: str,
        payload: Any = None,
        *,
        order_id: str | None = None,
        reference: str | None = None,
        source: str = "live",
    ) -> dict[str, Any]:
        event = self._publish(kind, payload, order_id=order_id, reference=reference, source=source)
        return self.ledger.append_event(event)


def _req(req: OrderRequest) -> dict[str, Any]:
    return {
        "code": req.code, "exchange": req.exchange.value, "direction": req.direction.value,
        "volume": req.volume, "price": req.price, "type": req.type.value, "offset": req.offset.value,
        "reference": req.reference,
    }


def _source(value: Any, engine: LiveEngine) -> str:
    return str(getattr(value, "gateway", "") or engine.trade_gateway.name or "live")


def _split_gateway_settings(setting: dict | None) -> tuple[dict, dict]:
    setting = setting or {}
    if "trade" in setting or "quote" in setting:
        trade = setting.get("trade") or {}
        quote = setting.get("quote") or trade
        return dict(trade), dict(quote)
    return dict(setting), dict(setting)


def _unique_gateways(*gateways: Any) -> list[Any]:
    seen: set[int] = set()
    unique: list[Any] = []
    for gateway in gateways:
        marker = id(gateway)
        if marker not in seen:
            seen.add(marker)
            unique.append(gateway)
    return unique

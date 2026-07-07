"""Runtime helpers for running AlphaPilot live trading from CLI/Portal/daemons.

The lower live stack already mirrors vn.py's gateway/OMS separation. This module
adds the missing orchestration layer: build the right broker from config, connect
with env-backed settings, wait for an account snapshot, reconcile target books,
route through :class:`LiveEngine.submit`, and persist a compact state snapshot.

It is intentionally usable as a one-shot CLI helper today and as the core of a
long-lived daemon later.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.engine import LiveEngine
from alphapilot.systems.live.executor import reconcile
from alphapilot.systems.live.risk import RiskGate
from alphapilot.systems.live.targets import TargetPortfolio
from alphapilot.systems.live.types import (
    CancelRequest,
    Direction,
    Exchange,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    TickData,
    normalize_symbol,
)


def clone_config(
    config: LiveConfig,
    *,
    mode: str | None = None,
    broker: str | None = None,
    ledger_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> LiveConfig:
    """Return a config copy with optional runtime overrides."""
    return replace(
        config,
        mode=mode or config.mode,
        broker=broker or config.broker,
        ledger_dir=Path(ledger_dir).expanduser() if ledger_dir else config.ledger_dir,
        state_dir=Path(state_dir).expanduser() if state_dir else config.state_dir,
    )


def require_live_confirmation(config: LiveConfig, *, confirm_live: bool) -> None:
    """Fail closed before any LIVE-mode route unless the caller confirms."""
    if config.mode == RunMode.LIVE and not confirm_live:
        raise ValueError("LIVE mode requires confirm_live=True")


class LiveRuntime:
    """A connected or connectable live trading runtime."""

    def __init__(self, config: LiveConfig, engine: LiveEngine) -> None:
        self.config = config
        self.engine = engine
        self.state_path = Path(config.state_dir).expanduser() / "runtime_state.json"
        self.recovery: dict[str, Any] | None = None

    # ---- construction ----------------------------------------------------- #
    @classmethod
    def create(
        cls,
        config: LiveConfig,
        *,
        broker: Any = None,
        now_fn=None,
        is_trading_day_fn=None,
    ) -> "LiveRuntime":
        """Build a runtime from config without connecting yet."""
        gateway = broker or _make_broker(config)
        engine = LiveEngine(
            config,
            gateway,
            now_fn=now_fn,
            is_trading_day_fn=is_trading_day_fn,
            risk=RiskGate(config.risk, enforce_session=config.mode == RunMode.LIVE),
        )
        return cls(config, engine)

    # ---- lifecycle -------------------------------------------------------- #
    def connect(self, *, setting: dict | None = None, paper_cash: float | None = None) -> dict[str, Any]:
        """Connect the underlying engine and write the first state snapshot."""
        if setting is None:
            setting = build_runtime_setting(self.config, paper_cash=paper_cash)
        self.engine.connect(setting)
        self.refresh_broker_state()
        self.recover()
        return self.write_state()

    def refresh_broker_state(
        self,
        *,
        include_orders: bool = False,
        include_trades: bool = False,
    ) -> dict[str, Any]:
        """Ask the broker for fresh snapshots and report what was requested."""
        tasks: list[tuple[str, Any, bool]] = [
            ("account", self.engine.gateway.query_account, True),
            ("position", self.engine.gateway.query_position, True),
            ("orders", getattr(self.engine.gateway, "query_orders", None), include_orders),
            ("trades", getattr(self.engine.gateway, "query_trades", None), include_trades),
        ]
        report: dict[str, Any] = {"requested": [], "unsupported": [], "errors": []}
        for kind, fn, enabled in tasks:
            if not enabled:
                continue
            if fn is None:
                report["unsupported"].append(kind)
                continue
            try:
                sent = fn()
            except NotImplementedError:
                report["unsupported"].append(kind)
            except Exception as exc:  # noqa: BLE001 - broker refresh is best-effort
                error = {"kind": kind, "error": str(exc)}
                report["errors"].append(error)
                self.engine.ledger.record("refresh_error", error)
            else:
                if sent is False:
                    report["unsupported"].append(kind)
                else:
                    report["requested"].append(kind)
        return report

    def settle_broker_events(self, seconds: float = 0.0) -> None:
        """Give async SDK callbacks a short window to enter the OMS."""
        if seconds <= 0:
            return
        deadline = time.time() + float(seconds)
        dispatcher = getattr(self.engine.gateway, "dispatcher", None)
        drain = getattr(dispatcher, "drain", None)
        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.0)
            if drain is not None:
                drain(timeout=min(0.2, remaining))
            time.sleep(min(0.05, remaining))

    def wait_for_order_ack(self, order_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
        """Wait until an order is visible and no longer only locally submitting."""
        return self._wait_for_order(order_id, timeout=timeout, require_ack=True)

    def wait_for_order_terminal(self, order_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
        """Wait until an order reaches a non-active terminal state."""
        return self._wait_for_order(order_id, timeout=timeout, require_terminal=True)

    def order_state(self, order_id: str) -> dict[str, Any]:
        """Return the current OMS projection for one order without waiting."""
        order = self.engine.oms.get_order(str(order_id).strip())
        return _order_wait_report(str(order_id).strip(), order, elapsed=0.0, timed_out=False)

    def _wait_for_order(
        self,
        order_id: str,
        *,
        timeout: float,
        require_ack: bool = False,
        require_terminal: bool = False,
    ) -> dict[str, Any]:
        oid = str(order_id).strip()
        started = time.time()
        deadline = started + max(float(timeout), 0.0)
        last_order = self.engine.oms.get_order(oid)
        while True:
            self._drain_gateway_callbacks(timeout=0.05)
            order = self.engine.oms.get_order(oid)
            if order is not None:
                last_order = order
                acknowledged = _order_acknowledged(order)
                terminal = not order.is_active()
                if (not require_ack or acknowledged) and (not require_terminal or terminal):
                    return _order_wait_report(oid, order, elapsed=time.time() - started, timed_out=False)
            if time.time() >= deadline:
                return _order_wait_report(oid, last_order, elapsed=time.time() - started, timed_out=True)
            time.sleep(0.05)

    def _drain_gateway_callbacks(self, *, timeout: float = 0.05) -> None:
        dispatcher = getattr(self.engine.gateway, "dispatcher", None)
        drain = getattr(dispatcher, "drain", None)
        if drain is not None:
            drain(timeout=max(float(timeout), 0.0))

    def close(self) -> None:
        self.engine.close()
        self.write_state()

    def recover(self) -> dict[str, Any]:
        """Refresh broker snapshots and restore local runtime counters."""
        from alphapilot.systems.live.recovery import RecoveryService

        self.recovery = RecoveryService(self).run()
        return self.recovery

    def reconnect(
        self,
        *,
        setting: dict | None = None,
        auto_resume: bool = False,
    ) -> dict[str, Any]:
        """Reconnect and reconcile broker state.

        By default this leaves the runtime halted after a disconnect; callers can
        explicitly ``resume`` after inspecting the recovery report.
        """
        if setting is None:
            setting = build_runtime_setting(self.config)
        reconnect = self.engine.reconcile_after_reconnect(setting=setting, auto_resume=auto_resume)
        recovery = self.recover()
        state = self.write_state()
        return {"reconnect": reconnect, "recovery": recovery, "state": state}

    def wait_ready(
        self,
        *,
        timeout: float = 20.0,
        require_account: bool = True,
        require_contracts: bool | None = None,
        settle_seconds: float | None = None,
    ) -> bool:
        """Wait until the runtime has the minimum state needed for execution."""
        if require_contracts is None:
            require_contracts = self.config.mode == RunMode.LIVE
        if settle_seconds is None:
            settle_seconds = 2.5 if self.config.mode == RunMode.LIVE else 0.0
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            oms = self.engine.oms
            account_ok = (not require_account) or oms.account is not None
            contracts_ok = (not require_contracts) or bool(oms.contracts)
            if account_ok and contracts_ok:
                if settle_seconds > 0:
                    time.sleep(float(settle_seconds))
                self.write_state()
                return True
            time.sleep(0.2)
        self.write_state()
        return False

    # ---- actions ---------------------------------------------------------- #
    def submit_order(
        self,
        symbol: str,
        *,
        side: str,
        volume: float,
        price: float = 0.0,
        order_type: str = "limit",
        reference: str = "",
    ) -> dict[str, Any]:
        """Submit one normalized order through the guarded engine path."""
        code, exchange = normalize_symbol(symbol)
        side_l = side.lower()
        typ = OrderType.MARKET if order_type.lower() == "market" else OrderType.LIMIT
        factory = OrderRequest.buy if side_l in ("buy", "long") else OrderRequest.sell
        req = factory(code, exchange, float(volume), float(price), type=typ, reference=reference)
        order_id = self.engine.submit(req)
        routing_event = None if order_id else _last_routing_event(self.engine.ledger, reference=req.reference)
        routing_payload = routing_event.get("payload") if isinstance(routing_event, dict) else {}
        if not isinstance(routing_payload, dict):
            routing_payload = {}
        return {
            "order_id": order_id,
            "submitted": bool(order_id),
            "request": order_request_to_dict(req),
            "routing_event": routing_event,
            "routing_rule": routing_payload.get("rule"),
            "routing_reason": routing_payload.get("reason"),
            "state": self.write_state(),
        }

    def cancel_order(
        self,
        order_id: str,
        *,
        symbol: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Cancel an active order through the guarded engine path.

        The default is intentionally conservative: only known active OMS orders
        are cancelled. ``force=True`` allows an operator to send a raw broker
        cancel by ``order_id`` and ``symbol`` when recovering from an OMS gap.
        """
        oid = str(order_id).strip()
        if not oid:
            raise ValueError("order_id is required")
        known = self.engine.oms.get_order(oid)
        if known is not None:
            result = self.engine.cancel(known, active_only=not force)
        elif force:
            if not symbol:
                raise ValueError("symbol is required for force cancel when order is not in OMS")
            code, exchange = normalize_symbol(symbol)
            result = self.engine.cancel_request(
                CancelRequest(order_id=oid, code=code, exchange=exchange),
                force=True,
            )
        else:
            result = self.engine.cancel(oid)
        return {
            "cancelled": bool(result.get("cancelled")),
            "order_id": oid,
            "result": result,
            "state": self.write_state(),
        }

    def plan_target(self, target: TargetPortfolio) -> list[OrderRequest]:
        """Diff target holdings against real OMS positions into order requests."""
        return reconcile(target, self.engine.oms, lot_size=self.config.risk.lot_size)

    def submit_target(self, target: TargetPortfolio, *, route: bool = False) -> dict[str, Any]:
        """Plan and optionally route all orders for a target portfolio."""
        requests = self.plan_target(target)
        routed: list[str] = []
        unrouted_requests: list[dict[str, Any]] = []
        if route:
            for req in requests:
                order_id = self.engine.submit(req)
                if order_id:
                    routed.append(order_id)
                else:
                    unrouted_requests.append(order_request_to_dict(req))
        return {
            "target": target_to_dict(target),
            "planned": len(requests),
            "requests": [order_request_to_dict(req) for req in requests],
            "routed": routed,
            "submitted": len(routed),
            "unrouted": len(unrouted_requests),
            "unrouted_requests": unrouted_requests,
            "fully_routed": (not route) or not unrouted_requests,
            "state": self.write_state(),
        }

    def run_loop(
        self,
        *,
        symbols: list[str] | None = None,
        interval: float = 2.0,
        duration: float | None = None,
    ) -> dict[str, Any]:
        """Keep the runtime alive, subscribed and periodically snapshotted.

        This is a deliberately small daemon core: broker SDK callbacks update
        the OMS asynchronously, while the loop advances the session clock and
        writes a state heartbeat. Strategy runners/algo steppers can later be
        attached around the same cadence.
        """
        if symbols:
            self.engine.gateway.subscribe(symbols)
        started = time.time()
        iterations = 0
        while True:
            self.engine.tick_session()
            self.write_state()
            iterations += 1
            if duration is not None and time.time() - started >= float(duration):
                break
            time.sleep(max(float(interval), 0.05))
        state = self.write_state()
        return {"iterations": iterations, "state": state}

    # ---- state ------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable runtime state projection."""
        oms = self.engine.oms
        account = oms.account
        return {
            "config": {
                "mode": self.config.mode,
                "broker": self.config.broker,
                "ledger_dir": str(self.config.ledger_dir),
                "state_dir": str(self.config.state_dir),
            },
            "engine": self.engine.snapshot(),
            "recovery": self.recovery,
            "account": None if account is None else {
                "account_id": account.account_id,
                "balance": account.balance,
                "available": account.available,
                "frozen": account.frozen,
                "gateway": account.gateway,
            },
            "positions": [
                {
                    "code": p.code,
                    "exchange": p.exchange.value,
                    "volume": p.volume,
                    "available": p.available,
                    "yd_volume": p.yd_volume,
                    "frozen": p.frozen,
                    "price": p.price,
                    "pnl": p.pnl,
                    "gateway": p.gateway,
                }
                for p in oms.get_positions()
            ],
            "orders": [
                order_to_dict(o)
                for o in oms.orders.values()
            ],
            "trades": [
                {
                    "trade_id": t.trade_id,
                    "order_id": t.order_id,
                    "code": t.code,
                    "exchange": t.exchange.value,
                    "side": side_from_direction(t.direction),
                    "price": t.price,
                    "volume": t.volume,
                    "gateway": t.gateway,
                }
                for t in oms.get_trades()
            ],
            "ticks": [
                tick_to_dict(tick)
                for tick in oms.ticks.values()
            ],
            "logs": [
                {"level": log.level, "msg": log.msg, "gateway": log.gateway}
                for log in list(oms.logs)[-50:]
            ],
            "ledger_tail": self.engine.ledger.events()[-50:],
        }

    def write_state(self) -> dict[str, Any]:
        """Persist and return the compact runtime snapshot."""
        state = self.snapshot()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return state


def _make_broker(config: LiveConfig):
    if config.mode == RunMode.LIVE:
        from alphapilot.systems.live.brokers.registry import create_gateway

        return create_gateway(config.broker)
    from alphapilot.systems.live.brokers.paper import PaperBroker

    return PaperBroker()


def build_runtime_setting(config: LiveConfig, *, paper_cash: float | None = None) -> dict[str, Any]:
    """Build broker connect settings from env for LIVE, simple cash for paper."""
    if config.mode == RunMode.LIVE:
        from alphapilot.systems.live.brokers.registry import build_connect_setting, missing_setting_fields

        missing = missing_setting_fields(config.broker)
        if missing:
            raise ValueError("missing live broker env fields: " + ", ".join(missing))
        return build_connect_setting(config.broker)
    return {"cash": float(paper_cash) if paper_cash is not None else 1_000_000.0}


def side_from_direction(direction: Direction) -> str:
    return "buy" if direction == Direction.LONG else "sell"


def order_to_dict(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "code": order.code,
        "exchange": order.exchange.value if isinstance(order.exchange, Exchange) else str(order.exchange),
        "side": side_from_direction(order.direction),
        "price": order.price,
        "volume": order.volume,
        "traded": order.traded,
        "status": _status_value(order.status),
        "active": order.is_active(),
        "reference": order.reference,
        "gateway": order.gateway,
        "message": order.message,
    }


def tick_to_dict(tick: TickData) -> dict[str, Any]:
    return {
        "code": tick.code,
        "exchange": tick.exchange.value if isinstance(tick.exchange, Exchange) else str(tick.exchange),
        "key": tick.key,
        "name": tick.name,
        "last_price": tick.last_price,
        "pre_close": tick.pre_close,
        "open_price": tick.open_price,
        "high_price": tick.high_price,
        "low_price": tick.low_price,
        "limit_up": tick.limit_up,
        "limit_down": tick.limit_down,
        "bid_price_1": tick.bid_price_1,
        "ask_price_1": tick.ask_price_1,
        "bid_volume_1": tick.bid_volume_1,
        "ask_volume_1": tick.ask_volume_1,
        "gateway": tick.gateway,
        "datetime": None if tick.datetime is None else tick.datetime.isoformat(),
    }


def order_request_to_dict(req: OrderRequest) -> dict[str, Any]:
    return {
        "code": req.code,
        "exchange": req.exchange.value if isinstance(req.exchange, Exchange) else str(req.exchange),
        "side": side_from_direction(req.direction),
        "volume": req.volume,
        "price": req.price,
        "type": req.type.value,
        "reference": req.reference,
    }


def target_to_dict(target: TargetPortfolio) -> dict[str, Any]:
    return {
        "date": target.date,
        "holdings": dict(target.holdings),
        "prices": dict(target.prices),
        "cash": target.cash,
        "source": target.source,
        "market": target.market,
    }


def _status_value(status: Any) -> str:
    return str(getattr(status, "value", status))


def _order_acknowledged(order: Order) -> bool:
    return _status_value(order.status) != OrderStatus.SUBMITTING.value


def _order_wait_report(
    order_id: str,
    order: Order | None,
    *,
    elapsed: float,
    timed_out: bool,
) -> dict[str, Any]:
    acknowledged = False if order is None else _order_acknowledged(order)
    terminal = False if order is None else not order.is_active()
    return {
        "order_id": order_id,
        "found": order is not None,
        "status": None if order is None else _status_value(order.status),
        "active": None if order is None else order.is_active(),
        "acknowledged": acknowledged,
        "terminal": terminal,
        "timed_out": bool(timed_out),
        "elapsed": round(float(elapsed), 3),
        "order": None if order is None else order_to_dict(order),
    }


def _last_routing_event(ledger: Any, *, reference: str = "") -> dict[str, Any] | None:
    if reference:
        events = ledger.events(reference=reference, limit=10)
    else:
        events = ledger.events(limit=10)
    for event in reversed(events):
        if event.get("kind") in {"rejected", "blocked", "dry_run_intent"}:
            return event
    return None

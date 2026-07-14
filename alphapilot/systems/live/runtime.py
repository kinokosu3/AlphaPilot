"""Runtime helpers for running AlphaPilot live trading from CLI/Portal/daemons.

The lower live stack already mirrors vn.py's gateway/OMS separation. This module
adds the missing orchestration layer: build the right broker from config, connect
with env-backed settings, wait for an account snapshot, reconcile target books,
route through :class:`LiveEngine.submit`, and persist a compact state snapshot.

It is intentionally usable as a one-shot CLI helper today and as the core of a
long-lived daemon later.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

from alphapilot.systems.live.config import (
    LiveConfig,
    RunMode,
    requires_live_market_safety,
    uses_real_providers,
)
from alphapilot.systems.live.engine import LiveEngine
from alphapilot.systems.live.executor import reconcile
from alphapilot.systems.live.risk import RiskGate
from alphapilot.systems.live.targets import TargetPortfolio
from alphapilot.systems.live.market_data import tick_to_dict
from alphapilot.systems.live.journal import InMemoryExecutionJournal
from alphapilot.systems.live.routing import AutomatedOrderRouter
from alphapilot.systems.live.state_io import atomic_write_json
from alphapilot.systems.live.types import (
    CancelRequest,
    Direction,
    Exchange,
    Offset,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    TickData,
    normalize_symbol,
)
from alphapilot.systems.trading.ports import RouteContext, RouteOrigin


def clone_config(
    config: LiveConfig,
    *,
    mode: str | None = None,
    broker: str | None = None,
    trade_broker: str | None = None,
    quote_provider: str | None = None,
    ledger_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> LiveConfig:
    """Return a config copy with optional runtime overrides."""
    trade_override = trade_broker or broker
    selected_trade = trade_override or config.trade_broker or config.broker
    selected_quote = quote_provider or (selected_trade if trade_override else config.quote_provider or selected_trade)
    return replace(
        config,
        mode=mode or config.mode,
        broker=selected_trade,
        trade_broker=selected_trade,
        quote_provider=selected_quote,
        ledger_dir=Path(ledger_dir).expanduser() if ledger_dir else config.ledger_dir,
        state_dir=Path(state_dir).expanduser() if state_dir else config.state_dir,
    )


def require_live_confirmation(config: LiveConfig, *, confirm_live: bool) -> None:
    """Fail closed before any LIVE-mode route unless the caller confirms."""
    if config.mode == RunMode.LIVE and not confirm_live:
        raise ValueError("LIVE mode requires confirm_live=True")


class LiveRuntime:
    """A connected or connectable live trading runtime."""

    def __init__(
        self,
        config: LiveConfig,
        engine: LiveEngine,
        *,
        execution_journal: Any | None = None,
        route_authorizer: Any | None = None,
        runtime_id: str | None = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.runtime_id = str(runtime_id or uuid.uuid4().hex)
        self.state_path = Path(config.state_dir).expanduser() / "runtime_state.json"
        self.recovery: dict[str, Any] | None = None
        self.market_data = None
        self.execution_journal = execution_journal or InMemoryExecutionJournal()
        # Compatibility attribute for callers written before the port split.
        self.strategy_store = self.execution_journal
        self.route_authorizer = route_authorizer

    # ---- construction ----------------------------------------------------- #
    @classmethod
    def create(
        cls,
        config: LiveConfig,
        *,
        broker: Any = None,
        quote_provider: Any = None,
        now_fn=None,
        is_trading_day_fn=None,
        execution_journal: Any | None = None,
        route_authorizer: Any | None = None,
        runtime_id: str | None = None,
    ) -> "LiveRuntime":
        """Build a runtime from config without connecting yet."""
        if broker is not None:
            gateway = broker
            quote_gateway = quote_provider or _make_quote_gateway(config, gateway)
        elif quote_provider is not None:
            gateway = _make_trade_gateway(config)
            quote_gateway = quote_provider
        elif uses_real_providers(config.mode):
            from alphapilot.systems.live.brokers.registry import create_gateway_pair

            gateway, quote_gateway = create_gateway_pair(
                config.trade_broker or config.broker,
                config.quote_provider or config.trade_broker or config.broker,
            )
        else:
            gateway = _make_trade_gateway(config)
            quote_gateway = gateway
        engine = LiveEngine(
            config,
            gateway,
            quote_gateway=quote_gateway,
            now_fn=now_fn,
            is_trading_day_fn=is_trading_day_fn,
            risk=RiskGate(config.risk, enforce_session=requires_live_market_safety(config.mode)),
        )
        return cls(
            config,
            engine,
            execution_journal=execution_journal,
            route_authorizer=route_authorizer,
            runtime_id=runtime_id,
        )

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
        trade_gateway = self.engine.trade_gateway
        tasks: list[tuple[str, Any, bool]] = [
            ("account", trade_gateway.query_account, True),
            ("position", trade_gateway.query_position, True),
            ("orders", getattr(trade_gateway, "query_orders", None), include_orders),
            ("trades", getattr(trade_gateway, "query_trades", None), include_trades),
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
        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.0)
            self._drain_gateway_callbacks(timeout=min(0.2, remaining))
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
        seen: set[int] = set()
        for gateway in (self.engine.trade_gateway, self.engine.quote_gateway):
            marker = id(gateway)
            if marker in seen:
                continue
            seen.add(marker)
            dispatcher = getattr(gateway, "dispatcher", None)
            drain = getattr(dispatcher, "drain", None)
            if drain is not None:
                drain(timeout=max(float(timeout), 0.0))

    def close(self) -> None:
        try:
            self.engine.close()
        finally:
            if self.market_data is not None:
                self.market_data.close()
            self.write_state()

    def enable_market_data(
        self,
        symbols: list[str],
        *,
        recording: bool | None = None,
    ):
        """Attach the daemon-owned quote projection and recorder before subscribing."""
        if self.market_data is not None:
            return self.market_data
        from alphapilot.systems.live.market_data import LiveMarketDataService

        service = LiveMarketDataService(
            self.config.market_data,
            self.config.quote_provider or self.config.trade_broker or self.config.broker or "quote",
            symbols,
            state_dir=self.config.state_dir,
            timezone=self.config.timezone,
            recording=recording,
        )
        service.start(self.engine)
        self.market_data = service
        return service

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
            require_contracts = requires_live_market_safety(self.config.mode)
        if settle_seconds is None:
            settle_seconds = 2.5 if uses_real_providers(self.config.mode) else 0.0
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
        exchange: str | None = None,
        offset: str = "none",
        product: str = "equity",
        reference: str = "",
        route_context: RouteContext | None = None,
    ) -> dict[str, Any]:
        """Submit one normalized order through the guarded engine path."""
        if product.lower() in {"future", "futures"} and self.config.mode == RunMode.LIVE:
            raise ValueError("futures live routing is not enabled")
        code, parsed_exchange = normalize_symbol(symbol)
        resolved_exchange = _parse_exchange(exchange) if exchange else parsed_exchange
        side_l = side.lower()
        try:
            typ = OrderType(order_type.lower())
        except ValueError:
            typ = OrderType.LIMIT
        try:
            req_offset = Offset(offset.lower())
        except ValueError:
            req_offset = Offset.NONE
        direction = Direction.LONG if side_l in ("buy", "long") else Direction.SHORT
        req = OrderRequest(
            code=code,
            exchange=resolved_exchange,
            direction=direction,
            volume=float(volume),
            price=float(price),
            type=typ,
            offset=req_offset,
            reference=reference,
        )
        context = route_context or RouteContext.manual()
        order_id = self._submit_request(req, context)
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
        return reconcile(
            target,
            self.engine.oms,
            lot_size=self.config.risk.lot_size,
            max_order_value=self.config.risk.max_order_value,
        )

    def submit_target(
        self,
        target: TargetPortfolio,
        *,
        route: bool = False,
        route_context: RouteContext | None = None,
    ) -> dict[str, Any]:
        """Plan and optionally route all orders for a target portfolio."""
        from alphapilot.systems.live.planner import ExecutionPlanner

        plan = ExecutionPlanner(
            lot_size=self.config.risk.lot_size,
            max_order_value=self.config.risk.max_order_value,
        ).plan(target, self.engine.oms)
        context = route_context or RouteContext.manual()
        if context.origin == RouteOrigin.AUTOMATED and (
            target.instance_id != context.instance_id
            or target.config_hash != context.config_hash
        ):
            from alphapilot.systems.live.planner import PlanIssue

            plan.issues.append(PlanIssue(
                "route_binding",
                "target instance_id/config_hash does not match automated route authorization",
            ))
        self._preflight_target(target, plan)
        requests = plan.requests
        target_payload = target_to_dict(target)
        self.execution_journal.record_decision(
            plan.decision_id, target.instance_id or "legacy", target.config_hash, target_payload
        )
        self.execution_journal.record_plan(
            plan.plan_id, plan.decision_id, target.instance_id or "legacy",
            {"target": target_payload, "requests": [order_request_to_dict(req) for req in requests]},
            "planned" if plan.ok else "blocked",
        )
        routed: list[str] = []
        unrouted_requests: list[dict[str, Any]] = []
        if route and plan.ok:
            for req in requests:
                inserted = self.execution_journal.record_child_order(
                    req.reference, plan.plan_id, order_request_to_dict(req), status="routing"
                )
                if not inserted:
                    unrouted_requests.append({
                        **order_request_to_dict(req),
                        "routing_rule": "duplicate_reference",
                        "routing_reason": "child order reference already journaled",
                    })
                    continue
                order_id = self._submit_request(req, context)
                if order_id:
                    routed.append(order_id)
                    self.execution_journal.update_child_order(
                        req.reference, status="submitted", order_id=str(order_id)
                    )
                else:
                    event = _last_routing_event(self.engine.ledger, reference=req.reference) or {}
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    unrouted_requests.append({
                        **order_request_to_dict(req),
                        "routing_rule": payload.get("rule"),
                        "routing_reason": payload.get("reason"),
                    })
                    self.execution_journal.update_child_order(req.reference, status="rejected")
        return {
            "target": target_payload,
            "plan_id": plan.plan_id,
            "decision_id": plan.decision_id,
            "preflight_ok": plan.ok,
            "issues": [issue.__dict__ for issue in plan.issues],
            "planned": len(requests),
            "requests": [order_request_to_dict(req) for req in requests],
            "routed": routed,
            "submitted": len(routed),
            "unrouted": len(unrouted_requests),
            "unrouted_requests": unrouted_requests,
            "fully_routed": ((not route) or not unrouted_requests) and plan.ok,
            "state": self.write_state(),
        }

    def automated_order_router(
        self,
        *,
        instance_id: str,
        config_hash: str,
        deployment_level: str,
    ) -> AutomatedOrderRouter:
        """Return the only route port supplied to an automated strategy runner."""

        def context() -> RouteContext:
            account = self.engine.oms.account
            return RouteContext(
                origin=RouteOrigin.AUTOMATED,
                instance_id=str(instance_id),
                config_hash=str(config_hash),
                account_id="" if account is None else str(account.account_id),
                broker=str(self.config.trade_broker or self.config.broker or ""),
                deployment_level=str(deployment_level),
                runtime_id=self.runtime_id,
            )

        return AutomatedOrderRouter(self.engine, self.route_authorizer, context)

    def _submit_request(self, request: OrderRequest, context: RouteContext) -> str | None:
        if context.origin == RouteOrigin.AUTOMATED:
            router = AutomatedOrderRouter(self.engine, self.route_authorizer, lambda: context)
            return router.submit(request)
        blocks = self._manual_route_blocks(context)
        if blocks:
            reason = ", ".join(f"{item['scope_type']}:{item['scope_id']}" for item in blocks)
            self.engine.ledger.record(
                "blocked",
                {
                    "origin": "manual",
                    "rule": "kill_switch",
                    "reason": f"route blocked by {reason}",
                },
                reference=request.reference,
            )
            return None
        return self.engine.submit(request, origin="manual")

    def _manual_route_blocks(self, context: RouteContext) -> list[dict[str, Any]]:
        lookup = getattr(self.execution_journal, "active_route_blocks", None)
        if not callable(lookup):
            return []
        account = self.engine.oms.account
        return lookup(
            instance_id=context.instance_id,
            account_id=context.account_id or ("" if account is None else str(account.account_id)),
        )

    def _preflight_target(self, target: TargetPortfolio, plan: Any) -> None:
        """Whole-book checks that cannot be expressed as independent order rules."""
        from alphapilot.systems.live.planner import PlanIssue

        oms = self.engine.oms
        account = oms.account
        if account is None or account.balance <= 0:
            plan.issues.append(PlanIssue("account_not_ready", "positive account balance is required"))
            return
        if self.engine.runmode.halted:
            plan.issues.append(PlanIssue("kill_switch", self.engine.runmode.halt_reason or "runtime halted"))
        now_value = (
            self.engine.session._now_fn()
            if hasattr(self.engine.session, "_now_fn")
            else datetime.now(timezone.utc)
        )
        if target.valid_until:
            try:
                expiry = datetime.fromisoformat(str(target.valid_until))
                comparable_now = now_value
                if expiry.tzinfo is not None and comparable_now.tzinfo is None:
                    comparable_now = comparable_now.replace(tzinfo=expiry.tzinfo)
                elif expiry.tzinfo is None and comparable_now.tzinfo is not None:
                    expiry = expiry.replace(tzinfo=comparable_now.tzinfo)
                if comparable_now > expiry:
                    plan.issues.append(PlanIssue("expired_target", "target validity window has expired"))
            except ValueError:
                plan.issues.append(PlanIssue("invalid_validity", "valid_until must be ISO-8601"))
        if requires_live_market_safety(self.config.mode) and target.effective_session:
            try:
                effective_day = datetime.fromisoformat(str(target.effective_session)[:10]).date()
                if effective_day != now_value.date():
                    plan.issues.append(PlanIssue(
                        "effective_session",
                        f"target is for {effective_day}, current session is {now_value.date()}",
                    ))
            except ValueError:
                plan.issues.append(PlanIssue("effective_session", "effective_session must start with ISO date"))
        total_weight = sum(max(float(value), 0.0) for value in target.target_weights.values())
        if total_weight > 1.0 + 1e-9:
            plan.issues.append(PlanIssue("total_weight", f"target weights sum to {total_weight:.2%} > 100%"))
        risk_state = self.engine.risk.snapshot() if hasattr(self.engine.risk, "snapshot") else {}
        planned_turnover = sum(
            float(request.volume) * float(request.price)
            for request in plan.requests if float(request.price) > 0
        )
        daily_limit = float(self.config.risk.max_daily_value)
        if daily_limit > 0 and float(risk_state.get("value_today") or 0.0) + planned_turnover > daily_limit:
            plan.issues.append(PlanIssue(
                "max_daily_value",
                f"planned turnover exceeds remaining daily cap {daily_limit:.0f}",
            ))
        order_limit = int(self.config.risk.max_orders_per_day)
        if order_limit > 0 and int(risk_state.get("orders_today") or 0) + len(plan.requests) > order_limit:
            plan.issues.append(PlanIssue(
                "max_orders_per_day",
                f"plan would exceed daily order cap {order_limit}",
            ))
        limit = float(self.config.risk.max_position_pct)
        for raw_symbol, shares in target.holdings.items():
            code, exchange = normalize_symbol(raw_symbol)
            key = f"{code}.{exchange.value}"
            price = float(target.prices.get(raw_symbol) or target.prices.get(key) or 0.0)
            tick = oms.get_tick(key)
            if price <= 0 and tick is not None:
                price = float(tick.last_price)
            weight = (float(shares) * price) / float(account.balance) if price > 0 else 0.0
            if limit > 0 and weight > limit + 1e-9:
                plan.issues.append(PlanIssue(
                    "max_position_pct",
                    f"target weight {weight:.1%} > cap {limit:.1%}", key,
                ))
            if not requires_live_market_safety(self.config.mode):
                continue
            if oms.get_contract(key) is None:
                plan.issues.append(PlanIssue("unknown_contract", "LIVE contract metadata is required", key))
            if tick is None or tick.last_price <= 0:
                plan.issues.append(PlanIssue("missing_quote", "LIVE quote is required", key))
                continue
            timestamp = tick.received_at or tick.datetime
            if timestamp is None:
                plan.issues.append(PlanIssue("stale_quote", "quote timestamp is required", key))
                continue
            now = (
                self.engine.session._now_fn()
                if hasattr(self.engine.session, "_now_fn")
                else datetime.now(timestamp.tzinfo or timezone.utc)
            )
            if timestamp.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=timestamp.tzinfo)
            elif timestamp.tzinfo is None and now.tzinfo is not None:
                timestamp = timestamp.replace(tzinfo=now.tzinfo)
            age = max((now - timestamp).total_seconds(), 0.0)
            if age > float(self.config.market_data.stale_after_seconds):
                plan.issues.append(PlanIssue(
                    "stale_quote", f"quote age {age:.1f}s exceeds limit", key
                ))

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
            self.engine.subscribe_market_data(symbols)
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
        plugins = None
        if uses_real_providers(self.config.mode):
            try:
                from alphapilot.systems.live.brokers.registry import provider_pair_metadata

                plugins = provider_pair_metadata(
                    self.config.trade_broker or self.config.broker,
                    self.config.quote_provider or self.config.trade_broker or self.config.broker,
                )
            except Exception as exc:  # noqa: BLE001 - keep state readable after uninstall
                plugins = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "runtime_id": self.runtime_id,
            "config": {
                "mode": self.config.mode,
                "broker": self.config.broker,
                "trade_broker": self.config.trade_broker,
                "quote_provider": self.config.quote_provider,
                "ledger_dir": str(self.config.ledger_dir),
                "state_dir": str(self.config.state_dir),
                "market_data_dir": str(self.config.market_data.data_dir),
                "plugins": plugins,
            },
            "engine": self.engine.snapshot(),
            "recovery": self.recovery,
            "account": None if account is None else {
                "account_id": account.account_id,
                "balance": account.balance,
                "available": account.available,
                "frozen": account.frozen,
                "margin": account.margin,
                "commission": account.commission,
                "close_profit": account.close_profit,
                "position_profit": account.position_profit,
                "risk_ratio": account.risk_ratio,
                "gateway": account.gateway,
            },
            "positions": [
                {
                    "code": p.code,
                    "exchange": p.exchange.value,
                    "volume": p.volume,
                    "available": p.available,
                    "yd_volume": p.yd_volume,
                    "today_volume": p.today_volume,
                    "frozen": p.frozen,
                    "price": p.price,
                    "settlement_price": p.settlement_price,
                    "margin": p.margin,
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
            "market_data": None if self.market_data is None else self.market_data.recorder.status(),
            "logs": [
                {"level": log.level, "msg": log.msg, "gateway": log.gateway}
                for log in list(oms.logs)[-50:]
            ],
            "ledger_tail": self.engine.ledger.events()[-50:],
        }

    def write_state(self) -> dict[str, Any]:
        """Persist and return the compact runtime snapshot."""
        state = self.snapshot()
        atomic_write_json(self.state_path, state)
        return state


def _make_trade_gateway(config: LiveConfig):
    if uses_real_providers(config.mode):
        from alphapilot.systems.live.brokers.registry import create_gateway

        return create_gateway(config.trade_broker or config.broker)
    from alphapilot.systems.live.brokers.paper import PaperBroker

    return PaperBroker()


def _make_quote_gateway(config: LiveConfig, trade_gateway: Any):
    if not uses_real_providers(config.mode):
        return trade_gateway
    quote_provider = config.quote_provider or config.trade_broker or config.broker
    trade_name = config.trade_broker or config.broker
    if quote_provider == trade_name:
        return trade_gateway
    from alphapilot.systems.live.brokers.registry import create_quote_gateway

    return create_quote_gateway(quote_provider)


def _make_broker(config: LiveConfig):
    """Backward-compatible alias for older callers."""
    return _make_trade_gateway(config)


def build_runtime_setting(config: LiveConfig, *, paper_cash: float | None = None) -> dict[str, Any]:
    """Build trade/quote connect settings from env for LIVE, simple cash for paper."""
    return {
        "trade": build_trade_setting(config, paper_cash=paper_cash),
        "quote": build_quote_setting(config, paper_cash=paper_cash),
    }


def build_trade_setting(config: LiveConfig, *, paper_cash: float | None = None) -> dict[str, Any]:
    """Build trade-gateway connect settings."""
    if uses_real_providers(config.mode):
        from alphapilot.systems.live.brokers.registry import build_connect_setting, missing_setting_fields

        broker = config.trade_broker or config.broker
        missing = missing_setting_fields(broker)
        if missing:
            raise ValueError("missing live broker env fields: " + ", ".join(missing))
        return build_connect_setting(broker)
    return {"cash": float(paper_cash) if paper_cash is not None else 1_000_000.0}


def build_quote_setting(config: LiveConfig, *, paper_cash: float | None = None) -> dict[str, Any]:
    """Build quote-provider connect settings."""
    if uses_real_providers(config.mode):
        from alphapilot.systems.live.brokers.registry import build_quote_connect_setting, missing_quote_setting_fields

        provider = config.quote_provider or config.trade_broker or config.broker
        if provider == "paper":
            return {"cash": float(paper_cash) if paper_cash is not None else 1_000_000.0}
        missing = missing_quote_setting_fields(provider)
        if missing:
            raise ValueError("missing live quote provider env fields: " + ", ".join(missing))
        return build_quote_connect_setting(provider)
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
        "type": order.type.value,
        "offset": order.offset.value,
        "volume": order.volume,
        "traded": order.traded,
        "status": _status_value(order.status),
        "active": order.is_active(),
        "reference": order.reference,
        "gateway": order.gateway,
        "message": order.message,
    }


def order_request_to_dict(req: OrderRequest) -> dict[str, Any]:
    return {
        "code": req.code,
        "exchange": req.exchange.value if isinstance(req.exchange, Exchange) else str(req.exchange),
        "side": side_from_direction(req.direction),
        "volume": req.volume,
        "price": req.price,
        "type": req.type.value,
        "offset": req.offset.value,
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
        "decision_id": target.decision_id,
        "instance_id": target.instance_id,
        "as_of": target.as_of,
        "effective_session": target.effective_session,
        "valid_until": target.valid_until,
        "config_hash": target.config_hash,
        "data_version": target.data_version,
        "model_version": target.model_version,
        "target_weights": dict(target.target_weights),
        "price_source": target.price_source,
        "positions": [
            {
                "symbol": item.symbol,
                "target_volume": item.target_volume,
                "direction": item.direction.value if hasattr(item.direction, "value") else str(item.direction),
                "price": item.price,
                "offset_policy": item.offset_policy,
            }
            for item in getattr(target, "positions", [])
        ],
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


def _parse_exchange(value: str) -> Exchange:
    if isinstance(value, Exchange):
        return value
    text = str(value).strip().upper()
    aliases = {"SH": Exchange.SSE, "SZ": Exchange.SZSE, "BJ": Exchange.BSE}
    return aliases.get(text) or Exchange(text)

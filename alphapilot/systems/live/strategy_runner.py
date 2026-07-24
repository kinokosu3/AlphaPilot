"""LiveTimingRunner — drive a timing strategy against the live engine.

Closes the loop the timing system never had: live ticks → bars → strategy →
:class:`OrderIntent` → ``orders_from_intents`` → ``engine.submit``. Everything
here reuses existing seams — the engine's tick listener, the executor's intent
translation, the session clock, and the call-auction algo — so the runner is
mostly wiring, not logic.

Two driving modes (mirroring how the strategies were backtested):

* ``freq="min"`` — ticks aggregate into N-second bars; each completed bar is
  fed to the strategy and any resulting intents are submitted immediately
  (continuous session, risk-gated).
* ``freq="day"`` — ticks aggregate into one daily bar; at ``POST_CLOSE`` the
  bar closes, signals are computed, and the resulting requests are armed as a
  :class:`CallAuctionAlgo` plan for the **next** morning's opening auction —
  matching the backtest's next-bar-open ``shift(1)`` semantics.

The runner is passive: like the algos, an outer loop calls :meth:`step` (a
few times per minute is plenty; e.g. ``EventDispatcher.add_periodic``). Ticks
arrive via the engine's tick listener independently of stepping.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time as datetime_time, timedelta
from collections import deque
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from alphapilot.systems.live.algos import AlgoState, CallAuctionAlgo
from alphapilot.systems.live.bars import Bar, BarAggregator, DAY_INTERVAL
from alphapilot.systems.live.executor import orders_from_intents
from alphapilot.systems.live.fsm.session_fsm import SessionState
from alphapilot.systems.live.types import OrderRequest, TickData
from alphapilot.systems.live.state_io import atomic_write_json
from alphapilot.systems.live.journal import InMemoryExecutionJournal
from alphapilot.systems.live.routing import AutomatedOrderRouter, utc_now_iso
from alphapilot.systems.live.config import RunMode, requires_live_market_safety
from alphapilot.systems.timing.base import OrderIntent
from alphapilot.systems.trading.ports import RouteContext, RouteOrigin


class BarStrategy(Protocol):
    """What the runner needs from a strategy adapter (see BatchStrategyAdapter)."""

    def on_bar(self, bar: Bar) -> list[Any]:
        """Return OrderIntent-shaped objects for one completed bar."""


class LiveTimingRunner:
    """Wire ticks → bars → strategy intents → guarded order submission."""

    def __init__(
        self,
        engine: Any,
        strategy: BarStrategy,
        symbols: list[str],
        *,
        freq: str = "day",
        bar_seconds: int = 60,
        lot_size: int = 100,
        auction_window: str = "open",
        instance_id: str = "legacy",
        config_hash: str = "",
        state_path: str | Path | None = None,
        bar_source: Any | None = None,
        execution_journal: Any | None = None,
        route_port: Any | None = None,
        runtime_id: str = "",
    ) -> None:
        if freq not in ("day", "min"):
            raise ValueError(f"freq must be 'day' or 'min', got {freq!r}")
        self.engine = engine
        self.strategy = strategy
        self.symbols = list(symbols)
        self.freq = freq
        self.lot_size = int(lot_size)
        self.auction_window = auction_window
        self.instance_id = str(instance_id or "legacy")
        self.config_hash = str(config_hash or "")
        self.state_path = Path(state_path).expanduser() if state_path else None
        self.bar_source = bar_source
        self.runtime_id = str(runtime_id or "")

        interval = DAY_INTERVAL if freq == "day" else int(bar_seconds)
        self.interval = interval
        self.bars = BarAggregator(interval, on_bar=self._on_bar_closed)

        self.pending_requests: list[OrderRequest] = []
        self.pending_intents: list[OrderIntent] = []
        self.algo: CallAuctionAlgo | None = None
        self._last_session_state: SessionState | None = None
        self._finalized_day: date | None = None
        self._started = False
        self._paused = False
        self._stopped = False
        self._reconcile_required = False
        self._bar_queue: deque[Bar] = deque(maxlen=10_000)
        self.runtime_store = execution_journal or InMemoryExecutionJournal()
        if route_port is not None:
            self.route_port = route_port
        elif engine.config.mode in {RunMode.LIVE, RunMode.SHADOW}:
            self.route_port = AutomatedOrderRouter(
                engine,
                None,
                lambda: RouteContext(
                    origin=RouteOrigin.AUTOMATED,
                    instance_id=self.instance_id,
                    config_hash=self.config_hash,
                    run_mode=engine.config.mode,
                    runtime_id=self.runtime_id,
                ),
            )
        else:
            # Standalone PAPER construction is retained for old tests and CLI.
            self.route_port = engine
        self._last_cancel_report: dict[str, Any] = {}

    # ---- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        """Attach to the engine's tick stream and subscribe market data."""
        if self._started and not self._stopped:
            return
        self._stopped = False
        self._paused = bool(self._reconcile_required)
        if self.bar_source is None:
            self.engine.add_tick_listener(self.on_tick)
        else:
            self.bar_source.add_bar_listener(self.interval, self._enqueue_bar)
        self.engine.subscribe_market_data(self.symbols)
        initialize = getattr(self.strategy, "initialize", None)
        if callable(initialize):
            initialize()
        sync = getattr(self.strategy, "synchronize_positions", None)
        if callable(sync):
            sync({position.key for position in self.engine.oms.get_positions() if position.volume > 0})
        self._started = True
        self._checkpoint()
        self._heartbeat()

    def pause(self, *, cancel_active: bool = True) -> dict[str, Any]:
        """Pause strategy signal generation without disconnecting market data."""
        if not self._stopped:
            self._paused = True
            if cancel_active:
                self._last_cancel_report = self._cancel_owned_orders()
        self._checkpoint()
        self._heartbeat()
        return self.status()

    def resume(self) -> dict[str, Any]:
        """Resume a paused runner."""
        if self._reconcile_required:
            raise RuntimeError("runner recovery must be reconciled before resume")
        if self._started and not self._stopped:
            self._paused = False
        self._checkpoint()
        self._heartbeat()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Stop strategy actions for the lifetime of this runner instance."""
        self._paused = False
        self._stopped = True
        self.pending_requests = []
        self.pending_intents = []
        self.algo = None
        self._last_cancel_report = self._cancel_owned_orders()
        if self.bar_source is not None:
            self.bar_source.remove_bar_listener(self.interval, self._enqueue_bar)
        else:
            self.engine.remove_tick_listener(self.on_tick)
        stop_strategy = getattr(self.strategy, "stop", None)
        if callable(stop_strategy):
            stop_strategy("runner_stopped")
        self._checkpoint()
        self._heartbeat()
        return self.status()

    def status(self) -> dict[str, Any]:
        """Return a compact lifecycle/status projection."""
        return {
            "started": self._started,
            "paused": self._paused,
            "stopped": self._stopped,
            "active": self._started and not self._paused and not self._stopped,
            "symbols": list(self.symbols),
            "freq": self.freq,
            "pending_requests": len(self.pending_requests),
            "pending_intents": len(self.pending_intents),
            "algo_armed": self.algo is not None or bool(self.pending_intents),
            "last_session": None if self._last_session_state is None else self._last_session_state.value,
            "instance_id": self.instance_id,
            "config_hash": self.config_hash,
            "runtime_id": self.runtime_id,
            "reconcile_required": self._reconcile_required,
            "cancel_report": dict(self._last_cancel_report),
            "queued_bars": len(self._bar_queue),
            "lifecycle": (
                "stopped" if self._stopped else
                "paused_pending_reconcile" if self._reconcile_required else
                "paused" if self._paused else
                "warming_up" if not self._is_ready() else "running"
            ),
        }

    # ---- event inputs -------------------------------------------------------- #
    def on_tick(self, tick: TickData) -> None:
        if self._paused or self._stopped:
            return
        self.bars.on_tick(tick)

    def step(self) -> dict[str, Any]:
        """Advance time-driven work; call periodically from the owner loop."""
        if self._stopped:
            status = self.status()
            status["session"] = None
            return status

        state = self.engine.session.tick()
        if self._paused:
            self._heartbeat()
            status = self.status()
            status["session"] = state.value
            return status

        while self._bar_queue:
            self._on_bar_closed(self._bar_queue.popleft())

        # Close partial bars at session boundaries so the last bar of a half-day
        # doesn't wait for a tick that will never come.
        if state != self._last_session_state:
            if self.bar_source is None and state in (SessionState.LUNCH_BREAK, SessionState.POST_CLOSE) and self.freq == "min":
                self.bars.flush()
            if state == SessionState.POST_CLOSE:
                self._finalize_day()
            self._last_session_state = state

        if self.freq == "day" and state == SessionState.CALL_AUCTION_OPEN:
            self._arm_pending_intents()

        if self.algo is not None:
            if self.algo.step() == AlgoState.DONE:
                self.algo = None

        self._heartbeat()
        return {
            **self.status(),
            "session": state.value,
        }

    # ---- internals ------------------------------------------------------------ #
    def _on_bar_closed(self, bar: Bar) -> None:
        if self._paused or self._stopped:
            return
        try:
            intents = self.strategy.on_bar(bar)
        except Exception as exc:
            self._record_runtime_event(
                "unresolved_errors",
                details={"where": "strategy.on_bar", "error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        if not intents:
            return
        if self.freq == "min":
            decision_id = self._decision_id(intents)
            requests = orders_from_intents(
                intents, self.engine.oms, {bar.instrument: bar.close}, lot_size=self.lot_size,
                instance_id=self.instance_id, config_hash=self.config_hash, decision_id=decision_id,
            )
            self._journal_decision(decision_id, intents)
            for req in self._journal_requests(decision_id, requests, status="routing"):
                self.submit(req)
        else:
            # Persist decisions, not stale close-priced orders. Requests are
            # built from next-session quotes immediately before the auction.
            self.pending_intents.extend(item for item in intents if self._intent_changes_account(item))
        self._checkpoint()

    def _enqueue_bar(self, bar: Bar) -> None:
        if not self._paused and not self._stopped:
            self._bar_queue.append(bar)

    def _finalize_day(self) -> None:
        today = self.engine.session._now_fn().date() if hasattr(self.engine.session, "_now_fn") else None
        if today is not None and today == self._finalized_day:
            return
        self._finalized_day = today

        if self.freq == "day" and self.bar_source is None:
            self.bars.flush()  # closes the daily bar -> _on_bar_closed -> pending
        self._checkpoint()

    def _arm_pending_intents(self) -> None:
        if not self.pending_intents or self.algo is not None:
            return
        prices: dict[str, float] = {}
        now = self.engine.session._now_fn() if hasattr(self.engine.session, "_now_fn") else datetime.now()
        signal_days = self._pending_signal_days()
        if len(signal_days) != 1:
            self._discard_pending_target(
                "pending daily intents do not share one valid signal session",
                event_type="unresolved_errors",
            )
            return
        effective_day = self._next_trading_day(next(iter(signal_days)), now)
        if effective_day is None:
            self._discard_pending_target(
                "the next trading session could not be resolved from the configured calendar",
                event_type="unresolved_errors",
            )
            return
        if now.date() < effective_day:
            return
        if now.date() > effective_day:
            self._discard_pending_target(
                f"daily target for {effective_day.isoformat()} expired before execution",
                event_type="expired_targets",
            )
            return
        for intent in self.pending_intents:
            from alphapilot.systems.live.types import normalize_symbol, symbol_key

            code, exchange = normalize_symbol(intent.instrument)
            tick = self.engine.oms.get_tick(symbol_key(code, exchange))
            if tick is None or tick.last_price <= 0:
                return
            if (
                requires_live_market_safety(self.engine.config.mode)
                and tick.datetime is not None
                and tick.datetime.date() != now.date()
            ):
                return
            prices[intent.instrument] = float(tick.last_price)
        decision_id = self._decision_id(self.pending_intents)
        self._journal_decision(decision_id, self.pending_intents)
        requests = orders_from_intents(
            self.pending_intents, self.engine.oms, prices, lot_size=self.lot_size,
            instance_id=self.instance_id, config_hash=self.config_hash, decision_id=decision_id,
        )
        self.pending_intents = []
        requests = self._journal_requests(decision_id, requests, status="armed")
        if requests:
            self.algo = CallAuctionAlgo(
                self.engine,
                requests,
                window=self.auction_window,
                route_port=self,
            )
        self._checkpoint()

    def _pending_signal_days(self) -> set[date]:
        days: set[date] = set()
        for intent in self.pending_intents:
            value = getattr(intent, "datetime", None)
            if isinstance(value, datetime):
                days.add(value.date())
                continue
            if isinstance(value, date):
                days.add(value)
                continue
            try:
                days.add(date.fromisoformat(str(value)[:10]))
            except ValueError:
                return set()
        return days

    def _next_trading_day(self, signal_day: date, now: datetime) -> date | None:
        predicate = getattr(self.engine.session, "_is_trading_day_fn", None)
        if not callable(predicate):
            return None
        for offset in range(1, 32):
            candidate = signal_day + timedelta(days=offset)
            probe = datetime.combine(candidate, datetime_time(12), tzinfo=now.tzinfo)
            try:
                if predicate(probe):
                    return candidate
            except Exception:  # noqa: BLE001 - calendar failure must fail closed
                return None
        return None

    def _discard_pending_target(self, reason: str, *, event_type: str) -> None:
        count = len(self.pending_intents)
        self.pending_intents = []
        self._record_runtime_event(
            event_type,
            details={"reason": reason, "discarded_intents": count},
        )
        self.engine.ledger.record(
            "blocked",
            {
                "origin": "automated",
                "rule": "effective_session",
                "reason": reason,
                "instance_id": self.instance_id,
            },
        )
        self._checkpoint()

    def _journal_decision(self, decision_id: str, intents: list[Any]) -> None:
        self.runtime_store.record_decision(
            decision_id,
            self.instance_id,
            self.config_hash,
            [asdict(item) if hasattr(item, "__dataclass_fields__") else repr(item) for item in intents],
        )

    def _journal_requests(
        self, decision_id: str, requests: list[OrderRequest], *, status: str
    ) -> list[OrderRequest]:
        from alphapilot.systems.live.runtime import order_request_to_dict

        plan_id = hashlib.sha256(f"plan:{decision_id}".encode("utf-8")).hexdigest()[:24]
        payload = [order_request_to_dict(request) for request in requests]
        self.runtime_store.record_plan(plan_id, decision_id, self.instance_id, payload, status)
        accepted: list[OrderRequest] = []
        for request in requests:
            if self.runtime_store.record_child_order(
                request.reference,
                plan_id,
                order_request_to_dict(request),
                status=status,
            ):
                accepted.append(request)
            else:
                self._record_runtime_event(
                    "duplicate_routes",
                    details={"reference": request.reference, "plan_id": plan_id},
                )
        return accepted

    def _decision_id(self, intents: list[Any]) -> str:
        payload = json.dumps(
            [asdict(item) if hasattr(item, "__dataclass_fields__") else repr(item) for item in intents],
            default=str, sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(
            f"{self.instance_id}:{self.config_hash}:{payload}".encode("utf-8")
        ).hexdigest()[:24]

    def _intent_changes_account(self, intent: Any) -> bool:
        if getattr(intent, "action", "") not in {"close", "target_percent", "target_shares"}:
            return True
        target = getattr(intent, "quantity", None)
        if getattr(intent, "action", "") == "target_percent":
            target = getattr(intent, "target_percent", None)
        if float(target or 0.0) > 0:
            return True
        from alphapilot.systems.live.types import normalize_symbol, symbol_key

        code, exchange = normalize_symbol(intent.instrument)
        key = symbol_key(code, exchange)
        position = self.engine.oms.get_position(key)
        if position is not None and position.volume > 0:
            return True
        return any(order.key == key for order in self.engine.oms.get_active_orders())

    def _is_ready(self) -> bool:
        history = getattr(self.strategy, "_history", {})
        required = int(getattr(self.strategy, "min_bars", 1) or 1)
        if not history or not self.symbols:
            return False
        from alphapilot.systems.live.types import normalize_symbol, symbol_key

        expected = []
        for raw_symbol in self.symbols:
            code, exchange = normalize_symbol(raw_symbol)
            expected.append(symbol_key(code, exchange))
        return all(len(history.get(key, ())) >= required for key in expected)

    def _cancel_owned_orders(self) -> dict[str, Any]:
        prefix = f"{self.instance_id}:"
        report: dict[str, Any] = {"attempted": [], "cancelled": [], "errors": []}
        for order in list(self.engine.oms.get_active_orders()):
            if str(order.reference).startswith(prefix):
                report["attempted"].append(str(order.order_id))
                try:
                    result = self.engine.cancel(order)
                    if result.get("cancelled"):
                        report["cancelled"].append(str(order.order_id))
                    else:
                        report["errors"].append({
                            "order_id": str(order.order_id),
                            "reason": result.get("reason") or "cancel was not accepted",
                        })
                except Exception as exc:  # noqa: BLE001 - halt remains fail-closed
                    report["errors"].append({
                        "order_id": str(order.order_id),
                        "reason": f"{type(exc).__name__}: {exc}",
                    })
        return report

    def submit(self, request: OrderRequest) -> str | None:
        """Route one already-journaled automated child and persist its result."""

        order_id = self.route_port.submit(request)
        self.runtime_store.update_child_order(
            request.reference,
            status="submitted" if order_id else "rejected",
            order_id=str(order_id or ""),
        )
        if not order_id:
            events = self.engine.ledger.events(reference=request.reference, limit=5)
            payload: dict[str, Any] = {}
            for event in reversed(events):
                if event.get("kind") in {"blocked", "rejected"}:
                    candidate = event.get("payload")
                    payload = candidate if isinstance(candidate, dict) else {}
                    break
            rule = str(payload.get("rule") or "")
            self._record_runtime_event(
                "route_rejections",
                details={"reference": request.reference, "rule": rule},
            )
            if rule == "duplicate":
                self._record_runtime_event(
                    "duplicate_routes",
                    details={"reference": request.reference, "source": "risk_gate"},
                )
            if rule in {
                "max_position_pct",
                "max_total_position_pct",
                "max_position_count",
            }:
                self._record_runtime_event(
                    "position_breaches",
                    details={"reference": request.reference},
                )
        return order_id

    def snapshot(self) -> dict[str, Any]:
        strategy_state = getattr(self.strategy, "snapshot", lambda: {})()
        return {
            "version": 1,
            "instance_id": self.instance_id,
            "config_hash": self.config_hash,
            "started": self._started,
            "paused": self._paused,
            "stopped": self._stopped,
            "finalized_day": None if self._finalized_day is None else self._finalized_day.isoformat(),
            "last_session": None if self._last_session_state is None else self._last_session_state.value,
            "pending_intents": [asdict(item) for item in self.pending_intents],
            "strategy": strategy_state,
        }

    def restore(self, state: dict[str, Any], *, require_reconcile: bool = True) -> None:
        if int(state.get("version") or 0) != 1:
            raise ValueError("unsupported LiveTimingRunner state version")
        if state.get("instance_id") != self.instance_id or state.get("config_hash", "") != self.config_hash:
            raise ValueError("runner checkpoint does not match instance/config")
        restore_strategy = getattr(self.strategy, "restore", None)
        if callable(restore_strategy):
            restore_strategy(state.get("strategy") or {})
        self.pending_intents = [OrderIntent(**item) for item in (state.get("pending_intents") or [])]
        self._finalized_day = date.fromisoformat(state["finalized_day"]) if state.get("finalized_day") else None
        self._paused = bool(require_reconcile)
        self._reconcile_required = bool(require_reconcile)

    def mark_reconciled(self, report: dict[str, Any] | None = None) -> dict[str, Any]:
        warnings = list((report or {}).get("warnings") or [])
        if warnings:
            raise RuntimeError("runner recovery has unresolved reconciliation warnings")
        self._reconcile_required = False
        self._checkpoint()
        self._heartbeat()
        return self.status()

    def mark_reconcile_required(self) -> dict[str, Any]:
        """Force a restored or newly started LIVE runner into fail-closed pause."""

        self._reconcile_required = True
        self._paused = True
        self._checkpoint()
        self._heartbeat()
        return self.status()

    def _heartbeat(self) -> None:
        run_mode = str(self.engine.config.mode)
        if run_mode in {RunMode.PAPER, RunMode.SIMULATION, RunMode.SHADOW, RunMode.LIVE}:
            record_session = getattr(self.runtime_store, "record_runtime_session", None)
            state_value = str(getattr(self.engine.session.state, "value", self.engine.session.state))
            if callable(record_session) and state_value not in {"closed", "pre_open", "post_close"}:
                now = (
                    self.engine.session._now_fn()
                    if hasattr(self.engine.session, "_now_fn") else datetime.now()
                )
                record_session(
                    self.instance_id,
                    config_hash=self.config_hash,
                    run_mode=run_mode,
                    session=now.date().isoformat(),
                )
        if not self.runtime_id:
            return
        record = getattr(self.runtime_store, "record_runtime_heartbeat", None)
        if not callable(record):
            return
        record(
            self.instance_id,
            config_hash=self.config_hash,
            runtime_id=self.runtime_id,
            heartbeat_at=utc_now_iso(),
            observed_state=self.status()["lifecycle"],
        )

    def _record_runtime_event(self, event_type: str, *, details: Any = None) -> None:
        run_mode = str(self.engine.config.mode)
        if run_mode not in {RunMode.PAPER, RunMode.SIMULATION, RunMode.SHADOW, RunMode.LIVE}:
            return
        record = getattr(self.runtime_store, "record_runtime_event", None)
        if callable(record):
            record(
                self.instance_id,
                config_hash=self.config_hash,
                run_mode=run_mode,
                event_type=event_type,
                details=details,
            )

    def _checkpoint(self) -> None:
        if self.state_path is not None:
            atomic_write_json(self.state_path, self.snapshot())

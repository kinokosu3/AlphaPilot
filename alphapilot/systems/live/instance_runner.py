"""Runtime for validated strategy instances using the shared decision pipeline."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import datetime, time, timedelta
import os
from typing import Any

from alphapilot.systems.live.bars import DAY_INTERVAL, Bar
from alphapilot.systems.live.execution_adapter import (
    LiveAccountSnapshotAdapter,
    LiveExecutionRouteAdapter,
)
from alphapilot.systems.trading.account_guard import AccountBoundaryGuard
from alphapilot.systems.trading.application import (
    DecisionPipeline,
    WarmupRequired,
    canonical_hash,
)
from alphapilot.systems.trading.contracts import (
    CompletedBar,
    ExecutionPhase,
    FeeSchedule,
    InstrumentMetadata,
    PortfolioDecision,
    PriceAdjustment,
)
from alphapilot.systems.live.fsm.session_fsm import SessionState
from alphapilot.systems.live.types import normalize_symbol
from alphapilot.systems.trading.domain import LifecycleState, StrategyInstanceConfig
from alphapilot.systems.trading.execution import ExecutionCoordinator
from alphapilot.systems.trading.planning import ExecutionPlanner
from alphapilot.systems.trading.registry import resolve_required_history


class LiveCalendarAdapter:
    def __init__(self, predicate: Any, *, minute_seconds: int = 60) -> None:
        if not callable(predicate):
            raise RuntimeError("a configured trading calendar is required")
        self.predicate = predicate
        self.minute_seconds = max(int(minute_seconds), 1)

    def is_trading_session(self, value: str) -> bool:
        probe = datetime.fromisoformat(str(value)[:10] + "T12:00:00")
        return bool(self.predicate(probe))

    def next_trading_session(self, value: str) -> str:
        start = datetime.fromisoformat(str(value)[:10] + "T12:00:00")
        for offset in range(1, 32):
            candidate = start + timedelta(days=offset)
            if self.predicate(candidate):
                return candidate.date().isoformat()
        raise RuntimeError("trading calendar has no next session within 31 days")

    def next_effective(self, value: str, frequency: str) -> str:
        if frequency == "day":
            return self.next_trading_session(value)
        current = datetime.fromisoformat(value)
        candidate = current + timedelta(seconds=self.minute_seconds)
        if current.time() < time(11, 30) <= candidate.time():
            candidate = current.replace(hour=13, minute=0, second=0, microsecond=0)
        if candidate.time() >= time(15, 0):
            next_day = self.next_trading_session(current.date().isoformat())
            next_date = datetime.fromisoformat(next_day).date()
            candidate = current.replace(
                year=next_date.year, month=next_date.month, day=next_date.day,
                hour=9, minute=30, second=0, microsecond=0,
            )
        return candidate.isoformat()

    def valid_until(self, effective: str, frequency: str) -> str:
        if frequency == "day":
            return f"{str(effective)[:10]}T15:00:00+08:00"
        return (
            datetime.fromisoformat(effective) + timedelta(seconds=self.minute_seconds)
        ).isoformat()


class LiveInstrumentMetadataAdapter:
    def __init__(self, oms: Any, *, default_lot_size: int = 100) -> None:
        self.oms = oms
        self.default_lot_size = int(default_lot_size)

    def get_instruments(self, instruments: list[str] | tuple[str, ...]) -> dict[str, InstrumentMetadata]:
        result: dict[str, InstrumentMetadata] = {}
        for instrument in instruments:
            contract = self.oms.get_contract(instrument)
            if contract is None:
                continue
            product = str(getattr(getattr(contract, "product", "equity"), "value", getattr(contract, "product", "equity")))
            result[instrument] = InstrumentMetadata(
                instrument=instrument,
                asset_type=product,
                lot_size=int(getattr(contract, "lot_size", 0) or self.default_lot_size),
                price_tick=float(getattr(contract, "price_tick", 0.0) or 0.01),
                settlement_days=max(int(getattr(contract, "settlement_days", 1)), 0),
                long_only=product in {"equity", "fund"},
            )
        return result


class StrategyInstanceRunner:
    """Evaluate providers and execute persisted decisions without exposing Broker."""

    def __init__(
        self,
        *,
        runtime: Any,
        trading: Any,
        instance: StrategyInstanceConfig,
        historical_data: Any,
        bar_source: Any,
        bar_seconds: int = 60,
        store: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.engine = runtime.engine
        self.trading = trading
        self.instance = instance
        self._universe = frozenset(
            f"{code}.{exchange.value}"
            for code, exchange in (
                normalize_symbol(symbol) for symbol in instance.universe
            )
        )
        self.historical_data = historical_data
        self.bar_source = bar_source
        self.store = store if store is not None else trading.store
        self.interval = DAY_INTERVAL if instance.frequency == "day" else int(bar_seconds)
        predicate = getattr(self.engine.session, "_is_trading_day_fn", None)
        self.calendar = LiveCalendarAdapter(predicate, minute_seconds=bar_seconds)
        self.account_port = LiveAccountSnapshotAdapter(
            runtime,
            instance_id=instance.instance_id,
            config_hash=instance.config_hash,
        )
        self.metadata_port = LiveInstrumentMetadataAdapter(
            self.engine.oms,
            default_lot_size=self.engine.config.risk.lot_size,
        )
        self.pipeline = DecisionPipeline(
            strategy_registry=trading.registry,
            policy_registry=trading.policy_registry,
            store=self.store,
            calendar=self.calendar,
        )
        self.planner = ExecutionPlanner(
            lot_size=self.engine.config.risk.lot_size,
            max_order_value=self.engine.config.risk.max_order_value,
            max_order_equity_pct=self.engine.config.risk.max_order_equity_pct,
        )
        runtime_state = self.store.get_runtime_state(instance.instance_id)
        deployment = self.store.get_deployment_spec(instance.instance_id)
        if deployment.get("stale"):
            raise ValueError("deployment is stale; configure it for the current instance")
        automated_router = runtime.automated_order_router(
            instance_id=instance.instance_id,
            config_hash=instance.config_hash,
            run_mode=deployment["run_mode"],
            execution_environment=str(runtime_state.get("execution_environment") or ""),
            trade_provider=str(runtime_state.get("trade_provider") or ""),
            quote_provider=str(runtime_state.get("quote_provider") or ""),
            quote_data_kind=str(runtime_state.get("quote_data_kind") or ""),
            binding_hash=str(runtime_state.get("binding_hash") or ""),
        )
        self.route_port = LiveExecutionRouteAdapter(runtime, automated_router)
        mode = str(getattr(self.engine.config.mode, "value", self.engine.config.mode))
        live_enabled = os.getenv("ALPHAPILOT_AUTOMATED_LIVE_ENABLED", "false").lower() in {
            "1", "true", "yes", "on",
        }
        can_route = (
            mode == "paper"
            or mode == "simulation"
            or (mode == "live" and live_enabled)
        )
        self.execution = ExecutionCoordinator(
            store=self.store,
            account_port=self.account_port,
            route_port=self.route_port,
            planner=self.planner,
            can_route=can_route,
            shadow=mode == "shadow",
            expected_account_id=str(runtime_state.get("account_id") or ""),
        )
        self.mode = mode
        self.history: deque[CompletedBar] = deque(maxlen=20_000)
        self._pending_session: dict[str, CompletedBar] = {}
        self._started = False
        self._paused = mode in {"simulation", "shadow", "live"}
        self._stopped = False
        self._reconcile_required = mode in {"simulation", "shadow", "live"}
        self._last_error: dict[str, Any] = {}
        self._last_decision_id = ""
        self._last_execution_plan = ""
        self._last_bar_session = ""
        self._last_cancel_report: dict[str, Any] = {"attempted": [], "errors": []}

    def start(self) -> None:
        if self._started and not self._stopped:
            return
        if self.instance.frequency != "day" and self.mode in {"simulation", "live"}:
            raise ValueError("minute strategy instances cannot run against external A-share accounts")
        self._load_history()
        try:
            self.bar_source.add_bar_listener(
                self.interval,
                self._on_bar,
                symbols=self._universe,
            )
        except TypeError:
            self.bar_source.add_bar_listener(self.interval, self._on_bar)
        try:
            self.engine.subscribe_market_data(
                list(self.instance.universe),
                subscription_type="strategy",
            )
        except TypeError:
            self.engine.subscribe_market_data(list(self.instance.universe))
        self._started = True
        self._stopped = False
        self._heartbeat()

    def pause(self, *, cancel_active: bool = True) -> dict[str, Any]:
        self._paused = True
        owned_orders = [
            order
            for order in self.engine.oms.get_active_orders()
            if str(order.reference or "").startswith(
                f"{self.instance.instance_id}:{self.instance.config_hash}:"
            )
        ]
        attempted = [str(order.order_id) for order in owned_orders]
        errors: list[dict[str, Any]] = []
        planned_references: set[str] = set()
        if cancel_active:
            for state in self.store.list_unfinished_execution_plans(self.instance.instance_id):
                planned_references.update(
                    str(child.get("reference") or "")
                    for child in ((state.get("payload") or {}).get("plan") or {}).get("children", [])
                )
                paused = self.execution.pause(state["plan_id"], "deployment paused")
                failures = list((paused.get("last_error") or {}).get("cancel_failures") or [])
                errors.extend(
                    {"reference": reference, "reason": "broker cancellation was not confirmed"}
                    for reference in failures
                )
            # An owned Broker order missing from the execution journal is an
            # inconsistency, but pause still makes a best-effort cancellation.
            for order in owned_orders:
                if str(order.reference or "") in planned_references:
                    continue
                try:
                    cancelled = self.runtime.cancel_order(str(order.order_id))
                except Exception as exc:  # noqa: BLE001 - report every failed cancellation
                    errors.append({
                        "order_id": str(order.order_id),
                        "reason": f"{type(exc).__name__}: {exc}",
                    })
                else:
                    if not bool(cancelled.get("cancelled")):
                        errors.append({
                            "order_id": str(order.order_id),
                            "reason": str(
                                (cancelled.get("result") or {}).get("reason")
                                or "cancellation was not sent"
                            ),
                        })
        self._last_cancel_report = {"attempted": attempted, "errors": errors}
        self._heartbeat()
        return self.status()

    def reconcile_recovery(self) -> dict[str, Any]:
        warnings: list[dict[str, Any]] = []
        for state in self.store.list_unfinished_execution_plans(self.instance.instance_id):
            recovered = self.execution.recover(state["plan_id"])
            if recovered["phase"] == "paused":
                warnings.append(recovered["last_error"])
                self.store.update_decision_status(recovered["decision_id"], "blocked")
            elif recovered["phase"] == "completed":
                self.store.update_decision_status(recovered["decision_id"], "completed")
        snapshot = self.account_port.account_snapshot()
        runtime_state = self.store.get_runtime_state(self.instance.instance_id)
        expected_positions: dict[str, float] | None = None
        latest_target = self.store.latest_execution_target(
            self.instance.instance_id, self.instance.config_hash,
        )
        if latest_target is not None:
            expected_positions = {
                str(key): float(value)
                for key, value in (latest_target.get("holdings") or {}).items()
            }
        boundary = AccountBoundaryGuard().validate(
            snapshot,
            universe=self.instance.universe,
            expected_account_id=runtime_state["account_id"],
            expected_positions=expected_positions,
            allow_position_changes=expected_positions is None,
        )
        warnings.extend(boundary.issues)
        self._reconcile_required = bool(warnings)
        self._paused = True
        self._heartbeat()
        return {"ok": not warnings, "warnings": warnings, "status": self.status()}

    def mark_reconciled(self, report: dict[str, Any] | None = None) -> dict[str, Any]:
        """Daemon control adapter: combine runtime and strategy-plan truth."""

        external = list((report or {}).get("warnings") or [])
        strategy = self.reconcile_recovery()
        warnings = [*external, *list(strategy.get("warnings") or [])]
        if report is not None:
            report["strategy_recovery"] = strategy
            report["warnings"] = warnings
        self._reconcile_required = bool(warnings)
        self._paused = True
        self._heartbeat()
        if warnings:
            raise RuntimeError("runner recovery has unresolved reconciliation warnings")
        return self.status()

    def mark_reconcile_required(self) -> dict[str, Any]:
        self._reconcile_required = True
        self._paused = True
        self._heartbeat()
        return self.status()

    def resume(self) -> dict[str, Any]:
        if self._reconcile_required:
            raise RuntimeError("runner recovery must be reconciled before resume")
        self._paused = False
        self._heartbeat()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self.pause(cancel_active=True)
        self._stopped = True
        self._paused = False
        self.bar_source.remove_bar_listener(self.interval, self._on_bar)
        self.pipeline.close("runner_stopped")
        self._heartbeat()
        return self.status()

    def step(self) -> dict[str, Any]:
        if not self._started or self._stopped:
            self._heartbeat()
            return {**self.status(), "session": self.engine.session.state.value}
        state = self.engine.tick_session()
        if self._paused:
            self._heartbeat()
            return {**self.status(), "session": state.value}
        now = self.engine.session._now_fn()
        session = now.date().isoformat() if self.instance.frequency == "day" else now.isoformat()
        observed_at = now.isoformat()
        may_create_plan = (
            state == SessionState.CALL_AUCTION_OPEN
            if self.instance.frequency == "day"
            else self.engine.session.can_submit()
        )
        unfinished_before = self.store.list_unfinished_execution_plans(
            self.instance.instance_id
        )
        paused_plans = [
            item for item in unfinished_before
            if item["phase"] == ExecutionPhase.PAUSED.value
        ]
        if paused_plans:
            self._halt(
                "an unfinished execution plan requires reconciliation: "
                f"{paused_plans[0]['plan_id']}"
            )
            return {**self.status(), "session": state.value}
        # A strategy instance owns one account target at a time. A later
        # decision cannot overlap an execution whose Broker truth is unsettled.
        may_create_plan = may_create_plan and not unfinished_before
        for row in self.store.list_due_decisions(self.instance.instance_id, session):
            if row["status"] != "pending":
                continue
            decision = PortfolioDecision.from_dict(row["decision"])
            if decision.valid_until and _timestamp_after(observed_at, decision.valid_until):
                self.store.update_decision_status(decision.decision_id, "expired")
                self._runtime_event(
                    "expired_targets",
                    details={
                        "decision_id": decision.decision_id,
                        "valid_until": decision.valid_until,
                        "observed_at": observed_at,
                    },
                )
                continue
            # Daily targets are concretised from D+1 account/quote truth only
            # in the opening auction. If that window is missed, the target is
            # left pending until it expires; it is never submitted at noon or
            # on a later session. Minute targets wait for a legal submit state.
            if not may_create_plan:
                continue
            try:
                self._plan_decision(decision, session)
            except Exception as exc:  # noqa: BLE001 - planning must fail closed
                self.store.update_decision_status(decision.decision_id, "blocked")
                self._halt(f"decision planning failed: {type(exc).__name__}: {exc}")
                return {**self.status(), "session": state.value}
        for state in self.store.list_unfinished_execution_plans(self.instance.instance_id):
            if state["phase"] != "paused":
                phase = ExecutionPhase(state["phase"])
                if (
                    phase in {
                        ExecutionPhase.PLANNED,
                        ExecutionPhase.SELLING,
                        ExecutionPhase.REFRESHING_ACCOUNT,
                        ExecutionPhase.BUYING,
                    }
                    and not self.engine.session.can_submit()
                ):
                    continue
                advanced = self.execution.advance(state["plan_id"])
                if advanced["phase"] == "paused":
                    self.store.update_decision_status(advanced["decision_id"], "blocked")
                    self._runtime_event("unresolved_errors", details=advanced["last_error"])
                    self._halt(
                        "execution plan paused and requires reconciliation: "
                        f"{advanced['plan_id']}"
                    )
                    return {**self.status(), "session": self.engine.session.state.value}
                elif advanced["phase"] == "completed":
                    self.store.update_decision_status(advanced["decision_id"], "completed")
        self._heartbeat()
        return {**self.status(), "session": self.engine.session.state.value}

    def status(self) -> dict[str, Any]:
        provider_required = resolve_required_history(
            self.trading.registry.get(self.instance.strategy_id),
            self.instance.params,
        )
        required = int(
            self.instance.data_policy.get("history_window") or provider_required
        )
        if required < provider_required:
            # Validation normally prevents this.  Keep runtime status fail-closed
            # even when a corrupted projection reaches the runner.
            required = provider_required
        counts = {
            instrument: sum(bar.instrument == instrument for bar in self.history)
            for instrument in self.instance.universe
        }
        available = min(counts.values(), default=0)
        lifecycle = (
            LifecycleState.STOPPED.value if self._stopped
            else LifecycleState.PAUSED_PENDING_RECONCILE.value if self._reconcile_required
            else LifecycleState.PAUSED.value if self._paused
            else LifecycleState.WARMING_UP.value if available < required
            else LifecycleState.RUNNING.value
        )
        return {
            "started": self._started,
            "paused": self._paused,
            "stopped": self._stopped,
            "active": self._started and not self._paused and not self._stopped,
            "instance_id": self.instance.instance_id,
            "config_hash": self.instance.config_hash,
            "runtime_id": self.runtime.runtime_id,
            "lifecycle": lifecycle,
            "required_history": required,
            "available_history": available,
            "warmup_progress": min(available / required, 1.0) if required else 1.0,
            "last_bar_session": self._last_bar_session,
            "last_decision_id": self._last_decision_id,
            "last_execution_plan": self._last_execution_plan,
            "reconcile_required": self._reconcile_required,
            "cancel_report": dict(self._last_cancel_report),
            "last_error": self._last_error,
        }

    def restore(self, state: dict[str, Any], *, require_reconcile: bool = True) -> None:
        if state.get("config_hash") and state["config_hash"] != self.instance.config_hash:
            raise ValueError("runner checkpoint config_hash mismatch")
        self._reconcile_required = bool(require_reconcile)
        self._paused = bool(require_reconcile)

    def _load_history(self) -> None:
        definition = self.trading.registry.get(self.instance.strategy_id)
        provider_required = resolve_required_history(definition, self.instance.params)
        required = int(
            self.instance.data_policy.get("history_window") or provider_required
        )
        if required < provider_required:
            raise ValueError(
                "data_policy.history_window is below the provider required history"
            )
        adjustment = str(self.instance.data_policy.get("feature_adjustment") or "none")
        try:
            bars = self.historical_data.load_completed_bars(
                instruments=self.instance.universe,
                start=None,
                end=None,
                frequency=self.instance.frequency,
                adjustment=adjustment,
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            # An empty or not-yet-provisioned history store is a warm-up state,
            # never a reason to route with an arbitrary fallback window.  Live
            # completed bars can still fill the exact configured window.
            self._last_error = {
                "kind": "warmup_history_unavailable",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            return
        runtime_version = str(
            self.instance.data_policy.get("data_version")
            or f"runtime:{self.instance.config_hash}"
        )
        bars = [
            CompletedBar.from_dict({**bar.to_dict(), "data_version": runtime_version})
            for bar in bars
        ]
        per_instrument: dict[str, list[CompletedBar]] = {}
        for bar in bars:
            per_instrument.setdefault(bar.instrument, []).append(bar)
        selected = [
            bar
            for instrument in self.instance.universe
            for bar in per_instrument.get(instrument, [])[-required:]
        ]
        self.history.extend(sorted(selected, key=lambda item: (item.datetime, item.instrument)))

    def _on_bar(self, bar: Bar) -> None:
        if (
            self._paused
            or self._stopped
            or bar.instrument not in self._universe
        ):
            return
        expected = PriceAdjustment(str(self.instance.data_policy.get("feature_adjustment") or "none"))
        if expected == PriceAdjustment.NONE:
            completed = CompletedBar(
                datetime=bar.datetime.isoformat(),
                instrument=bar.instrument,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                amount=bar.amount,
                frequency=self.instance.frequency,
                adjustment=PriceAdjustment.NONE,
                data_version=str(
                    self.instance.data_policy.get("data_version")
                    or f"runtime:{self.instance.config_hash}"
                ),
            )
        else:
            # Raw live bars are a completion watermark only. Load the declared
            # adjusted feature bar from the canonical store and require exact
            # instrument/session alignment before evaluating.
            try:
                stored = self.historical_data.load_completed_bars(
                    instruments=(bar.instrument,),
                    start=bar.datetime.date().isoformat(),
                    end=bar.datetime.date().isoformat(),
                    frequency=self.instance.frequency,
                    adjustment=expected.value,
                )
                candidate = next(
                    item for item in reversed(stored)
                    if item.instrument == bar.instrument
                    and (
                        item.datetime[:10] == bar.datetime.date().isoformat()
                        if self.instance.frequency == "day"
                        else item.datetime == bar.datetime.isoformat()
                    )
                )
                completed = CompletedBar.from_dict({
                    **candidate.to_dict(),
                    "data_version": str(
                        self.instance.data_policy.get("data_version")
                        or f"runtime:{self.instance.config_hash}"
                    ),
                })
            except (FileNotFoundError, StopIteration, ValueError) as exc:
                self._halt(
                    "completed adjusted feature bar is unavailable or not aligned: "
                    f"{type(exc).__name__}: {exc}"
                )
                return
        session = completed.datetime[:10] if self.instance.frequency == "day" else completed.datetime
        if self._last_bar_session and session != self._last_bar_session:
            # Never combine stale instruments from different completed-bar
            # timestamps into one cross-universe evaluation.
            self._pending_session.clear()
        self._last_bar_session = session
        self._pending_session[completed.instrument] = completed
        if set(self._pending_session) != self._universe:
            return
        self.history.extend(self._pending_session.values())
        self._pending_session.clear()
        try:
            snapshot = self.account_port.account_snapshot()
            active_run = (
                self.store.get_active_runtime_run(
                    self.instance.instance_id,
                    run_mode=self.mode,
                )
                if self.mode in {"paper", "simulation", "shadow", "live"}
                else None
            )
            result = self.pipeline.evaluate(
                self.instance,
                tuple(self.history),
                account=snapshot,
                persist=True,
                mode=self.mode,
                run_id=(
                    str(active_run["run_id"])
                    if active_run is not None
                    else str(self.runtime.runtime_id)
                ),
            )
            self._last_decision_id = result.decision.decision_id
            if self.mode in {"paper", "simulation", "shadow", "live"}:
                self.store.record_runtime_session(
                    self.instance.instance_id,
                    config_hash=self.instance.config_hash,
                    run_mode=self.mode,
                    session=result.decision.as_of[:10],
                )
        except WarmupRequired:
            return
        except Exception as exc:  # noqa: BLE001 - fail closed
            self._halt(f"{type(exc).__name__}: {exc}")
        self._heartbeat()

    def _plan_decision(self, decision: PortfolioDecision, session: str) -> None:
        snapshot = self.account_port.account_snapshot()
        quotes = self.account_port.quotes(list(self.instance.universe))
        metadata = self.metadata_port.get_instruments(self.instance.universe)
        expected_positions: dict[str, float] | None = None
        if self.mode == "live":
            latest_target = self.store.latest_execution_target(
                self.instance.instance_id,
                self.instance.config_hash,
            )
            if latest_target is not None:
                expected_positions = {
                    str(key): float(value)
                    for key, value in (latest_target.get("holdings") or {}).items()
                }
        boundary = AccountBoundaryGuard().validate(
            snapshot,
            universe=self.instance.universe,
            expected_account_id=self.store.get_runtime_state(self.instance.instance_id)["account_id"],
            expected_positions=expected_positions,
            allow_position_changes=expected_positions is None,
        )
        if not boundary.ok:
            self._reconcile_required = True
            self._runtime_event("reconciliation_warnings", details=list(boundary.issues))
            self._halt(str(list(boundary.issues)))
            return
        target = self.pipeline.size(
            decision,
            self.instance,
            account=snapshot,
            quotes=quotes,
            instruments=metadata,
            session=session,
            fees=FeeSchedule(
                buy_rate=self.engine.config.risk.buy_fee_rate,
                sell_rate=self.engine.config.risk.sell_fee_rate,
                min_fee=self.engine.config.risk.min_fee,
                max_order_value=self.engine.config.risk.max_order_value,
            ),
        )
        plan = self.planner.plan(target, snapshot, quotes=quotes, instruments=metadata)
        observation_mode = self.mode
        observations = [
            item for item in self.store.list_decision_observations(
                self.instance.instance_id,
                mode=observation_mode,
            )
            if item["config_hash"] == self.instance.config_hash
            and item["decision_id"] == decision.decision_id
        ]
        if not observations:
            raise RuntimeError("decision observation is missing before live execution planning")
        observation = max(observations, key=lambda item: str(item["created_at"]))
        self.store.attach_execution_observation(
            self.instance.instance_id,
            self.instance.config_hash,
            mode=observation_mode,
            run_id=str(observation["run_id"]),
            as_of=decision.as_of,
            account_hash=canonical_hash(asdict(snapshot)),
            quote_hash=canonical_hash({
                str(key): asdict(value) for key, value in quotes.items()
            }),
            instrument_hash=canonical_hash({
                str(key): asdict(value) for key, value in metadata.items()
            }),
            plan_hash=canonical_hash(plan.to_dict()),
        )
        state = self.execution.begin(
            plan,
            target,
            universe=self.instance.universe,
            quotes=quotes,
            instruments=metadata,
        )
        self._last_execution_plan = state["plan_id"]
        if state["phase"] == "paused":
            self.store.update_decision_status(decision.decision_id, "blocked")
            self._reconcile_required = True
            self._halt(f"execution plan blocked: {state['last_error']}")
        else:
            self.store.update_decision_status(decision.decision_id, "planned")

    def _halt(self, reason: str) -> None:
        self._paused = True
        self._reconcile_required = True
        self._last_error = {"reason": reason}
        self.engine.halt(reason)
        self._runtime_event("unresolved_errors", details=self._last_error)
        self.store.transition_runtime(
            self.instance.instance_id,
            lifecycle=LifecycleState.ERROR.value,
            desired_state=LifecycleState.PAUSED.value,
            observed_state=LifecycleState.ERROR.value,
            last_error=self._last_error,
            reconcile_required=True,
            binding_active=False,
        )

    def _heartbeat(self) -> None:
        try:
            accepted = self.store.record_runtime_heartbeat(
                self.instance.instance_id,
                config_hash=self.instance.config_hash,
                runtime_id=self.runtime.runtime_id,
                heartbeat_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                observed_state=self.status()["lifecycle"],
            )
            if not accepted:
                raise RuntimeError("runtime heartbeat binding was rejected")
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            self._paused = True
            self._reconcile_required = True
            reason = f"runtime heartbeat failed closed: {type(exc).__name__}: {exc}"
            self._last_error = {"reason": reason}
            self.engine.halt(reason)

    def _runtime_event(self, event_type: str, *, details: Any = None) -> None:
        self.store.record_runtime_event(
            self.instance.instance_id,
            config_hash=self.instance.config_hash,
            run_mode=self.mode,
            event_type=event_type,
            details=details,
        )


def _timestamp_after(left: str, right: str) -> bool:
    """Compare ISO timestamps without silently accepting timezone ambiguity."""

    if len(str(left)) == 10 and len(str(right)) >= 10:
        return str(left)[:10] > str(right)[:10]
    lhs = datetime.fromisoformat(str(left))
    rhs = datetime.fromisoformat(str(right))
    if lhs.tzinfo is None and rhs.tzinfo is not None:
        lhs = lhs.replace(tzinfo=rhs.tzinfo)
    elif lhs.tzinfo is not None and rhs.tzinfo is None:
        rhs = rhs.replace(tzinfo=lhs.tzinfo)
    return lhs > rhs

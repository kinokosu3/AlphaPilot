"""Persistent, broker-independent execution state machine."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Mapping

from alphapilot.systems.trading.account_guard import AccountBoundaryGuard
from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    ExecutionChild,
    ExecutionPhase,
    ExecutionPlan,
    TargetPortfolio,
    canonical_instrument,
)


_TERMINAL = {"alltraded", "filled", "cancelled", "canceled", "rejected"}
_ACTIVE = {"planned", "routing", "submitted", "submitting", "nottraded", "parttraded"}


class ExecutionCoordinator:
    """Advance only one persisted phase at a time and recover by broker truth."""

    def __init__(
        self,
        *,
        store: Any,
        account_port: Any,
        route_port: Any,
        planner: Any,
        can_route: bool,
        shadow: bool = False,
        max_replans: int = 8,
        expected_account_id: str = "",
    ) -> None:
        if shadow and can_route:
            raise ValueError("SHADOW execution can never enable routing")
        self.store = store
        self.account_port = account_port
        self.route_port = route_port
        self.planner = planner
        self.can_route = bool(can_route)
        self.shadow = bool(shadow)
        self.max_replans = max(int(max_replans), 1)
        self.expected_account_id = str(expected_account_id)
        self.account_guard = AccountBoundaryGuard()

    def begin(
        self,
        plan: ExecutionPlan,
        target: TargetPortfolio,
        *,
        universe: tuple[str, ...],
        quotes: Mapping[str, Any] | None = None,
        instruments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "plan": plan.to_dict(),
            "target": _target_to_dict(target),
            "universe": list(universe),
            "quotes": {key: asdict(value) for key, value in (quotes or {}).items()},
            "instruments": {key: asdict(value) for key, value in (instruments or {}).items()},
            "replans": 0,
            "route_mode": "shadow" if self.shadow else "routed" if self.can_route else "disabled",
        }
        self.store.record_plan(
            plan.plan_id,
            plan.decision_id,
            plan.instance_id,
            payload,
            "blocked" if not plan.ok else "planned",
        )
        if not plan.ok:
            phase = ExecutionPhase.PAUSED.value
            error = {"rule": "plan_issues", "issues": [asdict(item) for item in plan.issues]}
        else:
            phase = ExecutionPhase.PLANNED.value
            error = {}
        return self.store.save_execution_plan_state(
            plan.plan_id,
            plan.decision_id,
            plan.instance_id,
            plan.config_hash,
            phase=phase,
            payload=payload,
            last_error=error,
            next_child_index=_next_indices(plan.children),
        )

    def advance(self, plan_id: str) -> dict[str, Any]:
        state = self.store.get_execution_plan_state(plan_id)
        phase = ExecutionPhase(state["phase"])
        if phase in {ExecutionPhase.COMPLETED, ExecutionPhase.FAILED, ExecutionPhase.PAUSED}:
            return state
        payload = dict(state["payload"])
        plan = ExecutionPlan.from_dict(payload["plan"])
        target = _target_from_dict(payload["target"])
        snapshot = self.account_port.account_snapshot()
        if target.valid_until and _timestamp_after(snapshot.as_of, target.valid_until):
            return self.pause(
                plan_id,
                f"target expired at {target.valid_until}; observed {snapshot.as_of}",
            )
        boundary = self.account_guard.validate(
            snapshot,
            universe=tuple(payload.get("universe") or ()),
            expected_account_id=self.expected_account_id,
        )
        if not boundary.ok:
            return self._pause(state, {"rule": "account_boundary", "issues": list(boundary.issues)})

        if self.shadow:
            # Persist every mechanical phase, but deliberately never touch the
            # route port. Repeated calls make the progression observable.
            next_phase = {
                ExecutionPhase.PLANNED: ExecutionPhase.SELLING,
                ExecutionPhase.SELLING: ExecutionPhase.WAITING_SELL_REPORTS,
                ExecutionPhase.WAITING_SELL_REPORTS: ExecutionPhase.REFRESHING_ACCOUNT,
                ExecutionPhase.REFRESHING_ACCOUNT: ExecutionPhase.BUYING,
                ExecutionPhase.BUYING: ExecutionPhase.FINAL_RECONCILE,
                ExecutionPhase.FINAL_RECONCILE: ExecutionPhase.COMPLETED,
            }[phase]
            return self._save(state, next_phase, payload)

        if not self.can_route:
            return self._pause(state, {"rule": "routing_disabled", "reason": "runtime cannot route"})
        if phase == ExecutionPhase.PLANNED:
            next_phase = (
                ExecutionPhase.SELLING
                if any(child.side == "sell" for child in plan.children)
                else ExecutionPhase.REFRESHING_ACCOUNT
            )
            return self._save(state, next_phase, payload)
        if phase == ExecutionPhase.SELLING:
            return self._route_phase(state, plan, payload, side="sell")
        if phase == ExecutionPhase.WAITING_SELL_REPORTS:
            return self._wait_phase(
                state,
                plan,
                payload,
                side="sell",
                complete_phase=ExecutionPhase.REFRESHING_ACCOUNT,
            )
        if phase == ExecutionPhase.REFRESHING_ACCOUNT:
            return self._refresh(state, target, payload, snapshot)
        if phase == ExecutionPhase.BUYING:
            routed = self._route_phase(state, plan, payload, side="buy")
            # A routed buy waits in BUYING; _route_phase marks its substate.
            return routed
        if phase == ExecutionPhase.FINAL_RECONCILE:
            return self._final_reconcile(state, target, payload, snapshot)
        return self._pause(state, {"rule": "unknown_phase", "phase": phase.value})

    def recover(self, plan_id: str) -> dict[str, Any]:
        state = self.store.get_execution_plan_state(plan_id)
        if state["phase"] in {ExecutionPhase.COMPLETED.value, ExecutionPhase.FAILED.value}:
            return state
        snapshot = self.account_port.account_snapshot()
        if snapshot.external_orders:
            return self._pause(state, {
                "rule": "external_orders",
                "references": list(snapshot.external_orders),
            })
        payload = dict(state["payload"])
        plan = ExecutionPlan.from_dict(payload["plan"])
        # Future-phase children exist in the immutable plan but have never
        # been routed.  Only locally journalled children are expected to have
        # Broker truth during restart recovery.
        references = [
            child.reference for child in plan.children
            if self.store.get_child_order(child.reference) is not None
        ]
        statuses = self.route_port.child_statuses(references) if references else {}
        missing = sorted(
            reference for reference in references
            if reference not in statuses and _child_status(self.store, reference) not in _TERMINAL
        )
        if missing:
            return self._pause(state, {"rule": "missing_broker_reports", "references": missing})
        for child in plan.children:
            if child.reference in statuses:
                local = _child_status(self.store, child.reference)
                broker = str(statuses[child.reference]).lower()
                self.store.record_order_reconciliation(
                    plan_id,
                    child.reference,
                    local_status=local,
                    broker_status=broker,
                )
                self.store.update_child_order(child.reference, status=broker)
        try:
            self._capture_fills(plan_id, references)
        except Exception as exc:  # noqa: BLE001 - malformed Broker truth is unsafe
            return self._pause(state, {
                "rule": "fill_reconciliation_failed",
                "reason": f"{type(exc).__name__}: {exc}",
            })
        return self.store.get_execution_plan_state(plan_id)

    def pause(self, plan_id: str, reason: str) -> dict[str, Any]:
        state = self.store.get_execution_plan_state(plan_id)
        plan = ExecutionPlan.from_dict(state["payload"]["plan"])
        failures: list[str] = []
        statuses = self.route_port.child_statuses([item.reference for item in plan.children])
        for reference, status in statuses.items():
            if str(status).lower() in _ACTIVE and not self.route_port.cancel_child(reference):
                failures.append(reference)
        return self._pause(state, {
            "rule": "operator_pause",
            "reason": reason,
            "cancel_failures": failures,
        })

    def _route_phase(
        self,
        state: dict[str, Any],
        plan: ExecutionPlan,
        payload: dict[str, Any],
        *,
        side: str,
    ) -> dict[str, Any]:
        children = [child for child in plan.children if child.side == side]
        if not children:
            next_phase = (
                ExecutionPhase.REFRESHING_ACCOUNT if side == "sell"
                else ExecutionPhase.FINAL_RECONCILE
            )
            return self._save(state, next_phase, payload)
        statuses = self.route_port.child_statuses([item.reference for item in children])
        submitted = False
        for child in children:
            existing = str(statuses.get(child.reference) or _child_status(self.store, child.reference)).lower()
            if existing in _ACTIVE | _TERMINAL and existing != "planned":
                continue
            inserted = self.store.record_child_order(
                child.reference,
                plan.plan_id,
                asdict(child),
                status="routing",
            )
            if not inserted:
                continue
            order_id = self.route_port.submit_child(child)
            if not order_id:
                self.store.update_child_order(child.reference, status="rejected")
            else:
                submitted = True
                self.store.update_child_order(
                    child.reference,
                    status="submitted",
                    order_id=str(order_id),
                )
        if side == "sell":
            return self._save(state, ExecutionPhase.WAITING_SELL_REPORTS, payload)
        # BUYING doubles as its report-wait phase. The next advance checks truth.
        if submitted:
            payload["buy_waiting"] = True
            return self._save(state, ExecutionPhase.BUYING, payload)
        return self._wait_phase(
            state,
            plan,
            payload,
            side="buy",
            complete_phase=ExecutionPhase.FINAL_RECONCILE,
        )

    def _wait_phase(
        self,
        state: dict[str, Any],
        plan: ExecutionPlan,
        payload: dict[str, Any],
        *,
        side: str,
        complete_phase: ExecutionPhase,
    ) -> dict[str, Any]:
        children = [child for child in plan.children if child.side == side]
        statuses = self.route_port.child_statuses([item.reference for item in children])
        unknown: list[str] = []
        failed: list[dict[str, str]] = []
        active = False
        for child in children:
            broker_status = statuses.get(child.reference)
            local_status = _child_status(self.store, child.reference)
            if broker_status is None:
                if local_status in {"rejected", "cancelled", "canceled"}:
                    failed.append({"reference": child.reference, "status": local_status})
                elif local_status not in _TERMINAL:
                    unknown.append(child.reference)
                continue
            status = str(broker_status).lower()
            self.store.record_order_reconciliation(
                plan.plan_id,
                child.reference,
                local_status=local_status,
                broker_status=status,
            )
            self.store.update_child_order(child.reference, status=status)
            active = active or status in _ACTIVE
            if status in {"rejected", "cancelled", "canceled"}:
                failed.append({"reference": child.reference, "status": status})
        try:
            self._capture_fills(plan.plan_id, [item.reference for item in children])
        except Exception as exc:  # noqa: BLE001 - fill truth must be durable before proceeding
            return self._pause(state, {
                "rule": "fill_reconciliation_failed",
                "reason": f"{type(exc).__name__}: {exc}",
            })
        if unknown:
            return self._pause(state, {"rule": "missing_broker_reports", "references": unknown})
        if failed:
            return self._pause(state, {
                "rule": "child_order_not_filled",
                "orders": failed,
                "reason": "a rejected or externally cancelled child requires reconciliation",
            })
        if active:
            return state
        payload.pop("buy_waiting", None)
        return self._save(state, complete_phase, payload)

    def _capture_fills(self, plan_id: str, references: list[str]) -> None:
        loader = getattr(self.route_port, "child_fills", None)
        if not callable(loader) or not references:
            return
        wanted = set(references)
        for fill in loader(references):
            reference = str(fill.get("reference") or "")
            if reference not in wanted:
                raise ValueError(f"fill references an unexpected child {reference!r}")
            child = self.store.get_child_order(reference)
            if child is None or str(child["plan_id"]) != plan_id:
                raise ValueError(f"fill child {reference!r} is not journalled for this plan")
            volume = float(fill.get("volume") or 0.0)
            price = float(fill.get("price") or 0.0)
            if volume <= 0 or price <= 0:
                raise ValueError(f"fill {reference!r} has invalid volume or price")
            fill_key = str(fill.get("fill_key") or "")
            if not fill_key:
                raise ValueError(f"fill {reference!r} has no stable fill key")
            self.store.record_fill_reconciliation(
                fill_key,
                plan_id,
                reference,
                order_id=str(fill.get("order_id") or ""),
                volume=volume,
                price=price,
                payload=dict(fill.get("payload") or fill),
            )

    def _refresh(
        self,
        state: dict[str, Any],
        target: TargetPortfolio,
        payload: dict[str, Any],
        snapshot: AccountSnapshot,
    ) -> dict[str, Any]:
        plan = self.planner.plan(
            target,
            snapshot,
            quotes=_quotes_from_payload(payload),
            instruments=_instruments_from_payload(payload),
            next_child_index=state["next_child_index"],
        )
        if not plan.ok:
            return self._pause(state, {
                "rule": "replan_issues",
                "issues": [asdict(item) for item in plan.issues],
            })
        payload["plan"] = plan.to_dict()
        payload["replans"] = int(payload.get("replans") or 0) + 1
        if int(payload["replans"]) > self.max_replans:
            return self._pause(state, {"rule": "max_replans", "reason": "execution did not converge"})
        next_phase = (
            ExecutionPhase.SELLING
            if any(child.side == "sell" for child in plan.children)
            else ExecutionPhase.BUYING
            if any(child.side == "buy" for child in plan.children)
            else ExecutionPhase.FINAL_RECONCILE
        )
        return self._save(
            state,
            next_phase,
            payload,
            next_child_index=_next_indices(plan.children, state["next_child_index"]),
        )

    def _final_reconcile(
        self,
        state: dict[str, Any],
        target: TargetPortfolio,
        payload: dict[str, Any],
        snapshot: AccountSnapshot,
    ) -> dict[str, Any]:
        # BUYING report wait is checked before comparing final holdings.
        plan = ExecutionPlan.from_dict(payload["plan"])
        buys = [child for child in plan.children if child.side == "buy"]
        if buys:
            waited = self._wait_phase(
                state,
                plan,
                payload,
                side="buy",
                complete_phase=ExecutionPhase.FINAL_RECONCILE,
            )
            if waited["phase"] != ExecutionPhase.FINAL_RECONCILE.value:
                return waited
        # Broker status/fill polling above may have changed cash and holdings
        # after ``advance`` captured its initial snapshot.  Final comparison
        # must use post-report account truth or a late fill could be mistaken
        # for a remaining delta and generate a duplicate replan.
        snapshot = self.account_port.account_snapshot()
        actual = {
            canonical_instrument(key): float(value)
            for key, value in snapshot.positions.items() if float(value)
        }
        wanted = {
            canonical_instrument(key): float(value)
            for key, value in target.holdings.items() if float(value)
        }
        if actual == wanted and not snapshot.active_order_deltas:
            return self._save(state, ExecutionPhase.COMPLETED, payload)
        return self._refresh(state, target, payload, snapshot)

    def _save(
        self,
        state: dict[str, Any],
        phase: ExecutionPhase,
        payload: dict[str, Any],
        *,
        next_child_index: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        self.store.record_plan_attempt(state["plan_id"], phase.value, {"from": state["phase"]})
        return self.store.save_execution_plan_state(
            state["plan_id"],
            state["decision_id"],
            state["instance_id"],
            state["config_hash"],
            phase=phase.value,
            payload=payload,
            recovery_version=state["recovery_version"],
            next_child_index=next_child_index or state["next_child_index"],
        )

    def _pause(self, state: dict[str, Any], error: dict[str, Any]) -> dict[str, Any]:
        return self.store.save_execution_plan_state(
            state["plan_id"],
            state["decision_id"],
            state["instance_id"],
            state["config_hash"],
            phase=ExecutionPhase.PAUSED.value,
            payload=state["payload"],
            recovery_version=state["recovery_version"],
            next_child_index=state["next_child_index"],
            last_error=error,
        )


def _child_status(store: Any, reference: str) -> str:
    row = store.get_child_order(reference)
    return "" if row is None else str(row["status"]).lower()


def _next_indices(
    children: tuple[ExecutionChild, ...],
    existing: Mapping[str, int] | None = None,
) -> dict[str, int]:
    result = dict(existing or {})
    for child in children:
        key = f"{child.instrument}:{child.side}"
        result[key] = max(result.get(key, 0), int(child.child_index) + 1)
    return result


def _target_to_dict(target: TargetPortfolio) -> dict[str, Any]:
    data = asdict(target)
    return data


def _target_from_dict(data: Mapping[str, Any]) -> TargetPortfolio:
    payload = dict(data)
    payload["positions"] = []
    return TargetPortfolio(**payload)


def _quotes_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    from alphapilot.systems.trading.contracts import TradableQuote

    return {key: TradableQuote(**value) for key, value in (payload.get("quotes") or {}).items()}


def _instruments_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    from alphapilot.systems.trading.contracts import InstrumentMetadata

    return {
        key: InstrumentMetadata(**value)
        for key, value in (payload.get("instruments") or {}).items()
    }


def _timestamp_after(left: str, right: str) -> bool:
    """Compare account/target timestamps without accepting a later date."""

    if len(str(left)) == 10 and len(str(right)) >= 10:
        return str(left)[:10] > str(right)[:10]
    lhs = datetime.fromisoformat(str(left))
    rhs = datetime.fromisoformat(str(right))
    if lhs.tzinfo is None and rhs.tzinfo is not None:
        lhs = lhs.replace(tzinfo=rhs.tzinfo)
    elif lhs.tzinfo is not None and rhs.tzinfo is None:
        rhs = rhs.replace(tzinfo=lhs.tzinfo)
    return lhs > rhs

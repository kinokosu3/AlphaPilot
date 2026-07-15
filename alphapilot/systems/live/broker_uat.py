"""Local-only, event-derived XTP/EMT acceptance harness.

The harness intentionally has no HTTP mutation surface.  It uses the same
runtime, OMS and risk gate as normal LIVE trading, but routes through the
separate ``BROKER_UAT`` origin whose whitelist and notional cap are persisted
before an order can reach the gateway.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
from importlib.metadata import PackageNotFoundError, distribution
import json
import math
import os
from pathlib import Path
import platform
import socket
import struct
import subprocess
import time
from typing import Any, Callable

from alphapilot.systems.live.brokers.registry import get_broker
from alphapilot.systems.live.types import normalize_symbol, symbol_key
from alphapilot.systems.live.redaction import redact_secrets
from alphapilot.systems.trading.ports import RouteContext, RouteOrigin


CONFIRMATION = "I_UNDERSTAND_REAL_ORDERS"
SCENARIO_VERSION = 2
CAPABILITIES = (
    "account_contract_quote_ready",
    "marketable_fill_confirmed",
    "plan_partial_execution_observed",
    "cancel_confirmed",
    "disconnect_reconnect_reconciled",
    "restart_reference_idempotent",
    "instance_account_global_kill_switch",
)


class BrokerUATHarness:
    """Run one bounded real-broker scenario and persist callback-derived proof."""

    def __init__(
        self,
        store: Any,
        *,
        runtime_factory: Callable[[str], Any],
        sleep_fn: Callable[[float], None] = time.sleep,
        preflight_fn: Callable[[str, float], dict[str, Any]] | None = None,
        process_id_fn: Callable[[], int] = os.getpid,
    ) -> None:
        self.store = store
        self.runtime_factory = runtime_factory
        self.sleep_fn = sleep_fn
        self.preflight_fn = preflight_fn or _provider_preflight
        self.process_id_fn = process_id_fn

    @staticmethod
    def plugin_metadata(broker: str) -> dict[str, str]:
        return _plugin_metadata(str(broker).lower())

    def preflight(
        self,
        *,
        broker: str,
        symbols: list[str] | None = None,
        max_notional: float = 20_000.0,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Connect read-only and return redacted, executable UAT candidates."""

        selected = str(broker).strip().lower()
        if selected not in {"xtp", "emt"}:
            raise ValueError("broker must be xtp or emt")
        configured_cap = float(os.getenv("ALPHAPILOT_BROKER_UAT_MAX_NOTIONAL", "0") or 0)
        cap = min(float(max_notional), configured_cap) if configured_cap > 0 else float(max_notional)
        if cap <= 0:
            raise ValueError("Broker UAT preflight max_notional must be positive")
        provider = self.preflight_fn(selected, min(max(float(timeout), 0.1), 5.0))
        if not bool(provider.get("ok")):
            raise RuntimeError(
                "broker SDK/architecture/credentials/network preflight failed: "
                f"{_safe_evidence(provider)}"
            )
        metadata = _plugin_metadata(selected)
        runtime = self.runtime_factory(selected)
        try:
            runtime.connect()
            if not runtime.wait_ready(timeout=min(max(float(timeout), 1.0), 60.0)):
                raise RuntimeError("broker account/contracts did not become ready")
            oms = runtime.engine.oms
            account = oms.account
            if account is None or not str(account.account_id):
                raise RuntimeError("broker account callback is missing")
            requested = [symbol_key(*normalize_symbol(item)) for item in (symbols or [])]
            candidates = requested or _default_candidate_symbols(oms.contracts)
            candidates = [item for item in candidates if oms.get_contract(item) is not None][:30]
            if candidates:
                runtime.engine.subscribe_market_data(candidates)
                _wait_for_any_quote(runtime, candidates, timeout=min(float(timeout), 15.0))
            rows = []
            for instrument in candidates:
                contract = oms.get_contract(instrument)
                tick = oms.get_tick(instrument)
                if contract is None or tick is None:
                    continue
                lot = max(int(getattr(contract, "lot_size", 0) or 0), 1)
                price = float(getattr(tick, "ask_price_1", 0) or getattr(tick, "last_price", 0) or 0)
                max_volume = int(cap / price / lot) * lot if price > 0 else 0
                eligible = bool(
                    price > 0
                    and float(getattr(tick, "bid_price_1", 0) or 0) > 0
                    and max_volume >= lot * 2
                )
                rows.append({
                    "symbol": instrument,
                    "product": str(getattr(getattr(contract, "product", ""), "value", "")),
                    "lot_size": lot,
                    "price_tick": float(getattr(contract, "price_tick", 0) or 0),
                    "settlement_days": int(getattr(contract, "settlement_days", 1) or 0),
                    "last_price": float(getattr(tick, "last_price", 0) or 0),
                    "bid_price_1": float(getattr(tick, "bid_price_1", 0) or 0),
                    "ask_price_1": float(getattr(tick, "ask_price_1", 0) or 0),
                    "bid_volume_1": float(getattr(tick, "bid_volume_1", 0) or 0),
                    "ask_volume_1": float(getattr(tick, "ask_volume_1", 0) or 0),
                    "max_volume": max_volume,
                    "eligible": eligible,
                })
            return _safe_evidence({
                "broker": selected,
                "account_hash": _hash_text(str(account.account_id)),
                "contracts": len(oms.contracts),
                "positions": len(oms.get_positions()),
                "active_orders": len(oms.get_active_orders()),
                "max_notional": cap,
                "plugin": metadata,
                "provider": provider,
                "candidates": rows,
            })
        finally:
            runtime.close()

    def start(
        self,
        *,
        broker: str,
        symbol: str,
        side: str,
        volume: float,
        price: float,
        max_notional: float,
        confirmation: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        request = self._preflight_request(
            broker=broker,
            symbol=symbol,
            side=side,
            volume=volume,
            price=price,
            max_notional=max_notional,
            confirmation=confirmation,
        )
        metadata = _plugin_metadata(request["broker"])
        run = self.store.create_broker_uat_run(
            broker=request["broker"],
            account_hash="",
            environment=str(os.getenv("ALPHAPILOT_BROKER_UAT_ENVIRONMENT") or "").strip(),
            plugin_version=metadata["plugin_version"],
            plugin_hash=metadata["plugin_hash"],
            sdk_version=metadata["sdk_version"],
            sdk_hash=metadata["sdk_hash"],
            scenario_version=SCENARIO_VERSION,
            code_commit=metadata["code_commit"],
            runtime_code_hash=metadata["runtime_code_hash"],
            symbol=request["symbol"],
            max_notional=request["max_notional"],
        )
        self._record_preflight(
            run["run_id"], request=request, metadata=metadata, timeout=timeout,
        )
        return self._execute(run["run_id"], timeout=timeout, stop_for_restart=True)

    def resume(self, run_id: str, *, confirmation: str, timeout: float = 30.0) -> dict[str, Any]:
        self._require_enabled(confirmation)
        persisted = self.store.get_broker_uat_run(run_id)
        request = _request_from_run(persisted)
        self._preflight_request(**request, confirmation=confirmation)
        metadata = _plugin_metadata(str(persisted["broker"]))
        if (
            metadata["plugin_version"] != str(persisted["plugin_version"])
            or metadata["plugin_hash"] != str(persisted["plugin_hash"])
            or metadata["sdk_version"] != str(persisted["sdk_version"])
            or metadata["sdk_hash"] != str(persisted.get("sdk_hash") or "")
            or metadata["runtime_code_hash"]
            != str(persisted.get("runtime_code_hash") or "")
        ):
            raise ValueError("installed broker plugin or SDK changed since the UAT run started")
        if persisted["status"] == "restart_required":
            boundary = next(
                (
                    step for step in persisted.get("steps", [])
                    if step.get("step") == "process_restart_required"
                ),
                None,
            )
            origin_marker = str(
                ((boundary or {}).get("evidence") or {}).get("origin_process_marker") or ""
            )
            current_marker = self._process_marker(persisted)
            if not origin_marker or current_marker == origin_marker:
                raise RuntimeError(
                    "resume the Broker UAT from a newly started local CLI process"
                )
        run = self.store.resume_broker_uat_run(run_id)
        if run["broker"] not in {"xtp", "emt"}:
            raise ValueError("only XTP and EMT UAT runs can be resumed")
        self._record_preflight(
            run_id, request=request, metadata=metadata, timeout=timeout,
        )
        has_restart_boundary = any(
            step.get("step") == "process_restart_required"
            and step.get("status") == "passed"
            for step in persisted.get("steps", [])
        )
        return self._execute(
            run_id,
            timeout=timeout,
            stop_for_restart=not has_restart_boundary,
        )

    def abort(self, run_id: str, *, confirmation: str, reason: str) -> dict[str, Any]:
        self._require_enabled(confirmation)
        run = self.store.get_broker_uat_run(run_id)
        runtime = None
        try:
            runtime = self.runtime_factory(str(run["broker"]))
            runtime.connect()
            runtime.wait_ready(timeout=20.0)
            runtime.refresh_broker_state(include_orders=True, include_trades=True)
            runtime.settle_broker_events(1.0)
            owned_order_ids = set(_uat_order_ids(run))
            owned_order_ids.update(_active_uat_order_ids(runtime, run_id))
            for order_id in sorted(owned_order_ids):
                state = runtime.order_state(order_id)
                if state.get("active"):
                    runtime.cancel_order(order_id)
                    runtime.wait_for_order_terminal(order_id, timeout=10.0)
        finally:
            if runtime is not None:
                runtime.close()
        return self.store.abort_broker_uat_run(run_id, reason=reason)

    def _record_preflight(
        self,
        run_id: str,
        *,
        request: dict[str, Any],
        metadata: dict[str, Any],
        timeout: float,
    ) -> None:
        try:
            provider_preflight = self.preflight_fn(
                request["broker"], min(max(float(timeout), 0.1), 5.0),
            )
            if not bool(provider_preflight.get("ok")):
                raise RuntimeError(
                    "broker SDK/architecture/credentials/network preflight failed: "
                    f"{_safe_evidence(provider_preflight)}"
                )
            self.store.update_broker_uat_step(
                run_id,
                "preflight",
                status="passed",
                evidence={
                    "request": request,
                    "plugin": metadata,
                    "provider": _safe_evidence(provider_preflight),
                },
            )
        except Exception as exc:
            self.store.update_broker_uat_step(
                run_id,
                "preflight",
                status="failed",
                evidence={"request": request, "plugin": metadata},
                error={"type": type(exc).__name__, "message": redact_secrets(str(exc))},
            )
            raise

    def _process_marker(self, run: dict[str, Any]) -> str:
        return _hash_text(
            f"{run.get('environment') or ''}:{int(self.process_id_fn())}"
        )

    def _execute(
        self,
        run_id: str,
        *,
        timeout: float,
        stop_for_restart: bool,
    ) -> dict[str, Any]:
        run = self.store.get_broker_uat_run(run_id)
        request = _request_from_run(run)
        passed_steps = {
            str(step["step"])
            for step in run.get("steps", []) if step.get("status") == "passed"
        }
        runtime = None
        current_step = "connected"
        try:
            runtime = self.runtime_factory(str(run["broker"]))
            runtime.connect()
            if not runtime.wait_ready(timeout=min(max(float(timeout), 1.0), 60.0)):
                raise RuntimeError("broker account/contracts did not become ready")
            runtime.refresh_broker_state(include_orders=True, include_trades=True)
            runtime.settle_broker_events(0.5)
            account = runtime.engine.oms.account
            if account is None or not str(account.account_id):
                raise RuntimeError("broker account callback is missing")
            account_hash = _hash_text(str(account.account_id))
            self.store.bind_broker_uat_account(run_id, account_hash)
            readiness = {
                "account_hash": account_hash,
                "contracts": len(runtime.engine.oms.contracts),
                "positions": len(runtime.engine.oms.get_positions()),
                "connection": str(runtime.engine.connection.state),
            }
            if not runtime.engine.oms.get_contract(request["symbol"]):
                raise RuntimeError("whitelisted UAT contract metadata is unavailable")
            runtime.engine.subscribe_market_data([request["symbol"]])
            tick = _wait_for_quote(runtime, request["symbol"], timeout=timeout)
            if tick is None:
                raise RuntimeError("whitelisted UAT quote is unavailable after subscription")
            _assert_no_unknown_active_orders(runtime, run_id, _uat_order_ids(run))
            if "connected" not in passed_steps:
                self.store.update_broker_uat_step(
                    run_id,
                    "connected",
                    status="passed",
                    evidence={**readiness, "quote": _safe_quote(tick)},
                )

            current_step = "execution_plan"
            run = self.store.get_broker_uat_run(run_id)
            plan = _execution_plan_from_run(run)
            if not plan:
                plan = _build_execution_plan(
                    request,
                    contract=runtime.engine.oms.get_contract(request["symbol"]),
                    tick=tick,
                )
                self.store.update_broker_uat_step(
                    run_id, "execution_plan", status="passed", evidence=plan,
                )
                passed_steps.add("execution_plan")

            route_context = RouteContext(
                origin=RouteOrigin.BROKER_UAT,
                account_id=str(account.account_id),
                broker=str(run["broker"]),
                uat_run_id=run_id,
            )
            fill = dict(plan["fill"])
            remainder = dict(plan["remainder"])

            current_step = "marketable_order_acknowledged"
            run = self.store.get_broker_uat_run(run_id)
            fill_order_id = _order_id_for_step(run, "marketable_order_acknowledged")
            if not fill_order_id:
                submitted = runtime.submit_order(
                    request["symbol"],
                    side=request["side"],
                    volume=fill["volume"],
                    price=fill["price"],
                    order_type="limit",
                    reference=f"broker-uat/{run_id}/fill",
                    route_context=route_context,
                )
                if not submitted.get("submitted"):
                    raise RuntimeError(
                        "UAT marketable order was not routed: "
                        f"{submitted.get('routing_rule')} {submitted.get('routing_reason')}"
                    )
                fill_order_id = str(submitted["order_id"])
                ack = runtime.wait_for_order_ack(fill_order_id, timeout=timeout)
                self._record_order_events(runtime, run_id, fill_order_id)
                if not ack.get("acknowledged"):
                    raise RuntimeError("broker did not acknowledge the UAT marketable order")
                self.store.update_broker_uat_step(
                    run_id,
                    "marketable_order_acknowledged",
                    status="passed",
                    evidence=_safe_evidence({
                        "order_id": fill_order_id,
                        "ack": ack,
                        "reference": f"broker-uat/{run_id}/fill",
                    }),
                )

            current_step = "marketable_fill_observed"
            if "marketable_fill_observed" not in passed_steps:
                filled = self._wait_for_fill(
                    runtime, run_id, fill_order_id, timeout=timeout,
                )
                if filled.get("active"):
                    runtime.cancel_order(fill_order_id)
                    terminal = runtime.wait_for_order_terminal(fill_order_id, timeout=timeout)
                    self._record_order_events(runtime, run_id, fill_order_id)
                    if not terminal.get("terminal"):
                        raise RuntimeError("marketable UAT order remainder could not be cancelled")
                    filled = runtime.order_state(fill_order_id)
                self.store.update_broker_uat_step(
                    run_id,
                    "marketable_fill_observed",
                    status="passed",
                    evidence=_safe_evidence(filled),
                )

            current_step = "remainder_order_acknowledged"
            run = self.store.get_broker_uat_run(run_id)
            remainder_order_id = _order_id_for_step(run, "remainder_order_acknowledged")
            if not remainder_order_id:
                submitted = runtime.submit_order(
                    request["symbol"],
                    side=request["side"],
                    volume=remainder["volume"],
                    price=remainder["price"],
                    order_type="limit",
                    reference=f"broker-uat/{run_id}/remainder",
                    route_context=route_context,
                )
                if not submitted.get("submitted"):
                    raise RuntimeError(
                        "UAT remainder order was not routed: "
                        f"{submitted.get('routing_rule')} {submitted.get('routing_reason')}"
                    )
                remainder_order_id = str(submitted["order_id"])
                ack = runtime.wait_for_order_ack(remainder_order_id, timeout=timeout)
                self._record_order_events(runtime, run_id, remainder_order_id)
                if not ack.get("acknowledged"):
                    raise RuntimeError("broker did not acknowledge the UAT remainder order")
                active = self._wait_for_active_remainder(
                    runtime, run_id, remainder_order_id, timeout=timeout,
                )
                self.store.update_broker_uat_step(
                    run_id,
                    "remainder_order_acknowledged",
                    status="passed",
                    evidence=_safe_evidence({
                        "order_id": remainder_order_id,
                        "ack": ack,
                        "order": active,
                        "reference": f"broker-uat/{run_id}/remainder",
                    }),
                )

            current_step = "plan_partial_execution_observed"
            if "plan_partial_execution_observed" not in passed_steps:
                fill_state = runtime.order_state(fill_order_id)
                remainder_state = runtime.order_state(remainder_order_id)
                fill_order = dict(fill_state.get("order") or {})
                remainder_order = dict(remainder_state.get("order") or {})
                traded = float(fill_order.get("traded") or 0) + float(
                    remainder_order.get("traded") or 0
                )
                remaining = max(
                    float(remainder_order.get("volume") or 0)
                    - float(remainder_order.get("traded") or 0),
                    0.0,
                )
                if traded <= 0 or remaining <= 0 or not remainder_state.get("active"):
                    raise RuntimeError(
                        "Broker callbacks did not confirm a partially executed UAT plan"
                    )
                filled_notional = _actual_filled_notional(
                    runtime,
                    {
                        fill_order_id: float(fill.get("price") or 0),
                        remainder_order_id: float(remainder.get("price") or 0),
                    },
                )
                self.store.update_broker_uat_filled_notional(run_id, filled_notional)
                self.store.update_broker_uat_step(
                    run_id,
                    "plan_partial_execution_observed",
                    status="passed",
                    evidence=_safe_evidence({
                        "filled_volume": traded,
                        "remaining_volume": remaining,
                        "filled_notional": filled_notional,
                    }),
                )

            if stop_for_restart and "restart_reconciled" not in passed_steps:
                return self.store.mark_broker_uat_restart_required(
                    run_id,
                    process_marker=self._process_marker(run),
                )

            # ``resume`` is deliberately a separate CLI invocation. Recovery
            # must rediscover the broker order and stable reference from durable
            # state before any duplicate route is attempted.
            current_step = "restart_reconciled"
            restarted = runtime
            restarted.refresh_broker_state(include_orders=True, include_trades=True)
            restarted.settle_broker_events(1.0)
            recovered = restarted.order_state(remainder_order_id)
            self._record_order_events(restarted, run_id, remainder_order_id)
            if "restart_reconciled" not in passed_steps:
                if not recovered.get("found") or not recovered.get("active"):
                    raise RuntimeError(
                        "active broker order was not recovered after process restart"
                    )
                duplicate = restarted.submit_order(
                    request["symbol"],
                    side=request["side"],
                    volume=remainder["volume"],
                    price=remainder["price"],
                    order_type="limit",
                    reference=f"broker-uat/{run_id}/remainder",
                    route_context=route_context,
                )
                if duplicate.get("submitted") or duplicate.get("routing_rule") != "duplicate":
                    raise RuntimeError("stable UAT reference did not reject a duplicate route")
                self.store.update_broker_uat_step(
                    run_id,
                    "restart_reconciled",
                    status="passed",
                    evidence=_safe_evidence({"order": recovered, "duplicate": duplicate}),
                )

            current_step = "kill_switches_verified"
            if "kill_switches_verified" not in passed_steps:
                kill_evidence = self._verify_kill_switches(
                    restarted,
                    run_id=run_id,
                    account_id=str(restarted.engine.oms.account.account_id),
                    request={**request, "volume": remainder["volume"], "price": remainder["price"]},
                )
                self.store.update_broker_uat_step(
                    run_id,
                    "kill_switches_verified",
                    status="passed",
                    evidence=_safe_evidence(kill_evidence),
                )

            current_step = "cancel_confirmed"
            # Keep the global kill switch engaged while cancelling: cancel must
            # remain available even when every new order route is blocked.
            if "cancel_confirmed" not in passed_steps:
                prior_global = self._route_block("global", "*")
                if prior_global is not None and prior_global.get("active"):
                    raise RuntimeError("global kill switch was already active before UAT cancellation")
                self.store.set_route_block("global", "*", active=True, reason=f"Broker UAT {run_id}")
                try:
                    cancel = restarted.cancel_order(remainder_order_id)
                    terminal = restarted.wait_for_order_terminal(
                        remainder_order_id, timeout=timeout,
                    )
                    self._record_order_events(restarted, run_id, remainder_order_id)
                finally:
                    self.store.set_route_block(
                        "global",
                        "*",
                        active=False,
                        reason=str((prior_global or {}).get("reason") or f"Broker UAT {run_id} complete"),
                    )
                if not cancel.get("cancelled") or not terminal.get("terminal"):
                    raise RuntimeError("UAT remainder cancellation was not confirmed by broker callback")
                self.store.update_broker_uat_step(
                    run_id,
                    "cancel_confirmed",
                    status="passed",
                    evidence=_safe_evidence({"cancel": cancel, "terminal": terminal}),
                )

            current_step = "reconnect_reconciled"
            reconnect = restarted.reconnect(auto_resume=False)
            restarted.refresh_broker_state(include_orders=True, include_trades=True)
            restarted.settle_broker_events(1.0)
            final_order = restarted.order_state(remainder_order_id)
            self._record_order_events(restarted, run_id, remainder_order_id)
            warnings = list((reconnect.get("recovery") or {}).get("warnings") or [])
            if not final_order.get("terminal") or warnings:
                raise RuntimeError(
                    f"post-reconnect reconciliation is not clean: warnings={warnings}"
                )
            self.store.update_broker_uat_step(
                run_id,
                "reconnect_reconciled",
                status="passed",
                evidence=_safe_evidence({"order": final_order, "recovery": reconnect.get("recovery")}),
            )
            expires = datetime.now(timezone.utc) + timedelta(days=90)
            return self.store.complete_broker_uat_run(
                run_id,
                capabilities=list(CAPABILITIES),
                expires_at=expires.isoformat(timespec="seconds"),
            )
        except Exception as exc:
            try:
                if runtime is not None:
                    latest = self.store.get_broker_uat_run(run_id)
                    owned_order_ids = set(_uat_order_ids(latest))
                    owned_order_ids.update(_active_uat_order_ids(runtime, run_id))
                    for order_id in sorted(owned_order_ids):
                        if runtime.order_state(order_id).get("active"):
                            runtime.cancel_order(order_id)
                            runtime.wait_for_order_terminal(order_id, timeout=min(timeout, 10.0))
                        self._record_order_events(runtime, run_id, order_id)
            except Exception:  # noqa: BLE001 - retain primary UAT failure
                pass
            self.store.update_broker_uat_step(
                run_id,
                current_step,
                status="failed",
                error={"type": type(exc).__name__, "message": redact_secrets(str(exc))},
            )
            raise
        finally:
            if runtime is not None:
                runtime.close()

    def _wait_for_fill(
        self,
        runtime: Any,
        run_id: str,
        order_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(float(timeout), 0.1)
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            runtime.settle_broker_events(0.1)
            last = runtime.order_state(order_id)
            self._record_order_events(runtime, run_id, order_id)
            order = dict(last.get("order") or {})
            traded = float(order.get("traded") or 0.0)
            if traded > 0:
                return last
            if last.get("terminal"):
                break
            self.sleep_fn(0.1)
        raise RuntimeError(
            "a broker-confirmed marketable fill was not observed before timeout; "
            f"last_state={_safe_evidence(last)}"
        )

    def _wait_for_active_remainder(
        self,
        runtime: Any,
        run_id: str,
        order_id: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(float(timeout), 0.1)
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            runtime.settle_broker_events(0.1)
            last = runtime.order_state(order_id)
            self._record_order_events(runtime, run_id, order_id)
            order = dict(last.get("order") or {})
            if last.get("active") and float(order.get("traded") or 0) < float(
                order.get("volume") or 0
            ):
                return last
            if last.get("terminal"):
                break
            self.sleep_fn(0.1)
        raise RuntimeError(
            "the UAT remainder order did not remain active; "
            f"last_state={_safe_evidence(last)}"
        )

    def _record_order_events(self, runtime: Any, run_id: str, order_id: str) -> None:
        ledger = getattr(getattr(runtime, "engine", None), "ledger", None)
        events = [] if ledger is None or not hasattr(ledger, "events") else ledger.events(
            kind="order", order_id=order_id,
        )
        if not events:
            state = runtime.order_state(order_id)
            if state.get("found"):
                events = [{"ts": "", "payload": state.get("order") or {}}]
        for event in events:
            payload = dict(event.get("payload") or {})
            status = payload.get("status")
            if isinstance(status, dict):
                status = status.get("value")
            self.store.record_broker_uat_order_event(
                run_id,
                reference=str(event.get("reference") or payload.get("reference") or ""),
                order_id=str(event.get("order_id") or payload.get("order_id") or order_id),
                status=str(status or ""),
                traded=float(payload.get("traded") or 0),
                volume=float(payload.get("volume") or 0),
                payload=_safe_evidence(event),
                observed_at=str(event.get("ts") or ""),
            )

    def _verify_kill_switches(
        self,
        runtime: Any,
        *,
        run_id: str,
        account_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        scopes = (
            ("instance", f"broker-uat:{run_id}"),
            ("account", account_id),
            ("global", "*"),
        )
        for index, (scope, scope_id) in enumerate(scopes):
            prior = self._route_block(scope, scope_id)
            if prior is not None and prior.get("active"):
                raise RuntimeError(f"{scope} kill switch was already active before Broker UAT")
            self.store.set_route_block(scope, scope_id, active=True, reason=f"Broker UAT {run_id}")
            try:
                result = runtime.submit_order(
                    request["symbol"],
                    side=request["side"],
                    volume=request["volume"],
                    price=request["price"],
                    order_type="limit",
                    reference=f"broker-uat/{run_id}/kill-{index}",
                    route_context=RouteContext(
                        origin=RouteOrigin.BROKER_UAT,
                        account_id=account_id,
                        broker=str(request["broker"]),
                        uat_run_id=run_id,
                    ),
                )
            finally:
                self.store.set_route_block(
                    scope,
                    scope_id,
                    active=False,
                    reason=str((prior or {}).get("reason") or f"Broker UAT {run_id} complete"),
                )
            if result.get("submitted") or result.get("routing_rule") != "kill_switch":
                raise RuntimeError(f"{scope} kill switch did not block a UAT order")
            checks.append({"scope": scope, "routing_rule": result.get("routing_rule")})
        return {"checks": checks}

    def _route_block(self, scope: str, scope_id: str) -> dict[str, Any] | None:
        return next(
            (
                item for item in self.store.list_route_blocks()
                if item["scope_type"] == scope and item["scope_id"] == scope_id
            ),
            None,
        )

    @staticmethod
    def _require_enabled(confirmation: str) -> None:
        if os.getenv("ALPHAPILOT_BROKER_UAT_ENABLED", "false").strip().lower() not in {
            "1", "true", "yes", "on",
        }:
            raise PermissionError("Broker UAT is disabled")
        if str(confirmation) != CONFIRMATION:
            raise PermissionError(f"confirmation must equal {CONFIRMATION!r}")

    def _preflight_request(self, **payload: Any) -> dict[str, Any]:
        self._require_enabled(str(payload.get("confirmation") or ""))
        broker = str(payload.get("broker") or "").strip().lower()
        if broker not in {"xtp", "emt"}:
            raise ValueError("broker must be xtp or emt")
        if not str(os.getenv("ALPHAPILOT_BROKER_UAT_ENVIRONMENT") or "").strip():
            raise ValueError("ALPHAPILOT_BROKER_UAT_ENVIRONMENT is required")
        code, exchange = normalize_symbol(str(payload.get("symbol") or ""))
        instrument = symbol_key(code, exchange)
        whitelist = {
            symbol_key(*normalize_symbol(item.strip()))
            for item in os.getenv("ALPHAPILOT_BROKER_UAT_WHITELIST", "").split(",")
            if item.strip()
        }
        if not whitelist or instrument not in whitelist:
            raise PermissionError(f"{instrument} is not in ALPHAPILOT_BROKER_UAT_WHITELIST")
        side = str(payload.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        volume = float(payload.get("volume") or 0.0)
        price = float(payload.get("price") or 0.0)
        requested_cap = float(payload.get("max_notional") or 0.0)
        configured_cap = float(os.getenv("ALPHAPILOT_BROKER_UAT_MAX_NOTIONAL", "0") or 0.0)
        notional = volume * price
        if min(volume, price, requested_cap, configured_cap) <= 0:
            raise ValueError("volume, price and both UAT notional caps must be positive")
        cap = min(requested_cap, configured_cap)
        if notional > cap + 1e-9:
            raise ValueError(f"order notional {notional:.2f} exceeds UAT cap {cap:.2f}")
        return {
            "broker": broker,
            "symbol": instrument,
            "side": side,
            "volume": volume,
            "price": price,
            "max_notional": cap,
        }


def _plugin_metadata(broker: str) -> dict[str, str]:
    spec = get_broker(broker)
    if not str(spec.version).strip():
        raise ValueError(f"installed {broker} plugin distribution version is unavailable")
    sdk_hash = _native_sdk_hash(broker)
    sdk_version = f"native-sha256:{sdk_hash[:16]}"
    declared_sdk_version = str(
        os.getenv(f"ALPHAPILOT_{broker.upper()}_SDK_VERSION") or ""
    ).strip()
    module_name = str(spec.gateway_path).split(":", 1)[0]
    module_spec = importlib.util.find_spec(module_name)
    source_hash = ""
    if module_spec is not None and module_spec.origin and Path(module_spec.origin).is_file():
        source_hash = hashlib.sha256(Path(module_spec.origin).read_bytes()).hexdigest()
    distribution_hash = _distribution_hash(str(spec.distribution))
    if not source_hash and not distribution_hash:
        raise ValueError(f"installed {broker} plugin artifact could not be hashed")
    fingerprint = {
        "broker": broker,
        "plugin_id": spec.plugin_id,
        "distribution": spec.distribution,
        "plugin_version": spec.version,
        "gateway_path": spec.gateway_path,
        "gateway_source_hash": source_hash,
        "distribution_hash": distribution_hash,
        "sdk_version": sdk_version,
        "sdk_declared_version": declared_sdk_version,
        "sdk_hash": sdk_hash,
        "runtime_code_hash": _runtime_code_hash(),
        "code_commit": _git_commit(),
    }
    plugin_hash = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**fingerprint, "plugin_hash": plugin_hash}


def _provider_preflight(broker: str, timeout: float) -> dict[str, Any]:
    """Verify both provider channels without persisting endpoints or credentials."""

    from alphapilot.systems.live.brokers.registry import (
        build_connect_setting,
        build_quote_connect_setting,
        get_broker,
        get_quote_provider,
        missing_quote_setting_fields,
        missing_setting_fields,
        provider_availability,
    )

    checks: list[dict[str, Any]] = []
    for role, spec, missing, build_setting in (
        (
            "trade",
            get_broker(broker),
            missing_setting_fields(broker),
            build_connect_setting,
        ),
        (
            "quote",
            get_quote_provider(broker),
            missing_quote_setting_fields(broker),
            build_quote_connect_setting,
        ),
    ):
        available, availability_detail = provider_availability(broker, role=role)
        endpoint_checks: list[dict[str, Any]] = []
        if available and not missing:
            setting = build_setting(broker)
            for endpoint in spec.endpoints:
                host = setting.get(endpoint.host_key)
                port = setting.get(endpoint.port_key)
                if not host or not port:
                    continue
                try:
                    with socket.create_connection(
                        (str(host), int(port)), timeout=max(float(timeout), 0.1),
                    ):
                        reachable = True
                        error_type = ""
                except OSError as exc:
                    reachable = False
                    error_type = type(exc).__name__
                endpoint_checks.append({
                    "name": endpoint.name,
                    "reachable": reachable,
                    "error_type": error_type,
                })
        checks.append({
            "role": role,
            "available": bool(available),
            "availability": redact_secrets(str(availability_detail)),
            "missing_setting_fields": sorted(str(item) for item in missing),
            "endpoints": endpoint_checks,
            "ok": bool(available) and not missing and bool(endpoint_checks) and all(
                item["reachable"] for item in endpoint_checks
            ),
        })
    return {
        "ok": all(item["ok"] for item in checks),
        "architecture": {
            "machine": platform.machine(),
            "python_bits": struct.calcsize("P") * 8,
            "python_implementation": platform.python_implementation(),
        },
        "channels": checks,
    }


def _distribution_hash(name: str) -> str:
    """Hash installed plugin artifacts without recording their contents or paths."""

    try:
        package = distribution(name)
    except (PackageNotFoundError, ValueError):
        return ""
    digest = hashlib.sha256()
    hashed = 0
    for item in sorted(package.files or (), key=str):
        path = package.locate_file(item)
        if not path.is_file():
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        digest.update(str(item).encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        hashed += 1
    return digest.hexdigest() if hashed else ""


def _native_sdk_hash(broker: str) -> str:
    module_name = {
        "xtp": "alphapilot_xtpx.api",
        "emt": "alphapilot_emt.api",
    }.get(str(broker).lower(), "")
    if not module_name:
        raise ValueError(f"unsupported Broker SDK {broker!r}")
    module_spec = importlib.util.find_spec(module_name)
    if module_spec is None or not module_spec.origin:
        raise ValueError(f"installed {broker} native SDK module is unavailable")
    root = Path(module_spec.origin).resolve().parent
    artifacts = sorted(path for path in root.glob("*.so") if path.is_file())
    if not artifacts:
        raise ValueError(f"installed {broker} native SDK artifacts could not be hashed")
    digest = hashlib.sha256()
    for path in artifacts:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _runtime_code_hash() -> str:
    root = Path(__file__).resolve().parents[3]
    relative_paths = (
        "alphapilot/systems/live/broker_uat.py",
        "alphapilot/systems/live/runtime.py",
        "alphapilot/systems/live/engine.py",
        "alphapilot/systems/live/oms.py",
        "alphapilot/systems/live/risk.py",
        "alphapilot/systems/live/routing.py",
        "alphapilot/systems/trading/authorization.py",
        "alphapilot/systems/trading/store.py",
    )
    digest = hashlib.sha256()
    for relative in relative_paths:
        path = root / relative
        if not path.is_file():
            raise ValueError(f"runtime code artifact is missing: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _git_commit() -> str:
    root = Path(__file__).resolve().parents[3]
    try:
        value = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Broker UAT requires a Git-bound source checkout") from exc
    if len(value) != 40:
        raise ValueError("Broker UAT Git commit fingerprint is invalid")
    return value


def _request_from_run(run: dict[str, Any]) -> dict[str, Any]:
    preflight = next(
        (step for step in run.get("steps", []) if step.get("step") == "preflight"),
        None,
    )
    request = dict(((preflight or {}).get("evidence") or {}).get("request") or {})
    if not request:
        raise RuntimeError("Broker UAT run has no persisted preflight request")
    return request


def _primary_order_id(run: dict[str, Any]) -> str:
    if int(run.get("scenario_version") or 1) >= 2:
        return _order_id_for_step(run, "remainder_order_acknowledged")
    step = next(
        (item for item in run.get("steps", []) if item.get("step") == "order_acknowledged"),
        None,
    )
    return str(((step or {}).get("evidence") or {}).get("order_id") or "")


def _order_id_for_step(run: dict[str, Any], step_name: str) -> str:
    step = next(
        (item for item in run.get("steps", []) if item.get("step") == step_name),
        None,
    )
    return str(((step or {}).get("evidence") or {}).get("order_id") or "")


def _uat_order_ids(run: dict[str, Any]) -> list[str]:
    names = (
        "marketable_order_acknowledged",
        "remainder_order_acknowledged",
        "order_acknowledged",
    )
    return list(dict.fromkeys(
        identifier for identifier in (_order_id_for_step(run, name) for name in names)
        if identifier
    ))


def _execution_plan_from_run(run: dict[str, Any]) -> dict[str, Any]:
    step = next(
        (item for item in run.get("steps", []) if item.get("step") == "execution_plan"),
        None,
    )
    return dict((step or {}).get("evidence") or {})


def _build_execution_plan(
    request: dict[str, Any],
    *,
    contract: Any,
    tick: Any,
) -> dict[str, Any]:
    lot = max(int(getattr(contract, "lot_size", 0) or 100), 1)
    total_volume = int(float(request["volume"]) / lot) * lot
    if total_volume < lot * 2:
        raise ValueError(
            f"Broker UAT v2 requires at least two trading lots ({lot * 2} shares)"
        )
    price_tick = max(float(getattr(contract, "price_tick", 0) or 0.01), 0.000001)
    side = str(request["side"])
    requested_price = float(request["price"])
    bid = float(getattr(tick, "bid_price_1", 0) or 0)
    ask = float(getattr(tick, "ask_price_1", 0) or 0)
    reference = float(getattr(tick, "last_price", 0) or requested_price)
    if side == "buy":
        marketable = requested_price or ask
        if ask <= 0 or marketable + 1e-12 < ask:
            raise ValueError("buy UAT fill price must cross the current best ask")
        # A best-bid order can become marketable during the required process
        # restart. Keep the recovery child clearly away from the market while
        # remaining inside the default 5% fat-finger guard.
        resting = min(
            bid - price_tick if bid > price_tick else marketable - price_tick,
            _align_price(reference * 0.96, price_tick, direction="down"),
        )
    else:
        marketable = requested_price or bid
        if bid <= 0 or marketable - 1e-12 > bid:
            raise ValueError("sell UAT fill price must cross the current best bid")
        resting = max(
            ask + price_tick if ask > 0 else marketable + price_tick,
            _align_price(reference * 1.04, price_tick, direction="up"),
        )
    if min(marketable, resting) <= 0:
        raise ValueError("Broker UAT execution prices must be positive")
    fill_volume = lot
    remainder_volume = total_volume - fill_volume
    total_notional = fill_volume * marketable + remainder_volume * resting
    if total_notional > float(request["max_notional"]) + 1e-9:
        raise ValueError(
            f"two-child UAT notional {total_notional:.2f} exceeds cap "
            f"{float(request['max_notional']):.2f}"
        )
    return {
        "scenario_version": SCENARIO_VERSION,
        "symbol": request["symbol"],
        "side": side,
        "fill": {"volume": float(fill_volume), "price": float(marketable)},
        "remainder": {"volume": float(remainder_volume), "price": float(resting)},
        "requested_notional": float(total_notional),
        "lot_size": lot,
        "price_tick": price_tick,
    }


def _wait_for_quote(runtime: Any, symbol: str, *, timeout: float) -> Any | None:
    deadline = time.monotonic() + max(float(timeout), 0.1)
    while time.monotonic() < deadline:
        runtime.settle_broker_events(0.1)
        tick = runtime.engine.oms.get_tick(symbol)
        if tick is not None and float(getattr(tick, "last_price", 0) or 0) > 0:
            return tick
        time.sleep(0.05)
    return None


def _wait_for_any_quote(runtime: Any, symbols: list[str], *, timeout: float) -> None:
    deadline = time.monotonic() + max(float(timeout), 0.1)
    while time.monotonic() < deadline:
        runtime.settle_broker_events(0.1)
        if any(runtime.engine.oms.get_tick(item) is not None for item in symbols):
            return
        time.sleep(0.05)


def _default_candidate_symbols(contracts: dict[str, Any]) -> list[str]:
    rows = []
    for key, contract in contracts.items():
        product = str(getattr(getattr(contract, "product", ""), "value", ""))
        exchange = str(getattr(getattr(contract, "exchange", ""), "value", ""))
        if product not in {"fund", "equity"} or exchange not in {"SSE", "SZSE"}:
            continue
        priority = 0 if product == "fund" else 1
        rows.append((priority, str(key), str(key)))
    return [item[2] for item in sorted(rows)[:30]]


def _safe_quote(tick: Any) -> dict[str, Any]:
    return {
        "symbol": str(getattr(tick, "key", "")),
        "last_price": float(getattr(tick, "last_price", 0) or 0),
        "bid_price_1": float(getattr(tick, "bid_price_1", 0) or 0),
        "ask_price_1": float(getattr(tick, "ask_price_1", 0) or 0),
        "bid_volume_1": float(getattr(tick, "bid_volume_1", 0) or 0),
        "ask_volume_1": float(getattr(tick, "ask_volume_1", 0) or 0),
    }


def _assert_no_unknown_active_orders(
    runtime: Any,
    run_id: str,
    known_order_ids: list[str],
) -> None:
    known = set(known_order_ids)
    unknown = [
        str(order.order_id)
        for order in runtime.engine.oms.get_active_orders()
        if str(order.order_id) not in known
    ]
    if unknown:
        raise RuntimeError(
            f"unknown external active orders block Broker UAT: count={len(unknown)}"
        )


def _active_uat_order_ids(runtime: Any, run_id: str) -> list[str]:
    prefix = f"broker-uat/{run_id}/"
    return [
        str(order.order_id)
        for order in runtime.engine.oms.get_active_orders()
        if str(order.reference or "").startswith(prefix)
    ]


def _actual_filled_notional(
    runtime: Any,
    order_prices: dict[str, float],
) -> float:
    oms = runtime.engine.oms
    trades = oms.get_trades() if hasattr(oms, "get_trades") else []
    by_trade = sum(
        float(getattr(trade, "volume", 0) or 0)
        * float(getattr(trade, "price", 0) or 0)
        for trade in trades
        if str(getattr(trade, "order_id", "")) in order_prices
    )
    if by_trade > 0:
        return by_trade
    return sum(
        float((runtime.order_state(order_id).get("order") or {}).get("traded") or 0)
        * float(price)
        for order_id, price in order_prices.items()
    )


def _align_price(value: float, step: float, *, direction: str) -> float:
    units = float(value) / float(step)
    if direction == "down":
        rounded = math.floor(units + 1e-12)
    elif direction == "up":
        rounded = math.ceil(units - 1e-12)
    else:
        raise ValueError("price alignment direction must be up or down")
    return round(rounded * float(step), 8)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_evidence(value: Any) -> Any:
    """Remove credential-shaped fields before evidence reaches SQLite."""

    sensitive = ("password", "secret", "token", "software_key", "credentials")
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in sensitive):
                continue
            if (
                "account" in lowered
                and not lowered.endswith("hash")
                and isinstance(item, str)
            ):
                result[f"{key}_hash"] = _hash_text(item)
            else:
                result[str(key)] = _safe_evidence(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_evidence(item) for item in value]
    return redact_secrets(value)

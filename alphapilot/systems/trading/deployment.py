"""Deployment application service coordinating desired and observed state."""

from __future__ import annotations

import os
from typing import Any
import uuid

from alphapilot.systems.trading.account_identity import (
    ACCOUNT_HASH_PREFIX,
    account_identities_match,
)
from alphapilot.systems.trading.domain import (
    DeploymentMode,
    InstanceValidationState,
    LifecycleState,
)
from alphapilot.systems.trading.ports import RuntimeCommandResult, RuntimeControlPort


class DeploymentCoordinator:
    """Serialise lifecycle commands around a concrete runtime-control port."""

    def __init__(self, store: Any, control: RuntimeControlPort) -> None:
        self.store = store
        self.control = control

    def start(self, instance_id: str) -> dict[str, Any]:
        instance = self._instance(instance_id)
        mode = instance["deployment"]["run_mode"]
        live = mode == DeploymentMode.LIVE.value
        if live:
            _require_automated_live_enabled()
        external = mode in {
            DeploymentMode.SIMULATION.value,
            DeploymentMode.SHADOW.value,
            DeploymentMode.LIVE.value,
        }
        self._block(instance_id, True, "deployment start is awaiting runtime confirmation")
        runtime_id = str(instance["runtime"].get("runtime_id") or uuid.uuid4().hex)
        prepared = self.store.update_runtime_state(
            instance_id,
            expected_version=instance["runtime"]["version"],
            desired_state=(LifecycleState.PAUSED.value if external else LifecycleState.RUNNING.value),
            reconcile_required=external,
            reconciled=not external,
            binding_active=(
                mode in {DeploymentMode.SIMULATION.value, DeploymentMode.LIVE.value}
            ),
            runtime_id=runtime_id,
            last_error={},
        )
        instance["runtime"] = prepared
        result = self.control.start(instance)
        if not result.ok:
            return self._command_failed(instance_id, "start", result)
        current_runtime = self.store.get_runtime_state(instance_id)
        account_id, broker = _observed_binding(result)
        if mode in {DeploymentMode.SHADOW.value, DeploymentMode.LIVE.value}:
            if account_id and not account_identities_match(
                current_runtime["account_id"], account_id,
            ):
                mismatch = RuntimeCommandResult(
                    False,
                    runtime_id=result.runtime_id,
                    heartbeat_at=result.heartbeat_at,
                    runner_status=result.runner_status,
                    error="daemon account does not match the deployment binding",
                    raw=result.raw,
                )
                return self._command_failed(instance_id, "start", mismatch)
            if broker and broker.lower() != current_runtime["trade_provider"].lower():
                mismatch = RuntimeCommandResult(
                    False,
                    runtime_id=result.runtime_id,
                    heartbeat_at=result.heartbeat_at,
                    runner_status=result.runner_status,
                    error="daemon broker does not match the deployment provider",
                    raw=result.raw,
                )
                return self._command_failed(instance_id, "start", mismatch)
        observed = _observed_lifecycle(result, default=LifecycleState.WARMING_UP.value)
        if external:
            observed = LifecycleState.PAUSED_PENDING_RECONCILE.value
        runtime = self.store.transition_runtime(
            instance_id,
            lifecycle=observed,
            observed_state=observed,
            runtime_id=result.runtime_id,
            account_id=current_runtime["account_id"] or account_id,
            runner_heartbeat_at=result.heartbeat_at,
            last_command_id=result.command_id,
            last_error={},
            reconcile_required=external,
            reconciled=not external,
            binding_active=current_runtime["binding_active"],
        )
        if not external:
            self._block(instance_id, False)
            if self.store.get_active_runtime_run(instance_id, run_mode=mode) is None:
                self.store.start_runtime_run(instance_id, mode)
        elif not live:
            reconciled = self.reconcile(instance_id)
            if not reconciled["ok"]:
                return {
                    **reconciled,
                    "action": "start",
                    "startup_phase": "reconcile",
                    "auto_reconciled": False,
                }
            resumed = self.resume(instance_id)
            return {
                **resumed,
                "action": "start",
                "startup_phase": "running" if resumed["ok"] else "resume",
                "auto_reconciled": True,
            }
        return self._response(instance_id, "start", result, runtime)

    def pause(self, instance_id: str) -> dict[str, Any]:
        instance = self._instance(instance_id)
        self._block(instance_id, True, "deployment paused")
        self.store.update_runtime_state(
            instance_id,
            expected_version=instance["runtime"]["version"],
            desired_state=LifecycleState.PAUSED.value,
        )
        result = self.control.pause(instance)
        if not result.ok:
            return self._command_failed(instance_id, "pause", result)
        runtime = self.store.transition_runtime(
            instance_id,
            lifecycle=LifecycleState.PAUSED.value,
            observed_state=LifecycleState.PAUSED.value,
            runner_heartbeat_at=result.heartbeat_at,
            last_command_id=result.command_id,
            last_error={},
        )
        return self._response(instance_id, "pause", result, runtime)

    def reconcile(self, instance_id: str) -> dict[str, Any]:
        instance = self._instance(instance_id)
        self._block(instance_id, True, "deployment reconciliation in progress")
        self.store.update_runtime_state(
            instance_id,
            expected_version=instance["runtime"]["version"],
            desired_state=LifecycleState.PAUSED.value,
            reconcile_required=True,
        )
        result = self.control.reconcile(instance)
        warnings = list((result.recovery or {}).get("warnings") or [])
        if not result.ok:
            return self._command_failed(instance_id, "reconcile", result)
        if warnings:
            self._record_runtime_event(
                instance,
                "reconciliation_warnings",
                count=len(warnings),
                details={"warnings": warnings},
            )
            unresolved = RuntimeCommandResult(
                ok=False,
                command_id=result.command_id,
                runtime_id=result.runtime_id,
                heartbeat_at=result.heartbeat_at,
                runner_status=result.runner_status,
                recovery=result.recovery,
                error="reconciliation has unresolved warnings",
                raw=result.raw,
            )
            return self._command_failed(instance_id, "reconcile", unresolved, halted=False)
        runtime = self.store.transition_runtime(
            instance_id,
            lifecycle=LifecycleState.PAUSED.value,
            desired_state=LifecycleState.PAUSED.value,
            observed_state=LifecycleState.PAUSED.value,
            runtime_id=result.runtime_id or self.store.get_runtime_state(instance_id)["runtime_id"],
            runner_heartbeat_at=result.heartbeat_at,
            last_command_id=result.command_id,
            reconcile_required=False,
            reconciled=True,
            last_error={},
        )
        return self._response(instance_id, "reconcile", result, runtime)

    def resume(self, instance_id: str) -> dict[str, Any]:
        instance = self._instance(instance_id)
        mode = instance["deployment"]["run_mode"]
        if mode == DeploymentMode.LIVE.value:
            _require_automated_live_enabled()
        current = self.store.get_runtime_state(instance_id)
        if current["reconcile_required"]:
            raise ValueError("deployment must reconcile successfully before resume")
        self._block(instance_id, True, "deployment resume is awaiting runtime confirmation")
        self.store.update_runtime_state(
            instance_id,
            expected_version=current["version"],
            desired_state=LifecycleState.RUNNING.value,
        )
        result = self.control.resume(instance)
        if not result.ok:
            return self._command_failed(instance_id, "resume", result)
        observed = _observed_lifecycle(result, default=LifecycleState.RUNNING.value)
        runtime = self.store.transition_runtime(
            instance_id,
            lifecycle=observed,
            observed_state=observed,
            runner_heartbeat_at=result.heartbeat_at,
            last_command_id=result.command_id,
            last_error={},
        )
        # The command-level block is no longer needed after daemon confirmation.
        # WARMING_UP remains fail-closed through the desired/observed lifecycle
        # checks and will become routable only after a matching runner heartbeat.
        self._block(instance_id, False)
        if self.store.get_active_runtime_run(instance_id, run_mode=mode) is None:
            self.store.start_runtime_run(instance_id, mode)
        return self._response(instance_id, "resume", result, runtime)

    def stop(self, instance_id: str) -> dict[str, Any]:
        instance = self._instance(instance_id)
        mode = instance["deployment"]["run_mode"]
        active_run = self.store.get_active_runtime_run(
            instance_id,
            run_mode=mode,
        )
        self._block(instance_id, True, "deployment stopped")
        self.store.update_runtime_state(
            instance_id,
            expected_version=instance["runtime"]["version"],
            desired_state=LifecycleState.STOPPED.value,
        )
        result = self.control.stop(instance)
        if not result.ok:
            return self._command_failed(instance_id, "stop", result)
        runtime = self.store.transition_runtime(
            instance_id,
            lifecycle=LifecycleState.STOPPED.value,
            observed_state=LifecycleState.STOPPED.value,
            runtime_id="",
            runner_heartbeat_at="",
            last_command_id=result.command_id,
            reconcile_required=(
                mode in {
                    DeploymentMode.SIMULATION.value,
                    DeploymentMode.SHADOW.value,
                    DeploymentMode.LIVE.value,
                }
            ),
            reconciled=False,
            binding_active=False,
            last_error={},
        )
        if active_run is not None:
            self.store.finish_runtime_run(
                active_run["run_id"],
                trading_sessions=active_run["trading_sessions"],
                metrics={},
            )
        return self._response(instance_id, "stop", result, runtime)

    def status(self, instance_id: str) -> dict[str, Any]:
        instance = self._instance(instance_id)
        result = self.control.status(instance)
        if not result.ok:
            self._block(instance_id, True, "runtime status is unavailable")
            return self._command_failed(instance_id, "status", result)
        if result.ok and result.heartbeat_at:
            current = self.store.get_runtime_state(instance_id)
            observed = _observed_lifecycle(
                result,
                default=current["observed_state"],
            )
            if not _states_compatible(current["desired_state"], observed):
                self._block(instance_id, True, "desired and observed runtime state differ")
                mismatch = RuntimeCommandResult(
                    False,
                    runtime_id=result.runtime_id,
                    heartbeat_at=result.heartbeat_at,
                    runner_status=result.runner_status,
                    error=(
                        f"desired state {current['desired_state']!r} does not match "
                        f"daemon state {observed!r}"
                    ),
                    raw=result.raw,
                )
                runtime = self.store.transition_runtime(
                    instance_id,
                    lifecycle=LifecycleState.HALTED.value,
                    observed_state=observed,
                    runtime_id=result.runtime_id,
                    runner_heartbeat_at=result.heartbeat_at,
                    last_error={"action": "status", "error": mismatch.error},
                    reconcile_required=True,
                )
                self._record_runtime_event(
                    instance,
                    "unresolved_errors",
                    details={"action": "status", "error": mismatch.error},
                )
                return self._response(instance_id, "status", mismatch, runtime)
            if current["reconcile_required"]:
                self.store.update_runtime_state(
                    instance_id,
                    observed_state=observed,
                    runtime_id=result.runtime_id,
                    runner_heartbeat_at=result.heartbeat_at,
                )
            else:
                self.store.transition_runtime(
                    instance_id,
                    lifecycle=observed,
                    observed_state=observed,
                    runtime_id=result.runtime_id,
                    runner_heartbeat_at=result.heartbeat_at,
                    last_error={},
                )
        return self._response(instance_id, "status", result, self.store.get_runtime_state(instance_id))

    def subscribe_observer(
        self,
        instance_id: str,
        symbols: list[str],
    ) -> dict[str, Any]:
        """Send a neutral observer command without changing lifecycle truth."""
        instance = self._instance(instance_id)
        result = self.control.subscribe_observer(instance, symbols)
        last = (
            result.raw.get("last_command")
            if isinstance(result.raw.get("last_command"), dict)
            else {}
        )
        details = {
            key: value
            for key, value in last.items()
            if key not in {"id", "ts", "action"}
        }
        self._record_runtime_event(
            instance,
            "observer_subscription",
            count=max(len(details.get("added") or []), 1),
            details={
                "ok": result.ok,
                "command_id": result.command_id,
                "error": result.error,
                **details,
            },
        )
        error = result.error or str(details.get("error") or "")
        return {
            "ok": result.ok,
            "action": "observer_subscribe",
            "instance_id": instance_id,
            "command_id": result.command_id,
            "runtime": self.store.get_runtime_state(instance_id),
            "runner_status": result.runner_status,
            "error": error,
            "upgrade_required": "unsupported daemon command" in error.lower(),
            **details,
            "deployment": self.store.deployment(instance_id),
        }

    def market_snapshot(
        self,
        instance_id: str,
        symbols: list[str] | None = None,
    ) -> dict[str, Any]:
        instance = self._instance(instance_id)
        return self.control.market_snapshot(instance, symbols)

    def market_bars(
        self,
        instance_id: str,
        symbol: str,
        interval: int,
        *,
        limit: int = 300,
    ) -> dict[str, Any]:
        instance = self._instance(instance_id)
        return self.control.market_bars(
            instance,
            symbol,
            interval,
            limit=limit,
        )

    def _command_failed(
        self,
        instance_id: str,
        action: str,
        result: RuntimeCommandResult,
        *,
        halted: bool = True,
    ) -> dict[str, Any]:
        lifecycle = LifecycleState.HALTED.value if halted else LifecycleState.PAUSED_PENDING_RECONCILE.value
        observed = LifecycleState.ERROR.value if halted else LifecycleState.PAUSED_PENDING_RECONCILE.value
        runtime = self.store.transition_runtime(
            instance_id,
            lifecycle=lifecycle,
            desired_state=LifecycleState.PAUSED.value,
            observed_state=observed,
            runner_heartbeat_at=result.heartbeat_at,
            last_command_id=result.command_id,
            last_error={
                "action": action,
                "error": result.error or "runtime command was not confirmed",
                "timed_out": result.timed_out,
            },
            reconcile_required=True,
            reconciled=False,
        )
        instance = self._instance(instance_id)
        self._record_runtime_event(
            instance,
            "unresolved_errors",
            details={"action": action, "error": result.error, "timed_out": result.timed_out},
        )
        return self._response(instance_id, action, result, runtime)

    def _instance(self, instance_id: str) -> dict[str, Any]:
        instance = dict(self.store.get_instance(instance_id))
        if instance["config_hash"] != instance["config"].get("config_hash"):
            raise ValueError("strategy instance config projection is inconsistent")
        if instance["validation_state"] != InstanceValidationState.VALIDATED.value:
            raise ValueError("strategy instance must be validated before daemon control")
        deployment = self.store.get_deployment_spec(instance_id)
        if deployment["stale"]:
            raise ValueError("deployment is stale; validate and configure it again")
        instance["runtime"] = self.store.get_runtime_state(instance_id)
        instance["deployment"] = deployment
        return instance

    def _block(self, instance_id: str, active: bool, reason: str = "") -> None:
        self.store.set_route_block("runtime", instance_id, active=active, reason=reason)

    def _record_runtime_event(
        self,
        instance: dict[str, Any],
        event_type: str,
        *,
        count: int = 1,
        details: Any = None,
    ) -> None:
        mode = instance["deployment"]["run_mode"]
        self.store.record_runtime_event(
            instance["instance_id"],
            config_hash=instance["config_hash"],
            run_mode=mode,
            event_type=event_type,
            count=count,
            details=details,
        )

    def _response(
        self,
        instance_id: str,
        action: str,
        result: RuntimeCommandResult,
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "ok": result.ok,
            "action": action,
            "instance_id": instance_id,
            "runtime": runtime,
            "runner_status": result.runner_status,
            "recovery": result.recovery,
            "error": result.error,
            "deployment": self.store.deployment(instance_id),
        }


def _observed_lifecycle(result: RuntimeCommandResult, *, default: str) -> str:
    value = str((result.runner_status or {}).get("lifecycle") or default)
    allowed = {item.value for item in LifecycleState}
    return value if value in allowed else default


def _observed_binding(result: RuntimeCommandResult) -> tuple[str, str]:
    raw = result.raw or {}
    state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
    account = state.get("account") if isinstance(state.get("account"), dict) else {}
    account_id = str(account.get("account_id") or "")
    if not account_id and account.get("account_id_hash"):
        account_id = ACCOUNT_HASH_PREFIX + str(account["account_id_hash"]).lower()
    broker = str(raw.get("trade_broker") or raw.get("broker") or "")
    return account_id, broker


def _states_compatible(desired: str, observed: str) -> bool:
    if desired == LifecycleState.RUNNING.value:
        return observed in {
            LifecycleState.WARMING_UP.value,
            LifecycleState.READY.value,
            LifecycleState.RUNNING.value,
        }
    if desired == LifecycleState.PAUSED.value:
        return observed in {
            LifecycleState.PAUSED.value,
            LifecycleState.PAUSED_PENDING_RECONCILE.value,
        }
    return desired == observed


def _require_automated_live_enabled() -> None:
    value = os.getenv("ALPHAPILOT_AUTOMATED_LIVE_ENABLED", "").strip().lower()
    if value not in {"1", "true", "yes", "on"}:
        raise ValueError(
            "automated LIVE is disabled; set ALPHAPILOT_AUTOMATED_LIVE_ENABLED=true"
        )

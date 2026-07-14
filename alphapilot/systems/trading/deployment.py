"""Deployment application service coordinating desired and observed state."""

from __future__ import annotations

from typing import Any

from alphapilot.systems.trading.domain import DeploymentLevel, LifecycleState
from alphapilot.systems.trading.ports import RuntimeCommandResult, RuntimeControlPort


class DeploymentCoordinator:
    """Serialise lifecycle commands around a concrete runtime-control port."""

    def __init__(self, store: Any, control: RuntimeControlPort) -> None:
        self.store = store
        self.control = control

    def start(self, instance_id: str) -> dict[str, Any]:
        instance = self._instance(instance_id)
        level = instance["deployment_level"]
        if level == DeploymentLevel.REPLAY.value:
            raise ValueError("REPLAY instances cannot be started as a deployment")
        live = level == DeploymentLevel.LIVE.value
        self._block(instance_id, True, "deployment start is awaiting runtime confirmation")
        prepared = self.store.update_runtime_state(
            instance_id,
            expected_version=instance["runtime"]["version"],
            desired_state=(LifecycleState.PAUSED.value if live else LifecycleState.RUNNING.value),
            reconcile_required=live,
            binding_active=(True if live else instance["runtime"]["binding_active"]),
            last_error={},
        )
        instance["runtime"] = prepared
        result = self.control.start(instance)
        if not result.ok:
            return self._command_failed(instance_id, "start", result)
        current_runtime = self.store.get_runtime_state(instance_id)
        account_id, broker = _observed_binding(result)
        if live:
            if account_id and account_id != current_runtime["account_id"]:
                mismatch = RuntimeCommandResult(
                    False,
                    runtime_id=result.runtime_id,
                    heartbeat_at=result.heartbeat_at,
                    runner_status=result.runner_status,
                    error="daemon account does not match the LIVE promotion binding",
                    raw=result.raw,
                )
                return self._command_failed(instance_id, "start", mismatch)
            if broker and broker.lower() != current_runtime["broker"].lower():
                mismatch = RuntimeCommandResult(
                    False,
                    runtime_id=result.runtime_id,
                    heartbeat_at=result.heartbeat_at,
                    runner_status=result.runner_status,
                    error="daemon broker does not match the LIVE promotion binding",
                    raw=result.raw,
                )
                return self._command_failed(instance_id, "start", mismatch)
        observed = _observed_lifecycle(result, default=LifecycleState.WARMING_UP.value)
        if live:
            observed = LifecycleState.PAUSED_PENDING_RECONCILE.value
        runtime = self.store.transition_runtime(
            instance_id,
            lifecycle=observed,
            observed_state=observed,
            runtime_id=result.runtime_id,
            account_id=current_runtime["account_id"] or account_id,
            broker=current_runtime["broker"] or broker,
            runner_heartbeat_at=result.heartbeat_at,
            last_command_id=result.command_id,
            last_error={},
            reconcile_required=live,
            binding_active=current_runtime["binding_active"],
        )
        if not live:
            self._block(instance_id, False)
            if level in {DeploymentLevel.PAPER.value, DeploymentLevel.SHADOW.value}:
                if self.store.get_active_stage_run(instance_id, stage=level) is None:
                    self.store.start_stage_run(instance_id, level)
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
            self._record_stage_event(
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
            last_error={},
        )
        return self._response(instance_id, "reconcile", result, runtime)

    def resume(self, instance_id: str) -> dict[str, Any]:
        instance = self._instance(instance_id)
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
        return self._response(instance_id, "resume", result, runtime)

    def stop(self, instance_id: str) -> dict[str, Any]:
        instance = self._instance(instance_id)
        active_run = self.store.get_active_stage_run(
            instance_id,
            stage=instance["deployment_level"],
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
            runner_heartbeat_at=result.heartbeat_at,
            last_command_id=result.command_id,
            reconcile_required=instance["deployment_level"] == DeploymentLevel.LIVE.value,
            binding_active=False,
            last_error={},
        )
        if active_run is not None:
            self.store.finish_stage_run(
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
                self._record_stage_event(
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
        )
        instance = self.store.get_instance(instance_id)
        self._record_stage_event(
            instance,
            "unresolved_errors",
            details={"action": action, "error": result.error, "timed_out": result.timed_out},
        )
        return self._response(instance_id, action, result, runtime)

    def _instance(self, instance_id: str) -> dict[str, Any]:
        instance = dict(self.store.get_instance(instance_id))
        if instance["config_hash"] != instance["config"].get("config_hash"):
            raise ValueError("strategy instance config projection is inconsistent")
        instance["runtime"] = self.store.get_runtime_state(instance_id)
        return instance

    def _block(self, instance_id: str, active: bool, reason: str = "") -> None:
        self.store.set_route_block("instance", instance_id, active=active, reason=reason)

    def _record_stage_event(
        self,
        instance: dict[str, Any],
        event_type: str,
        *,
        count: int = 1,
        details: Any = None,
    ) -> None:
        level = instance["deployment_level"]
        if level not in {DeploymentLevel.PAPER.value, DeploymentLevel.SHADOW.value}:
            return
        self.store.record_stage_event(
            instance["instance_id"],
            config_hash=instance["config_hash"],
            stage=level,
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

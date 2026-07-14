"""Fail-closed authorization for automated order routing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from alphapilot.systems.trading.ports import (
    RouteAuthorization,
    RouteContext,
    RouteOrigin,
)


class AutomatedRouteAuthorizer:
    """Authorize one automated route against persisted deployment truth."""

    def __init__(
        self,
        store: Any,
        *,
        heartbeat_ttl_seconds: float = 15.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.heartbeat_ttl_seconds = max(float(heartbeat_ttl_seconds), 0.1)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def authorize(self, context: RouteContext) -> RouteAuthorization:
        if context.origin != RouteOrigin.AUTOMATED:
            return self._deny("invalid_origin", "automated authorizer only accepts automated routes")
        missing = [
            name for name in (
                "instance_id", "config_hash", "account_id", "broker",
                "deployment_level", "runtime_id",
            )
            if not str(getattr(context, name, "")).strip()
        ]
        if missing:
            return self._deny("missing_binding", "missing route binding: " + ", ".join(missing))
        try:
            instance = self.store.get_instance(context.instance_id)
            runtime = self.store.get_runtime_state(context.instance_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            return self._deny("state_unavailable", str(exc))

        if instance["config_hash"] != context.config_hash or runtime["config_hash"] != context.config_hash:
            return self._deny("config_hash", "route uses a stale or mismatched config_hash")
        if instance["deployment_level"] != context.deployment_level:
            return self._deny("deployment_level", "instance deployment level does not match route")
        if runtime["deployment_level"] != context.deployment_level:
            return self._deny("runtime_deployment", "runtime deployment level does not match route")
        if context.deployment_level not in {"paper", "live"}:
            return self._deny("routing_disabled", f"{context.deployment_level} cannot route orders")
        if runtime["account_id"] != context.account_id:
            return self._deny("account_binding", "account_id does not match deployment authorization")
        if runtime["broker"].lower() != context.broker.lower():
            return self._deny("broker_binding", "broker does not match deployment authorization")
        if runtime["runtime_id"] != context.runtime_id:
            return self._deny("runtime_binding", "runtime_id does not match the observed daemon")
        if context.deployment_level == "live" and not runtime["binding_active"]:
            return self._deny("writer_revoked", "LIVE automated-writer binding is not active")
        if runtime["reconcile_required"]:
            return self._deny("reconcile_required", "deployment must reconcile before routing")
        if runtime["desired_state"] != "running" or runtime["observed_state"] != "running":
            return self._deny(
                "lifecycle",
                f"deployment is {runtime['desired_state']}/{runtime['observed_state']}, not running",
            )
        if instance["lifecycle"] != "running":
            return self._deny("instance_lifecycle", "strategy instance is not running")

        blocks = self.store.active_route_blocks(
            instance_id=context.instance_id,
            account_id=context.account_id,
        )
        if blocks:
            scopes = ", ".join(f"{item['scope_type']}:{item['scope_id']}" for item in blocks)
            return self._deny("kill_switch", f"route blocked by {scopes}")

        heartbeat = _parse_timestamp(runtime.get("runner_heartbeat_at"))
        if heartbeat is None:
            return self._deny("heartbeat_missing", "runner heartbeat is missing")
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = max((now.astimezone(timezone.utc) - heartbeat).total_seconds(), 0.0)
        if age > self.heartbeat_ttl_seconds:
            return self._deny(
                "heartbeat_stale",
                f"runner heartbeat age {age:.1f}s exceeds {self.heartbeat_ttl_seconds:.1f}s",
            )
        return RouteAuthorization(True, "authorized", "deployment binding and runtime state match")

    @staticmethod
    def _deny(rule: str, reason: str) -> RouteAuthorization:
        return RouteAuthorization(False, rule, reason)


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A naive heartbeat is ambiguous across hosts/timezones and could make
        # a dead runner look fresh for hours.  New runtimes always persist UTC.
        return None
    return parsed.astimezone(timezone.utc)

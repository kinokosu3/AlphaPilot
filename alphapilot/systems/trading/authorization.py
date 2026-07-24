"""Fail-closed authorization for automated order routing."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Callable

from alphapilot.systems.trading.account_identity import account_identities_match
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
                "run_mode", "runtime_id", "binding_hash",
            )
            if not str(getattr(context, name, "")).strip()
        ]
        if missing:
            return self._deny("missing_binding", "missing route binding: " + ", ".join(missing))
        try:
            instance = self.store.get_instance(context.instance_id)
            deployment = self.store.get_deployment_spec(context.instance_id)
            runtime = self.store.get_runtime_state(context.instance_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            return self._deny("state_unavailable", str(exc))

        if instance.get("validation_state") != "validated":
            return self._deny("validation_state", "strategy instance is not validated")
        if deployment.get("stale"):
            return self._deny("stale_deployment", "deployment is bound to an old config_hash")
        if any(
            str(item.get("config_hash") or "") != context.config_hash
            for item in (instance, deployment, runtime)
        ):
            return self._deny("config_hash", "route uses a stale or mismatched config_hash")
        if deployment["run_mode"] != context.run_mode or runtime["run_mode"] != context.run_mode:
            return self._deny("run_mode", "deployment run mode does not match route")
        if context.run_mode == "shadow":
            return self._deny("shadow_no_route", "SHADOW deployments never route orders")
        if context.run_mode not in {"paper", "simulation", "live"}:
            return self._deny("routing_disabled", f"{context.run_mode} cannot route orders")
        if context.run_mode == "live" and not account_identities_match(
            str(deployment.get("account_id") or ""),
            str(runtime.get("account_id") or ""),
        ):
            return self._deny(
                "deployment_account_binding",
                "runtime account_id does not match the configured LIVE deployment",
            )
        if context.run_mode == "live" and os.getenv(
            "ALPHAPILOT_AUTOMATED_LIVE_ENABLED", ""
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            return self._deny("live_disabled", "automated LIVE is disabled by environment")
        for field in (
            "execution_environment", "trade_provider", "quote_provider",
            "quote_data_kind", "binding_hash",
        ):
            expected = str(deployment.get(field) or "").lower()
            if str(runtime.get(field) or "").lower() != expected:
                return self._deny(
                    f"runtime_{field}", f"runtime {field} does not match deployment",
                )
            if str(getattr(context, field, "") or "").lower() != expected:
                return self._deny(
                    f"{field}_binding", f"{field} does not match deployment authorization",
                )
        if str(runtime.get("account_profile") or "") != str(
            deployment.get("account_profile") or ""
        ):
            return self._deny(
                "runtime_account_profile",
                "runtime account_profile does not match the configured deployment",
            )
        environment = str(runtime.get("execution_environment") or "")
        if environment in {"live", "broker_simulation"} and not bool(runtime.get("reconciled")):
            return self._deny("not_reconciled", "external account truth has not been reconciled")
        if not account_identities_match(runtime["account_id"], context.account_id):
            return self._deny("account_binding", "account_id does not match deployment authorization")
        if runtime["trade_provider"].lower() != context.broker.lower():
            return self._deny("broker_binding", "broker does not match deployment authorization")
        if runtime["runtime_id"] != context.runtime_id:
            return self._deny("runtime_binding", "runtime_id does not match the observed daemon")
        if environment in {"live", "broker_simulation"} and not runtime["binding_active"]:
            return self._deny("writer_revoked", "external automated-writer binding is not active")
        if environment in {"live", "broker_simulation"}:
            writer = self.store.active_external_writer(
                execution_environment=environment,
                trade_provider=str(runtime.get("trade_provider") or context.broker),
                account_id=str(runtime.get("account_id") or ""),
                account_profile=str(runtime.get("account_profile") or ""),
            )
            if writer is None or writer["instance_id"] != context.instance_id:
                return self._deny(
                    "single_writer", "external account writer lock is missing or owned elsewhere",
                )
        if runtime["reconcile_required"]:
            return self._deny("reconcile_required", "deployment must reconcile before routing")
        if runtime["desired_state"] != "running" or runtime["observed_state"] != "running":
            return self._deny(
                "lifecycle",
                f"deployment is {runtime['desired_state']}/{runtime['observed_state']}, not running",
            )
        blocks = self.store.active_route_blocks(
            instance_id=context.instance_id,
            account_id=str(runtime.get("account_id") or context.account_id),
        )
        if blocks:
            return self._deny("kill_switch", f"route blocked by {route_block_summary(blocks)}")

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


def route_block_summary(blocks: list[dict[str, Any]]) -> str:
    """Describe active blocks without exposing account or instance identifiers."""

    rows: list[str] = []
    for item in blocks:
        scope_type = str(item.get("scope_type") or "unknown")
        scope_id = str(item.get("scope_id") or "")
        if scope_type == "global":
            rows.append("global:*")
            continue
        digest = hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:12]
        rows.append(f"{scope_type}:sha256:{digest}")
    return ", ".join(rows)

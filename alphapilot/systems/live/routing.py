"""Concrete automated order-routing adapter for a live runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from alphapilot.systems.trading.ports import (
    AutomatedRouteAuthorizerPort,
    RouteAuthorization,
    RouteContext,
)


class AutomatedOrderRouter:
    """Authorize every child order immediately before guarded submission."""

    def __init__(
        self,
        engine: Any,
        authorizer: AutomatedRouteAuthorizerPort | None,
        context_fn: Callable[[], RouteContext],
    ) -> None:
        self.engine = engine
        self.authorizer = authorizer
        self.context_fn = context_fn
        self.last_authorization: RouteAuthorization | None = None

    def submit(self, request: Any) -> str | None:
        context = self.context_fn()
        reference = str(getattr(request, "reference", "") or "")
        expected_prefix = f"{context.instance_id}:{context.config_hash or '-'}:"
        if not reference.startswith(expected_prefix):
            decision = RouteAuthorization(
                False,
                "reference_binding",
                "automated child reference does not match instance_id/config_hash",
            )
            self.last_authorization = decision
            self._record_block(request, decision)
            return None
        if self.authorizer is None:
            decision = RouteAuthorization(
                False,
                "authorizer_missing",
                "automated routing is disabled because no authorizer is installed",
            )
        else:
            try:
                decision = self.authorizer.authorize(context)
            except Exception as exc:  # noqa: BLE001 - authorization must fail closed
                decision = RouteAuthorization(False, "authorizer_error", f"{type(exc).__name__}: {exc}")
        self.last_authorization = decision
        if not decision.allowed:
            self._record_block(request, decision)
            return None
        return self.engine.submit(request, origin="automated")

    def _record_block(self, request: Any, decision: RouteAuthorization) -> None:
        self.engine.ledger.record_event(
            "blocked",
            {
                "origin": "automated",
                "rule": decision.rule,
                "reason": decision.reason,
                "reference": getattr(request, "reference", ""),
            },
            reference=getattr(request, "reference", ""),
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

"""Execution-journal fallback used when no trading application is installed."""

from __future__ import annotations

from threading import RLock
from typing import Any


class InMemoryExecutionJournal:
    """Process-local compatibility journal for standalone PAPER runtimes.

    Kernel-created runtimes inject ``StrategyRuntimeStore`` instead.  LIVE
    automated routing therefore never relies on this fallback.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self.decisions: set[str] = set()
        self.plans: set[str] = set()
        self.children: dict[str, dict[str, Any]] = {}

    def record_decision(self, decision_id: str, instance_id: str, config_hash: str, payload: Any) -> bool:
        del instance_id, config_hash, payload
        with self._lock:
            inserted = decision_id not in self.decisions
            self.decisions.add(decision_id)
            return inserted

    def record_plan(self, plan_id: str, decision_id: str, instance_id: str, payload: Any, status: str) -> bool:
        del decision_id, instance_id, payload, status
        with self._lock:
            inserted = plan_id not in self.plans
            self.plans.add(plan_id)
            return inserted

    def record_child_order(
        self,
        reference: str,
        plan_id: str,
        payload: Any,
        *,
        status: str,
        order_id: str = "",
    ) -> bool:
        with self._lock:
            if reference in self.children:
                return False
            self.children[reference] = {
                "plan_id": plan_id,
                "payload": payload,
                "status": status,
                "order_id": order_id,
            }
            return True

    def update_child_order(self, reference: str, *, status: str, order_id: str = "") -> None:
        with self._lock:
            if reference in self.children:
                self.children[reference].update(status=status, order_id=order_id)

    def record_runtime_heartbeat(self, *args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return False


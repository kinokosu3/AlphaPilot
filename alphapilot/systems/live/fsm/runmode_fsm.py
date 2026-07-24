"""Run-mode state + global kill-switch.

Two orthogonal pieces of safety state:

* **mode** — low-level runtimes may select any mode directly. Formal strategy
  deployment changes are guarded separately by stop + PUT deployment.
* **halted** — the kill-switch. Any component may ``halt(reason)``; while halted,
  :meth:`can_submit_orders` is ``False`` regardless of mode. ``resume`` clears it.

The risk gate checks this machine on every order.
"""

from __future__ import annotations

from alphapilot.systems.live.config import RunMode, allows_order_routing
from alphapilot.systems.live.fsm.base import check_transition

_MODES = {
    RunMode.DRY_RUN,
    RunMode.PAPER,
    RunMode.SIMULATION,
    RunMode.SHADOW,
    RunMode.LIVE,
}
ALLOWED: dict[str, set[str]] = {mode: set(_MODES) for mode in _MODES}


class RunModeMachine:
    """Run mode + kill-switch, guarding order submission."""

    def __init__(
        self,
        mode: str = RunMode.DRY_RUN,
        *,
        provider_routing_enabled: bool = True,
        provider_block_reason: str = "",
    ) -> None:
        if mode not in ALLOWED:
            raise ValueError(f"unknown run mode: {mode!r}")
        self.mode = mode
        self.halted = False
        self.halt_reason = ""
        self.provider_routing_enabled = bool(provider_routing_enabled)
        self.provider_block_reason = str(provider_block_reason)

    def set_mode(self, target: str) -> str:
        if target not in ALLOWED:
            raise ValueError(f"unknown run mode: {target!r}")
        check_transition(ALLOWED, self.mode, target, label="run-mode")
        self.mode = target
        return self.mode

    def halt(self, reason: str = "") -> None:
        """Engage the kill-switch (idempotent)."""
        self.halted = True
        self.halt_reason = reason or self.halt_reason or "halted"

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""

    def can_submit_orders(self) -> bool:
        """Orders may be routed only when not halted and not in dry-run."""
        return (
            not self.halted
            and self.provider_routing_enabled
            and allows_order_routing(self.mode)
        )

    def is_dry_run(self) -> bool:
        return self.mode == RunMode.DRY_RUN

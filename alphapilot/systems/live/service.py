"""LiveSystem — the kernel-registered capability provider for live trading.

Kept intentionally thin: it binds the engine context, owns the resolved
:class:`LiveConfig`, and (in later phases) builds a :class:`LiveEngine` on
demand. Registering it early gives the CLI / portal a stable ``context.system(
"live")`` handle while the heavier pieces (OMS, brokers, executor) are layered on.

Crucially, importing this module must **not** pull vn.py or any broker SDK — those
load only inside a concrete gateway when ``mode == LIVE``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from alphapilot.kernel.base import BaseSystem
from alphapilot.systems.live.config import LiveConfig, RunMode

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context


class LiveSystem(BaseSystem):
    """Live-trading capability provider (paper / sim now; real brokers later)."""

    name = "live"

    def setup(self, context: "Context") -> None:
        self.context = context
        # Prefer the engine-wide config's ``live`` block when present, else load
        # a standalone LiveConfig from the environment.
        self.config: LiveConfig = getattr(context.config, "live", None) or LiveConfig.load()

    # ---- engine factory -------------------------------------------------- #
    def make_broker(self):
        """Pick the broker gateway for the configured mode/broker.

        DRY_RUN / PAPER -> in-process :class:`PaperBroker`; LIVE -> resolve through
        the broker registry (native gateways constructed directly, vn.py gateways
        wrapped in an adapter — either way the SDK loads lazily, here)."""
        if self.config.mode == RunMode.LIVE:
            from alphapilot.systems.live.brokers.registry import create_gateway

            return create_gateway(self.config.trade_broker or self.config.broker)
        from alphapilot.systems.live.brokers.paper import PaperBroker

        return PaperBroker()

    def create_engine(self, *, now_fn=None, is_trading_day_fn=None, broker=None):
        """Build a wired :class:`LiveEngine` (OMS + FSMs + risk gate + ledger)."""
        from alphapilot.systems.live.engine import LiveEngine
        from alphapilot.systems.live.risk import RiskGate

        return LiveEngine(
            self.config,
            broker or self.make_broker(),
            now_fn=now_fn,
            is_trading_day_fn=is_trading_day_fn,
            risk=RiskGate(self.config.risk),
        )

    def create_paper_engine(self, *, cash: float | None = None, now_fn=None):
        """Build a **PAPER** sandbox engine regardless of the deployed mode.

        Used by the portal so the whole live stack (reconcile -> risk -> broker ->
        OMS -> ledger) can be exercised safely in-process, without vn.py or a real
        broker. Session gating is disabled so the sandbox works outside market hours.
        """
        from dataclasses import replace

        from alphapilot.systems.live.brokers.paper import PaperBroker
        from alphapilot.systems.live.config import RunMode
        from alphapilot.systems.live.engine import LiveEngine
        from alphapilot.systems.live.risk import RiskGate

        paper_cfg = replace(self.config, mode=RunMode.PAPER)
        broker = PaperBroker(cash=float(cash) if cash is not None else 1_000_000.0)
        return LiveEngine(
            paper_cfg, broker, now_fn=now_fn,
            risk=RiskGate(self.config.risk, enforce_session=False),
        )

    def create_runtime(
        self,
        *,
        mode: str | None = None,
        broker: str | None = None,
        trade_broker: str | None = None,
        quote_provider: str | None = None,
        ledger_dir: str | None = None,
        state_dir: str | None = None,
        now_fn=None,
        is_trading_day_fn=None,
    ):
        """Build a reusable live runtime for CLI / Portal / daemon control.

        This is the AlphaPilot analogue of vn.py's no-UI ``MainEngine`` process:
        a wired engine plus broker, ready to connect, reconcile and route through
        the same guarded submit path.
        """
        from alphapilot.systems.live.runtime import LiveRuntime, clone_config

        cfg = clone_config(
            self.config,
            mode=mode,
            broker=broker,
            trade_broker=trade_broker,
            quote_provider=quote_provider,
            ledger_dir=ledger_dir,
            state_dir=state_dir,
        )
        return LiveRuntime.create(
            cfg,
            now_fn=now_fn,
            is_trading_day_fn=is_trading_day_fn,
        )

    # ---- introspection (used by CLI / portal) ---------------------------- #
    def modes(self) -> list[str]:
        return [RunMode.DRY_RUN, RunMode.PAPER, RunMode.LIVE]

    def snapshot(self) -> dict[str, Any]:
        """Non-secret view of the resolved live configuration."""
        cfg = self.config
        return {
            "mode": cfg.mode,
            "broker": cfg.broker,
            "trade_broker": cfg.trade_broker,
            "quote_provider": cfg.quote_provider,
            "timezone": cfg.timezone,
            "ledger_dir": str(cfg.ledger_dir),
            "state_dir": str(cfg.state_dir),
            "risk": {
                "max_order_value": cfg.risk.max_order_value,
                "max_daily_value": cfg.risk.max_daily_value,
                "max_position_pct": cfg.risk.max_position_pct,
                "price_guard_pct": cfg.risk.price_guard_pct,
                "max_orders_per_day": cfg.risk.max_orders_per_day,
                "lot_size": cfg.risk.lot_size,
            },
        }

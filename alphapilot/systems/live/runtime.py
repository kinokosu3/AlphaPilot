"""Runtime helpers for running AlphaPilot live trading from CLI/Portal/daemons.

The lower live stack already mirrors vn.py's gateway/OMS separation. This module
adds the missing orchestration layer: build the right broker from config, connect
with env-backed settings, wait for an account snapshot, reconcile target books,
route through :class:`LiveEngine.submit`, and persist a compact state snapshot.

It is intentionally usable as a one-shot CLI helper today and as the core of a
long-lived daemon later.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.engine import LiveEngine
from alphapilot.systems.live.executor import reconcile
from alphapilot.systems.live.risk import RiskGate
from alphapilot.systems.live.targets import TargetPortfolio
from alphapilot.systems.live.types import (
    Direction,
    Exchange,
    OrderRequest,
    OrderType,
    normalize_symbol,
)


def clone_config(
    config: LiveConfig,
    *,
    mode: str | None = None,
    broker: str | None = None,
    ledger_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> LiveConfig:
    """Return a config copy with optional runtime overrides."""
    return replace(
        config,
        mode=mode or config.mode,
        broker=broker or config.broker,
        ledger_dir=Path(ledger_dir).expanduser() if ledger_dir else config.ledger_dir,
        state_dir=Path(state_dir).expanduser() if state_dir else config.state_dir,
    )


def require_live_confirmation(config: LiveConfig, *, confirm_live: bool) -> None:
    """Fail closed before any LIVE-mode route unless the caller confirms."""
    if config.mode == RunMode.LIVE and not confirm_live:
        raise ValueError("LIVE mode requires confirm_live=True")


class LiveRuntime:
    """A connected or connectable live trading runtime."""

    def __init__(self, config: LiveConfig, engine: LiveEngine) -> None:
        self.config = config
        self.engine = engine
        self.state_path = Path(config.state_dir).expanduser() / "runtime_state.json"

    # ---- construction ----------------------------------------------------- #
    @classmethod
    def create(
        cls,
        config: LiveConfig,
        *,
        broker: Any = None,
        now_fn=None,
        is_trading_day_fn=None,
    ) -> "LiveRuntime":
        """Build a runtime from config without connecting yet."""
        gateway = broker or _make_broker(config)
        engine = LiveEngine(
            config,
            gateway,
            now_fn=now_fn,
            is_trading_day_fn=is_trading_day_fn,
            risk=RiskGate(config.risk, enforce_session=config.mode == RunMode.LIVE),
        )
        return cls(config, engine)

    # ---- lifecycle -------------------------------------------------------- #
    def connect(self, *, setting: dict | None = None, paper_cash: float | None = None) -> dict[str, Any]:
        """Connect the underlying engine and write the first state snapshot."""
        if setting is None:
            setting = build_runtime_setting(self.config, paper_cash=paper_cash)
        self.engine.connect(setting)
        self.refresh_broker_state()
        return self.write_state()

    def refresh_broker_state(self) -> None:
        """Ask the broker for fresh account/position snapshots when supported."""
        for fn in (self.engine.gateway.query_account, self.engine.gateway.query_position):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - broker refresh is best-effort
                self.engine.ledger.record("refresh_error", {"error": str(exc)})

    def close(self) -> None:
        self.engine.close()
        self.write_state()

    def wait_ready(
        self,
        *,
        timeout: float = 20.0,
        require_account: bool = True,
        require_contracts: bool | None = None,
        settle_seconds: float | None = None,
    ) -> bool:
        """Wait until the runtime has the minimum state needed for execution."""
        if require_contracts is None:
            require_contracts = self.config.mode == RunMode.LIVE
        if settle_seconds is None:
            settle_seconds = 2.5 if self.config.mode == RunMode.LIVE else 0.0
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            oms = self.engine.oms
            account_ok = (not require_account) or oms.account is not None
            contracts_ok = (not require_contracts) or bool(oms.contracts)
            if account_ok and contracts_ok:
                if settle_seconds > 0:
                    time.sleep(float(settle_seconds))
                self.write_state()
                return True
            time.sleep(0.2)
        self.write_state()
        return False

    # ---- actions ---------------------------------------------------------- #
    def submit_order(
        self,
        symbol: str,
        *,
        side: str,
        volume: float,
        price: float = 0.0,
        order_type: str = "limit",
        reference: str = "",
    ) -> dict[str, Any]:
        """Submit one normalized order through the guarded engine path."""
        code, exchange = normalize_symbol(symbol)
        side_l = side.lower()
        typ = OrderType.MARKET if order_type.lower() == "market" else OrderType.LIMIT
        factory = OrderRequest.buy if side_l in ("buy", "long") else OrderRequest.sell
        req = factory(code, exchange, float(volume), float(price), type=typ, reference=reference)
        order_id = self.engine.submit(req)
        return {
            "order_id": order_id,
            "submitted": bool(order_id),
            "request": order_request_to_dict(req),
            "state": self.write_state(),
        }

    def plan_target(self, target: TargetPortfolio) -> list[OrderRequest]:
        """Diff target holdings against real OMS positions into order requests."""
        return reconcile(target, self.engine.oms, lot_size=self.config.risk.lot_size)

    def submit_target(self, target: TargetPortfolio, *, route: bool = False) -> dict[str, Any]:
        """Plan and optionally route all orders for a target portfolio."""
        requests = self.plan_target(target)
        routed: list[str] = []
        unrouted_requests: list[dict[str, Any]] = []
        if route:
            for req in requests:
                order_id = self.engine.submit(req)
                if order_id:
                    routed.append(order_id)
                else:
                    unrouted_requests.append(order_request_to_dict(req))
        return {
            "target": target_to_dict(target),
            "planned": len(requests),
            "requests": [order_request_to_dict(req) for req in requests],
            "routed": routed,
            "submitted": len(routed),
            "unrouted": len(unrouted_requests),
            "unrouted_requests": unrouted_requests,
            "fully_routed": (not route) or not unrouted_requests,
            "state": self.write_state(),
        }

    def run_loop(
        self,
        *,
        symbols: list[str] | None = None,
        interval: float = 2.0,
        duration: float | None = None,
    ) -> dict[str, Any]:
        """Keep the runtime alive, subscribed and periodically snapshotted.

        This is a deliberately small daemon core: broker SDK callbacks update
        the OMS asynchronously, while the loop advances the session clock and
        writes a state heartbeat. Strategy runners/algo steppers can later be
        attached around the same cadence.
        """
        if symbols:
            self.engine.gateway.subscribe(symbols)
        started = time.time()
        iterations = 0
        while True:
            self.engine.tick_session()
            self.write_state()
            iterations += 1
            if duration is not None and time.time() - started >= float(duration):
                break
            time.sleep(max(float(interval), 0.05))
        state = self.write_state()
        return {"iterations": iterations, "state": state}

    # ---- state ------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable runtime state projection."""
        oms = self.engine.oms
        account = oms.account
        return {
            "config": {
                "mode": self.config.mode,
                "broker": self.config.broker,
                "ledger_dir": str(self.config.ledger_dir),
                "state_dir": str(self.config.state_dir),
            },
            "engine": self.engine.snapshot(),
            "account": None if account is None else {
                "account_id": account.account_id,
                "balance": account.balance,
                "available": account.available,
                "frozen": account.frozen,
                "gateway": account.gateway,
            },
            "positions": [
                {
                    "code": p.code,
                    "exchange": p.exchange.value,
                    "volume": p.volume,
                    "available": p.available,
                    "yd_volume": p.yd_volume,
                    "frozen": p.frozen,
                    "price": p.price,
                    "pnl": p.pnl,
                    "gateway": p.gateway,
                }
                for p in oms.get_positions()
            ],
            "orders": [
                {
                    "order_id": o.order_id,
                    "code": o.code,
                    "exchange": o.exchange.value,
                    "side": side_from_direction(o.direction),
                    "price": o.price,
                    "volume": o.volume,
                    "traded": o.traded,
                    "status": o.status.value,
                    "active": o.is_active(),
                    "reference": o.reference,
                    "gateway": o.gateway,
                }
                for o in oms.orders.values()
            ],
            "trades": [
                {
                    "trade_id": t.trade_id,
                    "order_id": t.order_id,
                    "code": t.code,
                    "exchange": t.exchange.value,
                    "side": side_from_direction(t.direction),
                    "price": t.price,
                    "volume": t.volume,
                    "gateway": t.gateway,
                }
                for t in oms.get_trades()
            ],
            "logs": [
                {"level": log.level, "msg": log.msg, "gateway": log.gateway}
                for log in list(oms.logs)[-50:]
            ],
            "ledger_tail": self.engine.ledger.events()[-50:],
        }

    def write_state(self) -> dict[str, Any]:
        """Persist and return the compact runtime snapshot."""
        state = self.snapshot()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return state


def _make_broker(config: LiveConfig):
    if config.mode == RunMode.LIVE:
        from alphapilot.systems.live.brokers.registry import create_gateway

        return create_gateway(config.broker)
    from alphapilot.systems.live.brokers.paper import PaperBroker

    return PaperBroker()


def build_runtime_setting(config: LiveConfig, *, paper_cash: float | None = None) -> dict[str, Any]:
    """Build broker connect settings from env for LIVE, simple cash for paper."""
    if config.mode == RunMode.LIVE:
        from alphapilot.systems.live.brokers.registry import build_connect_setting, missing_setting_fields

        missing = missing_setting_fields(config.broker)
        if missing:
            raise ValueError("missing live broker env fields: " + ", ".join(missing))
        return build_connect_setting(config.broker)
    return {"cash": float(paper_cash) if paper_cash is not None else 1_000_000.0}


def side_from_direction(direction: Direction) -> str:
    return "buy" if direction == Direction.LONG else "sell"


def order_request_to_dict(req: OrderRequest) -> dict[str, Any]:
    return {
        "code": req.code,
        "exchange": req.exchange.value if isinstance(req.exchange, Exchange) else str(req.exchange),
        "side": side_from_direction(req.direction),
        "volume": req.volume,
        "price": req.price,
        "type": req.type.value,
        "reference": req.reference,
    }


def target_to_dict(target: TargetPortfolio) -> dict[str, Any]:
    return {
        "date": target.date,
        "holdings": dict(target.holdings),
        "prices": dict(target.prices),
        "cash": target.cash,
        "source": target.source,
        "market": target.market,
    }

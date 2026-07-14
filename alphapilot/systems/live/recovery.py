"""Recovery helpers for reconnecting a live runtime safely.

Broker state (account/positions/orders/trades) is authoritative and arrives via
gateway callbacks. The ledger is authoritative for AlphaPilot decisions, such as
which client references already passed the risk gate today. Recovery combines
both: request fresh broker snapshots and restore in-memory risk counters from
today's submitted orders.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from alphapilot.systems.live.config import uses_real_providers


class RecoveryService:
    """Best-effort recovery pass for a connected ``LiveRuntime``."""

    def __init__(self, runtime: Any, *, day: date | datetime | str | None = None) -> None:
        self.runtime = runtime
        self.day = day or datetime.now().date()

    def run(self) -> dict[str, Any]:
        engine = self.runtime.engine
        report: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "mode": self.runtime.config.mode,
            "broker": self.runtime.config.broker,
            "day": self.day.isoformat() if isinstance(self.day, (date, datetime)) else str(self.day),
            "broker_refresh_requested": True,
        }

        try:
            refresh = self.runtime.refresh_broker_state(include_orders=True, include_trades=True)
            settle_seconds = 1.5 if uses_real_providers(self.runtime.config.mode) else 0.0
            self.runtime.settle_broker_events(settle_seconds)
            report["broker_refresh"] = refresh
            report["broker_refresh_kinds"] = list(refresh.get("requested") or [])
            report["broker_refresh_unsupported"] = list(refresh.get("unsupported") or [])
            report["broker_refresh_ok"] = not bool(refresh.get("errors"))
        except Exception as exc:  # noqa: BLE001 - recovery should surface but not abort startup
            report["broker_refresh_ok"] = False
            report["broker_refresh_error"] = f"{type(exc).__name__}: {exc}"

        risk_state = recover_risk_state_from_ledger(engine.ledger, day=self.day)
        if engine.risk is not None and hasattr(engine.risk, "restore"):
            engine.risk.restore(risk_state)
            report["risk_restored"] = True
            report["risk"] = engine.risk.snapshot() if hasattr(engine.risk, "snapshot") else risk_state
        else:
            report["risk_restored"] = False
            report["risk"] = risk_state

        report["oms"] = {
            "account_present": engine.oms.account is not None,
            "positions": len(engine.oms.get_positions()),
            "orders": len(engine.oms.orders),
            "trades": len(engine.oms.trades),
            "active_orders": len(engine.oms.get_active_orders()),
            "contracts": len(engine.oms.contracts),
        }
        report["reconciliation"] = reconcile_ledger_with_oms(engine.ledger, engine.oms, day=self.day)
        report["warnings"] = _recovery_warnings(report)
        engine.ledger.record("recovery", report)
        return report


def recover_risk_state_from_ledger(ledger: Any, *, day: date | datetime | str | None = None) -> dict[str, Any]:
    """Build a ``RiskGate.restore`` payload from submitted order events."""
    events = ledger.events(kind="submit", day=day) if day is not None else ledger.events(kind="submit")
    refs: set[str] = set()
    value_today = 0.0
    orders_today = 0
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        req = payload.get("req") if isinstance(payload.get("req"), dict) else {}
        orders_today += 1
        value_today += float(req.get("volume") or 0.0) * float(req.get("price") or 0.0)
        ref = req.get("reference")
        if ref:
            refs.add(str(ref))
    return {
        "orders_today": orders_today,
        "value_today": value_today,
        "seen_refs": sorted(refs),
        "source": "ledger",
    }


def reconcile_ledger_with_oms(ledger: Any, oms: Any, *, day: date | datetime | str | None = None) -> dict[str, Any]:
    """Compare today's AlphaPilot decisions with the broker/OMS projection."""
    submit_events = ledger.events(kind="submit", day=day) if day is not None else ledger.events(kind="submit")
    ledger_order_ids = {
        str((event.get("payload") or {}).get("order_id"))
        for event in submit_events
        if (event.get("payload") or {}).get("order_id")
    }
    broker_order_ids = {str(order.order_id) for order in oms.orders.values()}

    ledger_trade_events = ledger.events(kind="trade", day=day) if day is not None else ledger.events(kind="trade")
    ledger_trade_ids = {
        str((event.get("payload") or {}).get("trade_id"))
        for event in ledger_trade_events
        if (event.get("payload") or {}).get("trade_id")
    }
    broker_trade_ids = {str(trade.trade_id) for trade in oms.trades.values()}

    missing_in_broker = sorted(ledger_order_ids - broker_order_ids)
    external_broker_orders = sorted(broker_order_ids - ledger_order_ids)
    missing_trades_in_broker = sorted(ledger_trade_ids - broker_trade_ids)
    external_broker_trades = sorted(broker_trade_ids - ledger_trade_ids)

    return {
        "ledger_orders": len(ledger_order_ids),
        "broker_orders": len(broker_order_ids),
        "matched_orders": len(ledger_order_ids & broker_order_ids),
        "missing_broker_order_ids": missing_in_broker,
        "external_broker_order_ids": external_broker_orders,
        "ledger_trades": len(ledger_trade_ids),
        "broker_trades": len(broker_trade_ids),
        "matched_trades": len(ledger_trade_ids & broker_trade_ids),
        "missing_broker_trade_ids": missing_trades_in_broker,
        "external_broker_trade_ids": external_broker_trades,
        "active_order_ids": [str(order.order_id) for order in oms.get_active_orders()],
    }


def _recovery_warnings(report: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if report.get("broker_refresh_ok") is False:
        warnings.append({"kind": "broker_refresh_failed", "detail": report.get("broker_refresh_error")})
    recon = report.get("reconciliation") or {}
    if recon.get("missing_broker_order_ids"):
        warnings.append({
            "kind": "ledger_orders_missing_in_broker",
            "order_ids": recon["missing_broker_order_ids"],
        })
    if recon.get("external_broker_order_ids"):
        warnings.append({
            "kind": "broker_orders_missing_in_ledger",
            "order_ids": recon["external_broker_order_ids"],
        })
    if recon.get("active_order_ids"):
        warnings.append({"kind": "active_orders_after_recovery", "order_ids": recon["active_order_ids"]})
    return warnings

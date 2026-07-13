"""Pre-trade risk gate — the single choke point every order passes before routing.

This is where the "dangerous situations" are contained. :meth:`RiskGate.check` runs
a fixed battery of rules and returns a structured :class:`RiskVerdict`; the engine
routes an order only on ``ok``. Rules (fail-fast, each names itself):

* **session** — reject outside legal submit windows (auction / continuous);
* **lot_size** — A-share board-lot integrality;
* **unknown_contract** / **price_tick** / **price_limit** — contract metadata
  and daily limit guards when available;
* **duplicate** — idempotency on the client reference (no double-submits);
* **max_orders_per_day** — runaway / throttle guard;
* **price_guard** — reject a limit price deviating too far from the reference
  (fat-finger / stale-quote protection);
* **max_order_value** — single-order notional cap;
* **insufficient_cash** / **insufficient_position** — buying power & T+1 sellable;
* **max_position_pct** — single-name concentration cap;
* **max_daily_value** — cumulative daily turnover cap.

The gate keeps small per-day mutable state (counts / turnover / seen references);
call :meth:`reset_day` at the session roll.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from alphapilot.systems.live.config import RiskLimits
from alphapilot.systems.live.config import RunMode
from alphapilot.systems.live.types import Direction, OrderRequest, OrderType, Product


@dataclass
class RiskVerdict:
    ok: bool
    rule: str = ""
    reason: str = ""

    @classmethod
    def passed(cls) -> "RiskVerdict":
        return cls(ok=True)

    @classmethod
    def reject(cls, rule: str, reason: str) -> "RiskVerdict":
        return cls(ok=False, rule=rule, reason=reason)


class RiskGate:
    """Stateful pre-trade risk checker."""

    def __init__(self, limits: RiskLimits, *, enforce_session: bool = True) -> None:
        self.limits = limits
        self.enforce_session = enforce_session
        self._orders_today = 0
        self._value_today = 0.0
        self._seen_refs: set[str] = set()

    def reset_day(self) -> None:
        self._orders_today = 0
        self._value_today = 0.0
        self._seen_refs.clear()

    def snapshot(self) -> dict[str, Any]:
        """Return the mutable risk state for runtime status/recovery."""
        return {
            "limits": asdict(self.limits),
            "enforce_session": self.enforce_session,
            "orders_today": int(self._orders_today),
            "value_today": float(self._value_today),
            "seen_refs": sorted(self._seen_refs),
        }

    def restore(self, state: dict[str, Any] | None) -> None:
        """Restore mutable counters from a trusted recovery snapshot."""
        if not state:
            return
        self._orders_today = int(state.get("orders_today") or 0)
        self._value_today = float(state.get("value_today") or 0.0)
        refs = state.get("seen_refs") or []
        self._seen_refs = {str(ref) for ref in refs if str(ref)}

    # ------------------------------------------------------------------ #
    def check(self, req: OrderRequest, oms, session, runmode) -> RiskVerdict:
        lim = self.limits

        if self.enforce_session and session is not None and not session.can_submit():
            return RiskVerdict.reject("session", f"submission not allowed in {session.state.value}")

        if req.volume <= 0:
            return RiskVerdict.reject("volume", "order volume must be positive")

        contract = oms.get_contract(req.key) if hasattr(oms, "get_contract") else None
        live_mode = getattr(runmode, "mode", None) == RunMode.LIVE
        if (
            live_mode
            and getattr(contract, "product", None) == Product.FUTURES
        ):
            return RiskVerdict.reject("unsupported_product", "futures live routing is not enabled")
        if live_mode and contract is None:
            return RiskVerdict.reject("unknown_contract", f"{req.key} contract metadata is required in LIVE mode")
        if live_mode:
            tick = oms.get_tick(req.key) if hasattr(oms, "get_tick") else None
            if tick is None or float(getattr(tick, "last_price", 0.0) or 0.0) <= 0:
                return RiskVerdict.reject("missing_quote", f"{req.key} fresh quote is required in LIVE mode")
            timestamp = getattr(tick, "received_at", None) or getattr(tick, "datetime", None)
            if timestamp is None:
                return RiskVerdict.reject("stale_quote", f"{req.key} quote timestamp is required")
            now = session._now_fn() if session is not None and hasattr(session, "_now_fn") else datetime.now(timestamp.tzinfo or timezone.utc)
            if timestamp.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=timestamp.tzinfo)
            elif timestamp.tzinfo is None and now.tzinfo is not None:
                timestamp = timestamp.replace(tzinfo=now.tzinfo)
            age = max((now - timestamp).total_seconds(), 0.0)
            if lim.max_quote_age_seconds > 0 and age > lim.max_quote_age_seconds:
                return RiskVerdict.reject(
                    "stale_quote", f"{req.key} quote age {age:.1f}s > {lim.max_quote_age_seconds:.1f}s"
                )

        lot_size = int(getattr(contract, "lot_size", 0) or lim.lot_size)
        if lot_size > 0 and abs(req.volume) % lot_size != 0:
            return RiskVerdict.reject("lot_size", f"volume {req.volume} is not a multiple of {lot_size}")

        if req.reference and req.reference in self._seen_refs:
            return RiskVerdict.reject("duplicate", f"duplicate client reference {req.reference}")

        if lim.max_orders_per_day > 0 and self._orders_today + 1 > lim.max_orders_per_day:
            return RiskVerdict.reject("max_orders_per_day", f"daily order cap {lim.max_orders_per_day} reached")

        ref = self._ref_price(oms, req)
        if req.type == OrderType.LIMIT and req.price > 0:
            tick_verdict = self._check_contract_price(req, oms, contract)
            if not tick_verdict.ok:
                return tick_verdict
        if req.type == OrderType.LIMIT and req.price > 0 and ref > 0 and lim.price_guard_pct > 0:
            if abs(req.price - ref) / ref > lim.price_guard_pct:
                return RiskVerdict.reject(
                    "price_guard",
                    f"limit {req.price} deviates > {lim.price_guard_pct:.1%} from ref {ref}",
                )

        notional = req.volume * (req.price if req.price > 0 else ref)

        if lim.max_order_value > 0 and notional > lim.max_order_value:
            return RiskVerdict.reject("max_order_value", f"notional {notional:.0f} > cap {lim.max_order_value:.0f}")

        if req.direction == Direction.LONG:                 # buy
            if notional > oms.buying_power() + 1e-6:
                return RiskVerdict.reject(
                    "insufficient_cash", f"notional {notional:.0f} > buying power {oms.buying_power():.0f}"
                )
            verdict = self._check_concentration(req, oms, ref, notional)
            if not verdict.ok:
                return verdict
        else:                                               # sell
            available = oms.available_shares(req.key)
            if req.volume > available + 1e-6:
                return RiskVerdict.reject(
                    "insufficient_position",
                    f"sell {req.volume} > sellable {available} (T+1 / frozen)",
                )

        if lim.max_daily_value > 0 and self._value_today + notional > lim.max_daily_value:
            return RiskVerdict.reject(
                "max_daily_value",
                f"daily turnover {self._value_today + notional:.0f} > cap {lim.max_daily_value:.0f}",
            )

        # accepted — record for the daily counters / idempotency
        self._orders_today += 1
        self._value_today += notional
        if req.reference:
            self._seen_refs.add(req.reference)
        return RiskVerdict.passed()

    @staticmethod
    def _check_contract_price(req: OrderRequest, oms, contract) -> RiskVerdict:
        if contract is not None:
            tick_size = float(getattr(contract, "price_tick", 0.0) or 0.0)
            if tick_size > 0 and not _is_multiple(req.price, tick_size):
                return RiskVerdict.reject("price_tick", f"limit {req.price} is not aligned to tick {tick_size}")
        tick = oms.get_tick(req.key) if hasattr(oms, "get_tick") else None
        if tick is not None:
            limit_up = float(getattr(tick, "limit_up", 0.0) or 0.0)
            limit_down = float(getattr(tick, "limit_down", 0.0) or 0.0)
            if limit_up > 0 and req.price > limit_up + 1e-9:
                return RiskVerdict.reject("price_limit", f"limit {req.price} > limit_up {limit_up}")
            if limit_down > 0 and req.price < limit_down - 1e-9:
                return RiskVerdict.reject("price_limit", f"limit {req.price} < limit_down {limit_down}")
        return RiskVerdict.passed()

    # ------------------------------------------------------------------ #
    def _check_concentration(self, req: OrderRequest, oms, ref: float, notional: float) -> RiskVerdict:
        lim = self.limits
        if lim.max_position_pct <= 0 or ref <= 0:
            return RiskVerdict.passed()
        equity = self._equity(oms)
        if equity <= 0:
            return RiskVerdict.passed()
        pos = oms.get_position(req.key)
        current = pos.volume if pos else 0.0
        resulting_value = (current + req.volume) * ref
        if resulting_value / equity > lim.max_position_pct:
            return RiskVerdict.reject(
                "max_position_pct",
                f"post-trade {req.key} weight {resulting_value / equity:.1%} > cap {lim.max_position_pct:.1%}",
            )
        return RiskVerdict.passed()

    @staticmethod
    def _ref_price(oms, req: OrderRequest) -> float:
        tick = oms.get_tick(req.key)
        if tick is not None and tick.last_price > 0:
            return float(tick.last_price)
        return float(req.price)

    @staticmethod
    def _equity(oms) -> float:
        if oms.account is not None and oms.account.balance > 0:
            return float(oms.account.balance)
        return float(oms.buying_power())


def _is_multiple(value: float, step: float, *, tolerance: float = 1e-9) -> bool:
    if step <= 0:
        return True
    units = round(float(value) / float(step))
    return abs(float(value) - units * float(step)) <= tolerance

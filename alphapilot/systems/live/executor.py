"""Executor — turn a target book / order intents into concrete OrderRequests.

Reconciliation diffs the :class:`TargetPortfolio` against the **real** positions in
the OMS (not a simulated roll), respecting board lots and — for sells — the T+1
sellable quantity. The resulting requests are handed to the engine's guarded
:meth:`~alphapilot.systems.live.engine.LiveEngine.submit` (which applies risk), or
to an execution algo for timed placement (call auction / TWAP).
"""

from __future__ import annotations

import math
import hashlib
from typing import Any, Iterable

from alphapilot.systems.live.targets import TargetPortfolio
from alphapilot.systems.live.types import OrderRequest, normalize_symbol, symbol_key


def _floor_lot(volume: float, lot: int) -> float:
    if lot and lot > 0:
        return float(math.floor(volume / lot) * lot)
    return float(math.floor(volume)) if volume > 0 else 0.0


def reconcile(
    target: TargetPortfolio,
    oms,
    *,
    lot_size: int = 100,
    max_order_value: float = 0.0,
) -> list[OrderRequest]:
    """Diff target shares vs real OMS positions -> buy/sell requests (lot-rounded).

    Sells are capped at the sellable (T+1, non-frozen) quantity from the OMS.
    Instruments held but absent from the target are fully liquidated (target 0).
    """
    from alphapilot.systems.live.planner import ExecutionPlanner

    return ExecutionPlanner(
        lot_size=lot_size,
        max_order_value=max_order_value,
    ).plan(target, oms).requests


def orders_from_intents(
    intents: Iterable[Any],
    oms,
    prices: dict[str, float],
    *,
    lot_size: int = 100,
    instance_id: str = "legacy",
    config_hash: str = "",
    decision_id: str = "",
) -> list[OrderRequest]:
    """Translate timing :class:`OrderIntent` objects into concrete requests.

    Supports ``buy`` / ``sell`` / ``close`` (share quantities) and the
    ``target_percent`` / ``target_shares`` rebalancing actions (sized against the
    OMS equity / current holding).
    """
    reqs: list[OrderRequest] = []
    for index, intent in enumerate(intents):
        code, exchange = normalize_symbol(intent.instrument)
        key = symbol_key(code, exchange)
        price = float(prices.get(intent.instrument) or prices.get(key) or 0.0)
        pos = oms.get_position(key)
        current = pos.volume if pos else 0.0
        for active in oms.get_active_orders():
            if active.key != key:
                continue
            current += active.remaining if active.direction.value == "long" else -active.remaining
        action = intent.action
        ref = _intent_reference(
            intent, index=index, key=key, instance_id=instance_id,
            config_hash=config_hash, decision_id=decision_id,
        )

        if action == "buy" and intent.quantity:
            vol = _floor_lot(intent.quantity, lot_size)
            if vol > 0:
                reqs.append(OrderRequest.buy(code, exchange, vol, price, reference=ref))
        elif action == "sell" and intent.quantity:
            vol = _floor_lot(min(intent.quantity, oms.available_shares(key)), lot_size)
            if vol > 0:
                reqs.append(OrderRequest.sell(code, exchange, vol, price, reference=ref))
        elif action == "close":
            vol = _floor_lot(oms.available_shares(key), lot_size)
            if vol > 0:
                reqs.append(OrderRequest.sell(code, exchange, vol, price, reference=ref))
        elif action in ("target_percent", "target_shares"):
            target_shares = _target_shares(intent, oms, price, lot_size)
            delta = target_shares - current
            if delta >= lot_size:
                reqs.append(OrderRequest.buy(code, exchange, _floor_lot(delta, lot_size), price, reference=ref))
            elif -delta >= lot_size:
                vol = _floor_lot(min(-delta, oms.available_shares(key)), lot_size)
                if vol > 0:
                    reqs.append(OrderRequest.sell(code, exchange, vol, price, reference=ref))
    return reqs


def _target_shares(intent: Any, oms, price: float, lot_size: int) -> float:
    if intent.action == "target_shares" and intent.quantity is not None:
        return _floor_lot(intent.quantity, lot_size)
    pct = intent.target_percent or 0.0
    equity = oms.account.balance if (oms.account and oms.account.balance > 0) else oms.buying_power()
    if price <= 0 or equity <= 0:
        return 0.0
    return _floor_lot((pct * equity) / price, lot_size)


def _intent_reference(
    intent: Any,
    *,
    index: int,
    key: str,
    instance_id: str,
    config_hash: str,
    decision_id: str,
) -> str:
    decision = decision_id or hashlib.sha256(
        f"{getattr(intent, 'datetime', '')}:{key}:{getattr(intent, 'action', '')}".encode("utf-8")
    ).hexdigest()[:24]
    action = getattr(intent, "action", "")
    target = getattr(intent, "target_percent", None) if action == "target_percent" else getattr(intent, "quantity", None)
    side = "B" if action == "buy" or (action in {"target_percent", "target_shares"} and float(target or 0) > 0) else "S"
    return f"{instance_id}:{config_hash or '-'}:{decision}:{key}:{side}:{index}"

"""Concrete live adapters for the broker-independent execution coordinator."""

from __future__ import annotations

from typing import Any, Sequence

from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    ExecutionChild,
    TradableQuote,
)
from alphapilot.systems.live.types import Exchange, OrderRequest


class LiveAccountSnapshotAdapter:
    def __init__(self, runtime: Any, *, instance_id: str, config_hash: str) -> None:
        self.runtime = runtime
        self.reference_prefix = f"{instance_id}:{config_hash or '-'}:"

    def account_snapshot(self) -> AccountSnapshot:
        now = self.runtime.engine.session._now_fn()
        snapshot = AccountSnapshot.from_oms(
            self.runtime.engine.oms,
            as_of=now.isoformat(),
        )
        external = tuple(
            str(order.reference or order.order_id)
            for order in self.runtime.engine.oms.get_active_orders()
            if not str(order.reference or "").startswith(self.reference_prefix)
        )
        return AccountSnapshot(
            **{**snapshot.__dict__, "external_orders": external},
        )

    def quotes(self, instruments: Sequence[str]) -> dict[str, TradableQuote]:
        result: dict[str, TradableQuote] = {}
        for instrument in instruments:
            tick = self.runtime.engine.oms.get_tick(instrument)
            if tick is None:
                continue
            timestamp = tick.received_at or tick.datetime
            now = self.runtime.engine.session._now_fn()
            age = None
            if timestamp is not None:
                probe = timestamp
                if probe.tzinfo is None and getattr(now, "tzinfo", None) is not None:
                    probe = probe.replace(tzinfo=now.tzinfo)
                age = max((now - probe).total_seconds(), 0.0)
            stale_after = float(
                getattr(self.runtime.engine.config.market_data, "stale_after_seconds", 0.0) or 0.0
            )
            result[instrument] = TradableQuote(
                instrument=instrument,
                as_of="" if timestamp is None else timestamp.isoformat(),
                last=float(tick.last_price or 0.0),
                open=float(getattr(tick, "open_price", 0.0) or 0.0),
                bid=float(getattr(tick, "bid_price_1", 0.0) or 0.0),
                ask=float(getattr(tick, "ask_price_1", 0.0) or 0.0),
                limit_up=float(getattr(tick, "limit_up", 0.0) or 0.0),
                limit_down=float(getattr(tick, "limit_down", 0.0) or 0.0),
                suspended=bool(getattr(tick, "suspended", False)),
                stale=timestamp is None or (stale_after > 0 and age is not None and age > stale_after),
                price_source="raw",
            )
        return result


class LiveExecutionRouteAdapter:
    """Convert trusted execution children to the runtime's authorized route port."""

    def __init__(self, runtime: Any, automated_router: Any) -> None:
        self.runtime = runtime
        self.automated_router = automated_router

    def submit_child(self, child: ExecutionChild) -> str | None:
        code, exchange = _split_instrument(child.instrument)
        factory = OrderRequest.buy if child.side == "buy" else OrderRequest.sell
        request = factory(
            code,
            exchange,
            float(child.volume),
            float(child.price),
            reference=child.reference,
        )
        return self.automated_router.submit(request)

    def child_statuses(self, references: Sequence[str]) -> dict[str, str]:
        wanted = set(references)
        return {
            str(order.reference): str(getattr(order.status, "value", order.status)).lower()
            for order in self.runtime.engine.oms.orders.values()
            if str(order.reference) in wanted
        }

    def child_fills(self, references: Sequence[str]) -> list[dict[str, Any]]:
        wanted = set(references)
        orders = {
            str(order.order_id): order
            for order in self.runtime.engine.oms.orders.values()
            if str(order.reference) in wanted
        }
        result: list[dict[str, Any]] = []
        for trade in self.runtime.engine.oms.get_trades():
            order = orders.get(str(trade.order_id))
            if order is None:
                continue
            result.append({
                "fill_key": str(trade.trade_id),
                "reference": str(order.reference),
                "order_id": str(trade.order_id),
                "volume": float(trade.volume),
                "price": float(trade.price),
                "payload": {
                    "trade_id": str(trade.trade_id),
                    "instrument": trade.key,
                    "direction": str(getattr(trade.direction, "value", trade.direction)),
                },
            })
        return result

    def cancel_child(self, reference: str) -> bool:
        order = next(
            (
                item for item in self.runtime.engine.oms.get_active_orders()
                if str(item.reference) == reference
            ),
            None,
        )
        if order is None:
            return True
        result = self.runtime.cancel_order(str(order.order_id))
        return bool(result.get("cancelled"))


def _split_instrument(instrument: str) -> tuple[str, Exchange]:
    code, _, suffix = str(instrument).partition(".")
    aliases = {"SSE": Exchange.SSE, "SZSE": Exchange.SZSE, "BSE": Exchange.BSE}
    if suffix not in aliases:
        raise ValueError(f"unsupported live exchange for {instrument}")
    return code, aliases[suffix]

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from alphapilot.systems.live.brokers.paper import PaperBroker
from alphapilot.systems.live.clock import SimulatedClock
from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.engine import LiveEngine
from alphapilot.systems.live.events import LiveEvent, LiveEventBus
from alphapilot.systems.live.ledger import Ledger
from alphapilot.systems.live.types import Exchange, OrderRequest, TickData


KEY = "600000.SSE"


def test_live_event_bus_filters_and_isolates_handler_errors() -> None:
    bus = LiveEventBus()
    all_seen: list[str] = []
    order_seen: list[str] = []

    bus.subscribe(None, lambda event: all_seen.append(event.kind))
    bus.subscribe("order", lambda event: order_seen.append(event.order_id or ""))
    bus.subscribe("order", lambda event: (_ for _ in ()).throw(RuntimeError("observer boom")))

    event = bus.publish("order", {"x": 1}, order_id="ord-1", source="test")
    bus.publish("trade", {"x": 2}, order_id="ord-1", source="test")

    assert isinstance(event, LiveEvent)
    assert all_seen == ["order", "trade"]
    assert order_seen == ["ord-1"]
    assert len(bus.errors) == 1
    assert bus.errors[0]["kind"] == "order"
    assert "observer boom" in bus.errors[0]["error"]


def test_engine_publishes_live_events_and_structured_audit(tmp_path: Path) -> None:
    bus = LiveEventBus()
    seen: list[LiveEvent] = []
    bus.subscribe(None, seen.append)

    clock = SimulatedClock(datetime(2026, 7, 6, 10, 0))
    cfg = LiveConfig(mode=RunMode.PAPER, ledger_dir=tmp_path / "ledger")
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0}, open_cost=0.0, min_cost=0.0)
    engine = LiveEngine(
        cfg,
        broker,
        ledger=Ledger(tmp_path / "ledger", now_fn=clock),
        event_bus=bus,
        now_fn=clock,
    )

    engine.connect({})
    order_id = engine.submit(OrderRequest.buy("600000", Exchange.SSE, 100, 10.0, reference="bus-ref"))
    engine.on_tick(TickData(code="600000", exchange=Exchange.SSE, last_price=10.1, gateway="paper"))

    kinds = [event.kind for event in seen]
    assert {"log", "account", "connected", "order", "trade", "position", "submit", "tick"} <= set(kinds)
    assert [event.order_id for event in seen if event.kind == "submit"] == [order_id]
    assert [event.reference for event in seen if event.kind == "submit"] == ["bus-ref"]
    assert [event.source for event in seen if event.kind == "order"][-1] == "paper"

    submit = engine.ledger.events(kind="submit")[-1]
    assert submit["order_id"] == order_id
    assert submit["reference"] == "bus-ref"
    assert submit["payload"]["req"]["reference"] == "bus-ref"

    # High-volume state events are observable in memory but not persisted by default.
    assert engine.ledger.events(kind="tick") == []


def test_gateway_disconnect_events_are_audited_and_trade_disconnect_halts(tmp_path: Path) -> None:
    clock = SimulatedClock(datetime(2026, 7, 6, 10, 0))
    cfg = LiveConfig(mode=RunMode.PAPER, ledger_dir=tmp_path / "ledger")
    broker = PaperBroker(cash=100_000.0, prices={KEY: 10.0}, open_cost=0.0, min_cost=0.0)
    engine = LiveEngine(
        cfg,
        broker,
        ledger=Ledger(tmp_path / "ledger", now_fn=clock),
        now_fn=clock,
    )
    engine.connect({})

    engine.on_gateway_disconnected("xtp", "quote", "7", halt=False)

    assert engine.runmode.halted is False
    quote_event = engine.ledger.events(kind="gateway_disconnected")[-1]
    assert quote_event["payload"] == {
        "gateway": "xtp",
        "channel": "quote",
        "reason": "7",
        "halt": False,
    }
    assert engine.ledger.events(kind="disconnected") == []

    engine.on_gateway_disconnected("xtp", "trade", "8", halt=True)

    assert engine.runmode.halted is True
    assert engine.ledger.events(kind="disconnected")[-1]["payload"]["reason"] == "trade:8"
    assert engine.ledger.events(kind="gateway_disconnected")[-1]["payload"]["halt"] is True

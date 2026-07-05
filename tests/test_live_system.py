"""Phase 0: LiveSystem is registered on the engine + timing back-compat holds."""

from __future__ import annotations

import pytest


def test_live_system_registered_and_snapshot(engine) -> None:
    live = engine.get_system("live")
    assert live.name == "live"
    snap = live.snapshot()
    assert snap["broker"] == "paper"
    assert snap["mode"] in set(live.modes())
    assert snap["risk"]["lot_size"] == 100


def test_live_module_status_and_modes(engine) -> None:
    live = engine.get_module("live")
    snap = live.live_status()
    assert snap["broker"] == "paper"
    assert "dry_run" in live.live_modes()


def test_live_system_create_engine_paper(engine) -> None:
    live = engine.get_system("live")
    live_engine = live.create_engine()
    # default mode is dry_run -> paper broker, risk gate attached
    assert live_engine.gateway.name == "paper"
    assert live_engine.risk is not None
    assert live_engine.runmode.mode == "dry_run"
    assert live_engine.snapshot()["mode"] == "dry_run"


def test_timing_still_imports_order_status_after_refactor() -> None:
    # timing/__init__ re-exports OrderStatus (now sourced from systems/live).
    from alphapilot.systems.timing import OrderStatus

    assert OrderStatus.SUBMITTED is OrderStatus.SUBMITTING
    assert OrderStatus.CANCELLED.value == "cancelled"


def test_timing_legacy_broker_stack_removed() -> None:
    # The old request/reply stack (timing.broker.PaperBroker, ExecutionReport,
    # the second BrokerGateway protocol) was superseded by the live stack;
    # OrderIntent stays as the strategy-side contract.
    import alphapilot.systems.timing as timing

    assert not hasattr(timing, "ExecutionReport")
    assert not hasattr(timing, "BrokerGateway")
    from alphapilot.systems.timing.base import OrderIntent  # noqa: F401 - still exported

    with pytest.raises(ImportError):
        from alphapilot.systems.timing.broker import PaperBroker  # noqa: F401

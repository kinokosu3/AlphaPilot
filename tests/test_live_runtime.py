from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from alphapilot.systems.live.brokers.paper import PaperBroker
from alphapilot.systems.live.clock import SimulatedClock
from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.runtime import LiveRuntime, require_live_confirmation
from alphapilot.systems.live.targets import TargetPortfolio


def _runtime(tmp_path: Path) -> LiveRuntime:
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
    )
    broker = PaperBroker(cash=100_000.0, prices={"600000.SSE": 10.0}, open_cost=0.0, min_cost=0.0)
    clock = SimulatedClock(datetime(2026, 7, 1, 10, 0))
    return LiveRuntime.create(cfg, broker=broker, now_fn=clock)


def test_runtime_connect_plan_route_and_persist(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.connect(paper_cash=100_000.0)
    assert runtime.wait_ready(timeout=1, require_contracts=False)

    target = TargetPortfolio(
        date="2026-07-01",
        holdings={"SH600000": 1000},
        prices={"SH600000": 10.0},
        source="test",
    )
    planned = runtime.submit_target(target, route=False)
    assert planned["planned"] == 1
    assert planned["routed"] == []
    assert planned["fully_routed"] is True
    assert runtime.engine.oms.get_position("600000.SSE") is None

    routed = runtime.submit_target(target, route=True)
    assert routed["planned"] == 1
    assert len(routed["routed"]) == 1
    assert routed["submitted"] == 1
    assert routed["unrouted"] == 0
    assert routed["fully_routed"] is True
    assert runtime.engine.oms.get_position("600000.SSE").volume == 1000
    assert runtime.state_path.exists()

    runtime.close()


def test_runtime_live_requires_explicit_confirmation() -> None:
    cfg = LiveConfig(mode=RunMode.LIVE, broker="xtp")
    with pytest.raises(ValueError, match="confirm_live"):
        require_live_confirmation(cfg, confirm_live=False)
    require_live_confirmation(cfg, confirm_live=True)

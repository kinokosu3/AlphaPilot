"""Phase 0: LiveSystem is registered on the engine + timing back-compat holds."""

from __future__ import annotations

import json
import time

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
    commands = live.commands()
    assert {"live_connect", "live_state", "live_order", "live_submit_target"} <= set(commands)


def test_live_module_brokers_expose_capabilities(engine) -> None:
    live = engine.get_module("live")
    brokers = {row["name"]: row for row in live.live_brokers()}
    assert "xtp" in brokers
    assert brokers["xtp"]["capabilities"]["supports_tick"] is True
    assert "ALPHAPILOT_LIVE_XTP_ACCOUNT" in brokers["xtp"]["env_fields"]


def test_live_module_preflight_and_run_loop(engine, tmp_path) -> None:
    live = engine.get_module("live")
    paper_preflight = live.live_preflight(network=False)
    assert paper_preflight["broker"] == "paper"
    assert paper_preflight["ok"] is True

    preflight = live.live_preflight(broker="xtp", network=False)
    assert preflight["broker"] == "xtp"
    assert "gateway_importable" in preflight
    assert preflight["network_checked"] is False

    run = live.live_run(
        mode="paper",
        cash=50_000.0,
        interval=0.05,
        duration=0.05,
        timeout=1.0,
        ledger_dir=str(tmp_path / "ledger"),
        state_dir=str(tmp_path / "state"),
    )
    assert run["ready"] is True
    assert run["iterations"] >= 1
    assert run["state"]["account"]["available"] == 50_000.0


def test_live_module_daemon_start_status_stop(engine, tmp_path) -> None:
    live = engine.get_module("live")
    state_dir = tmp_path / "daemon_state"
    ledger_dir = tmp_path / "daemon_ledger"

    initial = live.live_daemon_status(mode="paper", state_dir=str(state_dir))
    assert initial["running"] is False

    started = live.live_daemon_start(
        mode="paper",
        symbols="600000",
        cash=10_000.0,
        interval=0.05,
        timeout=1.0,
        state_dir=str(state_dir),
        ledger_dir=str(ledger_dir),
        timing_strategy="sma_filter",
        timing_params=json.dumps({"window": 2, "target_percent": 0.5}),
        timing_freq="min",
        min_bars=2,
    )
    assert started["started"] is True
    try:
        deadline = time.time() + 5.0
        status = live.live_daemon_status(mode="paper", state_dir=str(state_dir))
        while time.time() < deadline and not (status.get("running") and status.get("runner_status") is not None):
            time.sleep(0.1)
            status = live.live_daemon_status(mode="paper", state_dir=str(state_dir))
        assert status["running"] is True, status
        assert status["pid"] == started["pid"]
        assert "runtime_state.json" in status["state_path"]
        assert status["runner"]["enabled"] is True
        assert status["runner"]["strategy"] == "sma_filter"
        assert status.get("runner_status") is not None

        halted = live.live_daemon_halt(reason="test", state_dir=str(state_dir), wait=True, timeout=5.0)
        assert halted["accepted"] is True
        assert halted["daemon"]["last_command"]["ok"] is True
        status = live.live_daemon_status(mode="paper", state_dir=str(state_dir))
        assert status["state"]["engine"]["halted"] is True

        resumed = live.live_daemon_resume(state_dir=str(state_dir), wait=True, timeout=5.0)
        assert resumed["accepted"] is True
        assert resumed["daemon"]["last_command"]["ok"] is True
        status = live.live_daemon_status(mode="paper", state_dir=str(state_dir))
        assert status["state"]["engine"]["halted"] is False

        order = live.live_daemon_order(
            "SH600000",
            "buy",
            100,
            price=10.0,
            state_dir=str(state_dir),
            wait=True,
            timeout=5.0,
            reference="test-daemon-order",
        )
        assert order["accepted"] is True
        assert order["daemon"]["last_command"]["ok"] is True
        assert order["daemon"]["last_command"]["order_id"]
        status = live.live_daemon_status(mode="paper", state_dir=str(state_dir))
        positions = {row["code"]: row for row in status["state"]["positions"]}
        assert positions["600000"]["volume"] == 100

        target = live.live_daemon_submit_target(
            holdings=json.dumps({"SH600000": 200}),
            prices=json.dumps({"SH600000": 10.0}),
            route=True,
            state_dir=str(state_dir),
            wait=True,
            timeout=5.0,
        )
        assert target["accepted"] is True
        assert target["daemon"]["last_command"]["ok"] is True
        assert target["daemon"]["last_command"]["planned"] == 1
        assert len(target["daemon"]["last_command"]["routed"]) == 1
        status = live.live_daemon_status(mode="paper", state_dir=str(state_dir))
        positions = {row["code"]: row for row in status["state"]["positions"]}
        assert positions["600000"]["volume"] == 200

        refreshed = live.live_daemon_refresh(state_dir=str(state_dir), wait=True, timeout=5.0)
        assert refreshed["accepted"] is True
        assert refreshed["daemon"]["last_command"]["action"] == "refresh"
    finally:
        stopped = live.live_daemon_stop(state_dir=str(state_dir), timeout=5.0)
    assert stopped["running"] is False


def test_live_module_submit_inline_target_plans_only(engine) -> None:
    live = engine.get_module("live")
    result = live.live_submit_target(
        holdings=json.dumps({"SH600000": 1000}),
        prices=json.dumps({"SH600000": 10.0}),
        mode="dry_run",
        cash=100_000.0,
        route=False,
        timeout=1.0,
    )
    assert result["planned"] == 1
    assert result["routed"] == []
    assert result["requests"][0]["code"] == "600000"


def test_live_system_create_engine_paper(engine) -> None:
    live = engine.get_system("live")
    live_engine = live.create_engine()
    # default mode is dry_run -> paper broker, risk gate attached
    assert live_engine.gateway.name == "paper"
    assert live_engine.risk is not None
    assert live_engine.runmode.mode == "dry_run"
    assert live_engine.snapshot()["mode"] == "dry_run"
    assert live_engine.snapshot()["contracts"] == 0
    assert live_engine.snapshot()["ticks"] == 0


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

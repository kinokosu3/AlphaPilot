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
    assert brokers["xtp"]["capabilities"]["supports_order_query"] is False
    assert brokers["xtp"]["capabilities"]["supports_trade_query"] is True
    assert brokers["emt"]["capabilities"]["supports_order_query"] is True
    assert brokers["emt"]["capabilities"]["supports_trade_query"] is True
    assert "ALPHAPILOT_LIVE_XTP_ACCOUNT" in brokers["xtp"]["env_fields"]


def test_live_module_risk_status_and_ledger_events(engine, tmp_path) -> None:
    live = engine.get_module("live")
    state_dir = tmp_path / "state"
    ledger_dir = tmp_path / "ledger"
    order = live.live_order(
        "SH600000",
        "buy",
        100,
        price=10.0,
        mode="paper",
        cash=10_000.0,
        timeout=1.0,
        ledger_dir=str(ledger_dir),
        state_dir=str(state_dir),
        reference="risk-cli",
    )
    assert order["submitted"] is True

    risk = live.live_risk_status(mode="paper", ledger_dir=str(ledger_dir), state_dir=str(state_dir))
    assert risk["exists"] is True
    assert risk["risk"]["orders_today"] == 1
    assert risk["recovery"]["risk_restored"] is True

    events = live.live_ledger_events(
        kind="submit",
        reference="risk-cli",
        ledger_dir=str(ledger_dir),
        state_dir=str(state_dir),
    )
    assert events["count"] == 1
    assert events["events"][0]["payload"]["req"]["reference"] == "risk-cli"


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


def test_live_daemon_start_rejects_uninstalled_provider(engine, tmp_path) -> None:
    live = engine.get_module("live")
    with pytest.raises(ValueError, match="unknown trade broker"):
        live.live_daemon_start(
            mode="live",
            broker="not-installed",
            quote_provider="paper",
            state_dir=str(tmp_path / "state"),
            ledger_dir=str(tmp_path / "ledger"),
        )


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
        assert status["runner_status"]["active"] is True

        strategy_status = live.live_daemon_strategy_status(state_dir=str(state_dir), wait=True, timeout=5.0)
        assert strategy_status["accepted"] is True
        assert strategy_status["daemon"]["last_command"]["ok"] is True
        assert strategy_status["daemon"]["last_command"]["runner_status"]["active"] is True

        paused = live.live_daemon_strategy_pause(state_dir=str(state_dir), wait=True, timeout=5.0)
        assert paused["daemon"]["last_command"]["runner_status"]["paused"] is True
        assert paused["daemon"]["last_command"]["runner_status"]["active"] is False

        resumed_strategy = live.live_daemon_strategy_resume(state_dir=str(state_dir), wait=True, timeout=5.0)
        assert resumed_strategy["daemon"]["last_command"]["runner_status"]["active"] is True

        stopped_strategy = live.live_daemon_strategy_stop(state_dir=str(state_dir), wait=True, timeout=5.0)
        assert stopped_strategy["daemon"]["last_command"]["runner_status"]["stopped"] is True
        status = live.live_daemon_status(mode="paper", state_dir=str(state_dir))
        assert status["runner"]["enabled"] is False

        restarted_strategy = live.live_daemon_strategy_start(
            "sma_filter",
            symbols="600000",
            timing_params=json.dumps({"window": 2, "target_percent": 0.5}),
            timing_freq="min",
            min_bars=2,
            state_dir=str(state_dir),
            wait=True,
            timeout=5.0,
        )
        assert restarted_strategy["accepted"] is True
        assert restarted_strategy["daemon"]["last_command"]["runner_status"]["active"] is True

        halted = live.live_daemon_halt(reason="test", state_dir=str(state_dir), wait=True, timeout=5.0)
        assert halted["accepted"] is True
        assert halted["daemon"]["last_command"]["ok"] is True
        assert halted["daemon"]["command_status_tail"][-1]["stage"] == "done"
        assert halted["daemon"]["command_status_tail"][-1]["id"] == halted["command"]["id"]
        status = live.live_daemon_status(mode="paper", state_dir=str(state_dir))
        assert status["state"]["engine"]["halted"] is True
        assert status["state"]["recovery"]["risk_restored"] is True

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
        assert order["daemon"]["last_command"]["order_acknowledged"] is True
        assert order["daemon"]["last_command"]["order_ack"]["found"] is True

        cancel = live.live_daemon_cancel(
            order["daemon"]["last_command"]["order_id"],
            state_dir=str(state_dir),
            wait=True,
            timeout=5.0,
        )
        assert cancel["accepted"] is True
        assert cancel["daemon"]["last_command"]["action"] == "cancel"
        assert cancel["daemon"]["last_command"]["ok"] is False
        assert cancel["daemon"]["last_command"]["cancel"]["reason"] == "not_active"
        assert cancel["daemon"]["last_command"]["cancel_terminal"] is True

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

        reconnected = live.live_daemon_reconnect(state_dir=str(state_dir), wait=True, timeout=5.0)
        assert reconnected["accepted"] is True
        assert reconnected["daemon"]["last_command"]["action"] == "reconnect"
        assert reconnected["daemon"]["last_command"]["ok"] is True
        assert reconnected["daemon"]["state"]["engine"]["halted"] is True
    finally:
        stopped = live.live_daemon_stop(state_dir=str(state_dir), timeout=5.0)
    assert stopped["running"] is False


def test_stop_daemon_waits_until_process_is_no_longer_alive(tmp_path, monkeypatch) -> None:
    from alphapilot.systems.live import daemon as daemon_module
    from alphapilot.systems.live.config import LiveConfig, RunMode

    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        state_dir=tmp_path / "state",
        ledger_dir=tmp_path / "ledger",
    )
    statuses = [
        {"pid": 12345, "status": "running", "alive": True, "running": True},
        {"pid": 12345, "status": "stopped", "alive": True, "running": False},
        {"pid": 12345, "status": "stopped", "alive": False, "running": False},
    ]
    calls = 0

    def fake_load_daemon(_state_dir):
        nonlocal calls
        item = statuses[min(calls, len(statuses) - 1)]
        calls += 1
        return dict(item)

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(daemon_module, "load_daemon", fake_load_daemon)
    monkeypatch.setattr(daemon_module.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(daemon_module.time, "sleep", lambda _seconds: None)

    stopped = daemon_module.stop_daemon(cfg, timeout=1.0)

    assert calls == 3
    assert signals == [(12345, daemon_module.signal.SIGTERM)]
    assert stopped["stopped"] is True
    assert stopped["alive"] is False
    assert stopped["running"] is False


def test_daemon_wait_uses_command_status_log_when_last_command_is_missing(tmp_path) -> None:
    from alphapilot.systems.live.config import LiveConfig, RunMode
    from alphapilot.systems.live.daemon import record_command_status, wait_for_command, write_daemon

    state_dir = tmp_path / "daemon_wait"
    cfg = LiveConfig(mode=RunMode.PAPER, broker="paper", state_dir=state_dir, ledger_dir=tmp_path / "ledger")
    command = {"id": "cmd-1", "action": "halt"}
    write_daemon(
        state_dir,
        {
            "pid": 0,
            "status": "running",
            "mode": "paper",
            "broker": "paper",
            "last_command": {"id": "old-cmd", "action": "refresh", "ok": True},
        },
    )
    record_command_status(
        state_dir,
        command,
        stage="done",
        result={"id": "cmd-1", "action": "halt", "ok": True},
    )

    status = wait_for_command(cfg, "cmd-1", timeout=0.1)

    assert status["last_command"] == {"id": "cmd-1", "action": "halt", "ok": True}
    assert "wait_timeout" not in status


def test_daemon_order_cancel_records_async_confirmations(tmp_path) -> None:
    from alphapilot.systems.live.brokers.sim import SimBroker
    from alphapilot.systems.live.config import LiveConfig, RunMode
    from alphapilot.systems.live.daemon import _apply_command
    from alphapilot.systems.live.runtime import LiveRuntime

    cfg = LiveConfig(mode=RunMode.PAPER, broker="paper", state_dir=tmp_path / "state", ledger_dir=tmp_path / "ledger")
    broker = SimBroker(
        cash=100_000.0,
        prices={"600000.SSE": 10.0},
        partial_ratio=0.0,
        open_cost=0.0,
        min_cost=0.0,
    )
    runtime = LiveRuntime.create(cfg, broker=broker)
    runtime.connect(paper_cash=100_000.0)

    order = _apply_command(
        runtime,
        {
            "id": "order-1",
            "action": "order",
            "payload": {
                "symbol": "SH600000",
                "side": "buy",
                "volume": 100,
                "price": 10.0,
                "reference": "daemon-ack-test",
                "event_timeout": 0.5,
            },
        },
    )

    assert order["ok"] is True
    assert order["message"] == "order_acknowledged"
    assert order["order_acknowledged"] is True
    assert order["order_status"] == "nottraded"
    assert order["order_active"] is True
    assert order["order_ack"]["found"] is True

    cancel = _apply_command(
        runtime,
        {
            "id": "cancel-1",
            "action": "cancel",
            "payload": {"order_id": order["order_id"], "event_timeout": 0.5},
        },
    )

    assert cancel["ok"] is True
    assert cancel["message"] == "cancel_confirmed"
    assert cancel["cancelled"] is True
    assert cancel["cancel_confirmed"] is True
    assert cancel["cancel_terminal"] is True
    assert cancel["cancel_confirmation"]["status"] == "cancelled"
    runtime.close()


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

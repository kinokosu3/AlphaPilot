from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from alphapilot.systems.live.brokers.paper import PaperBroker
from alphapilot.systems.live.clock import SimulatedClock
from alphapilot.systems.live.config import LiveConfig, RiskLimits, RunMode
from alphapilot.systems.live.ledger import Ledger
from alphapilot.systems.live.recovery import (
    _persisted_risk_state,
    recover_risk_state_from_ledger,
    reconcile_ledger_with_oms,
)
from alphapilot.systems.live.runtime import LiveRuntime
from alphapilot.systems.live.types import Account, Exchange, OrderRequest


class UnsupportedSnapshotBroker(PaperBroker):
    def query_orders(self) -> bool:
        return False

    def query_trades(self) -> bool:
        return False


def test_ledger_structured_event_filters(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger", now_fn=lambda: datetime(2026, 7, 6, 10, 0))
    ledger.record_event(
        "daemon_command",
        {"id": "cmd-1", "order_id": "ord-1", "request": {"reference": "ref-1"}},
        command_id="cmd-1",
        order_id="ord-1",
        reference="ref-1",
    )
    ledger.record("submit", {"order_id": "ord-2", "req": {"reference": "ref-2"}})

    assert len(ledger.events(kind="daemon_command")) == 1
    assert ledger.events(command_id="cmd-1")[0]["payload"]["id"] == "cmd-1"
    assert ledger.events(order_id="ord-2")[0]["payload"]["order_id"] == "ord-2"
    assert ledger.events(reference="ref-1")[0]["reference"] == "ref-1"
    assert ledger.events(day="20260706", limit=1)[0]["kind"] == "submit"


def test_recover_risk_state_from_ledger_counts_today_submits(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger", now_fn=lambda: datetime(2026, 7, 6, 10, 0))
    req = OrderRequest.buy("600000", Exchange.SSE, 100, 10.0, reference="cid-1")
    ledger.record("submit", {"order_id": "paper-1", "req": _req(req)})
    ledger.record("rejected", {"rule": "duplicate", "req": _req(req)})

    state = recover_risk_state_from_ledger(ledger, day="20260706")
    assert state["orders_today"] == 1
    assert state["value_today"] == 1000.0
    assert state["seen_refs"] == ["cid-1"]


def test_reconcile_ledger_with_oms_reports_missing_and_external_orders(tmp_path: Path) -> None:
    from alphapilot.systems.live.oms import OMS
    from alphapilot.systems.live.types import OrderStatus

    ledger = Ledger(tmp_path / "ledger", now_fn=lambda: datetime(2026, 7, 6, 10, 0))
    req = OrderRequest.buy("600000", Exchange.SSE, 100, 10.0, reference="ledger-ref")
    ledger.record("submit", {"order_id": "ledger-only", "req": _req(req)})

    oms = OMS()
    oms.on_order(req.create_order("broker-only", "paper", status=OrderStatus.NOTTRADED))

    report = reconcile_ledger_with_oms(ledger, oms, day="20260706")
    assert report["ledger_orders"] == 1
    assert report["broker_orders"] == 1
    assert report["missing_broker_order_ids"] == ["ledger-only"]
    assert report["external_broker_order_ids"] == ["broker-only"]
    assert report["active_order_ids"] == ["broker-only"]


def test_runtime_recovery_restores_risk_duplicate_refs(tmp_path: Path) -> None:
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
        risk=RiskLimits(
            max_order_value=1e12,
            max_daily_value=1e15,
            max_position_pct=1.0,
            price_guard_pct=0.1,
            max_orders_per_day=1000,
            lot_size=100,
        ),
    )
    clock = SimulatedClock(datetime(2026, 7, 6, 10, 0))
    first = LiveRuntime.create(
        cfg,
        broker=PaperBroker(cash=100_000.0, prices={"600000.SSE": 10.0}, open_cost=0.0, min_cost=0.0),
        now_fn=clock,
    )
    first.connect()
    sent = first.submit_order("SH600000", side="buy", volume=100, price=10.0, reference="dup-ref")
    assert sent["submitted"] is True
    first.close()

    second = LiveRuntime.create(
        cfg,
        broker=PaperBroker(cash=100_000.0, prices={"600000.SSE": 10.0}, open_cost=0.0, min_cost=0.0),
        now_fn=clock,
    )
    second.connect()
    risk = second.engine.risk.snapshot()
    assert risk["orders_today"] == 1
    assert risk["seen_refs"] == ["dup-ref"]
    assert second.snapshot()["recovery"]["reconciliation"]["missing_broker_order_ids"]
    assert second.snapshot()["recovery"]["warnings"][0]["kind"] == "ledger_orders_missing_in_broker"
    rejected = second.submit_order("SH600000", side="buy", volume=100, price=10.0, reference="dup-ref")
    assert rejected["submitted"] is False
    assert second.engine.ledger.events(kind="rejected")[-1]["payload"]["rule"] == "duplicate"
    assert second.snapshot()["recovery"]["risk_restored"] is True
    second.close()


def test_runtime_recovery_preserves_loss_latch_and_canary_baseline(
    tmp_path: Path,
) -> None:
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        ledger_dir=tmp_path / "loss-ledger",
        state_dir=tmp_path / "loss-state",
        risk=RiskLimits(
            max_order_value=10_000,
            max_daily_value=0,
            max_position_pct=0.02,
            price_guard_pct=0.02,
            max_orders_per_day=20,
            lot_size=100,
            max_daily_loss_pct=0.01,
            max_canary_loss_pct=0.03,
        ),
    )
    first = LiveRuntime.create(
        cfg,
        broker=PaperBroker(cash=100_000.0),
    )
    first.connect(paper_cash=100_000)
    first.engine.on_account(
        Account("paper", balance=98_900, available=98_900, gateway="paper")
    )
    assert first.engine.runmode.halted
    first.write_state()
    first.close()

    second = LiveRuntime.create(
        cfg,
        broker=PaperBroker(cash=98_900.0),
    )
    second.connect(paper_cash=98_900)

    risk = second.engine.risk.snapshot()
    assert second.engine.runmode.halted
    assert risk["loss_halt_rule"] == "daily_loss"
    assert risk["canary_start_equity"] == 100_000
    with pytest.raises(ValueError, match="has not recovered"):
        second.engine.resume()
    second.close()


def test_persisted_daily_risk_uses_configured_trading_timezone(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime-state.json"
    config = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        timezone="Asia/Shanghai",
    )
    state_path.write_text(
        json.dumps(
            {
                "written_at": "2026-07-17T16:05:00+00:00",
                "config": {
                    "mode": config.mode,
                    "trade_broker": config.trade_broker,
                },
                "account": {"account_id": "paper"},
                "engine": {
                    "risk": {
                        "day_start_equity": 100_000.0,
                        "canary_start_equity": 100_000.0,
                        "loss_halt_rule": "daily_loss",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(
        state_path=state_path,
        config=config,
        engine=SimpleNamespace(
            oms=SimpleNamespace(
                account=Account(
                    "paper",
                    balance=98_900,
                    available=98_900,
                    gateway="paper",
                )
            )
        ),
    )

    same_session = _persisted_risk_state(runtime, day=date(2026, 7, 18))
    next_session = _persisted_risk_state(runtime, day=date(2026, 7, 19))

    assert same_session["day_start_equity"] == 100_000.0
    assert same_session["loss_halt_rule"] == "daily_loss"
    assert next_session["day_start_equity"] == 0.0
    assert next_session["canary_start_equity"] == 100_000.0


def test_recovery_reports_unsupported_order_trade_queries(tmp_path: Path) -> None:
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        ledger_dir=tmp_path / "ledger",
        state_dir=tmp_path / "state",
    )
    runtime = LiveRuntime.create(cfg, broker=UnsupportedSnapshotBroker(cash=100_000.0))

    runtime.connect()

    recovery = runtime.snapshot()["recovery"]
    assert recovery["broker_refresh_ok"] is True
    assert "orders" in recovery["broker_refresh_unsupported"]
    assert "trades" in recovery["broker_refresh_unsupported"]
    runtime.close()


def test_recovery_blocks_on_previous_position_snapshot_difference(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "runtime_state.json").write_text(
        json.dumps({
            "account": {"account_id": "paper"},
            "positions": [{
                "code": "600000", "exchange": "SSE", "volume": 100,
                "yd_volume": 100, "today_volume": 0,
            }],
        }),
        encoding="utf-8",
    )
    cfg = LiveConfig(
        mode=RunMode.PAPER,
        broker="paper",
        ledger_dir=tmp_path / "ledger",
        state_dir=state_dir,
    )
    runtime = LiveRuntime.create(cfg, broker=PaperBroker(cash=100_000.0))

    runtime.connect()

    recovery = runtime.snapshot()["recovery"]
    assert recovery["state_reconciliation"]["passed"] is False
    assert recovery["state_reconciliation"]["position_differences"] == [{
        "instrument": "600000.SSE",
        "expected_volume": 100.0,
        "observed_volume": 0.0,
    }]
    assert "runtime_positions_changed" in {
        warning["kind"] for warning in recovery["warnings"]
    }
    # connect() has already written the new snapshot; the process-start
    # baseline must still survive until a warning-free formal reconcile.
    repeated = runtime.recover()
    assert "runtime_positions_changed" in {
        warning["kind"] for warning in repeated["warnings"]
    }
    runtime.accept_recovery_baseline()
    accepted = runtime.recover()
    assert "runtime_positions_changed" not in {
        warning["kind"] for warning in accepted["warnings"]
    }
    runtime.close()


def _req(req: OrderRequest) -> dict:
    return {
        "code": req.code,
        "exchange": req.exchange.value,
        "direction": req.direction.value,
        "volume": req.volume,
        "price": req.price,
        "type": req.type.value,
        "reference": req.reference,
    }

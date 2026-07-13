"""Portal live-trading endpoints: the in-process PAPER sandbox flow."""

from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from alphapilot.modules.portal.api import create_app
from alphapilot.systems.live.market_data import LiveMarketDataService
from alphapilot.systems.live.types import Exchange, TickData


def _client(engine):
    return TestClient(create_app(engine=engine))


def _listen_local() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


def test_live_status_before_connect(engine) -> None:
    client = _client(engine)
    data = client.get("/api/live/status").json()
    assert data["config"]["broker"] == "paper"
    assert data["config"]["mode"] in set(data["modes"])
    assert data["running"] is False
    assert "state" not in data


def test_live_market_snapshot_and_bars_are_read_only(engine, isolated_env) -> None:
    client = _client(engine)
    state_dir = isolated_env.root / "market_state"
    state_dir.mkdir(parents=True)
    (state_dir / "runtime_daemon.json").write_text(
        json.dumps({"pid": 99_999_999, "status": "running"}),
        encoding="utf-8",
    )
    empty = client.get("/api/live/market/snapshot", params={"state_dir": str(state_dir)}).json()
    assert empty["exists"] is False
    assert empty["ticks"] == []
    assert empty["daemon_running"] is False
    assert empty["daemon_status"] == "stopped"

    cfg = engine.get_system("live").config
    now = datetime.now().replace(microsecond=0)
    service = LiveMarketDataService(
        cfg.market_data,
        "paper",
        ["600000"],
        state_dir=state_dir,
        now_fn=lambda: now,
    )
    service.recorder.start()
    service.on_tick(TickData(
        code="600000", exchange=Exchange.SSE, datetime=now.replace(second=1),
        received_at=now, last_price=10.0, pre_close=9.9, volume=100, turnover=1_000,
        bid_price_1=9.99, ask_price_1=10.01, gateway="paper",
    ))
    service.on_tick(TickData(
        code="600000", exchange=Exchange.SSE, datetime=now.replace(second=30),
        received_at=now, last_price=10.2, pre_close=9.9, volume=180, turnover=1_820,
        gateway="paper",
    ))
    service.on_tick(TickData(
        code="600000", exchange=Exchange.SSE, datetime=(now + timedelta(minutes=1)).replace(second=0),
        received_at=now, last_price=10.1, pre_close=9.9, volume=200, turnover=2_020,
        gateway="paper",
    ))
    service.write_snapshot()
    service.close()

    snapshot = client.get(
        "/api/live/market/snapshot",
        params={"state_dir": str(state_dir), "symbols": "600000"},
    ).json()
    assert snapshot["exists"] is True
    assert snapshot["ticks"][0]["key"] == "600000.SSE"
    assert snapshot["ticks"][0]["turnover"] == 2_020
    assert snapshot["recorder"]["written_ticks"] == 3

    bars = client.get(
        "/api/live/market/bars",
        params={"state_dir": str(state_dir), "symbol": "600000", "interval": 60},
    ).json()
    assert bars["symbol"] == "600000.SSE"
    assert bars["rows"][0]["high"] == 10.2
    assert client.get(
        "/api/live/market/bars",
        params={"state_dir": str(state_dir), "symbol": "600000", "interval": 10},
    ).status_code == 400


def test_live_broker_catalog_exposes_capabilities_without_secret_values(engine, monkeypatch) -> None:
    monkeypatch.setenv("ALPHAPILOT_LIVE_XTP_ACCOUNT", "portal-secret-account")
    client = _client(engine)
    resp = client.get("/api/live/brokers")
    brokers = {row["name"]: row for row in resp.json()}

    assert {"emt", "xtp"} <= set(brokers)
    assert brokers["emt"]["capabilities"]["supports_order_query"] is True
    assert brokers["xtp"]["capabilities"]["supports_trade_query"] is True
    assert "ALPHAPILOT_LIVE_XTP_ACCOUNT" in brokers["xtp"]["env_fields"]
    assert "portal-secret-account" not in resp.text

    quote_resp = client.get("/api/live/quote-providers")
    providers = {row["name"]: row for row in quote_resp.json()}
    assert {"paper", "emt", "xtp"} <= set(providers)
    assert providers["paper"]["gateway_importable"] is True
    assert "portal-secret-account" not in quote_resp.text

    plugins = client.get("/api/live/plugins").json()
    assert plugins["api_version"] == 1
    assert {row["plugin_id"] for row in plugins["plugins"]} >= {"emt", "xtp"}
    assert "portal-secret-account" not in str(plugins)


def test_live_paper_full_flow(engine) -> None:
    client = _client(engine)

    # connect a paper account
    st = client.post("/api/live/paper/connect", json={"cash": 100000}).json()
    assert st["account"]["buying_power"] == 100000
    assert st["snapshot"]["mode"] == "paper"

    # manual buy fills immediately in the paper broker
    st = client.post(
        "/api/live/paper/order",
        json={"code": "SH600000", "side": "buy", "volume": 1000, "price": 10.0},
    ).json()
    positions = {p["code"]: p for p in st["positions"]}
    assert positions["600000"]["volume"] == 1000
    assert positions["600000"]["available"] == 0          # bought today -> T+1
    assert st["account"]["buying_power"] < 100000          # cash spent (+ fee)

    # submit a target -> reconcile against real positions -> buy the delta up to 2000
    st = client.post(
        "/api/live/paper/submit-target",
        json={"holdings": {"SH600000": 2000}, "prices": {"SH600000": 10.0}},
    ).json()
    assert st["planned"] >= 1
    positions = {p["code"]: p for p in st["positions"]}
    assert positions["600000"]["volume"] == 2000

    # kill-switch: halts and blocks further routing
    st = client.post("/api/live/paper/halt", json={}).json()
    assert st["snapshot"]["halted"] is True
    st = client.post(
        "/api/live/paper/order",
        json={"code": "SZ000001", "side": "buy", "volume": 100, "price": 5.0},
    ).json()
    assert all(p["code"] != "000001" for p in st["positions"])   # not routed while halted

    # resume, then reset the sandbox
    assert client.post("/api/live/paper/resume", json={}).json()["snapshot"]["halted"] is False
    assert client.post("/api/live/paper/reset", json={}).json()["running"] is False
    assert client.get("/api/live/status").json()["running"] is False


def test_live_paper_requires_connect(engine) -> None:
    client = _client(engine)
    # order without a session -> 400 from _api_error
    resp = client.post("/api/live/paper/order", json={"code": "SH600000", "side": "buy", "volume": 100})
    assert resp.status_code >= 400


def test_live_runtime_control_endpoints(engine, isolated_env) -> None:
    client = _client(engine)

    state = client.get("/api/live/runtime/state", params={"mode": "paper"}).json()
    assert state["exists"] is False
    assert state["state_path"].endswith("runtime_state.json")

    preflight = client.post("/api/live/runtime/preflight", json={"broker": "xtp", "network": False}).json()
    assert preflight["broker"] == "xtp"
    assert preflight["trade_broker"] == "xtp"
    assert preflight["quote_provider"] == "xtp"
    assert "trade" in preflight and "quote" in preflight
    assert preflight["network_checked"] is False
    assert "gateway_importable" in preflight

    connected = client.post(
        "/api/live/runtime/connect",
        json={
            "mode": "paper",
            "cash": 12345,
            "timeout": 1,
            "state_dir": str(isolated_env.root / "portal_live_state"),
            "ledger_dir": str(isolated_env.root / "portal_live_ledger"),
        },
    ).json()
    assert connected["ready"] is True
    assert connected["state"]["account"]["available"] == 12345
    assert connected["state"]["engine"]["mode"] == "paper"

    risk = client.get(
        "/api/live/risk/status",
        params={
            "mode": "paper",
            "state_dir": str(isolated_env.root / "portal_live_state"),
            "ledger_dir": str(isolated_env.root / "portal_live_ledger"),
        },
    ).json()
    assert risk["exists"] is True
    assert risk["recovery"]["risk_restored"] is True

    events = client.get(
        "/api/live/ledger/events",
        params={
            "kind": "connected",
            "state_dir": str(isolated_env.root / "portal_live_state"),
            "ledger_dir": str(isolated_env.root / "portal_live_ledger"),
        },
    ).json()
    assert events["count"] >= 1


def test_live_runtime_preflight_can_probe_configured_network_endpoints(engine, monkeypatch) -> None:
    quote_sock = _listen_local()
    trade_sock = _listen_local()
    try:
        quote_port = quote_sock.getsockname()[1]
        trade_port = trade_sock.getsockname()[1]
        monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_ACCOUNT", "portal-test-account")
        monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_PASSWORD", "portal-test-password")
        monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_QUOTE_ACCOUNT", "portal-test-quote-account")
        monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_QUOTE_PASSWORD", "portal-test-quote-password")
        monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_QUOTE_HOST", "127.0.0.1")
        monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_QUOTE_PORT", str(quote_port))
        monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_TRADE_HOST", "127.0.0.1")
        monkeypatch.setenv("ALPHAPILOT_LIVE_EMT_TRADE_PORT", str(trade_port))

        preflight = _client(engine).post(
            "/api/live/runtime/preflight",
            json={"broker": "emt", "network": True, "timeout": 0.5},
        ).json()
    finally:
        quote_sock.close()
        trade_sock.close()

    assert preflight["broker"] == "emt"
    assert preflight["trade"]["name"] == "emt"
    assert preflight["quote"]["name"] == "emt"
    assert preflight["network_checked"] is True
    assert preflight["missing_env"] == []
    assert {item["name"]: item["ok"] for item in preflight["endpoints"]} == {"quote": True, "trade": True}


def test_live_daemon_control_endpoints(engine, isolated_env) -> None:
    client = _client(engine)
    state_dir = isolated_env.root / "portal_daemon_state"
    ledger_dir = isolated_env.root / "portal_daemon_ledger"

    initial = client.get("/api/live/daemon/status", params={"mode": "paper", "state_dir": str(state_dir)}).json()
    assert initial["running"] is False

    started = client.post(
        "/api/live/daemon/start",
            json={
                "mode": "paper",
                "cash": 10000,
                "symbols": ["600000"],
                "interval": 0.05,
                "timeout": 1,
                "state_dir": str(state_dir),
            "ledger_dir": str(ledger_dir),
        },
    ).json()
    assert started["started"] is True
    assert started["record_market_data"] is True
    try:
        deadline = time.time() + 5.0
        status = client.get("/api/live/daemon/status", params={"mode": "paper", "state_dir": str(state_dir)}).json()
        while time.time() < deadline and not status.get("running"):
            time.sleep(0.1)
            status = client.get("/api/live/daemon/status", params={"mode": "paper", "state_dir": str(state_dir)}).json()
        assert status["running"] is True, status
        assert status["pid"] == started["pid"]
        market = client.get(
            "/api/live/market/snapshot",
            params={"mode": "paper", "state_dir": str(state_dir)},
        ).json()
        assert market["exists"] is True
        assert market["subscribed_symbols"] == ["600000.SSE"]
        assert market["recorder"]["enabled"] is True

        runner_status = client.post(
            "/api/live/daemon/strategy/status",
            json={"state_dir": str(state_dir), "wait": True, "timeout": 5},
        ).json()
        assert runner_status["accepted"] is True
        assert runner_status["daemon"]["last_command"]["runner_status"]["enabled"] is False

        strategy_started = client.post(
            "/api/live/daemon/strategy/start",
            json={
                "state_dir": str(state_dir),
                "timing_strategy": "sma_filter",
                "symbols": ["600000"],
                "timing_params": {"window": 2, "target_percent": 0.2},
                "timing_freq": "min",
                "min_bars": 2,
                "wait": True,
                "timeout": 5,
            },
        ).json()
        assert strategy_started["accepted"] is True
        assert strategy_started["daemon"]["last_command"]["runner_status"]["active"] is True

        strategy_paused = client.post(
            "/api/live/daemon/strategy/pause",
            json={"state_dir": str(state_dir), "wait": True, "timeout": 5},
        ).json()
        assert strategy_paused["daemon"]["last_command"]["runner_status"]["paused"] is True

        strategy_resumed = client.post(
            "/api/live/daemon/strategy/resume",
            json={"state_dir": str(state_dir), "wait": True, "timeout": 5},
        ).json()
        assert strategy_resumed["daemon"]["last_command"]["runner_status"]["active"] is True

        strategy_stopped = client.post(
            "/api/live/daemon/strategy/stop",
            json={"state_dir": str(state_dir), "wait": True, "timeout": 5},
        ).json()
        assert strategy_stopped["daemon"]["last_command"]["runner_status"]["stopped"] is True

        halted = client.post(
            "/api/live/daemon/halt",
            json={"state_dir": str(state_dir), "reason": "portal-test", "wait": True, "timeout": 5},
        ).json()
        assert halted["accepted"] is True
        assert halted["daemon"]["last_command"]["ok"] is True
        status = client.get("/api/live/daemon/status", params={"mode": "paper", "state_dir": str(state_dir)}).json()
        assert status["state"]["engine"]["halted"] is True

        resumed = client.post(
            "/api/live/daemon/resume",
            json={"state_dir": str(state_dir), "wait": True, "timeout": 5},
        ).json()
        assert resumed["accepted"] is True
        assert resumed["daemon"]["last_command"]["ok"] is True
        status = client.get("/api/live/daemon/status", params={"mode": "paper", "state_dir": str(state_dir)}).json()
        assert status["state"]["engine"]["halted"] is False

        order = client.post(
            "/api/live/daemon/order",
            json={
                "state_dir": str(state_dir),
                "symbol": "SH600000",
                "side": "buy",
                "volume": 100,
                "price": 10.0,
                "wait": True,
                "timeout": 5,
                "reference": "portal-daemon-order",
            },
        ).json()
        assert order["accepted"] is True
        assert order["daemon"]["last_command"]["ok"] is True
        order_id = order["daemon"]["last_command"]["order_id"]

        cancel = client.post(
            "/api/live/daemon/cancel",
            json={"state_dir": str(state_dir), "order_id": order_id, "wait": True, "timeout": 5},
        ).json()
        assert cancel["accepted"] is True
        assert cancel["daemon"]["last_command"]["action"] == "cancel"
        assert cancel["daemon"]["last_command"]["ok"] is False
        assert cancel["daemon"]["last_command"]["cancel"]["reason"] == "not_active"

        status = client.get("/api/live/daemon/status", params={"mode": "paper", "state_dir": str(state_dir)}).json()
        positions = {row["code"]: row for row in status["state"]["positions"]}
        assert positions["600000"]["volume"] == 100

        target = client.post(
            "/api/live/daemon/submit-target",
            json={
                "state_dir": str(state_dir),
                "holdings": {"SH600000": 200},
                "prices": {"SH600000": 10.0},
                "route": True,
                "wait": True,
                "timeout": 5,
            },
        ).json()
        assert target["accepted"] is True
        assert target["daemon"]["last_command"]["ok"] is True
        assert target["daemon"]["last_command"]["planned"] == 1
        status = client.get("/api/live/daemon/status", params={"mode": "paper", "state_dir": str(state_dir)}).json()
        positions = {row["code"]: row for row in status["state"]["positions"]}
        assert positions["600000"]["volume"] == 200

        refreshed = client.post(
            "/api/live/daemon/refresh",
            json={"state_dir": str(state_dir), "wait": True, "timeout": 5},
        ).json()
        assert refreshed["accepted"] is True
        assert refreshed["daemon"]["last_command"]["action"] == "refresh"

        reconnected = client.post(
            "/api/live/daemon/reconnect",
            json={"state_dir": str(state_dir), "wait": True, "timeout": 5},
        ).json()
        assert reconnected["accepted"] is True
        assert reconnected["daemon"]["last_command"]["action"] == "reconnect"
        assert reconnected["daemon"]["state"]["engine"]["halted"] is True
    finally:
        stopped = client.post("/api/live/daemon/stop", json={"state_dir": str(state_dir), "timeout": 5}).json()
    assert stopped["running"] is False

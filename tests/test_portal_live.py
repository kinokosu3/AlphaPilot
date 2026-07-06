"""Portal live-trading endpoints: the in-process PAPER sandbox flow."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from alphapilot.modules.portal.api import create_app


def _client(engine):
    return TestClient(create_app(engine=engine))


def test_live_status_before_connect(engine) -> None:
    client = _client(engine)
    data = client.get("/api/live/status").json()
    assert data["config"]["broker"] == "paper"
    assert data["config"]["mode"] in set(data["modes"])
    assert data["running"] is False
    assert "state" not in data


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
                "interval": 0.05,
                "timeout": 1,
                "state_dir": str(state_dir),
            "ledger_dir": str(ledger_dir),
        },
    ).json()
    assert started["started"] is True
    try:
        deadline = time.time() + 5.0
        status = client.get("/api/live/daemon/status", params={"mode": "paper", "state_dir": str(state_dir)}).json()
        while time.time() < deadline and not status.get("running"):
            time.sleep(0.1)
            status = client.get("/api/live/daemon/status", params={"mode": "paper", "state_dir": str(state_dir)}).json()
        assert status["running"] is True, status
        assert status["pid"] == started["pid"]

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
    finally:
        stopped = client.post("/api/live/daemon/stop", json={"state_dir": str(state_dir), "timeout": 5}).json()
    assert stopped["running"] is False

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from alphapilot.modules.portal.api import create_app
from alphapilot.modules.portal.settings import (
    load_file_portal_settings,
    resolve_operator_auth,
    save_portal_settings,
    set_operator_auth_required,
    settings_path,
)


def test_operator_auth_settings_default_saved_and_environment_precedence(
    isolated_env,
    monkeypatch,
) -> None:
    monkeypatch.delenv("ALPHAPILOT_OPERATOR_AUTH_REQUIRED")

    initial = resolve_operator_auth()
    assert initial["required"] is True
    assert initial["saved_required"] is True
    assert initial["source"] == "default"

    saved_to = set_operator_auth_required(False)
    assert saved_to == settings_path()
    assert load_file_portal_settings()["operator_auth_required"] is False
    assert resolve_operator_auth()["source"] == "settings"
    assert resolve_operator_auth()["required"] is False

    monkeypatch.setenv("ALPHAPILOT_OPERATOR_AUTH_REQUIRED", "true")
    overridden = resolve_operator_auth()
    assert overridden["required"] is True
    assert overridden["saved_required"] is False
    assert overridden["source"] == "environment"


def test_old_portal_settings_and_runtime_default_operator_auth_to_required(
    engine,
    isolated_env,
    monkeypatch,
) -> None:
    from alphapilot.modules.portal.runtime import clear_runtime, write_runtime

    monkeypatch.delenv("ALPHAPILOT_OPERATOR_AUTH_REQUIRED")
    save_portal_settings({
        "host": "0.0.0.0",
        "port": 19903,
        "timezone": "Asia/Shanghai",
    })
    # Simulate an old settings file with no security field.
    raw = json.loads(settings_path().read_text(encoding="utf-8"))
    raw.pop("operator_auth_required")
    settings_path().write_text(json.dumps(raw), encoding="utf-8")
    assert resolve_operator_auth()["required"] is True

    # Old runtime metadata also has no field and must be interpreted as required.
    write_runtime(host="0.0.0.0", port=19903)
    try:
        status = engine.get_module("portal").portal_operator_auth()
        assert status["running"] is True
        assert status["running_required"] is True
        assert status["running_mode"] == "required"
    finally:
        clear_runtime()


def test_portal_operator_auth_cli_validates_persists_and_audits(
    engine,
    isolated_env,
    monkeypatch,
) -> None:
    monkeypatch.delenv("ALPHAPILOT_OPERATOR_AUTH_REQUIRED")
    portal = engine.get_module("portal")

    with pytest.raises(ValueError, match="operator_id"):
        portal.portal_operator_auth(required=False, reason="lab")
    with pytest.raises(ValueError, match="reason"):
        portal.portal_operator_auth(required=False, operator_id="alice")
    with pytest.raises(ValueError, match="acknowledge_network_risk"):
        portal.portal_operator_auth(
            required=False,
            operator_id="alice",
            reason="trusted lab",
        )

    changed = portal.portal_operator_auth(
        required="false",
        operator_id="alice",
        reason="trusted lab",
        acknowledge_network_risk=True,
    )
    assert changed["changed"] is True
    assert changed["required"] is False
    assert changed["mode"] == "optional"
    assert changed["warning"]
    assert changed["restart"] is None

    unchanged = portal.portal_operator_auth(
        required=False,
        operator_id="alice",
        reason="confirm setting",
        acknowledge_network_risk=True,
    )
    assert unchanged["changed"] is False

    events = engine.get_system("trading").audit_events(limit=20)
    changes = [
        event for event in events
        if event["action"] == "portal_operator_auth_change"
    ]
    assert {event["result"] for event in changes} == {"requested", "ok"}
    assert all(event["operator_id"] == "alice" for event in changes)
    assert all(event["auth_source"] == "local-cli" for event in changes)
    assert any(event["details"]["new_required"] is False for event in changes)


def test_portal_operator_auth_cli_handles_env_conflict_restart_and_write_failure(
    engine,
    isolated_env,
    monkeypatch,
) -> None:
    portal = engine.get_module("portal")
    monkeypatch.setenv("ALPHAPILOT_OPERATOR_AUTH_REQUIRED", "true")
    with pytest.raises(ValueError, match="overrides the CLI setting"):
        portal.portal_operator_auth(
            required=False,
            operator_id="alice",
            reason="conflicting change",
            acknowledge_network_risk=True,
        )

    monkeypatch.delenv("ALPHAPILOT_OPERATOR_AUTH_REQUIRED")
    restarted = portal.portal_operator_auth(
        required=False,
        operator_id="alice",
        reason="apply on next start",
        acknowledge_network_risk=True,
        restart=True,
    )
    assert restarted["required"] is False
    assert restarted["restart"]["accepted"] is False
    assert "No running portal process" in restarted["restart"]["error"]

    def fail_write(_required: bool):
        raise OSError("read-only settings")

    monkeypatch.setattr(
        "alphapilot.modules.portal.settings.set_operator_auth_required",
        fail_write,
    )
    with pytest.raises(OSError, match="read-only settings"):
        portal.portal_operator_auth(
            required=True,
            operator_id="alice",
            reason="restore auth",
        )
    events = engine.get_system("trading").audit_events(limit=20)
    assert any(
        event["action"] == "portal_operator_auth_change"
        and event["result"] == "failed"
        and event["operator_id"] == "alice"
        for event in events
    )


def test_portal_security_is_read_only_and_reports_pending_restart(
    engine,
    isolated_env,
    monkeypatch,
) -> None:
    monkeypatch.delenv("ALPHAPILOT_OPERATOR_AUTH_REQUIRED")
    set_operator_auth_required(True)
    client = TestClient(
        create_app(engine=engine, portal_host="0.0.0.0", portal_port=19909),
    )

    initial = client.get("/api/portal/security")
    assert initial.status_code == 200
    assert initial.json() == {
        "operator_auth_required": True,
        "operator_auth_mode": "required",
        "source": "settings",
        "pending_required": True,
        "pending_mode": "required",
        "pending_source": "settings",
        "restart_required": False,
        "bind_host": "0.0.0.0",
        "bind_port": 19909,
        "bind_address": "0.0.0.0:19909",
        "network_exposed": True,
        "automated_live_enabled": False,
        "cors_policy": "wildcard",
        "warning": "",
    }
    ignored = client.patch(
        "/api/portal/settings",
        json={
            "host": "0.0.0.0",
            "port": 19909,
            "timezone": "Asia/Shanghai",
            "operator_auth_required": False,
        },
    )
    assert ignored.status_code == 200
    assert load_file_portal_settings()["operator_auth_required"] is True
    assert client.patch(
        "/api/portal/env",
        json={"values": {"ALPHAPILOT_OPERATOR_AUTH_REQUIRED": "false"}},
    ).status_code == 400

    set_operator_auth_required(False)
    pending = client.get("/api/portal/security").json()
    assert pending["operator_auth_mode"] == "required"
    assert pending["pending_mode"] == "optional"
    assert pending["restart_required"] is True
    assert client.put(
        "/api/portal/security",
        json={"operator_auth_required": False},
    ).status_code == 405
    assert "put" not in client.get("/openapi.json").json()["paths"][
        "/api/portal/security"
    ]


def test_optional_auth_covers_trading_live_audit_and_supplied_tokens(
    engine,
    isolated_env,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALPHAPILOT_OPERATOR_AUTH_REQUIRED", "false")
    monkeypatch.setenv("ALPHAPILOT_AUTOMATED_LIVE_ENABLED", "true")
    trading = engine.get_system("trading")
    live_module = engine.get_module("live")
    monkeypatch.setattr(
        live_module,
        "live_daemon_halt",
        lambda **kwargs: {"accepted": True, "reason": kwargs["reason"]},
    )
    valid_token = trading.create_operator_token("real-operator")["token"]
    client = TestClient(create_app(engine=engine, portal_host="0.0.0.0"))
    anonymous_headers = {
        "Origin": "https://untrusted.example",
        "User-Agent": "portal-security-test",
        "X-Request-ID": "anonymous-request",
    }

    instance_id = f"anonymous-{uuid4().hex}"
    created = client.post(
        "/api/trading/strategy-instances",
        json={
            "instance_id": instance_id,
            "strategy_id": "sma_filter",
            "params": {"window": 5},
            "universe": ["600000.SSE"],
            "data_policy": {"history_window": 6, "data_version": "auth-test-v1"},
        },
        headers=anonymous_headers,
    )
    assert created.status_code == 200
    validated = client.post(
        f"/api/trading/strategy-instances/{instance_id}/validate",
        headers=anonymous_headers,
    )
    assert validated.status_code == 200
    assert validated.json()["ok"] is True
    assert client.put(
        f"/api/trading/deployments/{instance_id}",
        json={"run_mode": "paper", "reason": "optional auth test"},
        headers=anonymous_headers,
    ).status_code == 200
    assert client.post(
        "/api/trading/kill-switches/instance/"
        f"{instance_id}/engage",
        json={"reason": "optional auth test"},
        headers=anonymous_headers,
    ).status_code == 200
    assert client.post(
        "/api/live/daemon/halt",
        json={"reason": "optional auth test"},
        headers=anonymous_headers,
    ).status_code == 200
    assert client.post(
        "/api/live/paper/connect",
        json={"cash": 100_000},
        headers=anonymous_headers,
    ).status_code == 200
    assert client.post(
        "/api/live/paper/order",
        json={"code": "600000.SSE", "side": "buy", "volume": 100, "price": 10},
        headers=anonymous_headers,
    ).status_code == 200

    token_instance = f"token-{uuid4().hex}"
    assert client.post(
        "/api/trading/strategy-instances",
        json={
            "instance_id": token_instance,
            "strategy_id": "sma_filter",
            "params": {"window": 5},
            "universe": ["600000.SSE"],
        },
        headers={"Authorization": f"Bearer {valid_token}"},
    ).status_code == 200
    rejected_instance = f"rejected-{uuid4().hex}"
    rejected = client.post(
        "/api/trading/strategy-instances",
        json={
            "instance_id": rejected_instance,
            "strategy_id": "sma_filter",
        },
        headers={"Authorization": "Bearer invalid"},
    )
    assert rejected.status_code == 401
    assert all(
        row["instance_id"] != rejected_instance
        for row in trading.list_instances()
    )

    events = trading.audit_events(limit=200)
    anonymous_events = [
        event for event in events
        if event["request_id"] == "anonymous-request"
    ]
    assert anonymous_events
    assert all(
        event["operator_id"] == "portal-unauthenticated"
        for event in anonymous_events
    )
    transport = [
        event for event in anonymous_events
        if event["action"] == "portal_write:/api/trading/strategy-instances"
    ]
    assert {event["result"] for event in transport} == {"requested", "ok"}
    assert any(
        event["details"].get("path") == "/api/trading/strategy-instances"
        and event["details"].get("method") == "POST"
        and event["details"].get("origin") == "https://untrusted.example"
        and event["details"].get("user_agent") == "portal-security-test"
        and event["details"].get("client_address")
        for event in transport
    )
    assert any(
        event["action"] == "create_instance"
        and event["instance_id"] == token_instance
        and event["operator_id"] == "real-operator"
        for event in events
    )
    assert "Bearer invalid" not in json.dumps(events)

    security = client.get("/api/portal/security").json()
    assert security["operator_auth_mode"] == "optional"
    assert security["source"] == "environment"
    assert security["network_exposed"] is True
    assert security["automated_live_enabled"] is True
    assert security["warning"]
    assert client.get("/api/live/brokers").status_code == 200
    assert client.post(
        "/api/live/runtime/preflight",
        json={"broker": "paper", "network": False},
    ).status_code == 200

    cors = client.options(
        "/api/live/paper/order",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert cors.status_code == 200
    assert cors.headers["access-control-allow-origin"] == "*"

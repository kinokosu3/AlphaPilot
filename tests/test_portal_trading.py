from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from alphapilot.modules.portal.api import create_app


def _operator_headers(engine) -> dict[str, str]:  # noqa: ANN001
    token = engine.get_system("trading").create_operator_token("portal-test")["token"]
    return {"Authorization": f"Bearer {token}", "X-Request-ID": uuid4().hex}


def test_trading_definition_and_instance_api(engine) -> None:
    client = TestClient(create_app(engine=engine))
    headers = _operator_headers(engine)
    definitions = client.get("/api/trading/strategy-definitions")
    assert definitions.status_code == 200
    body = definitions.json()
    dual = next(item for item in body["definitions"] if item["strategy_id"] == "dual_ma")
    assert dual["required_history"] == 21
    assert dual["parameter_schema"]["properties"]["short_window"]["default"] == 5

    instance_id = f"ma-{uuid4().hex}"
    created = client.post(
        "/api/trading/strategy-instances",
        json={
            "instance_id": instance_id,
            "strategy_id": "dual_ma",
            "params": {"short_window": 5, "long_window": 20, "target_percent": 0.2},
            "universe": ["SH600000"],
            "frequency": "day",
        },
        headers=headers,
    )
    assert created.status_code == 200
    assert created.json()["config_hash"]

    validated = client.post(
        f"/api/trading/strategy-instances/{instance_id}/validate", headers=headers,
    )
    assert validated.status_code == 200
    assert validated.json()["ok"] is True

    updated = client.patch(
        f"/api/trading/strategy-instances/{instance_id}",
        json={"params": {"short_window": 10, "long_window": 30, "target_percent": 0.2}},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["deployment_level"] == "replay"


def test_legacy_timing_catalog_declares_compatibility_route(engine) -> None:
    client = TestClient(create_app(engine=engine))
    body = client.get("/api/timing/strategies").json()
    assert body["deprecated"] is True
    assert body["replacement"] == "/api/trading/strategy-definitions"


def test_trading_kill_switch_api_engages_lists_and_releases(engine) -> None:
    client = TestClient(create_app(engine=engine))
    headers = _operator_headers(engine)

    engaged = client.post(
        "/api/trading/kill-switches/global/all/engage",
        json={"reason": "operator test"},
        headers=headers,
    )
    assert engaged.status_code == 200
    assert engaged.json() == {
        "scope_type": "global",
        "scope_id": "*",
        "active": True,
        "reason": "operator test",
    }
    listed = client.get("/api/trading/kill-switches")
    assert listed.status_code == 200
    assert any(
        row["scope_type"] == "global" and row["active"] is True
        for row in listed.json()["kill_switches"]
    )
    released = client.post(
        "/api/trading/kill-switches/global/all/release",
        json={"reason": "operator test complete"},
        headers=headers,
    )
    assert released.status_code == 200
    assert released.json()["active"] is False


def test_trading_write_api_requires_operator_token(engine) -> None:
    client = TestClient(create_app(engine=engine))
    response = client.post(
        "/api/trading/strategy-instances",
        json={"instance_id": "unauthorized", "strategy_id": "dual_ma"},
    )
    assert response.status_code == 401

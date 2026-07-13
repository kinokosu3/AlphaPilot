from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from alphapilot.modules.portal.api import create_app


def test_trading_definition_and_instance_api(engine) -> None:
    client = TestClient(create_app(engine=engine))
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
    )
    assert created.status_code == 200
    assert created.json()["config_hash"]

    validated = client.post(f"/api/trading/strategy-instances/{instance_id}/validate")
    assert validated.status_code == 200
    assert validated.json()["ok"] is True

    updated = client.patch(
        f"/api/trading/strategy-instances/{instance_id}",
        json={"params": {"short_window": 10, "long_window": 30, "target_percent": 0.2}},
    )
    assert updated.status_code == 200
    assert updated.json()["deployment_level"] == "replay"


def test_legacy_timing_catalog_declares_compatibility_route(engine) -> None:
    client = TestClient(create_app(engine=engine))
    body = client.get("/api/timing/strategies").json()
    assert body["deprecated"] is True
    assert body["replacement"] == "/api/trading/strategy-definitions"

from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from alphapilot.modules.portal.api import create_app
from alphapilot.systems.live.brokers import registry as live_registry
from alphapilot.systems.live.plugin import (
    GatewayCapabilities,
    LivePluginSpec,
    ProviderSpec,
    QuoteChannelSpec,
    TradeChannelSpec,
)
def _tts_contract_spec() -> LivePluginSpec:
    """Portal contract fixture for the separately installed TTS plugin."""

    return LivePluginSpec(
        plugin_id="tts-contract-fixture",
        providers=(
            ProviderSpec(
                name="tts",
                factory_path="external_tts_plugin:create_gateway",
                gateway_name="TTS",
                trade=TradeChannelSpec(
                    account_kind="simulation",
                    capabilities=GatewayCapabilities(
                        routable_asset_classes=("stock", "fund"),
                    ),
                ),
            ),
            ProviderSpec(
                name="tts_7x24",
                factory_path="external_tts_plugin:create_gateway",
                gateway_name="TTS_7X24",
                quote=QuoteChannelSpec(
                    data_kind="replay",
                    capabilities=GatewayCapabilities(
                        routable_asset_classes=(),
                        supports_cancel=False,
                    ),
                ),
            ),
        ),
    )


def _operator_headers(engine) -> dict[str, str]:  # noqa: ANN001
    token = engine.get_system("trading").create_operator_token("portal-test")["token"]
    return {"Authorization": f"Bearer {token}", "X-Request-ID": uuid4().hex}


def test_tts_deployment_api_is_local_unauthenticated_and_provider_filtered(engine) -> None:
    trading = engine.get_system("trading")
    client = TestClient(create_app(engine=engine))
    instance_id = f"tts-binding-{uuid4().hex}"
    trading.create_instance({
        "instance_id": instance_id,
        "strategy_id": "dual_ma",
        "params": {"short_window": 5, "long_window": 20},
        "universe": ["SH600000"],
        "frequency": "day",
        "data_policy": {
            "feature_adjustment": "backward",
            "history_window": 21,
            "data_version": "portal-test-v1",
        },
    })
    assert trading.validate_instance(instance_id)["ok"] is True
    try:
        live_registry.get_broker("tts")
    except ValueError:
        live_registry.register_plugin_spec(
            _tts_contract_spec(), distribution="external-alphapilot-tts", version="test",
        )

    payload = {
        "run_mode": "simulation",
        "trade_provider": "tts",
        "quote_provider": "emt",
        "account_profile": "tts-uat-main",
    }
    updated = client.put(
        f"/api/trading/deployments/{instance_id}",
        json=payload,
    )
    assert updated.status_code == 200
    configuration = updated.json()["configuration"]
    assert configuration["run_mode"] == "simulation"
    assert configuration["trade_provider"] == "tts"
    assert configuration["quote_provider"] == "emt"
    assert configuration["quote_data_kind"] == "realtime"
    assert client.put(
        f"/api/trading/deployments/{instance_id}",
        json={**payload, "account_profile": ""},
    ).status_code == 400

    simulation_brokers = client.get(
        "/api/live/brokers?account_kind=simulation"
    ).json()
    assert [row["name"] for row in simulation_brokers] == ["tts"]
    replay_quotes = client.get(
        "/api/live/quote-providers?data_kind=replay"
    ).json()
    assert [row["name"] for row in replay_quotes] == ["tts_7x24"]


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
    assert updated.json()["validation_state"] == "created"


def test_deployment_configuration_requires_explicit_instance_validation(engine) -> None:
    trading = engine.get_system("trading")
    client = TestClient(create_app(engine=engine))
    instance_id = f"unvalidated-deployment-{uuid4().hex}"
    created = trading.create_instance({
        "instance_id": instance_id,
        "strategy_id": "sma_filter",
        "params": {"window": 5},
        "universe": ["600000.SSE"],
        "data_policy": {"history_window": 6, "data_version": "portal-v10"},
    })
    assert created["validation_state"] == "created"

    rejected = client.put(
        f"/api/trading/deployments/{instance_id}", json={"run_mode": "paper"},
    )
    assert rejected.status_code == 400
    assert "must be validated" in rejected.json()["detail"]
    assert trading.store.get_instance(instance_id)["validation_state"] == "created"


def test_removed_timing_routes_are_absent_but_catalog_remains_auditable(engine) -> None:
    client = TestClient(create_app(engine=engine))
    assert client.get("/api/timing/strategies").status_code == 404
    assert client.post("/api/timing/signal", json={}).status_code == 404
    assert client.post("/api/timing/backtest", json={}).status_code == 404
    assert client.get("/api/timing/jobs/missing/detail").status_code == 404
    compatibility = client.get("/api/trading/compatibility").json()
    assert compatibility["schema_version"] == 10
    assert compatibility["environment_id"]
    catalog = {
        row["entrypoint"]: row for row in compatibility["entrypoints"]
    }
    assert catalog["GET /api/timing/strategies"]["status"] == "removed"
    assert catalog["GET /api/timing/strategies"]["removal_release"] == "0.2.0"


def test_indirect_timing_job_and_module_dispatch_are_removed(engine) -> None:  # noqa: ANN001
    trading = engine.get_system("trading")
    client = TestClient(create_app(engine=engine))
    before = {
        row["entrypoint"]: row["call_count"]
        for row in trading.compatibility_status()["entrypoints"]
    }
    job = client.post(
        "/api/jobs",
        json={"kind": "timing_backtest", "kwargs": {"strategy_name": "sma_filter"}},
    )
    assert job.status_code == 400

    modules = client.get("/api/modules")
    assert "timing" not in modules.json()
    dispatched = client.post(
        "/api/modules/run",
        json={"module": "timing", "command": "timing_strategies", "kwargs": {}},
    )
    assert dispatched.status_code == 404

    after = {
        row["entrypoint"]: row["call_count"]
        for row in trading.compatibility_status()["entrypoints"]
    }
    assert after == before


def test_broker_uat_http_surface_is_strictly_read_only(engine) -> None:
    client = TestClient(create_app(engine=engine))
    listed = client.get("/api/trading/broker-uat-runs")
    assert listed.status_code == 200
    assert isinstance(listed.json()["runs"], list)
    assert client.post("/api/trading/broker-uat-runs", json={}).status_code == 405
    operations = client.get("/openapi.json").json()["paths"][
        "/api/trading/broker-uat-runs"
    ]
    assert set(operations) == {"get"}


def test_decision_comparison_is_local_diagnostic_and_old_gate_routes_are_absent(engine) -> None:
    client = TestClient(create_app(engine=engine))
    headers = _operator_headers(engine)
    instance_id = f"diagnostics-{uuid4().hex}"
    created = client.post(
        "/api/trading/strategy-instances",
        json={
            "instance_id": instance_id,
            "strategy_id": "sma_filter",
            "params": {"window": 5},
            "universe": ["SH600000"],
            "data_policy": {"history_window": 6, "data_version": "api-v1"},
        },
        headers=headers,
    )
    assert created.status_code == 200
    assert client.post(
        f"/api/trading/strategy-instances/{instance_id}/validate",
        headers=headers,
    ).status_code == 200

    diagnostics = client.get(
        f"/api/trading/deployments/{instance_id}/diagnostics"
    )
    assert diagnostics.status_code == 200
    assert diagnostics.json()["modes"]["paper"]["trading_sessions"] == 0

    invalid = client.post(
        f"/api/trading/deployments/{instance_id}/decision-comparisons",
        json={"left_mode": "replay", "right_mode": "shadow"},
    )
    assert invalid.status_code == 422
    validation_errors = invalid.json()["detail"]
    assert {item["loc"][-1] for item in validation_errors} == {
        "left_run_id", "right_run_id",
    }
    assert all("required" in item["msg"].lower() for item in validation_errors)
    assert client.get(
        f"/api/trading/deployments/{instance_id}/qualification"
    ).status_code == 404
    assert client.post(
        f"/api/trading/deployments/{instance_id}/parity-runs", json={}
    ).status_code == 404


def test_preimported_legacy_timing_job_remains_available_through_formal_detail(
    engine,
    isolated_env,
) -> None:
    job_id = "legacy-timing-job"
    artifact = isolated_env.portal_job_root / "legacy-artifacts"
    artifact.mkdir(parents=True)
    (artifact / "summary.json").write_text(
        json.dumps({"strategy_name": "sma_filter", "total_return": 0.12}),
        encoding="utf-8",
    )
    (artifact / "signals.csv").write_text(
        "datetime,instrument,signal\n2026-07-01,600000.SSE,1\n",
        encoding="utf-8",
    )
    job_dir = isolated_env.portal_job_root / job_id
    job_dir.mkdir(parents=True)
    params = {
        "strategy_name": "sma_filter",
        "symbols": ["sh.600000"],
        "adjust_mode": "none",
        "output_dir": str(artifact),
    }
    (job_dir / "job.json").write_text(json.dumps({
        "job_id": job_id,
        "kind": "timing_backtest",
        "status": "succeeded",
        "params": params,
        "created_at": "2026-07-01T00:00:00+00:00",
        "finished_at": "2026-07-01T00:01:00+00:00",
    }), encoding="utf-8")
    (job_dir / "result.json").write_text(json.dumps({
        "result": {"artifact_dir": str(artifact), "total_return": 0.12},
    }), encoding="utf-8")

    trading = engine.get_system("trading")
    instance_id = f"legacy-import-{uuid4().hex}"
    trading.create_instance({
        "instance_id": instance_id,
        "strategy_id": "sma_filter",
        "params": {"window": 5},
        "universe": ["600000.SSE"],
    })
    imported = trading.store.import_legacy_backtest_job(
        job_id,
        instance_id=instance_id,
        request=params,
        result={"artifact_dir": str(artifact), "total_return": 0.12},
        artifact_dir=str(artifact),
    )
    imports = trading.store.list_legacy_job_imports()

    assert len(imports) == 1
    run_id = imported["run_id"]
    client = TestClient(create_app(engine=engine))
    detail = client.get(f"/api/trading/backtest-runs/{run_id}/detail")
    assert detail.status_code == 200
    assert detail.json()["origin"] == "legacy_import"
    assert detail.json()["detail"]["signals"][0]["instrument"] == "600000.SSE"
    # Re-registering the historical mapping is idempotent.
    trading.store.import_legacy_backtest_job(
        job_id,
        instance_id=instance_id,
        request=params,
        result={"changed": True},
        artifact_dir=str(artifact),
    )
    assert len(engine.get_system("trading").store.list_legacy_job_imports()) == 1


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


def test_formal_deployment_and_operator_routes_cover_success_and_validation(
    engine,
    monkeypatch,
) -> None:  # noqa: ANN001
    trading = engine.get_system("trading")
    client = TestClient(create_app(engine=engine))
    headers = _operator_headers(engine)
    instance_id = f"deployment-routes-{uuid4().hex}"
    trading.create_instance({
        "instance_id": instance_id,
        "strategy_id": "sma_filter",
        "params": {"window": 5},
        "universe": ["600000.SSE"],
        "data_policy": {"history_window": 6, "data_version": "portal-v10"},
    })
    assert trading.validate_instance(instance_id)["ok"] is True

    monkeypatch.setattr(
        trading,
        "audit_events",
        lambda limit=200: [{"action": "test", "limit": limit}],
    )

    configured = client.put(
        f"/api/trading/deployments/{instance_id}",
        json={"run_mode": "paper"},
    )
    assert configured.status_code == 200
    assert configured.json()["configuration"]["run_mode"] == "paper"
    deployment = client.get(f"/api/trading/deployments/{instance_id}")
    assert deployment.json()["runtime"]["observed_state"] == "ready"

    monkeypatch.setattr(
        trading,
        "lifecycle_action",
        lambda requested, action: {
            "instance_id": requested,
            "observed_state": action,
        },
    )
    started = client.post(
        f"/api/trading/deployments/{instance_id}/start",
        json={},
    )
    assert started.status_code == 200
    assert started.json()["observed_state"] == "start"
    assert client.post(
        f"/api/trading/deployments/{instance_id}/promote", json={"to": "paper"}
    ).status_code == 404
    assert client.post(
        f"/api/trading/deployments/{instance_id}/authorize-live",
        json={"account_id": "sim"},
    ).status_code == 404
    assert client.get("/api/trading/audit-events?limit=7").json()["events"][0]["limit"] == 7
    assert client.post(
        "/api/trading/kill-switches/global/all/invalid",
        json={"reason": "invalid action"},
        headers=headers,
    ).status_code == 400
    assert client.post(
        "/api/trading/kill-switches/global/all/engage",
        json={},
        headers=headers,
    ).status_code == 400


def test_trading_write_api_requires_operator_token(engine) -> None:
    client = TestClient(create_app(engine=engine))
    response = client.post(
        "/api/trading/strategy-instances",
        json={"instance_id": "unauthorized", "strategy_id": "dual_ma"},
    )
    assert response.status_code == 401


def test_removed_migration_write_endpoints_are_not_dispatchable(engine) -> None:
    trading = engine.get_system("trading")
    client = TestClient(create_app(engine=engine))
    headers = _operator_headers(engine)
    instance_id = f"migration-{uuid4().hex}"
    assert client.post(
        "/api/trading/strategy-instances",
        json={
            "instance_id": instance_id,
            "strategy_id": "sma_filter",
            "params": {"window": 5},
            "universe": ["600000.SSE"],
        },
        headers=headers,
    ).status_code == 200

    before = {
        row["entrypoint"]: row["call_count"]
        for row in trading.compatibility_status()["entrypoints"]
    }
    probes = (
        ("POST", f"/api/trading/strategy-instances/{instance_id}/backtest"),
        ("POST", f"/api/trading/stage-runs/{instance_id}/paper/start"),
        ("POST", "/api/trading/stage-runs/missing/finish"),
        ("POST", f"/api/trading/stage-runs/{instance_id}/paper/evaluate"),
        ("POST", f"/api/trading/deployments/{instance_id}/promote"),
        ("POST", f"/api/trading/deployments/{instance_id}/authorize-live"),
        ("GET", f"/api/trading/deployments/{instance_id}/qualification"),
        ("POST", f"/api/trading/deployments/{instance_id}/parity-runs"),
        ("GET", f"/api/trading/deployments/{instance_id}/execution-binding"),
        ("PUT", f"/api/trading/deployments/{instance_id}/execution-binding"),
        ("POST", f"/api/trading/deployments/{instance_id}/unknown-action"),
    )
    for method, path in probes:
        response = client.request(method, path, json={}, headers=headers)
        assert response.status_code in {404, 405}, (path, response.text)

    stage_runs = client.get(f"/api/trading/deployments/{instance_id}/stage-runs")
    assert stage_runs.status_code == 404

    after = {
        row["entrypoint"]: row["call_count"]
        for row in trading.compatibility_status()["entrypoints"]
    }
    assert after == before
    removed = {
        row["entrypoint"]: row["status"]
        for row in trading.compatibility_status()["entrypoints"]
    }
    assert removed["POST /api/trading/strategy-instances/{id}/backtest"] == "removed"
    assert removed["POST /api/trading/stage-runs/*"] == "removed"
    assert removed["POST /api/trading/deployments/{id}/{action}"] == "removed"


def test_trading_decision_comparison_and_read_only_uat_detail_routes(
    engine, monkeypatch,
) -> None:  # noqa: ANN001
    trading = engine.get_system("trading")
    instance_id = f"comparison-api-{uuid4().hex}"
    trading.create_instance({
        "instance_id": instance_id,
        "strategy_id": "sma_filter",
        "params": {"window": 5},
        "universe": ["600000.SSE"],
    })
    comparison = {
        "comparison_id": "comparison-1", "instance_id": instance_id,
        "status": "passed", "results": [],
    }
    monkeypatch.setattr(
        trading, "compare_decisions", lambda _instance_id, _payload: comparison,
    )
    monkeypatch.setattr(
        trading, "get_decision_comparison", lambda _comparison_id: comparison,
    )
    monkeypatch.setattr(
        trading, "list_decision_comparisons", lambda _instance_id: [comparison],
    )
    client = TestClient(create_app(engine=engine))

    started = client.post(
        f"/api/trading/deployments/{instance_id}/decision-comparisons",
        json={
            "left_mode": "replay", "left_run_id": "replay",
            "right_mode": "shadow", "right_run_id": "shadow",
        },
    )
    assert started.status_code == 200
    assert client.get(
        "/api/trading/decision-comparisons/comparison-1"
    ).json()["status"] == "passed"
    assert client.get(
        f"/api/trading/deployments/{instance_id}/decision-comparisons"
    ).json()["comparisons"][0]["comparison_id"] == "comparison-1"
    missing_uat = client.get("/api/trading/broker-uat-runs/missing")
    assert missing_uat.status_code == 404


def test_new_read_only_trading_routes_convert_internal_failures_to_http_errors(
    engine,
    monkeypatch,
) -> None:  # noqa: ANN001
    trading = engine.get_system("trading")
    client = TestClient(create_app(engine=engine))

    def fail(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ValueError("injected read failure")

    for method, path in (
        ("compatibility_status", "/api/trading/compatibility"),
        ("list_broker_uat_runs", "/api/trading/broker-uat-runs"),
        ("get_decision_comparison", "/api/trading/decision-comparisons/missing"),
        ("deployment_diagnostics", "/api/trading/deployments/missing/diagnostics"),
    ):
        original = getattr(trading, method)
        monkeypatch.setattr(trading, method, fail)
        response = client.get(path)
        assert response.status_code == 400
        monkeypatch.setattr(trading, method, original)


def test_deployment_routes_require_a_loopback_portal_boundary(
    engine, monkeypatch,
) -> None:
    client = TestClient(create_app(engine=engine, portal_host="0.0.0.0"))
    assert client.get("/api/trading/deployments").status_code == 401
    assert client.get(
        "/api/trading/deployments/missing/diagnostics"
    ).status_code == 401

    monkeypatch.setenv("ALPHAPILOT_PORTAL_BIND_HOST", "0.0.0.0")
    reload_client = TestClient(create_app(engine=engine))
    assert reload_client.get("/api/trading/deployments").status_code == 401


def test_portal_startup_does_not_execute_removed_timing_job_importer(
    engine,
    isolated_env,
) -> None:  # noqa: ANN001
    unrelated = isolated_env.portal_job_root / "unrelated"
    unrelated.mkdir(parents=True)
    (unrelated / "job.json").write_text(json.dumps({
        "job_id": "unrelated", "kind": "market_download", "status": "succeeded",
    }), encoding="utf-8")
    corrupt = isolated_env.portal_job_root / "corrupt-timing"
    corrupt.mkdir(parents=True)
    (corrupt / "job.json").write_text(json.dumps({
        "job_id": "corrupt-timing", "kind": "timing_backtest", "status": "succeeded",
    }), encoding="utf-8")

    TestClient(create_app(engine=engine))

    assert engine.get_system("trading").store.list_legacy_job_imports() == []
    assert (corrupt / "job.json").is_file()

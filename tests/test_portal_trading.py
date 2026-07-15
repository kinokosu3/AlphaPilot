from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from alphapilot.modules.portal.api import create_app, _import_completed_timing_jobs


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
    response = client.get("/api/timing/strategies", headers={"X-Request-ID": "legacy-call"})
    body = response.json()
    assert body["deprecated"] is True
    assert body["replacement"] == "/api/trading/strategy-definitions"
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Sunset"] == "Thu, 31 Dec 2026 00:00:00 GMT"
    assert 'rel="successor-version"' in response.headers["Link"]
    compatibility = client.get("/api/trading/compatibility").json()
    assert compatibility["schema_version"] == 8
    assert compatibility["environment_id"]
    catalog = {
        row["entrypoint"]: row for row in compatibility["entrypoints"]
    }
    assert catalog["GET /api/timing/strategies"]["call_count"] >= 1


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


def test_parity_write_is_authenticated_and_qualification_is_runtime_derived(engine) -> None:
    client = TestClient(create_app(engine=engine))
    headers = _operator_headers(engine)
    instance_id = f"qualification-{uuid4().hex}"
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

    qualification = client.get(
        f"/api/trading/deployments/{instance_id}/qualification"
    )
    assert qualification.status_code == 200
    assert qualification.json()["eligible_for_live_authorization"] is False
    assert qualification.json()["paper"]["trading_sessions"] == 0

    unauthenticated = client.post(
        f"/api/trading/deployments/{instance_id}/parity-runs",
        json={"replay_run_id": "missing", "shadow_stage_run_id": "missing"},
    )
    assert unauthenticated.status_code == 401
    invalid = client.post(
        f"/api/trading/deployments/{instance_id}/parity-runs",
        json={},
        headers=headers,
    )
    assert invalid.status_code == 400
    assert "required" in invalid.json()["detail"]


def test_completed_legacy_timing_job_is_imported_into_formal_read_only_detail(
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

    client = TestClient(create_app(engine=engine))
    import_report = _import_completed_timing_jobs(engine)
    imports = engine.get_system("trading").store.list_legacy_job_imports()

    assert import_report["skipped"] == []
    assert len(imports) == 1
    run_id = imports[0]["run_id"]
    detail = client.get(f"/api/trading/backtest-runs/{run_id}/detail")
    assert detail.status_code == 200
    assert detail.json()["origin"] == "legacy_import"
    assert detail.json()["detail"]["signals"][0]["instrument"] == "600000.SSE"
    # Startup import is idempotent and cannot overwrite the original result.
    TestClient(create_app(engine=engine))
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


def test_trading_write_api_requires_operator_token(engine) -> None:
    client = TestClient(create_app(engine=engine))
    response = client.post(
        "/api/trading/strategy-instances",
        json={"instance_id": "unauthorized", "strategy_id": "dual_ma"},
    )
    assert response.status_code == 401


def test_trading_migration_endpoints_are_audited_and_legacy_stage_counts_are_not_forgeable(
    engine,
    monkeypatch,
) -> None:  # noqa: ANN001
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

    monkeypatch.setattr(
        trading,
        "backtest_instance",
        lambda _instance_id, _payload: SimpleNamespace(
            summary={"total_return": 0.1}, artifact_dir="/tmp/formal-replay",
        ),
    )
    sync = client.post(
        f"/api/trading/strategy-instances/{instance_id}/backtest",
        json={"reason": "compatibility verification"},
        headers=headers,
    )
    assert sync.status_code == 200
    assert sync.headers["Deprecation"] == "true"
    trading.store.record_stage(instance_id, "replay", passed=True)
    trading.store.promote(instance_id, "paper")

    started = client.post(
        f"/api/trading/stage-runs/{instance_id}/paper/start", headers=headers,
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    finished = client.post(
        f"/api/trading/stage-runs/{run_id}/finish",
        json={"trading_sessions": 999, "status": "completed"},
        headers=headers,
    )
    assert finished.status_code == 200
    assert finished.json()["trading_sessions"] == 0
    evaluated = client.post(
        f"/api/trading/stage-runs/{instance_id}/paper/evaluate", headers=headers,
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["passed"] is False

    fallback = client.post(
        f"/api/trading/deployments/{instance_id}/unknown-action",
        json={},
        headers=headers,
    )
    assert fallback.status_code == 400
    compatibility = client.get("/api/trading/compatibility").json()
    counts = {row["entrypoint"]: row["call_count"] for row in compatibility["entrypoints"]}
    assert counts["POST /api/trading/strategy-instances/{id}/backtest"] >= 1
    assert counts["POST /api/trading/stage-runs/*"] >= 3
    assert counts["POST /api/trading/deployments/{id}/{action}"] >= 1


def test_trading_parity_and_read_only_uat_detail_routes(engine, monkeypatch) -> None:  # noqa: ANN001
    trading = engine.get_system("trading")
    instance_id = f"parity-api-{uuid4().hex}"
    trading.create_instance({
        "instance_id": instance_id,
        "strategy_id": "sma_filter",
        "params": {"window": 5},
        "universe": ["600000.SSE"],
    })
    parity = {
        "parity_run_id": "parity-1", "instance_id": instance_id,
        "status": "passed", "results": [],
    }
    monkeypatch.setattr(trading, "start_parity_run", lambda _instance_id, _payload: parity)
    monkeypatch.setattr(trading, "get_parity_run", lambda _run_id: parity)
    client = TestClient(create_app(engine=engine))
    headers = _operator_headers(engine)

    started = client.post(
        f"/api/trading/deployments/{instance_id}/parity-runs",
        json={"replay_run_id": "replay", "shadow_stage_run_id": "shadow"},
        headers=headers,
    )
    assert started.status_code == 200
    assert client.get("/api/trading/parity-runs/parity-1").json()["status"] == "passed"
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
        ("get_parity_run", "/api/trading/parity-runs/missing"),
        ("qualification", "/api/trading/deployments/missing/qualification"),
    ):
        original = getattr(trading, method)
        monkeypatch.setattr(trading, method, fail)
        response = client.get(path)
        assert response.status_code == 400
        monkeypatch.setattr(trading, method, original)


def test_completed_timing_job_import_skips_unrelated_and_corrupt_jobs(
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

    report = _import_completed_timing_jobs(engine)

    assert report["imported"] == 0
    assert report["skipped"][0]["job_id"] == "corrupt-timing"

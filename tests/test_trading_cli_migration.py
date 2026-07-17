from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alphapilot.modules.trading.module import TradingModule, _object, _symbols


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def audit(self, _operator: Any, **payload: Any) -> None:
        self.events.append(payload)


class _TradingCLIStore:
    def get_instance(self, instance_id: str) -> dict[str, Any]:
        return {"instance_id": instance_id, "config_hash": "config-hash"}

    def get_runtime_state(self, instance_id: str) -> dict[str, Any]:
        return {"instance_id": instance_id, "account_id": "account", "broker": "xtp"}


class _TradingCLISystem:
    def __init__(self) -> None:
        self.operator_auth = _Audit()
        self.store = _TradingCLIStore()
        self.fail_uat = False
        self.calls: list[tuple[str, Any]] = []

    def _result(self, name: str, payload: Any = None) -> dict[str, Any]:
        self.calls.append((name, payload))
        return {"name": name, "payload": payload}

    def list_definitions(self):  # noqa: ANN201
        return self._result("definitions")

    def list_portfolio_policy_definitions(self):  # noqa: ANN201
        return self._result("policies")

    def list_instances(self):  # noqa: ANN201
        return [self._result("instance")]

    def create_instance(self, payload):  # noqa: ANN001, ANN201
        return self._result("create", payload)

    def create_instance_from_research_asset(self, payload):  # noqa: ANN001, ANN201
        return self._result("research", payload)

    def validate_instance(self, instance_id):  # noqa: ANN001, ANN201
        return self._result("validate", instance_id)

    def start_stage_run(self, instance_id, stage):  # noqa: ANN001, ANN201
        return self._result("stage-start", {"instance_id": instance_id, "stage": stage})

    def finish_stage_run(self, run_id, payload):  # noqa: ANN001, ANN201
        return self._result("stage-finish", {"run_id": run_id, **payload})

    def evaluate_stage(self, instance_id, stage):  # noqa: ANN001, ANN201
        return self._result("stage-evaluate", {"instance_id": instance_id, "stage": stage})

    def preview_instance(self, instance_id, payload):  # noqa: ANN001, ANN201
        self.calls.append(("preview", {"instance_id": instance_id, "payload": payload}))
        return {"signal": {"payload": dict(payload.get("signal_payload") or {})}}

    def start_backtest_run(self, instance_id, payload):  # noqa: ANN001, ANN201
        self.calls.append(("backtest", {"instance_id": instance_id, "payload": payload}))
        return {"run_id": "run-1", "status": "queued"}

    def get_backtest_run(self, run_id, detail=False):  # noqa: ANN001, ANN201
        return {"run_id": run_id, "status": "completed", "detail": bool(detail)}

    def cancel_backtest_run(self, run_id):  # noqa: ANN001, ANN201
        return self._result("cancel", run_id)

    def promote(self, instance_id, payload):  # noqa: ANN001, ANN201
        self.calls.append(("promote", payload))
        return {"instance_id": instance_id, "config_hash": "config-hash"}

    def create_operator_token(self, operator_id, **payload):  # noqa: ANN001, ANN201
        return self._result("token", {"operator_id": operator_id, **payload})

    def authorize_live(self, instance_id, payload, operator):  # noqa: ANN001, ANN201
        return self._result("authorize", {
            "instance_id": instance_id, "payload": payload,
            "operator_id": operator.operator_id,
        })

    def lifecycle_action(self, instance_id, action):  # noqa: ANN001, ANN201
        return {"ok": True, "instance_id": instance_id, "action": action}

    def deployment(self, instance_id):  # noqa: ANN001, ANN201
        return self._result("deployment", instance_id)

    def set_kill_switch(self, scope_type, scope_id, **payload):  # noqa: ANN001, ANN201
        return self._result("kill", {"scope_type": scope_type, "scope_id": scope_id, **payload})

    def audit_events(self, **payload):  # noqa: ANN001, ANN201
        return [self._result("audit", payload)]

    def import_compatibility_environment_report(self, payload):  # noqa: ANN001, ANN201
        return self._result("import", payload)

    def set_compatibility_cutoff(self):  # noqa: ANN201
        return {
            "local_environment_report": {
                "ready": True, "environment_id": "env-a", "evidence_hash": "hash",
            },
        }

    def compatibility_status(self):  # noqa: ANN201
        return {"local_environment_report": {"ready": False}}

    def removal_check(self, instance_id):  # noqa: ANN001, ANN201
        return self._result("removal", instance_id)

    def start_parity_run(self, instance_id, payload):  # noqa: ANN001, ANN201
        return self._result("parity-start", {"instance_id": instance_id, **payload})

    def get_parity_run(self, run_id):  # noqa: ANN001, ANN201
        return self._result("parity-status", run_id)

    def qualification(self, instance_id, **payload):  # noqa: ANN001, ANN201
        return self._result("qualification", {"instance_id": instance_id, **payload})

    def start_broker_uat(self, payload):  # noqa: ANN001, ANN201
        if self.fail_uat:
            raise RuntimeError("preflight failed")
        return {
            "run_id": "uat-1", "status": "restart_required", "broker": payload["broker"],
            "symbol": payload["symbol"],
        }

    def broker_uat_preflight(self, payload):  # noqa: ANN001, ANN201
        return {"broker": payload["broker"], "candidates": [], "query_only": True}

    def get_broker_uat_run(self, run_id):  # noqa: ANN001, ANN201
        return {"run_id": run_id, "status": "restart_required"}

    def list_broker_uat_runs(self, broker):  # noqa: ANN001, ANN201
        return [{"run_id": "uat-1", "broker": broker}]

    def resume_broker_uat(self, run_id, payload):  # noqa: ANN001, ANN201
        return {"run_id": run_id, "status": "passed", "broker": "xtp", "payload": payload}

    def abort_broker_uat(self, run_id, payload):  # noqa: ANN001, ANN201
        return {"run_id": run_id, "status": "aborted", "broker": "xtp", "payload": payload}


def _module(system: _TradingCLISystem) -> TradingModule:
    module = TradingModule()
    module.context = SimpleNamespace(system=lambda name: system if name == "trading" else None)
    return module


def test_trading_cli_formal_surface_and_file_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    system = _TradingCLISystem()
    module = _module(system)
    monkeypatch.setattr("alphapilot.modules.trading.module.time.sleep", lambda _seconds: None)

    assert _object(None) == {}
    assert _object({"a": 1}) == {"a": 1}
    assert _symbols(None) == []
    assert _symbols(["600000.SSE", " "]) == ["600000.SSE"]
    with pytest.raises(ValueError, match="JSON object"):
        _object("[]")

    module.trading_definitions()
    module.trading_policies()
    module.trading_instances()
    created = module.trading_instance_create(
        "alpha", "dual_ma", "600000.SSE 510300.SSE",
        params='{"short_window": 5}', data_policy='{"data_version": "v1"}',
        portfolio_policy='{"policy_id": "timing_fixed_exposure"}',
    )
    assert created["payload"]["universe"] == ["600000.SSE", "510300.SSE"]
    module.trading_instance_from_research(
        "selection", "research-a", ["600000.SSE"], '{"policy_id":"selection"}',
    )
    module.trading_instance_validate("alpha")
    module.trading_stage_start("alpha", "paper")
    finished_stage = module.trading_stage_finish(
        "stage-1", 1, metrics='{"unresolved_errors": 0}',
    )
    assert finished_stage["payload"]["metrics"]["unresolved_errors"] == 0
    module.trading_stage_evaluate("alpha", "paper")

    json_path = tmp_path / "preview.json"
    module.trading_preview("alpha", '{"signal_payload":{"score":1}}', str(json_path))
    assert json.loads(json_path.read_text(encoding="utf-8"))["signal"]["payload"] == {"score": 1}
    score_path = tmp_path / "scores.csv"
    module.trading_preview(
        "alpha", {"signal_payload": {"scores": {"B": 2, "A": 1}}},
        str(score_path), "csv",
    )
    assert score_path.read_text(encoding="utf-8").splitlines()[1].startswith("A,")
    states_path = tmp_path / "states.csv"
    module.trading_preview(
        "alpha", {"signal_payload": {"states": {"A": 1}}}, str(states_path), "csv",
    )
    payload_path = tmp_path / "payload.csv"
    module.trading_preview(
        "alpha", {"signal_payload": {"state": 1}}, str(payload_path), "csv",
    )
    with pytest.raises(ValueError, match="json or csv"):
        module.trading_preview("alpha", {}, str(tmp_path / "bad.txt"), "text")

    assert module.trading_backtest("alpha")["status"] == "queued"
    completed = module.trading_backtest("alpha", wait=True, output_dir=str(tmp_path / "run"))
    assert completed["status"] == "completed"
    assert module.trading_backtest_status("run-1", detail=True)["detail"] is True
    module.trading_backtest_cancel("run-1")
    module.trading_promote("alpha", "paper", account_id="account", broker="xtp")
    module.trading_operator_token("operator", label="release", expires_in_days=1)
    module.trading_authorize_live(
        "alpha", "account", "xtp", "approved", "operator",
        baseline_positions='{"600000.SSE": 100}',
    )
    for action in ("start", "pause", "reconcile", "resume", "stop"):
        getattr(module, f"trading_{action}")("alpha")
    module.trading_status("alpha")
    with pytest.raises(ValueError, match="operator reason"):
        module.trading_kill_switch("global", "*", True, "")
    module.trading_kill_switch("global", "*", True, "test")
    module.trading_audit(limit=5)

    imported_path = tmp_path / "environment.json"
    imported_path.write_text('{"environment_id":"env-b"}', encoding="utf-8")
    exported_path = tmp_path / "exported.json"
    compatibility = module.trading_compatibility(
        set_cutoff=True, export_path=str(exported_path), import_path=str(imported_path),
    )
    assert compatibility["export_path"] == str(exported_path)
    assert compatibility["imported_environment"]["name"] == "import"
    with pytest.raises(ValueError, match="migration cutoff"):
        module.trading_compatibility(export_path=str(tmp_path / "blocked.json"))

    module.trading_removal_check("alpha")
    module.trading_parity_start("alpha", "replay", "shadow")
    module.trading_parity_status("parity")
    module.trading_qualification("alpha")
    started = module.trading_broker_uat_start(
        "xtp", "600000.SSE", "buy", 100, 10, 1500,
        "I_UNDERSTAND_REAL_ORDERS",
    )
    assert started["status"] == "restart_required"
    assert module.trading_broker_uat_preflight(
        "xtp", "600000.SSE 510300.SSE", 20000, 5,
    )["query_only"] is True
    module.trading_broker_uat_status(run_id="uat-1")
    module.trading_broker_uat_status(broker="xtp")
    module.trading_broker_uat_resume("uat-1", "I_UNDERSTAND_REAL_ORDERS")
    module.trading_broker_uat_abort(
        "uat-1", "I_UNDERSTAND_REAL_ORDERS", "operator stop",
    )
    system.fail_uat = True
    with pytest.raises(RuntimeError, match="preflight failed"):
        module.trading_broker_uat_start(
            "xtp", "600000.SSE", "buy", 100, 10, 1500,
            "I_UNDERSTAND_REAL_ORDERS",
        )

    assert set(module.commands()) >= {
        "trading_preview", "trading_backtest", "trading_broker_uat_start",
        "trading_broker_uat_preflight",
        "trading_removal_check",
    }
    assert len(system.operator_auth.events) >= 10
    assert capsys.readouterr().out

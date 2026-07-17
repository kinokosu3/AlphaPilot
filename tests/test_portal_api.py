from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from alphapilot.modules.portal import jobs
from alphapilot.modules.portal.api import create_app
from alphapilot.systems.notify import config as notify_config
from alphapilot.systems.notify.commands import authorize, dispatch_text, parse_command
from alphapilot.systems.notify.inbound import InboundMessage


class FakeDataSystem:
    def __init__(self) -> None:
        self.actions: list[tuple[str, dict[str, Any]]] = []

    def list_symbols(self, *_args: Any, **_kwargs: Any) -> dict[str, list[str]]:
        return {"none": ["sh600000"], "backward": ["sh600000"]}

    def get_universe(self, **_options: Any) -> list[str]:
        return ["sh600000", "sz000001"]

    def run_action(self, action: str, **options: Any) -> dict[str, Any]:
        self.actions.append((action, options))
        if action == "bad":
            raise ValueError("bad action")
        return {"action": action, "options": options}

    def delete_symbol(self, symbol: str, **options: Any) -> dict[str, Any]:
        return {"symbol": symbol, "deleted": True, "options": options}

    def refresh_symbol(self, symbol: str, **options: Any) -> dict[str, Any]:
        return {"symbol": symbol, "refreshed": True, "options": options}

    def trim_symbol(self, symbol: str, **options: Any) -> dict[str, Any]:
        return {"symbol": symbol, "trimmed": True, "options": options}

    def apply_adjust_symbol(self, symbol: str, **options: Any) -> dict[str, Any]:
        return {"symbol": symbol, "adjusted": True, "options": options}


@dataclass
class FakeValidation:
    acceptable: bool
    code: str = "ok"
    message: str = "ok"
    details: dict[str, Any] | None = None


class FakeFactorDb:
    supports_categories = True

    def __init__(self) -> None:
        self.categories = ["momentum"]
        self.factors: list[dict[str, Any]] = []

    def list_categories(self) -> list[str]:
        return self.categories

    def save(self, output_path: str) -> None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("factor_name,factor_expression\n", encoding="utf-8")

    def create_category(self, name: str) -> bool:
        self.categories.append(name)
        return True

    def add_factors_to_category(self, factor_names: list[str], category: str) -> dict[str, Any]:
        return {"category": category, "requested": factor_names, "changed": factor_names, "unchanged": [], "missing": []}


class FakeFactorSystem:
    def __init__(self) -> None:
        self.database = FakeFactorDb()

    def list_factors(self) -> list[dict[str, Any]]:
        return self.database.factors

    def add_factor(self, factor_name: str, factor_expression: str, categories: list[str] | None = None) -> FakeValidation:
        self.database.factors.append(
            {"factor_name": factor_name, "factor_expression": factor_expression, "categories": categories or []}
        )
        return FakeValidation(True, details={"factor_name": factor_name})

    def validate_expression(self, expression: str) -> FakeValidation:
        return FakeValidation(bool(expression.strip()))

    def delete_factor(self, factor_name: str) -> bool:
        before = len(self.database.factors)
        self.database.factors = [f for f in self.database.factors if f["factor_name"] != factor_name]
        return len(self.database.factors) != before

    def import_factors(self, source: Any, *, kind: str = "csv") -> dict[str, Any]:
        return {"kind": kind, "source": str(source), "imported": 1}

    def create_category(self, name: str) -> bool:
        return self.database.create_category(name)

    def rename_category(self, old_name: str, new_name: str) -> bool:
        self.database.categories = [new_name if item == old_name else item for item in self.database.categories]
        return True

    def delete_category(self, name: str) -> bool:
        self.database.categories = [item for item in self.database.categories if item != name]
        return True

    def add_factors_to_category(self, factor_names: list[str], category: str) -> dict[str, Any]:
        return self.database.add_factors_to_category(factor_names, category)

    def remove_factors_from_category(self, factor_names: list[str], category: str) -> dict[str, Any]:
        return {"category": category, "requested": factor_names, "changed": factor_names, "unchanged": [], "missing": []}

    def set_factor_categories(self, factor_name: str, categories: list[str]) -> bool:
        for row in self.database.factors:
            if row["factor_name"] == factor_name:
                row["categories"] = categories
                return True
        return False

    def factors_in_category(self, name: str) -> list[dict[str, Any]]:
        return [row for row in self.database.factors if name in row.get("categories", [])]

    def export_category_csv(self, name: str, _output_path: Any) -> int:
        return len(self.factors_in_category(name))


class FakeStrategyDb:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def list_strategies(self) -> list[str]:
        return sorted(self.items)

    def save(self, strategy_name: str, params: dict[str, Any]) -> None:
        self.items[strategy_name] = {"strategy_name": strategy_name, **params}

    def load(self, strategy_name: str) -> dict[str, Any] | None:
        return self.items.get(strategy_name)


class FakeStrategySystem:
    def __init__(self) -> None:
        self.param_database = FakeStrategyDb()

    def get_strategy(self, strategy_name: str) -> dict[str, Any] | None:
        return self.param_database.load(strategy_name)

    def import_strategy(self, source: Any, *, kind: str = "pdf") -> dict[str, Any]:
        self.param_database.save("imported", {"source": str(source), "kind": kind})
        return {"strategy_name": "imported", "kind": kind}

    def delete_strategy(self, strategy_name: str) -> bool:
        return self.param_database.items.pop(strategy_name, None) is not None


class FakeBacktestResults:
    def list_runs(self) -> list[dict[str, Any]]:
        return []


class FakeBacktestSystem:
    results = FakeBacktestResults()

    def delete_workspace(self, workspace_id: str) -> bool:
        return workspace_id == "run1"


class FakeConfig:
    class data:
        qlib_data_dir = "qlib"
        raw_data_dir = "raw"

    class factor:
        zoo_dir = "zoo"

    class strategy:
        param_dir = "strategies"

    class backtest:
        workspace_root = "workspaces"

    def summary(self) -> str:
        return "fake"


class FakeEngine:
    def __init__(self) -> None:
        self.config = FakeConfig()
        self.systems = {
            "data": FakeDataSystem(),
            "factor": FakeFactorSystem(),
            "strategy": FakeStrategySystem(),
            "backtest": FakeBacktestSystem(),
        }
        self.modules = {}

    def get_system(self, name: str) -> Any:
        return self.systems[name]

    def get_module(self, name: str) -> Any:
        return self.modules[name]


def client(tmp_path: Path, monkeypatch) -> TestClient:  # noqa: ANN001
    monkeypatch.setenv("ALPHAPILOT_IMPORTANT_DATA_DIR", str(tmp_path / "important_data"))
    monkeypatch.setenv("ALPHAPILOT_PORTAL_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setenv("ALPHAPILOT_PORTAL_SCHEDULE_ROOT", str(tmp_path / "schedules"))
    monkeypatch.setenv("ALPHAPILOT_PORTAL_ENV_PATH", str(tmp_path / "portal-env.json"))
    monkeypatch.setenv("ALPHAPILOT_NOTIFY_CREDENTIALS_PATH", str(tmp_path / "notify.json"))
    monkeypatch.setenv("ALPHAPILOT_NOTIFY_COMMAND_ROOT", str(tmp_path / "notify-commands"))
    engine = FakeEngine()
    engine.config.log_dir = tmp_path / "log"
    return TestClient(create_app(engine=engine))


def test_status_and_factor_crud(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)

    status = c.get("/api/status")
    assert status.status_code == 200
    assert status.json()["metrics"]["symbols"] == 1

    created = c.post(
        "/api/factors",
        json={"factor_name": "mom", "factor_expression": "$close", "categories": ["momentum"]},
    )
    assert created.status_code == 200
    assert created.json()["acceptable"] is True

    factors = c.get("/api/factors").json()
    assert factors["factors"][0]["factor_name"] == "mom"
    assert factors["categories"] == ["momentum"]

    deleted = c.delete("/api/factors/mom")
    assert deleted.json()["deleted"] is True


def test_schedule_crud(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)

    created = c.post(
        "/api/schedules",
        json={"name": "download", "kind": "data", "time": "18:00", "kwargs": {"action": "download"}},
    )
    assert created.status_code == 200
    sid = created.json()["schedule_id"]

    patched = c.patch(f"/api/schedules/{sid}", json={"enabled": False})
    assert patched.json()["enabled"] is False

    listed = c.get("/api/schedules")
    assert len(listed.json()) == 1

    deleted = c.delete(f"/api/schedules/{sid}")
    assert deleted.json()["deleted"] is True


def test_portal_env_api_masks_and_saves(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)

    saved = c.patch(
        "/api/portal/env",
        json={"values": {"OPENAI_API_KEY": "secret-key", "CHAT_MODEL": "qwen-plus", "ALPHAPILOT_PICKLE_CACHE_ENABLED": "false"}},
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["values"]["OPENAI_API_KEY"] == "********"
    assert body["values"]["CHAT_MODEL"] == "qwen-plus"
    assert body["values"]["ALPHAPILOT_PICKLE_CACHE_ENABLED"] == "false"

    kept = c.patch("/api/portal/env", json={"values": {"OPENAI_API_KEY": "", "CHAT_MODEL": ""}})
    assert kept.status_code == 200
    body = kept.json()
    assert body["values"]["OPENAI_API_KEY"] == "********"
    assert "CHAT_MODEL" not in body["values"]


def test_portal_env_rejects_unknown_key(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    response = c.patch("/api/portal/env", json={"values": {"BAD_KEY": "x"}})
    assert response.status_code == 400


def test_portal_env_precedence(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from alphapilot.modules.portal.env_config import apply_portal_env, save_env_values

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPHAPILOT_PORTAL_ENV_PATH", str(tmp_path / "env.json"))
    (tmp_path / ".env").write_text("CHAT_MODEL=from-dotenv\nOPENAI_BASE_URL=https://dotenv.example\n", encoding="utf-8")
    save_env_values({"CHAT_MODEL": "from-portal", "OPENAI_BASE_URL": "https://portal.example"})

    target = {"CHAT_MODEL": "from-dotenv", "OPENAI_BASE_URL": "https://system.example"}
    applied = apply_portal_env(target)
    assert target["CHAT_MODEL"] == "from-portal"
    assert target["OPENAI_BASE_URL"] == "https://system.example"
    assert applied == {"CHAT_MODEL": "from-portal"}


def test_log_cleanup_api_previews_and_deletes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    log_root = tmp_path / "log"
    empty = log_root / "empty-session"
    keep = log_root / "keep-session"
    empty.mkdir(parents=True)
    keep.mkdir(parents=True)
    (keep / "result.json").write_text("{}", encoding="utf-8")

    preview = c.post("/api/logs/cleanup", json={"log_dir": str(log_root)})

    assert preview.status_code == 200
    assert preview.json()["execute"] is False
    assert preview.json()["removed"] == 1
    assert preview.json()["paths"] == ["empty-session"]
    assert empty.exists()

    deleted = c.post("/api/logs/cleanup", json={"log_dir": str(log_root), "execute": True})

    assert deleted.status_code == 200
    assert deleted.json()["execute"] is True
    assert deleted.json()["removed"] == 1
    assert not empty.exists()
    assert keep.exists()


def test_portal_file_roots_reject_unconfigured_paths(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("date,close\n2026-01-01,1\n", encoding="utf-8")

    assert c.post("/api/logs/cleanup", json={"log_dir": str(outside), "execute": True}).status_code == 400
    assert c.get("/api/market/symbols", params={"data_dir": str(outside)}).status_code == 400
    assert c.get(
        "/api/market/kline", params={"data_dir": str(outside), "symbol": "secret"}
    ).status_code == 400
    assert c.get("/api/backtests", params={"workspace_root": str(outside)}).status_code == 400
    assert c.get("/api/backtests/leaderboards", params={"workspace_root": str(outside)}).status_code == 400
    assert c.get(
        "/api/backtests/leaderboard",
        params={"workspace_root": str(outside), "file": "secret_leaderboard.csv"},
    ).status_code == 400


def test_backtest_workspace_traversal_never_reaches_legacy_delete(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    called = False

    def unsafe_delete(_workspace_id: str) -> bool:
        nonlocal called
        called = True
        return True

    c.app.state.engine.systems["backtest"].delete_workspace = unsafe_delete
    response = c.delete("/api/backtests/%2E%2E")
    assert response.status_code == 400
    assert called is False


def test_schedule_id_path_traversal_is_rejected(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from alphapilot.modules.portal import schedules

    root = tmp_path / "schedules"
    root.mkdir()
    outside = tmp_path / "secret.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    for schedule_id in ("..", "../secret", str(outside)):
        with pytest.raises(ValueError, match="Invalid schedule id"):
            schedules.get_schedule(schedule_id, schedule_root=root)
        with pytest.raises(ValueError, match="Invalid schedule id"):
            schedules.delete_schedule(schedule_id, schedule_root=root)


def test_job_routes_are_json_wrappers(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)

    def fake_start(kind: str, kwargs: dict[str, Any], **_opts: Any) -> dict[str, Any]:
        return {"job_id": "j1", "kind": kind, "status": "running", "params": kwargs}

    monkeypatch.setattr(jobs, "start_job", fake_start)
    monkeypatch.setattr(jobs, "list_jobs", lambda **_opts: [{"job_id": "j1", "kind": "mine", "status": "running"}])
    monkeypatch.setattr(jobs, "read_log_tail", lambda job_id, **_opts: f"log:{job_id}")
    monkeypatch.setattr(jobs, "read_result", lambda job_id, **_opts: {"result": job_id})
    monkeypatch.setattr(jobs, "cancel_job", lambda job_id, **_opts: {"job_id": job_id, "status": "cancelled"})

    started = c.post("/api/jobs", json={"kind": "mine", "kwargs": {"step_n": 1}})
    assert started.json()["job_id"] == "j1"
    assert c.get("/api/jobs").json()[0]["status"] == "running"
    assert c.get("/api/jobs/j1/log").json()["log"] == "log:j1"
    assert c.get("/api/jobs/j1/result").json()["result"] == "j1"
    assert c.post("/api/jobs/j1/cancel").json()["status"] == "cancelled"


def test_notify_command_parse_auth_and_dispatch(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    client(tmp_path, monkeypatch)

    action = parse_command('/run mine {"step_n": 1, "notify": true}')
    assert action.action == "start_job"
    assert action.job_kind == "mine"
    assert action.kwargs == {"step_n": 1, "notify": True}

    data = parse_command("/data action=download source=baostock_cn all_market=true")
    assert data.job_kind == "data"
    assert data.kwargs == {"action": "download", "source": "baostock_cn", "all_market": True}

    message = InboundMessage(channel="telegram", text="/jobs", user_id="42", chat_id="100")
    ok, reason = authorize(message)
    assert ok is False
    assert "disabled" in reason

    notify_config.save_notify_config(
        {
            "telegram": {"receive_enabled": True, "allowed_user_ids": ["42"], "allowed_chat_ids": ["100"]},
            "feishu": {},
            "email": {},
            "options": {},
        }
    )
    ok, reason = authorize(message)
    assert ok is True
    assert reason == "allowed"

    started: list[tuple[str, dict[str, Any]]] = []

    def fake_start_job(kind: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        started.append((kind, kwargs))
        return {"job_id": "job1", "kind": kind, "status": "running", "result_summary": None}

    monkeypatch.setattr(jobs, "start_job", fake_start_job)
    result = dispatch_text('/run mine {"step_n": 1}', enforce_auth=False)
    assert result["ok"] is True
    assert started == [("mine", {"step_n": 1})]
    assert "job1" in result["reply"]


def test_notify_natural_language_confirm_and_api_masking(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    started: list[tuple[str, dict[str, Any]]] = []

    class FakeLLM:
        def chat_completion(self, *_args: Any, **_kwargs: Any) -> str:
            return '{"action":"start_job","job_kind":"factor_backtest","kwargs":{"factor_names":["mom"]},"summary":"run backtest","risk_level":"medium","requires_confirmation":true}'

    monkeypatch.setattr(
        jobs,
        "start_job",
        lambda kind, kwargs: started.append((kind, kwargs)) or {"job_id": "job2", "kind": kind, "status": "running"},
    )

    planned = dispatch_text("帮我回测 mom 因子", user_id="u1", chat_id="c1", llm_factory=lambda: FakeLLM())
    assert planned["ok"] is True
    assert "/confirm" in planned["reply"]
    confirm_id = planned["data"]["pending"]["id"]

    confirmed = dispatch_text(f"/confirm {confirm_id}", user_id="u1", chat_id="c1")
    assert confirmed["ok"] is True
    assert started == [("factor_backtest", {"factor_names": ["mom"]})]

    notify_config.save_notify_config(
        {
            "telegram": {"enabled": True, "bot_token": "secret-token", "chat_id": "100"},
            "feishu": {"secret": "feishu-secret"},
            "email": {},
            "options": {},
        }
    )
    body = c.get("/api/notify").json()
    assert body["config"]["telegram"]["bot_token"] == notify_config.MASKED_SECRET
    assert body["config"]["feishu"]["secret"] == notify_config.MASKED_SECRET

    body["config"]["telegram"]["chat_id"] = "200"
    saved = c.patch("/api/notify", json={"config": body["config"]})
    assert saved.status_code == 200
    private = notify_config.load_file_config()
    assert private["telegram"]["bot_token"] == "secret-token"
    assert private["telegram"]["chat_id"] == "200"

    response = c.post("/api/notify/commands/dispatch", json={"text": "/jobs"})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_data_action_error_response(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)

    ok = c.post("/api/data/actions", json={"action": "download", "options": {"start_date": "2020-01-01"}})
    assert ok.status_code == 200
    assert ok.json()["action"] == "download"

    bad = c.post("/api/data/actions", json={"action": "bad", "options": {}})
    assert bad.status_code == 400
    assert "bad action" in bad.json()["detail"]


def test_data_management_routes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)

    universe = c.get("/api/data/universe")
    assert universe.status_code == 200
    assert universe.json()["count"] == 2

    symbols = c.get("/api/data/symbols")
    assert symbols.json()["backward"] == ["sh600000"]

    refreshed = c.post("/api/data/symbols/refresh", json={"symbol": "sh600000", "options": {"start_date": "2024-01-01"}})
    assert refreshed.json()["refreshed"] is True


def test_factor_category_and_backtest_routes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    c.post("/api/factors", json={"factor_name": "mom", "factor_expression": "$close", "categories": ["momentum"]})

    created = c.post("/api/factors/categories", json={"name": "quality"})
    assert created.json()["created"] is True

    bulk = c.post("/api/factors/categories/bulk?op=add", json={"factor_names": ["mom"], "category": "quality"})
    assert bulk.json()["category"] == "quality"

    def fake_start(kind: str, kwargs: dict[str, Any], **_opts: Any) -> dict[str, Any]:
        assert kind == "factor_backtest"
        assert Path(str(kwargs["factor_path"])).exists()
        return {"job_id": "bt1", "kind": kind, "status": "running", "params": kwargs}

    monkeypatch.setattr(jobs, "start_job", fake_start)
    started = c.post("/api/factors/backtest", json={"factor_names": ["mom"], "options": {"scenario": "factor_backtest"}})
    assert started.json()["job_id"] == "bt1"


def test_portal_settings_and_extended_factor_strategy_routes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from alphapilot.modules.portal.api import _count_unique_symbols

    c = client(tmp_path, monkeypatch)
    assert _count_unique_symbols(["600000", "600000", " "]) == 1

    settings = c.patch(
        "/api/portal/settings",
        json={"host": "127.0.0.1", "port": 19902, "timezone": "Asia/Shanghai"},
    )
    assert settings.status_code == 200
    assert settings.json()["settings"]["port"] == 19902
    monkeypatch.setattr(
        "alphapilot.modules.portal.api.schedule_current_process_restart",
        lambda: {"pid": 42, "scheduled": True},
    )
    restarted = c.post("/api/portal/restart")
    assert restarted.json()["restart"]["scheduled"] is True
    assert c.get("/api/portal/env").status_code == 200

    c.post(
        "/api/factors",
        json={"factor_name": "mom", "factor_expression": "$close", "categories": ["momentum"]},
    )
    removed = c.post(
        "/api/factors/categories/bulk?op=remove",
        json={"factor_names": ["mom"], "category": "momentum"},
    )
    assert removed.json()["changed"] == ["mom"]
    updated = c.post(
        "/api/factors/categories/bulk?op=set",
        json={"name": "mom", "categories": ["quality"]},
    )
    assert updated.json()["updated"] is True
    exported = c.post(
        "/api/factors/categories/bulk?op=export",
        json={
            "category": "quality",
            "output_path": str(tmp_path / "important_data" / "factor_zoo" / "quality.csv"),
        },
    )
    assert exported.json()["count"] == 1
    invalid = c.post("/api/factors/categories/bulk?op=invalid", json={})
    assert invalid.status_code == 400

    def fake_start(kind: str, kwargs: dict[str, Any], **_opts: Any) -> dict[str, Any]:
        return {"job_id": "category-bt", "kind": kind, "params": kwargs}

    monkeypatch.setattr(jobs, "start_job", fake_start)
    category_run = c.post(
        "/api/factors/backtest",
        json={"category": "quality", "options": {"mode": "single_ic"}},
    )
    assert category_run.json()["job_id"] == "category-bt"
    assert c.post("/api/factors/backtest", json={"factor_names": ["missing"]}).status_code == 400

    strategy_system = c.app.state.engine.get_system("strategy")
    monkeypatch.setattr(
        strategy_system,
        "create_strategy_from_factors",
        lambda **payload: {"strategy_name": payload["strategy_name"], "saved": True},
        raising=False,
    )
    created = c.post(
        "/api/strategies/from-factors",
        json={"strategy_name": "factor-strategy", "factor_names": ["mom"]},
    )
    assert created.json()["saved"] is True

    def fail_strategy(**_payload):  # noqa: ANN003, ANN202
        raise ValueError("bad strategy")

    monkeypatch.setattr(
        strategy_system, "create_strategy_from_factors", fail_strategy,
    )
    assert c.post(
        "/api/strategies/from-factors",
        json={"strategy_name": "broken", "factor_names": ["mom"]},
    ).status_code == 400
    missing_source = tmp_path / "important_data" / "imports" / "missing.pdf"
    assert c.post(
        "/api/strategies/import",
        json={"kind": "pdf", "source": str(missing_source)},
    ).status_code == 404
    assert c.get("/api/strategies").status_code == 200
    missing_export = c.post(
        "/api/strategies/export",
        json={
            "strategy_name": "missing",
            "output_path": str(tmp_path / "important_data" / "strategy_zoo" / "missing.json"),
        },
    )
    assert missing_export.status_code == 404
    assert c.get("/api/strategies/missing/export").status_code == 404
    assert c.delete("/api/strategies/missing").json()["deleted"] is False

    leaderboard_root = tmp_path / "workspaces"
    leaderboard = leaderboard_root / "batch" / "factor_leaderboard.csv"
    leaderboard.parent.mkdir(parents=True)
    pd.DataFrame({"factor_name": ["mom"], "ic": [0.12]}).to_csv(
        leaderboard, index=False,
    )
    c.app.state.engine.config.backtest.workspace_root = str(leaderboard_root)
    listed = c.get(
        "/api/backtests/leaderboards", params={"workspace_root": str(leaderboard_root)},
    )
    assert listed.json()[0]["file"] == "batch/factor_leaderboard.csv"
    detail = c.get(
        "/api/backtests/leaderboard",
        params={
            "file": "batch/factor_leaderboard.csv",
            "workspace_root": str(leaderboard_root),
        },
    )
    assert detail.json()["numeric_columns"] == ["ic"]
    assert c.get(
        "/api/backtests/leaderboard",
        params={"file": "../outside.csv", "workspace_root": str(leaderboard_root)},
    ).status_code == 400

    def fail(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ValueError("injected portal failure")

    monkeypatch.setattr("alphapilot.modules.portal.api.save_portal_settings", fail)
    assert c.patch(
        "/api/portal/settings",
        json={"host": "127.0.0.1", "port": 19903},
    ).status_code == 400
    monkeypatch.setattr(
        "alphapilot.modules.portal.api.schedule_current_process_restart", fail,
    )
    assert c.post("/api/portal/restart").status_code == 400
    monkeypatch.setattr(strategy_system.param_database, "save", fail)
    assert c.post(
        "/api/strategies", json={"strategy_name": "broken", "params": {}},
    ).status_code == 400
    monkeypatch.setattr(jobs, "start_job", fail)
    assert c.post("/api/daily-trade", json={"strategy_name": "broken"}).status_code == 400


def test_strategy_import_export_routes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    c.post("/api/strategies", json={"strategy_name": "s1", "params": {"a": 1}})

    out = tmp_path / "important_data" / "strategy_zoo" / "strategy.json"
    exported = c.post("/api/strategies/export", json={"strategy_name": "s1", "output_path": str(out)})
    assert exported.json()["saved"] is True
    assert out.exists()

    source = tmp_path / "important_data" / "imports" / "paper.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    imported = c.post("/api/strategies/import", json={"kind": "pdf", "source": str(source)})
    assert imported.json()["strategy_name"] == "imported"


def test_portal_asset_import_export_rejects_paths_outside_important_data(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    c.post("/api/strategies", json={"strategy_name": "s1", "params": {"a": 1}})
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}', encoding="utf-8")

    exported = c.post(
        "/api/strategies/export",
        json={"strategy_name": "s1", "output_path": str(outside)},
    )
    imported = c.post(
        "/api/factors/import",
        json={"kind": "json", "source": str(outside)},
    )

    assert exported.status_code == 400
    assert imported.status_code == 400
    assert outside.read_text(encoding="utf-8") == '{"secret": true}'


def test_report_factor_upload_extract_and_delete(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)

    class ReportFactorApiModule:
        def ocr_providers(self) -> dict[str, Any]:
            return {
                "default_provider": "azure",
                "modes": ["auto", "local", "azure", "vendor"],
                "providers": [
                    {
                        "provider_id": "azure",
                        "display_name": "Azure Document Intelligence",
                        "source": "built_in",
                    },
                    {
                        "provider_id": "vendor",
                        "display_name": "Vendor OCR",
                        "source": "entry_point",
                    },
                ],
            }

        def validate_ocr_mode(self, mode: str) -> str:
            normalized = mode.strip().lower()
            if normalized not in {"auto", "local", "azure", "vendor"}:
                raise ValueError(f"Unknown OCR mode/provider {normalized!r}")
            return normalized

    c.app.state.engine.modules["report_factor"] = ReportFactorApiModule()
    providers = c.get("/api/report-factors/ocr-providers")
    assert providers.status_code == 200
    assert providers.json()["modes"] == ["auto", "local", "azure", "vendor"]

    uploaded = c.post(
        "/api/report-factors/upload",
        files={"file": ("research.pdf", b"%PDF-1.7 offline fixture", "application/pdf")},
    )
    assert uploaded.status_code == 200
    upload = uploaded.json()
    assert upload["file_name"] == "research.pdf"
    assert Path(upload["source"]).is_file()

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_start(kind: str, kwargs: dict[str, Any], **_options: Any) -> dict[str, Any]:
        calls.append((kind, kwargs))
        return {"job_id": "report-job", "kind": kind, "status": "running"}

    monkeypatch.setattr(jobs, "start_job", fake_start)
    started = c.post(
        "/api/report-factors/extract",
        json={"source": upload["source"], "ocr_mode": "local"},
    )
    assert started.status_code == 200
    assert started.json()["job_id"] == "report-job"
    assert calls[0][0] == "report_factor_extract"
    assert calls[0][1]["source"] == upload["source"]

    unknown = c.post(
        "/api/report-factors/extract",
        json={"source": upload["source"], "ocr_mode": "missing"},
    )
    assert unknown.status_code == 400
    assert "Unknown OCR mode/provider" in unknown.json()["detail"]

    deleted = c.delete(f"/api/report-factors/uploads/{upload['upload_id']}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert not Path(upload["source"]).exists()


def test_report_factor_upload_and_source_guards(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    bad_upload = c.post(
        "/api/report-factors/upload",
        files={"file": ("fake.pdf", b"not really a pdf", "application/pdf")},
    )
    assert bad_upload.status_code == 400

    traversal = c.post(
        "/api/report-factors/upload",
        files={"file": ("../escape.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert traversal.status_code == 400

    wrong_extension = c.post(
        "/api/report-factors/upload",
        files={"file": ("report.txt", b"%PDF-1.7", "application/pdf")},
    )
    assert wrong_extension.status_code == 400

    monkeypatch.setenv("ALPHAPILOT_REPORT_FACTOR_MAX_UPLOAD_MB", "1")
    oversized = c.post(
        "/api/report-factors/upload",
        files={"file": ("large.pdf", b"%PDF-1.7" + b"x" * (1024 * 1024), "application/pdf")},
    )
    assert oversized.status_code == 400

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.7 outside")
    bad_source = c.post(
        "/api/report-factors/extract",
        json={"source": str(outside), "ocr_mode": "local"},
    )
    assert bad_source.status_code == 400

    legacy = c.post(
        "/api/factors/import",
        json={"kind": "pdf", "source": str(outside)},
    )
    assert legacy.status_code == 422


def test_report_factor_commit_checks_job_membership(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    class ReportFactorModule:
        def commit_factors(self, job_id: str, factors: list[dict[str, Any]]) -> dict[str, Any]:
            calls.append((job_id, factors))
            return {
                "job_id": job_id,
                "committed": factors,
                "rejected": [],
                "n_requested": len(factors),
                "n_committed": len(factors),
                "n_rejected": 0,
            }

    c.app.state.engine.modules["report_factor"] = ReportFactorModule()
    monkeypatch.setattr(
        jobs,
        "get_job",
        lambda _job_id: {"job_id": "report-job", "kind": "report_factor_extract", "status": "succeeded"},
    )
    monkeypatch.setattr(
        jobs,
        "read_result",
        lambda _job_id: {"result": {"factors": [{"draft_id": "draft-1"}]}},
    )

    committed = c.post(
        "/api/report-factors/commit",
        json={
            "job_id": "report-job",
            "factors": [
                {
                    "draft_id": "draft-1",
                    "factor_name": "momentum",
                    "factor_expression": "$close/Ref($close,5)-1",
                    "categories": ["report"],
                }
            ],
        },
    )
    assert committed.status_code == 200
    assert committed.json()["n_committed"] == 1
    assert calls[0][0] == "report-job"

    unknown = c.post(
        "/api/report-factors/commit",
        json={
            "job_id": "report-job",
            "factors": [
                {
                    "draft_id": "not-from-job",
                    "factor_name": "bad",
                    "factor_expression": "$close",
                }
            ],
        },
    )
    assert unknown.status_code == 400


def test_portal_asset_repo_relative_path_follows_relocated_important_data(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    response = c.post("/api/factors/export", json={"output_path": "important_data/factor_zoo/export.csv"})
    expected = tmp_path / "important_data" / "factor_zoo" / "export.csv"

    assert response.status_code == 200
    assert Path(response.json()["output_path"]) == expected


def test_generic_module_run_cannot_bypass_live_or_destructive_api_guards(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    class LiveModule:
        def commands(self) -> dict[str, Any]:
            return {
                "live_order": lambda **kwargs: calls.append(kwargs),
                "live_preflight": lambda **kwargs: calls.append(kwargs),
            }

    c.app.state.engine.modules["live"] = LiveModule()
    order = c.post(
        "/api/modules/run",
        json={
            "module": "live",
            "command": "live_order",
            "kwargs": {"mode": "live", "confirm_live": True, "symbol": "SH600000"},
        },
    )
    network = c.post(
        "/api/modules/run",
        json={"module": "live", "command": "live_preflight", "kwargs": {"network": True}},
    )

    assert order.status_code == 400
    assert network.status_code == 400
    assert calls == []


def test_removed_timing_routes_and_job_kind_are_rejected(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)

    assert c.get("/api/timing/strategies").status_code == 404
    assert c.post("/api/timing/signal", json={}).status_code == 404
    assert c.post("/api/timing/backtest", json={}).status_code == 404
    assert c.get("/api/timing/jobs/timing1/detail").status_code == 404

    removed_job = c.post(
        "/api/jobs",
        json={"kind": "timing_backtest", "kwargs": {"strategy_name": "dual_ma"}},
    )
    assert removed_job.status_code == 400
    assert "Unsupported portal job kind" in removed_job.json()["detail"]


def test_mining_session_routes_reject_percent_encoded_parent_escape(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    c = client(tmp_path, monkeypatch)
    log_root = tmp_path / "log"
    session = log_root / "safe-session"
    session.mkdir(parents=True)
    (session / "run.log").write_text("safe", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("must-not-leak", encoding="utf-8")

    detail = c.get("/api/mining/sessions/%2E%2E")
    file_read = c.get("/api/mining/sessions/%2E%2E/files/secret.txt")
    valid = c.get("/api/mining/sessions/safe-session/files/run.log")

    assert detail.status_code == 400
    assert file_read.status_code == 400
    assert "must-not-leak" not in detail.text + file_read.text
    assert valid.status_code == 200
    assert valid.json()["content"] == "safe"

from __future__ import annotations

from typing import Any

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

import alphapilot.kernel

from alphapilot.modules.portal import jobs


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.started = False

    def start(self) -> None:
        self.started = True


def test_start_job_persists_unique_directories(tmp_path):
    pids = iter([1111, 2222])

    def factory(_target, _args):
        return FakeProcess(next(pids))

    first = jobs.start_job(
        "mine",
        {"step_n": 1, "scenario": "alpha_factor_mining"},
        job_root=tmp_path,
        process_factory=factory,
    )
    second = jobs.start_job(
        "factor_backtest",
        {"factor_path": "factor.csv"},
        job_root=tmp_path,
        process_factory=factory,
    )

    assert first["job_id"] != second["job_id"]
    assert first["status"] == "running"
    assert second["status"] == "running"
    assert (tmp_path / first["job_id"] / "job.json").exists()
    assert (tmp_path / first["job_id"] / "run.log").exists()
    assert (tmp_path / second["job_id"] / "job.json").exists()

    listed = jobs.list_jobs(job_root=tmp_path, refresh=False)
    assert {job["job_id"] for job in listed} == {first["job_id"], second["job_id"]}


def test_timing_backtest_job_kind_dispatches_to_timing_module(monkeypatch):
    calls: list[dict[str, Any]] = []

    class FakeTimingModule:
        def timing_backtest(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"strategy": kwargs["strategy_name"], "artifact_dir": "/tmp/timing"}

    class FakeEngine:
        def get_module(self, name: str) -> Any:
            assert name == "timing"
            return FakeTimingModule()

    monkeypatch.setattr(alphapilot.kernel, "build_engine", lambda discover=True: FakeEngine())

    result = jobs._run_target("timing_backtest", {"strategy_name": "dual_ma", "symbols": ["000001"]})

    assert "timing_backtest" in jobs.VALID_KINDS
    assert result["strategy"] == "dual_ma"
    assert calls == [{"strategy_name": "dual_ma", "symbols": ["000001"]}]


@pytest.mark.parametrize(
    ("kind", "target_name", "method", "kwargs"),
    [
        ("mine", "alpha_mining", "run_mining", {"step_n": 1}),
        ("report_factor_extract", "report_factor", "extract_pdf", {"source": "report.pdf"}),
        ("factor_backtest", "alpha_mining", "run_backtest", {"mode": "single_ic"}),
        ("strategy_backtest", "strategy_backtest", "strategy_backtest", {"strategy_name": "qa"}),
        ("daily_signals", "daily_trade", "daily_signals", {"strategy_name": "qa"}),
        ("data", "data", "pipeline", {"action": "pipeline", "market": "qa"}),
        ("timing_backtest", "timing", "timing_backtest", {"strategy_name": "dual_ma"}),
        ("mine_aff", "alphaforge_aff", "mine_aff", {"epochs": 1}),
        ("mine_gp", "alphaforge_search", "mine_gp", {"generations": 1}),
        ("mine_rl", "alphaforge_search", "mine_rl", {"steps": 128}),
    ],
)
def test_all_job_kinds_have_stable_dispatch_contract(
    monkeypatch, kind: str, target_name: str, method: str, kwargs: dict[str, Any]
) -> None:  # noqa: ANN001
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class Target:
        def __init__(self, name: str) -> None:
            self.name = name

        def __getattr__(self, method_name: str):  # noqa: ANN204, ANN001
            def invoke(**call_kwargs: Any) -> dict[str, Any]:
                calls.append((self.name, method_name, call_kwargs))
                return {"target": self.name, "method": method_name}

            return invoke

    class Engine:
        def get_module(self, name: str) -> Target:
            return Target(name)

        def get_system(self, name: str) -> Target:
            return Target(name)

    monkeypatch.setattr(alphapilot.kernel, "build_engine", lambda discover=True: Engine())
    result = jobs._run_target(kind, dict(kwargs))

    expected_kwargs = {key: value for key, value in kwargs.items() if key != "action"}
    if kind == "report_factor_extract":
        callback = calls[0][2].pop("progress_callback")
        assert callback is jobs.update_current_job_progress
    assert result == {"target": target_name, "method": method}
    assert calls == [(target_name, method, expected_kwargs)]


def test_job_kwargs_are_bounded_json_and_secrets_are_masked(tmp_path) -> None:
    def factory(_target, _args):
        return FakeProcess(2323)

    job = jobs.start_job(
        "mine",
        {"step_n": 1, "api_key": "do-not-persist", "nested": {"access_token": "also-secret"}},
        job_root=tmp_path,
        process_factory=factory,
    )
    assert job["params"]["api_key"] == "********"
    assert job["params"]["nested"]["access_token"] == "********"
    assert "do-not-persist" not in (tmp_path / job["job_id"] / "job.json").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="Unsafe job kwargs key"):
        jobs.start_job("mine", {"__class__": "exploit"}, job_root=tmp_path, process_factory=factory)
    with pytest.raises(ValueError, match="exceed"):
        jobs.start_job(
            "mine",
            {"payload": "x" * (jobs.MAX_JOB_KWARGS_BYTES + 1)},
            job_root=tmp_path,
            process_factory=factory,
        )
    with pytest.raises(ValueError, match="non-finite"):
        jobs.start_job("mine", {"score": float("nan")}, job_root=tmp_path, process_factory=factory)
    with pytest.raises(TypeError, match="non-JSON"):
        jobs.start_job("mine", {"bad": object()}, job_root=tmp_path, process_factory=factory)


def test_concurrent_job_patches_are_atomic_and_cancel_is_terminal(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    def factory(_target, _args):
        return FakeProcess(2424)

    job = jobs.start_job("mine", {"step_n": 1}, job_root=tmp_path, process_factory=factory)
    job_dir = tmp_path / job["job_id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: jobs._patch_job(job_dir, {f"field_{i}": i}), range(24)))

    persisted = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert all(persisted[f"field_{i}"] == i for i in range(24))

    monkeypatch.setattr(jobs, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(jobs.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(jobs.time, "sleep", lambda _seconds: None)
    cancelled = jobs.cancel_job(job["job_id"], job_root=tmp_path)
    late = jobs._patch_job(job_dir, {"status": "succeeded", "returncode": 0})
    assert cancelled["status"] == "cancelled"
    assert late["status"] == "cancelled"


def test_running_job_without_process_is_marked_lost(tmp_path, monkeypatch):
    def factory(_target, _args):
        return FakeProcess(3333)

    job = jobs.start_job(
        "mine",
        {"step_n": 1},
        job_root=tmp_path,
        process_factory=factory,
    )
    monkeypatch.setattr(jobs, "_pid_exists", lambda _pid: False)

    refreshed = jobs.get_job(job["job_id"], job_root=tmp_path)

    assert refreshed["status"] == "lost"
    assert "Worker process is no longer running" in refreshed["error"]


def test_cancel_job_records_cancelled_status(tmp_path, monkeypatch):
    killed: list[tuple[int, int]] = []

    def factory(_target, _args):
        return FakeProcess(4444)

    job = jobs.start_job(
        "strategy_backtest",
        {"strategy_name": "demo", "mode": "both"},
        job_root=tmp_path,
        process_factory=factory,
    )
    monkeypatch.setattr(jobs, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(jobs.time, "sleep", lambda _seconds: None)

    cancelled = jobs.cancel_job(job["job_id"], job_root=tmp_path)

    assert cancelled["status"] == "cancelled"
    assert cancelled["returncode"] < 0
    assert killed


def test_read_progress_infers_tqdm_percentage(tmp_path, monkeypatch):
    def factory(_target, _args):
        return FakeProcess(5555)

    job = jobs.start_job("data", {"action": "download"}, job_root=tmp_path, process_factory=factory)
    monkeypatch.setattr(jobs, "_pid_exists", lambda _pid: True)
    (tmp_path / job["job_id"] / "run.log").write_text("下载进度:  42%|####      | 42/100\n", encoding="utf-8")

    progress = jobs.read_progress(job["job_id"], job_root=tmp_path)

    assert progress["status"] == "running"
    assert progress["percent"] == 42
    assert "42%" in progress["message"]


def test_read_progress_preserves_structured_fields(tmp_path, monkeypatch):
    def factory(_target, _args):
        return FakeProcess(6666)

    job = jobs.start_job("data", {"action": "download"}, job_root=tmp_path, process_factory=factory)
    monkeypatch.setattr(jobs, "_pid_exists", lambda _pid: True)
    monkeypatch.setenv("ALPHAPILOT_PORTAL_JOB_DIR", str(tmp_path / job["job_id"]))

    jobs.update_current_job_progress(
        12,
        "download:baostock",
        "下载仍在进行 1/10，等待 9 个任务返回",
        completed=1,
        total=10,
        pending=9,
        current_symbol="sh600000",
    )
    progress = jobs.read_progress(job["job_id"], job_root=tmp_path)

    assert progress["percent"] == 12
    assert progress["completed"] == 1
    assert progress["total"] == 10
    assert progress["pending"] == 9
    assert progress["current_symbol"] == "sh600000"


def test_data_job_progress_can_be_computed_from_raw_csv_files(tmp_path, monkeypatch):
    def factory(_target, _args):
        return FakeProcess(7777)

    stock_csv = tmp_path / "stocks.csv"
    stock_csv.write_text("code\nsh.600000\nsz.000001\nsh.600001\n", encoding="utf-8")
    raw_dir = tmp_path / "raw_none"
    raw_dir.mkdir()
    (raw_dir / "sh600000.csv").write_text("date,code,close\n2026-06-18,sh600000,10\n", encoding="utf-8")
    (raw_dir / "sz000001.csv").write_text("date,code,close\n2026-06-17,sz000001,9\n", encoding="utf-8")

    job = jobs.start_job(
        "data",
        {
            "action": "download",
            "source": "baostock_cn",
            "start_date": "2026-06-01",
            "end_date": "2026-06-18",
            "stock_csv": str(stock_csv),
            "adjust_mode": "none",
            "data_dir": str(raw_dir),
        },
        job_root=tmp_path / "jobs",
        process_factory=factory,
    )
    monkeypatch.setattr(jobs, "_pid_exists", lambda _pid: True)

    progress = jobs.read_progress(job["job_id"], job_root=tmp_path / "jobs")

    assert progress["progress_source"] == "disk"
    assert progress["completed"] == 1
    assert progress["total"] == 3
    assert progress["pending"] == 2
    assert progress["latest_data_date"] == "2026-06-18"


@pytest.mark.parametrize("job_id", ["..", "../secret", "/tmp/secret"])
def test_job_id_path_traversal_is_rejected(tmp_path, job_id):
    with pytest.raises(ValueError, match="Invalid job id"):
        jobs.get_job(job_id, job_root=tmp_path)
    with pytest.raises(ValueError, match="Invalid job id"):
        jobs.delete_job(job_id, job_root=tmp_path)


def test_persisted_job_paths_cannot_redirect_log_or_result_reads(tmp_path):
    def factory(_target, _args):
        return FakeProcess(8888)

    root = tmp_path / "jobs"
    job = jobs.start_job("mine", {"step_n": 1}, job_root=root, process_factory=factory)
    outside_log = tmp_path / "secret.log"
    outside_result = tmp_path / "secret.json"
    outside_log.write_text("do-not-read", encoding="utf-8")
    outside_result.write_text('{"secret": true}', encoding="utf-8")
    metadata_path = root / job["job_id"] / "job.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["log_path"] = str(outside_log)
    metadata["result_path"] = str(outside_result)
    metadata["job_dir"] = str(tmp_path)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert jobs.read_log_tail(job["job_id"], job_root=root) == ""
    assert jobs.read_result(job["job_id"], job_root=root) is None

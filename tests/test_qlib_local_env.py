from pathlib import Path
from types import SimpleNamespace

import pytest

from alphapilot.utils import env as env_module


def test_qlib_local_env_uses_current_interpreter_entrypoints(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python"
    qrun = bin_dir / "qrun"
    python.touch()
    qrun.touch()
    calls: list[list[str]] = []

    monkeypatch.setattr(env_module.sys, "executable", str(python))

    def fake_run(command, **_kwargs):  # noqa: ANN001
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(env_module.subprocess, "run", fake_run)
    local = env_module.QlibLocalEnv()

    assert local.run("qrun conf.yaml", local_path=str(tmp_path)) == "ok"
    assert local.run("python read_exp_res.py", local_path=str(tmp_path)) == "ok"
    assert calls == [
        [str(qrun), "conf.yaml"],
        [str(python), "read_exp_res.py"],
    ]


def test_qlib_local_env_raises_when_subprocess_fails(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        env_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="broken config",
        ),
    )

    with pytest.raises(RuntimeError, match="broken config"):
        env_module.QlibLocalEnv().run("python broken.py", local_path=str(tmp_path))

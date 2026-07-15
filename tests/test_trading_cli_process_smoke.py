from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def _isolated_cli_env(tmp_path: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ALPHAPILOT_LIVE_")
        and not key.startswith("ALPHAPILOT_BROKER_UAT_")
    }
    state = tmp_path / "state"
    env.update(
        {
            "ALPHAPILOT_LIVE_STATE_DIR": str(state),
            "ALPHAPILOT_LIVE_LEDGER_DIR": str(state / "ledger"),
            "ALPHAPILOT_LIVE_MARKET_DATA_DIR": str(state / "market"),
            "ALPHAPILOT_OPERATOR_AUTH_REQUIRED": "false",
            "ALPHAPILOT_AUTOMATED_LIVE_ENABLED": "false",
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", "")))
            ),
        }
    )
    return env


def _run_cli(tmp_path: Path, env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from alphapilot.app.cli import app; app()",
            *arguments,
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_every_formal_trading_command_has_real_process_help(
    engine,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    commands = sorted(
        name for name in engine.collect_commands() if name.startswith("trading_")
    )
    assert commands
    env = _isolated_cli_env(tmp_path)

    failures: list[str] = []
    for command in commands:
        result = _run_cli(tmp_path, env, command, "--", "--help")
        if result.returncode != 0 or command not in result.stdout + result.stderr:
            failures.append(
                f"{command}: exit={result.returncode}, output="
                f"{(result.stdout + result.stderr)[-300:]}"
            )

    assert not failures, "\n".join(failures)


def test_formal_trading_read_commands_execute_in_an_isolated_process(
    tmp_path: Path,
) -> None:
    env = _isolated_cli_env(tmp_path)
    for command in (
        "trading_definitions",
        "trading_policies",
        "trading_instances",
        "trading_compatibility",
        "trading_broker_uat_status",
    ):
        result = _run_cli(tmp_path, env, command)
        assert result.returncode == 0, f"{command}: {result.stderr[-500:]}"
        assert result.stdout.strip()
        assert any(marker in result.stdout for marker in ("{", "[", "definitions", "runs"))

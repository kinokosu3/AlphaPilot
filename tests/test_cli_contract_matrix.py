"""Stable discovery/help/error contract for every public AlphaPilot CLI command."""

from __future__ import annotations

import contextlib
import inspect
import io

import fire
import pytest

from conftest import EXPECTED_CLI_COMMANDS


def _fire_exit_code(commands, command: list[str]) -> tuple[int, str]:  # noqa: ANN001
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            fire.Fire(commands, command=command)
        except fire.core.FireExit as exc:
            return int(exc.code), output.getvalue()
        except Exception as exc:  # the real CLI converts command validation errors to exit 1
            return 1, f"{output.getvalue()}\n{type(exc).__name__}: {exc}"
    return 0, output.getvalue()


def test_all_125_cli_commands_have_help_and_inspectable_signatures(engine) -> None:  # noqa: ANN001
    commands = engine.collect_commands()
    assert len(commands) == 125
    assert set(commands) == set(EXPECTED_CLI_COMMANDS)

    for name, command in sorted(commands.items()):
        assert callable(command)
        inspect.signature(command)
        code, help_text = _fire_exit_code(commands, [name, "--", "--help"])
        assert code == 0, f"{name} --help exited with {code}"
        assert "NAME" in help_text and name in help_text


def test_cli_unknown_command_has_nonzero_stable_error(engine) -> None:  # noqa: ANN001
    code, output = _fire_exit_code(engine.collect_commands(), ["__not_a_command__"])
    assert code != 0
    assert "Could not consume arg" in output or "ERROR" in output


@pytest.mark.parametrize("command_name", ["pool_create", "live_order", "backtest", "timing_signal"])
def test_representative_cli_missing_required_args_are_rejected(engine, command_name: str) -> None:  # noqa: ANN001
    code, output = _fire_exit_code(engine.collect_commands(), [command_name])
    assert code != 0
    assert "required" in output.lower()

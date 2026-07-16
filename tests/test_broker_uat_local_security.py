from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import broker_uat_local
from scripts import check_secret_leaks


def _private_file(path: Path, payload: str) -> Path:
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_xtp_wrapper_imports_only_broker_fields(tmp_path: Path) -> None:
    secret = _private_file(
        tmp_path / ".env",
        "\n".join(
            (
                "ALPHAPILOT_LIVE_XTP_ACCOUNT=test-account",
                "ALPHAPILOT_LIVE_XTP_PASSWORD=test-password",
                "ALPHAPILOT_LIVE_XTP_TRADE_HOST=127.0.0.1",
                "UNRELATED_SUDO_PASSWORD=must-not-be-imported",
                "UNRELATED_TOKEN=must-not-be-imported-either",
            )
        ),
    )

    values = broker_uat_local._load_xtp(secret)

    assert values == {
        "ALPHAPILOT_LIVE_XTP_ACCOUNT": "test-account",
        "ALPHAPILOT_LIVE_XTP_PASSWORD": "test-password",
        "ALPHAPILOT_LIVE_XTP_TRADE_HOST": "127.0.0.1",
    }


def test_emt_wrapper_maps_only_known_section_fields(tmp_path: Path) -> None:
    secret = _private_file(
        tmp_path / "secrets.txt",
        """EMT 模拟账户
测试账户：emt-account
登入密码：emt-password
交易地址：tcp://127.0.0.1:12001
行情账户：quote-account
行情密码：quote-password
L1行情地址：tcp://127.0.0.1:12002

sudo
sudo 密码：must-not-be-imported
""",
    )

    values = broker_uat_local._load_emt(secret, client_id=7)

    assert values == {
        "ALPHAPILOT_LIVE_EMT_ACCOUNT": "emt-account",
        "ALPHAPILOT_LIVE_EMT_PASSWORD": "emt-password",
        "ALPHAPILOT_LIVE_EMT_CLIENT_ID": "7",
        "ALPHAPILOT_LIVE_EMT_TRADE_HOST": "127.0.0.1",
        "ALPHAPILOT_LIVE_EMT_TRADE_PORT": "12001",
        "ALPHAPILOT_LIVE_EMT_QUOTE_ACCOUNT": "quote-account",
        "ALPHAPILOT_LIVE_EMT_QUOTE_PASSWORD": "quote-password",
        "ALPHAPILOT_LIVE_EMT_QUOTE_HOST": "127.0.0.1",
        "ALPHAPILOT_LIVE_EMT_QUOTE_PORT": "12002",
        "ALPHAPILOT_LIVE_EMT_QUOTE_PROTOCOL": "TCP",
        "ALPHAPILOT_LIVE_EMT_LOG_LEVEL": "INFO",
    }
    assert all("must-not-be-imported" not in value for value in values.values())


def test_broker_secret_files_must_be_private(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("ALPHAPILOT_LIVE_XTP_ACCOUNT=test-account\n", encoding="utf-8")
    secret.chmod(0o640)

    with pytest.raises(PermissionError, match="0600"):
        broker_uat_local._load_xtp(secret)
    with pytest.raises(PermissionError, match="0600"):
        check_secret_leaks._secret_values(secret)


def test_secret_scanner_finds_values_without_treating_labels_as_secrets(
    tmp_path: Path,
) -> None:
    secret = _private_file(
        tmp_path / ".env",
        "ALPHAPILOT_LIVE_XTP_ACCOUNT=fake-account-123\n"
        "ALPHAPILOT_LIVE_XTP_PASSWORD=fake-password-456\n",
    )
    values = check_secret_leaks._secret_values(secret)

    assert b"fake-account-123" in values
    assert b"fake-password-456" in values
    assert all(b"ALPHAPILOT" not in value for value in values)


def test_secret_scanner_does_not_match_a_short_pin_inside_a_timestamp() -> None:
    secrets = {b"2026"}

    assert check_secret_leaks._scan_file(
        b'{"datetime":"2026-07-15T13:00:00"}',
        secrets,
    ) == 0
    assert check_secret_leaks._scan_file(
        b'{"account_id":"2026"}',
        secrets,
    ) > 0
    assert check_secret_leaks._scan_file(
        b"broker login account=2026 failed",
        secrets,
    ) > 0
    assert check_secret_leaks._scan_file(
        b'{"datetime":"20260715T13:00:00"}',
        {b"20260715"},
    ) == 0


def test_recovery_restores_whitelist_from_persisted_uat_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSystem:
        def get_broker_uat_run(self, run_id: str) -> dict[str, str]:
            assert run_id == "run-1"
            return {"broker": "emt", "symbol": "513100.SSE"}

    module = SimpleNamespace(_system=lambda: FakeSystem())
    args = SimpleNamespace(
        command="resume", run_id="run-1", broker="emt", symbol="",
    )
    monkeypatch.delenv("ALPHAPILOT_BROKER_UAT_WHITELIST", raising=False)

    broker_uat_local._bind_persisted_run_symbol(module, args)

    assert os.environ["ALPHAPILOT_BROKER_UAT_WHITELIST"] == "513100.SSE"


def test_recovery_rejects_broker_or_symbol_mismatch() -> None:
    class FakeSystem:
        def get_broker_uat_run(self, run_id: str) -> dict[str, str]:
            return {"broker": "emt", "symbol": "513100.SSE"}

    module = SimpleNamespace(_system=lambda: FakeSystem())
    with pytest.raises(ValueError, match="belongs to broker"):
        broker_uat_local._bind_persisted_run_symbol(
            module,
            SimpleNamespace(
                command="resume", run_id="run-1", broker="xtp", symbol="",
            ),
        )
    with pytest.raises(ValueError, match="does not match"):
        broker_uat_local._bind_persisted_run_symbol(
            module,
            SimpleNamespace(
                command="abort", run_id="run-1", broker="emt", symbol="510300.SSE",
            ),
        )


def test_local_wrapper_binds_uat_and_compatibility_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _private_file(tmp_path / ".env", "unused=value\n")
    monkeypatch.setattr(
        broker_uat_local,
        "_load_xtp",
        lambda _path: {"ALPHAPILOT_LIVE_XTP_ACCOUNT": "test-account"},
    )
    args = SimpleNamespace(
        broker="xtp",
        secret_file=str(secret),
        client_id=1,
        state_dir=str(tmp_path / "state"),
        environment="acceptance-host-a",
        max_notional=20_000,
        symbol="510300.SSE",
        command="preflight",
    )

    broker_uat_local._configure(args)

    assert os.environ["ALPHAPILOT_ENVIRONMENT_ID"] == "acceptance-host-a"
    assert os.environ["ALPHAPILOT_BROKER_UAT_ENVIRONMENT"] == "acceptance-host-a"


@pytest.mark.parametrize("value", (0, -1, 20_000.01))
def test_local_wrapper_rejects_values_outside_the_uat_cap(
    value: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "broker_uat_local.py",
            "preflight",
            "--broker",
            "xtp",
            "--secret-file",
            ".env",
            "--max-notional",
            str(value),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        broker_uat_local.main()

    assert exc.value.code == 2

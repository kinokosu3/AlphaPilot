"""Daemon implementation of the trading application's runtime-control port."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import re
import time
from typing import Any

from alphapilot.systems.live.config import LiveConfig, RunMode
from alphapilot.systems.live.daemon import (
    daemon_status,
    load_daemon,
    send_daemon_command,
    start_daemon,
    stop_daemon,
)
from alphapilot.systems.trading.ports import RuntimeCommandResult
from alphapilot.systems.trading.account_identity import (
    ACCOUNT_HASH_PREFIX,
    account_identities_match,
)
from alphapilot.systems.live.runtime import clone_config


class DaemonRuntimeControl:
    def __init__(self, config: LiveConfig, *, timeout: float = 10.0) -> None:
        self.config = config
        self.timeout = max(float(timeout), 0.5)

    def status(self, instance: dict[str, Any]) -> RuntimeCommandResult:
        config = self._config_for(instance)
        status = daemon_status(config)
        result = self._from_status(status, status_only=True)
        binding_error = self._binding_error(result, instance)
        if result.ok and binding_error:
            return RuntimeCommandResult(
                False,
                runtime_id=result.runtime_id,
                heartbeat_at=result.heartbeat_at,
                runner_status=result.runner_status,
                error=binding_error,
                raw=status,
            )
        return result

    def start(self, instance: dict[str, Any]) -> RuntimeCommandResult:
        level = str(instance["deployment_level"])
        runtime = instance.get("runtime") or {}
        mode = _mode_for_level(level, str(runtime.get("execution_environment") or ""))
        config = self._config_for(instance)
        broker = str(runtime.get("trade_provider") or runtime.get("broker") or config.trade_broker or config.broker or "paper")
        quote_provider = str(runtime.get("quote_provider") or config.quote_provider or broker)
        if mode == RunMode.PAPER:
            broker = "paper"
            quote_provider = "paper"
        status = load_daemon(config.state_dir)
        if status.get("running"):
            if status.get("mode") != mode:
                return RuntimeCommandResult(
                    False,
                    runtime_id=str(status.get("runtime_id") or ""),
                    heartbeat_at=str(status.get("heartbeat_at") or ""),
                    error=f"running daemon mode {status.get('mode')!r} does not match {mode!r}",
                    raw=status,
                )
            running_broker = str(status.get("trade_broker") or status.get("broker") or "")
            if broker and running_broker and running_broker.lower() != broker.lower():
                return RuntimeCommandResult(
                    False,
                    runtime_id=str(status.get("runtime_id") or ""),
                    heartbeat_at=str(status.get("heartbeat_at") or ""),
                    error=f"running daemon broker {running_broker!r} does not match {broker!r}",
                    raw=status,
                )
            current = (status.get("runner_status") or {}).get("instance_id") or (
                (status.get("runner") or {}).get("instance_id")
                if isinstance(status.get("runner"), dict) else None
            )
            if current == instance["instance_id"]:
                runner_status = status.get("runner_status") or {}
                if str(runner_status.get("config_hash") or "") != str(instance["config_hash"]):
                    return RuntimeCommandResult(
                        False,
                        runtime_id=str(status.get("runtime_id") or ""),
                        heartbeat_at=str(status.get("heartbeat_at") or ""),
                        runner_status=dict(runner_status),
                        error="running daemon strategy config_hash does not match the deployment",
                        raw=status,
                    )
                if not runner_status.get("stopped"):
                    observed_status = daemon_status(config)
                    observed = self._from_status(observed_status, status_only=True)
                    binding_error = self._binding_error(observed, instance)
                    if binding_error:
                        return RuntimeCommandResult(
                            False,
                            runtime_id=observed.runtime_id,
                            heartbeat_at=observed.heartbeat_at,
                            runner_status=observed.runner_status,
                            error=binding_error,
                            raw=observed_status,
                        )
                    return observed
                restarted = self._command(
                    "strategy_start",
                    {
                        "instance_id": instance["instance_id"],
                        "symbols": list((instance.get("config") or {}).get("universe") or []),
                        "confirm_live": mode == RunMode.LIVE,
                    },
                    config=config,
                )
                binding_error = self._binding_error(restarted, instance)
                if restarted.ok and binding_error:
                    return RuntimeCommandResult(
                        False,
                        command_id=restarted.command_id,
                        runtime_id=restarted.runtime_id,
                        heartbeat_at=restarted.heartbeat_at,
                        runner_status=restarted.runner_status,
                        error=binding_error,
                        raw=restarted.raw,
                    )
                return restarted
            runner_status = status.get("runner_status") or {}
            if current and not runner_status.get("stopped"):
                return RuntimeCommandResult(
                    False,
                    runtime_id=str(status.get("runtime_id") or ""),
                    heartbeat_at=str(status.get("heartbeat_at") or ""),
                    error=f"daemon already owns strategy instance {current!r}",
                    raw=status,
                )
            return self._command(
                "strategy_start",
                {
                    "instance_id": instance["instance_id"],
                    "symbols": list((instance.get("config") or {}).get("universe") or []),
                    "confirm_live": mode == RunMode.LIVE,
                },
                config=config,
            )

        start = start_daemon(
            config,
            mode=mode,
            broker=broker,
            trade_broker=broker,
            quote_provider=quote_provider,
            symbols=list((instance.get("config") or {}).get("universe") or []),
            state_dir=config.state_dir,
            ledger_dir=config.ledger_dir,
            strategy_instance_id=instance["instance_id"],
            runtime_id=str(runtime.get("runtime_id") or "") or None,
            strategy_store_path=self._strategy_store_path(),
            timeout=self.timeout,
        )
        if not start.get("started") and not start.get("running") and not start.get("starting"):
            return RuntimeCommandResult(False, error=str(start.get("error") or "daemon did not start"), raw=start)
        deadline = time.time() + self.timeout
        latest = daemon_status(config)
        while time.time() < deadline:
            latest = daemon_status(config)
            if latest.get("running") and _runner_matches(
                latest.get("runner_status") or {}, instance["instance_id"]
            ):
                observed = self._from_status(latest, status_only=True)
                binding_error = self._binding_error(observed, instance)
                if binding_error:
                    return RuntimeCommandResult(
                        False,
                        runtime_id=observed.runtime_id,
                        heartbeat_at=observed.heartbeat_at,
                        runner_status=observed.runner_status,
                        error=binding_error,
                        raw=latest,
                    )
                return observed
            if latest.get("status") == "error" or not latest.get("alive", True):
                break
            time.sleep(0.1)
        return RuntimeCommandResult(
            False,
            runtime_id=str(latest.get("runtime_id") or start.get("runtime_id") or ""),
            heartbeat_at=str(latest.get("heartbeat_at") or ""),
            runner_status=dict(latest.get("runner_status") or {}),
            error=str(latest.get("error") or "daemon start was not confirmed before timeout"),
            timed_out=bool(latest.get("alive")),
            raw=latest,
        )

    def pause(self, instance: dict[str, Any]) -> RuntimeCommandResult:
        return self._owned_command(instance, "strategy_pause")

    def reconcile(self, instance: dict[str, Any]) -> RuntimeCommandResult:
        return self._owned_command(instance, "strategy_reconcile")

    def resume(self, instance: dict[str, Any]) -> RuntimeCommandResult:
        return self._owned_command(
            instance,
            "strategy_resume",
            {"confirm_live": instance["deployment_level"] == "live"},
        )

    def stop(self, instance: dict[str, Any]) -> RuntimeCommandResult:
        result = self._owned_command(instance, "strategy_stop")
        if not result.ok:
            return result
        stopped = stop_daemon(
            self._config_for(instance), timeout=min(self.timeout, 5.0),
        )
        if not stopped.get("stopped") or stopped.get("alive"):
            return RuntimeCommandResult(
                False,
                command_id=result.command_id,
                runtime_id=result.runtime_id,
                heartbeat_at=result.heartbeat_at,
                runner_status=result.runner_status,
                error="strategy stopped but its dedicated daemon did not terminate",
                raw=stopped,
            )
        return result

    def _owned_command(
        self,
        instance: dict[str, Any],
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> RuntimeCommandResult:
        config = self._config_for(instance)
        before_status = daemon_status(config)
        before = self._from_status(before_status, status_only=True)
        error = self._binding_error(before, instance)
        if not before.ok or error:
            return RuntimeCommandResult(
                False,
                runtime_id=before.runtime_id,
                heartbeat_at=before.heartbeat_at,
                runner_status=before.runner_status,
                error=error or before.error,
                raw=before_status,
            )
        result = self._command(action, payload, config=config)
        error = self._binding_error(result, instance)
        if result.ok and error:
            return RuntimeCommandResult(
                False,
                command_id=result.command_id,
                runtime_id=result.runtime_id,
                heartbeat_at=result.heartbeat_at,
                runner_status=result.runner_status,
                recovery=result.recovery,
                error=error,
                raw=result.raw,
            )
        return result

    @staticmethod
    def _binding_error(result: RuntimeCommandResult, instance: dict[str, Any]) -> str:
        if not result.ok:
            return ""
        if not _runner_matches(result.runner_status, instance["instance_id"]):
            return "daemon is not running the requested strategy instance"
        runner_hash = str(result.runner_status.get("config_hash") or "")
        if runner_hash != str(instance.get("config_hash") or ""):
            return "daemon strategy config_hash does not match the deployment"
        runtime = instance.get("runtime") or {}
        expected_runtime = str(runtime.get("runtime_id") or "")
        if expected_runtime and result.runtime_id != expected_runtime:
            return "daemon runtime_id does not match the observed deployment runtime"
        raw = result.raw or {}
        try:
            expected_mode = _mode_for_level(
                str(instance.get("deployment_level") or ""),
                str(runtime.get("execution_environment") or ""),
            )
        except ValueError:
            return "deployment level has no controllable runtime mode"
        actual_mode = str(raw.get("mode") or "")
        if actual_mode != expected_mode:
            return "daemon run mode does not match the deployment level"
        expected_broker = str(runtime.get("broker") or "")
        actual_broker = str(raw.get("trade_broker") or raw.get("broker") or "")
        if expected_broker and actual_broker.lower() != expected_broker.lower():
            return "daemon broker does not match the deployment binding"
        expected_quote = str(runtime.get("quote_provider") or "")
        actual_quote = str(raw.get("quote_provider") or "")
        if expected_quote and actual_quote.lower() != expected_quote.lower():
            return "daemon quote provider does not match the deployment binding"
        expected_account = str(runtime.get("account_id") or "")
        state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
        account = state.get("account") if isinstance(state.get("account"), dict) else {}
        actual_account = str(account.get("account_id") or "")
        if not actual_account and account.get("account_id_hash"):
            actual_account = ACCOUNT_HASH_PREFIX + str(account["account_id_hash"]).lower()
        if expected_account and not account_identities_match(expected_account, actual_account):
            return "daemon account does not match the deployment binding"
        return ""

    def _command(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        config: LiveConfig | None = None,
    ) -> RuntimeCommandResult:
        sent = send_daemon_command(
            config or self.config,
            action,
            payload=payload,
            wait=True,
            timeout=self.timeout,
        )
        if not sent.get("accepted"):
            daemon = sent.get("daemon") if isinstance(sent.get("daemon"), dict) else {}
            return RuntimeCommandResult(
                False,
                runtime_id=str(daemon.get("runtime_id") or ""),
                heartbeat_at=str(daemon.get("heartbeat_at") or ""),
                error=str(sent.get("reason") or "daemon command was rejected"),
                raw=sent,
            )
        command = sent.get("command") if isinstance(sent.get("command"), dict) else {}
        status = sent.get("daemon") if isinstance(sent.get("daemon"), dict) else {}
        return self._from_status(status, command_id=str(command.get("id") or ""))

    def _strategy_store_path(self) -> Path:
        configured = os.getenv("ALPHAPILOT_STRATEGY_RUNTIME_STORE")
        return (
            Path(configured).expanduser()
            if configured else Path(self.config.state_dir) / "strategy_runtime.sqlite3"
        )

    def _config_for(self, instance: dict[str, Any]) -> LiveConfig:
        runtime = instance.get("runtime") or {}
        environment = str(runtime.get("execution_environment") or "local_paper")
        trade = str(runtime.get("trade_provider") or runtime.get("broker") or "paper")
        quote = str(runtime.get("quote_provider") or ("paper" if trade == "paper" else trade))
        binding_hash = str(runtime.get("binding_hash") or instance.get("config_hash") or "unbound")
        namespace = (
            Path("runtimes") / _slug(environment)
            / f"{_slug(trade)}--{_slug(quote)}" / binding_hash[:16]
        )
        mode = _mode_for_level(str(instance.get("deployment_level") or ""), environment)
        config = clone_config(
            self.config,
            mode=mode,
            broker=trade,
            trade_broker=trade,
            quote_provider=quote,
            state_dir=Path(self.config.state_dir) / namespace,
            ledger_dir=Path(self.config.ledger_dir) / namespace,
            execution_environment=environment,
            quote_data_kind=str(runtime.get("quote_data_kind") or ""),
        )
        return replace(
            config,
            market_data=replace(
                config.market_data,
                data_dir=Path(self.config.market_data.data_dir) / namespace,
            ),
        )

    @staticmethod
    def _from_status(
        status: dict[str, Any],
        *,
        command_id: str = "",
        status_only: bool = False,
    ) -> RuntimeCommandResult:
        last = status.get("last_command") if isinstance(status.get("last_command"), dict) else {}
        timed_out = bool(status.get("wait_timeout"))
        if status_only:
            ok = bool(status.get("running"))
            error = "" if ok else str(status.get("error") or "daemon is not running")
        else:
            confirmed = bool(last) and (not command_id or last.get("id") == command_id)
            ok = confirmed and bool(last.get("ok")) and not timed_out
            error = "" if ok else str(
                last.get("error") or ("daemon command timed out" if timed_out else "daemon did not confirm command")
            )
        runner = last.get("runner_status") if isinstance(last.get("runner_status"), dict) else None
        if runner is None:
            runner = status.get("runner_status") if isinstance(status.get("runner_status"), dict) else {}
        recovery = last.get("recovery") if isinstance(last.get("recovery"), dict) else {}
        return RuntimeCommandResult(
            ok=ok,
            command_id=command_id or str(last.get("id") or ""),
            runtime_id=str(status.get("runtime_id") or ""),
            heartbeat_at=str(status.get("heartbeat_at") or status.get("updated_at") or ""),
            runner_status=dict(runner),
            recovery=dict(recovery),
            error=error,
            timed_out=timed_out,
            raw=status,
        )


def _mode_for_level(level: str, execution_environment: str = "") -> str:
    if level == "paper":
        return (
            RunMode.SIMULATION
            if execution_environment == "broker_simulation" else RunMode.PAPER
        )
    if level == "shadow":
        return RunMode.SHADOW
    if level == "live":
        return RunMode.LIVE
    raise ValueError(f"deployment level {level!r} has no runtime mode")


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value).strip().lower()).strip("-.")
    return normalized or "unknown"


def _runner_matches(status: dict[str, Any], instance_id: str) -> bool:
    return str(status.get("instance_id") or "") == str(instance_id)

"""CLI commands for strategy instances, replays and controlled deployment."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Callable
import uuid

from alphapilot.kernel.base import BaseModule
from alphapilot.systems.trading.contracts import OperatorContext

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context


def _object(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("value must be a JSON object")
    return parsed


def _symbols(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).replace(" ", ",").split(",") if item.strip()]


class TradingModule(BaseModule):
    name = "trading_cli"

    def setup(self, context: "Context") -> None:
        self.context = context

    def _system(self):
        return self.context.system("trading")

    @staticmethod
    def _print(value: Any) -> Any:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return value

    @staticmethod
    def _operator(operator_id: str, reason: str) -> OperatorContext:
        return OperatorContext(
            operator_id=str(operator_id or "local-cli"),
            request_id=uuid.uuid4().hex,
            reason=str(reason),
            auth_source="local-cli",
        )

    def trading_definitions(self) -> dict[str, Any]:
        return self._print(self._system().list_definitions())

    def trading_policies(self) -> dict[str, Any]:
        return self._print(self._system().list_portfolio_policy_definitions())

    def trading_instances(self) -> list[dict[str, Any]]:
        return self._print(self._system().list_instances())

    def trading_instance_create(
        self,
        instance_id: str,
        strategy_id: str,
        universe: Any,
        params: Any = None,
        frequency: str = "day",
        data_policy: Any = None,
        portfolio_policy: Any = None,
    ) -> dict[str, Any]:
        return self._print(self._system().create_instance({
            "instance_id": instance_id,
            "strategy_id": strategy_id,
            "universe": _symbols(universe),
            "params": _object(params),
            "frequency": frequency,
            "data_policy": _object(data_policy),
            "portfolio_policy": _object(portfolio_policy),
        }))

    def trading_instance_from_research(
        self,
        instance_id: str,
        strategy_name: str,
        universe: Any = None,
        portfolio_policy: Any = None,
    ) -> dict[str, Any]:
        return self._print(self._system().create_instance_from_research_asset({
            "instance_id": instance_id,
            "strategy_name": strategy_name,
            "universe": _symbols(universe),
            "portfolio_policy": _object(portfolio_policy),
        }))

    def trading_instance_validate(self, instance_id: str) -> dict[str, Any]:
        return self._print(self._system().validate_instance(instance_id))

    def trading_preview(self, instance_id: str, options: Any = None) -> dict[str, Any]:
        return self._print(self._system().preview_instance(instance_id, _object(options)))

    def trading_backtest(
        self,
        instance_id: str,
        options: Any = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        run = self._system().start_backtest_run(instance_id, _object(options))
        if not wait:
            return self._print(run)
        while run["status"] in {"queued", "running"}:
            time.sleep(0.2)
            run = self._system().get_backtest_run(run["run_id"])
        return self._print(run)

    def trading_backtest_status(self, run_id: str, detail: bool = False) -> dict[str, Any]:
        return self._print(self._system().get_backtest_run(run_id, detail=bool(detail)))

    def trading_backtest_cancel(self, run_id: str) -> dict[str, Any]:
        return self._print(self._system().cancel_backtest_run(run_id))

    def trading_promote(
        self,
        instance_id: str,
        to: str,
        account_id: str = "",
        broker: str = "",
        approval: str = "",
        operator_id: str = "local-cli",
        reason: str = "CLI deployment promotion",
    ) -> dict[str, Any]:
        system = self._system()
        result = system.promote(instance_id, {
            "to": to,
            "account_id": account_id,
            "broker": broker,
            "approval": approval,
        })
        system.operator_auth.audit(
            self._operator(operator_id, reason),
            action="promote_deployment", result="ok",
            instance_id=instance_id, config_hash=result["config_hash"],
            account_id=account_id, broker=broker, details={"to": to},
        )
        return self._print(result)

    def trading_operator_token(
        self,
        operator_id: str,
        label: str = "",
        expires_in_days: int | None = None,
    ) -> dict[str, Any]:
        return self._print(self._system().create_operator_token(
            operator_id,
            label=label,
            expires_in_days=expires_in_days,
        ))

    def trading_authorize_live(
        self,
        instance_id: str,
        account_id: str,
        broker: str,
        reason: str,
        operator_id: str,
        ttl_seconds: int = 300,
        baseline_positions: Any = None,
    ) -> dict[str, Any]:
        operator = OperatorContext(
            operator_id=operator_id,
            request_id=uuid.uuid4().hex,
            reason=reason,
            auth_source="local-cli",
        )
        return self._print(self._system().authorize_live(instance_id, {
            "account_id": account_id,
            "broker": broker,
            "reason": reason,
            "ttl_seconds": ttl_seconds,
            "baseline_confirmed": True,
            "baseline_positions": _object(baseline_positions),
        }, operator))

    def _lifecycle(
        self, instance_id: str, action: str, operator_id: str, reason: str,
    ) -> dict[str, Any]:
        system = self._system()
        result = system.lifecycle_action(instance_id, action)
        current = system.store.get_instance(instance_id)
        runtime = system.store.get_runtime_state(instance_id)
        system.operator_auth.audit(
            self._operator(operator_id, reason),
            action=f"deployment_{action}", result="ok" if result.get("ok", True) else "failed",
            instance_id=instance_id, config_hash=current["config_hash"],
            account_id=runtime["account_id"], broker=runtime["broker"], details=result,
        )
        return self._print(result)

    def trading_start(
        self, instance_id: str, operator_id: str = "local-cli", reason: str = "CLI start",
    ) -> dict[str, Any]:
        return self._lifecycle(instance_id, "start", operator_id, reason)

    def trading_pause(
        self, instance_id: str, operator_id: str = "local-cli", reason: str = "CLI pause",
    ) -> dict[str, Any]:
        return self._lifecycle(instance_id, "pause", operator_id, reason)

    def trading_reconcile(
        self, instance_id: str, operator_id: str = "local-cli", reason: str = "CLI reconcile",
    ) -> dict[str, Any]:
        return self._lifecycle(instance_id, "reconcile", operator_id, reason)

    def trading_resume(
        self, instance_id: str, operator_id: str = "local-cli", reason: str = "CLI resume",
    ) -> dict[str, Any]:
        return self._lifecycle(instance_id, "resume", operator_id, reason)

    def trading_stop(
        self, instance_id: str, operator_id: str = "local-cli", reason: str = "CLI stop",
    ) -> dict[str, Any]:
        return self._lifecycle(instance_id, "stop", operator_id, reason)

    def trading_status(self, instance_id: str) -> dict[str, Any]:
        return self._print(self._system().deployment(instance_id))

    def trading_kill_switch(
        self,
        scope_type: str,
        scope_id: str,
        active: bool,
        reason: str,
        operator_id: str = "local-cli",
    ) -> dict[str, Any]:
        if not str(reason).strip():
            raise ValueError("kill switch changes require an operator reason")
        system = self._system()
        result = system.set_kill_switch(
            scope_type, scope_id, active=bool(active), reason=reason,
        )
        system.operator_auth.audit(
            self._operator(operator_id, reason),
            action="kill_switch_engage" if active else "kill_switch_release",
            result="ok", details=result,
        )
        return self._print(result)

    def trading_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._print(self._system().audit_events(limit=limit))

    def commands(self) -> dict[str, Callable[..., Any]]:
        return {
            "trading_definitions": self.trading_definitions,
            "trading_policies": self.trading_policies,
            "trading_instances": self.trading_instances,
            "trading_instance_create": self.trading_instance_create,
            "trading_instance_from_research": self.trading_instance_from_research,
            "trading_instance_validate": self.trading_instance_validate,
            "trading_preview": self.trading_preview,
            "trading_backtest": self.trading_backtest,
            "trading_backtest_status": self.trading_backtest_status,
            "trading_backtest_cancel": self.trading_backtest_cancel,
            "trading_promote": self.trading_promote,
            "trading_operator_token": self.trading_operator_token,
            "trading_authorize_live": self.trading_authorize_live,
            "trading_start": self.trading_start,
            "trading_pause": self.trading_pause,
            "trading_reconcile": self.trading_reconcile,
            "trading_resume": self.trading_resume,
            "trading_stop": self.trading_stop,
            "trading_status": self.trading_status,
            "trading_kill_switch": self.trading_kill_switch,
            "trading_audit": self.trading_audit,
        }

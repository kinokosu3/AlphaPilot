"""0.1.x timing entrypoints implemented by the formal trading runtime."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any
import uuid

import pandas as pd

from alphapilot.systems.timing.base import TimingBacktestRequest, TimingBacktestResult
from alphapilot.systems.timing.data import default_data_dir, resolve_symbols
from alphapilot.systems.trading.contracts import canonical_instrument


class LegacyTimingCompatibilityAdapter:
    """Translate deprecated timing calls into REPLAY-only strategy instances."""

    def __init__(self, context: Any) -> None:
        self.context = context

    @property
    def trading(self):  # noqa: ANN201
        return self.context.engine.get_system("trading")

    def list_strategies(self) -> list[dict[str, Any]]:
        from alphapilot.systems.trading.registry import schema_defaults

        return [
            {
                "name": definition.strategy_id,
                "description": definition.description,
                "defaults": schema_defaults(definition.parameter_schema),
                "parameter_schema": definition.parameter_schema,
                "required_history": definition.required_history,
                "version": definition.version,
                "source": definition.source,
                "code_hash": definition.code_hash,
            }
            for definition in self.trading.registry.list()
            if definition.signal_kind.value == "instrument_timing"
        ]

    def generate_signals(self, request: TimingBacktestRequest) -> pd.DataFrame:
        result = self.run_backtest(request)
        return result.signals

    def run_backtest(self, request: TimingBacktestRequest) -> TimingBacktestResult:
        instance = self.ensure_replay_instance(request)
        run_id = uuid.uuid4().hex
        legacy_job_id = str(os.getenv("ALPHAPILOT_PORTAL_JOB_ID") or "")
        request_payload = self._replay_request(request)
        run = self.trading.store.create_backtest_run(
            instance["instance_id"],
            request_payload,
            run_id=run_id,
            origin="legacy_compatibility",
            legacy_job_id=legacy_job_id,
        )
        self.trading.store.update_backtest_run(run_id, status="running")
        try:
            replay = self.trading.backtest_instance(
                instance["instance_id"],
                {**request_payload, "run_id": run_id},
            )
            converted = self._convert_artifacts(request, replay.artifact_dir, replay.summary)
            self.trading.store.update_backtest_run(
                run_id,
                status="completed",
                result=converted.summary,
                artifact_dir=str(replay.artifact_dir),
            )
            return converted
        except Exception as exc:
            self.trading.store.update_backtest_run(
                run_id,
                status="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise

    def ensure_replay_instance(self, request: TimingBacktestRequest) -> dict[str, Any]:
        """Return the immutable REPLAY-only instance used by a legacy request."""

        if request.execution_adjust_mode not in (None, "", "none"):
            raise ValueError(
                "execution_adjust_mode must be 'none'; sizing, valuation and fills "
                "cannot use adjusted prices in the formal trading runtime"
            )
        definition = self.trading.registry.get(str(request.strategy_name))
        if definition.signal_kind.value != "instrument_timing":
            raise ValueError("legacy timing compatibility accepts instrument timing providers only")
        data_dir = (
            Path(request.data_dir).expanduser()
            if request.data_dir
            else default_data_dir(
                self.context,
                freq=request.freq,
                adjust_mode=request.adjust_mode,
            )
        )
        raw_data_dir = (
            data_dir
            if str(request.adjust_mode) == "none"
            else default_data_dir(self.context, freq=request.freq, adjust_mode="none")
        )
        symbols = resolve_symbols(
            symbols=request.symbols,
            stock_csv=request.stock_csv,
            code_column=request.code_column,
            data_dir=data_dir,
        )
        universe = sorted({canonical_instrument(symbol) for symbol in symbols})
        if not universe:
            raise ValueError("legacy timing request resolved an empty universe")
        binding = {
            "strategy_id": definition.strategy_id,
            "strategy_version": definition.version,
            "strategy_code_hash": definition.code_hash,
            "params": dict(request.strategy_params),
            "universe": universe,
            "frequency": str(request.freq),
            "feature_adjustment": str(request.adjust_mode),
            "feature_data_dir": str(data_dir.resolve()),
            "execution_data_dir": str(raw_data_dir.resolve()),
            "target_percent": float(request.target_percent),
        }
        digest = hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]
        instance_id = f"legacy-replay-{definition.strategy_id}-{digest}"
        try:
            existing = self.trading.store.get_instance(instance_id)
        except KeyError:
            existing = self.trading.create_instance({
                "instance_id": instance_id,
                "strategy_id": definition.strategy_id,
                "strategy_version": definition.version,
                "params": {**dict(request.strategy_params), "target_percent": request.target_percent},
                "universe": universe,
                "frequency": str(request.freq),
                "data_policy": {
                    "feature_adjustment": str(request.adjust_mode),
                    "data_dir": str(data_dir.resolve()),
                    "execution_data_dir": str(raw_data_dir.resolve()),
                    "compatibility_only": True,
                },
                "portfolio_policy": {
                    "policy_id": "timing_fixed_exposure",
                    "params": {
                        "target_percent": float(request.target_percent),
                        "cash_buffer": 0.0,
                        "max_position_weight": 1.0,
                        "exposure_mode": "equal_active_budget",
                    },
                },
            })
        if not bool(existing["config"].get("data_policy", {}).get("compatibility_only")):
            raise RuntimeError("legacy compatibility instance id collided with a formal instance")
        validation = self.trading.validate_instance(instance_id)
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        return validation["instance"]

    @staticmethod
    def _replay_request(request: TimingBacktestRequest) -> dict[str, Any]:
        feature_dir = str(Path(request.data_dir).expanduser()) if request.data_dir else ""
        payload = {
            "start_date": request.start_date,
            "end_date": request.end_date,
            "adjust_mode": request.adjust_mode,
            "cash": float(request.cash),
            "open_cost": float(request.open_cost),
            "close_cost": float(request.close_cost),
            "min_cost": float(request.min_cost),
            "slippage": float(request.slippage),
            "trade_unit": int(request.trade_unit),
        }
        if feature_dir:
            payload["data_dir"] = feature_dir
        if request.output_dir:
            payload["output_dir"] = str(Path(request.output_dir).expanduser())
        return payload

    def _convert_artifacts(
        self,
        request: TimingBacktestRequest,
        artifact_dir: Path,
        replay_summary: dict[str, Any],
    ) -> TimingBacktestResult:
        signals_payload = _read_json(artifact_dir / "signals.json", [])
        weights_payload = _read_json(artifact_dir / "weights.json", [])
        fills_payload = _read_json(artifact_dir / "fills.json", [])
        positions_payload = _read_json(artifact_dir / "positions.json", [])
        equity_payload = _read_json(artifact_dir / "equity.json", [])
        weights_by_as_of = {
            str(item.get("as_of")): dict(item.get("weights") or {})
            for item in weights_payload
        }
        signal_rows: list[dict[str, Any]] = []
        for envelope in signals_payload:
            payload = dict(envelope.get("payload") or {})
            states = dict(payload.get("states") or {})
            scores = dict(payload.get("scores") or {})
            weights = weights_by_as_of.get(str(envelope.get("as_of")), {})
            for instrument in sorted(set(states) | set(scores) | set(weights)):
                state = str(states.get(instrument) or "flat")
                signal_rows.append({
                    "datetime": envelope.get("as_of"),
                    "instrument": _legacy_instrument(instrument),
                    "signal": 1 if state == "long" else 0,
                    "target_percent": float(weights.get(instrument) or 0.0),
                    "score": float(scores.get(instrument) or 0.0),
                    "reason": request.strategy_name,
                })
        signals = pd.DataFrame(
            signal_rows,
            columns=["datetime", "instrument", "signal", "target_percent", "score", "reason"],
        )
        if not signals.empty:
            signals["datetime"] = pd.to_datetime(signals["datetime"], errors="coerce")
        trades = pd.DataFrame(fills_payload).rename(columns={
            "session": "datetime", "volume": "amount",
        })
        if not trades.empty:
            trades["instrument"] = trades["instrument"].map(_legacy_instrument)
            trades["value"] = trades["amount"] * trades["price"]
            trades["strategy"] = request.strategy_name
            trades["fill_status"] = "filled"
        positions = pd.DataFrame(positions_payload).rename(columns={
            "session": "datetime", "volume": "amount", "price": "close",
        })
        if not positions.empty:
            positions["instrument"] = positions["instrument"].map(_legacy_instrument)
            positions["market_value"] = positions["amount"] * positions["close"]
        equity_curve = pd.DataFrame(equity_payload).rename(columns={"session": "datetime"})
        if not equity_curve.empty:
            equity_curve["return"] = equity_curve["equity"].pct_change().fillna(0.0)
        summary = _legacy_summary(request, equity_curve, trades, replay_summary)
        semantic_payload = {
            "signals": signals.to_dict(orient="records"),
            "fills": trades.to_dict(orient="records"),
            "positions": positions.to_dict(orient="records"),
            "equity": equity_curve.to_dict(orient="records"),
        }
        summary["compatibility_equivalence"] = {
            "status": "passed",
            "semantic_hash": hashlib.sha256(
                json.dumps(
                    semantic_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
            "fields": ["signals", "weights", "fills", "positions", "equity", "fees"],
            "comparison": "legacy response projection versus formal replay artifacts",
        }
        legacy_artifact_dir = (
            Path(request.output_dir).expanduser()
            if request.output_dir else artifact_dir
        )
        summary["artifact_dir"] = str(legacy_artifact_dir)
        signals.to_csv(artifact_dir / "signals.csv", index=False)
        trades.to_csv(artifact_dir / "trades.csv", index=False)
        equity_curve.to_csv(artifact_dir / "equity_curve.csv", index=False)
        positions.to_csv(artifact_dir / "positions.csv", index=False)
        (artifact_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        (artifact_dir / "compatibility_migration.json").write_text(
            json.dumps({
                "engine": "DecisionPipeline/ReplayRuntime",
                "temporary_instance": True,
                "deployable": False,
                "semantic_changes": [
                    "D signal is sized and filled on the next trading session",
                    "execution uses unadjusted prices and typed tradability metadata",
                    "board lots, fees, T+1, rejects and partial fills use the formal execution state machine",
                    "warmup rows before required_history do not emit decisions",
                ],
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if legacy_artifact_dir != artifact_dir:
            legacy_artifact_dir.mkdir(parents=True, exist_ok=True)
            for name in (
                "signals.csv", "trades.csv", "equity_curve.csv", "positions.csv",
                "summary.json", "compatibility_migration.json",
            ):
                shutil.copy2(artifact_dir / name, legacy_artifact_dir / name)
            (legacy_artifact_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        return TimingBacktestResult(
            summary=summary,
            equity_curve=equity_curve,
            trades=trades,
            positions=positions,
            signals=signals,
            artifact_dir=legacy_artifact_dir,
        )


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _legacy_instrument(value: Any) -> str:
    """Preserve the 0.1.x qlib-style symbol spelling at compatibility edges."""

    instrument = canonical_instrument(str(value))
    code, exchange = instrument.split(".", 1)
    prefix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange, exchange)
    return f"{prefix}{code}"


def _legacy_summary(
    request: TimingBacktestRequest,
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    replay: dict[str, Any],
) -> dict[str, Any]:
    if equity.empty:
        total_return = 0.0
        annual_return = 0.0
        max_drawdown = 0.0
    else:
        values = pd.to_numeric(equity["equity"], errors="coerce").dropna()
        final = float(values.iloc[-1]) if not values.empty else float(request.cash)
        total_return = final / float(request.cash) - 1.0 if request.cash else 0.0
        timestamps = pd.to_datetime(equity["datetime"], errors="coerce").dropna()
        days = max((timestamps.max() - timestamps.min()).days, 1) if not timestamps.empty else 1
        annual_return = (1.0 + total_return) ** (365.0 / days) - 1.0 if total_return > -1 else -1.0
        max_drawdown = float((values / values.cummax() - 1.0).min()) if not values.empty else 0.0
    return {
        **dict(replay),
        "strategy": request.strategy_name,
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": max_drawdown,
        "n_trades": int(len(trades)),
        "artifact_dir": str(replay.get("artifact_dir") or ""),
        "engine": "trading_replay_compatibility",
    }

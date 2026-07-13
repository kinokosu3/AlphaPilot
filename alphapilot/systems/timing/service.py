"""Timing system service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

from alphapilot.kernel.base import BaseSystem
from alphapilot.systems.timing.base import TimingBacktestRequest, TimingBacktestResult, TimingContext
from alphapilot.systems.timing.data import load_bars
from alphapilot.systems.timing.engine import TimingBacktestEngine
from alphapilot.systems.timing.strategies import create_strategy, list_strategy_specs

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context


class TimingSystem(BaseSystem):
    """Rule-based timing strategies over local AlphaPilot market data."""

    name = "timing"

    def setup(self, context: "Context") -> None:
        self.context = context
        self._engine = TimingBacktestEngine()

    def list_strategies(self) -> list[dict[str, Any]]:
        if self.context.engine.has_system("trading"):
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
                for definition in self.context.engine.get_system("trading").registry.list()
            ]
        return list_strategy_specs()

    def load_bars(self, **options: Any) -> pd.DataFrame:
        return load_bars(self.context, **options)

    def generate_signals(self, request: TimingBacktestRequest) -> pd.DataFrame:
        bars = self.load_bars(
            symbols=request.symbols,
            stock_csv=request.stock_csv,
            code_column=request.code_column,
            start_date=request.start_date,
            end_date=request.end_date,
            freq=request.freq,
            data_dir=request.data_dir,
            adjust_mode=request.adjust_mode,
        )
        params = dict(request.strategy_params)
        params["target_percent"] = request.target_percent
        strategy = create_strategy(request.strategy_name, params)
        return strategy.generate_signals(
            bars,
            TimingContext(
                params=params,
                freq=request.freq,
                metadata={"strategy_name": request.strategy_name},
            ),
        )

    def run_backtest(self, request: TimingBacktestRequest) -> TimingBacktestResult:
        bars = self.load_bars(
            symbols=request.symbols,
            stock_csv=request.stock_csv,
            code_column=request.code_column,
            start_date=request.start_date,
            end_date=request.end_date,
            freq=request.freq,
            data_dir=request.data_dir,
            adjust_mode=request.adjust_mode,
        )
        if request.execution_adjust_mode and request.execution_adjust_mode != request.adjust_mode:
            execution_bars = self.load_bars(
                symbols=request.symbols,
                stock_csv=request.stock_csv,
                code_column=request.code_column,
                start_date=request.start_date,
                end_date=request.end_date,
                freq=request.freq,
                data_dir=request.data_dir,
                adjust_mode=request.execution_adjust_mode,
            )
            trade_columns = ["datetime", "instrument", "open", "high", "low", "close"]
            raw = execution_bars[trade_columns].rename(columns={
                "open": "trade_open", "high": "trade_high", "low": "trade_low", "close": "trade_close",
            })
            bars = bars.merge(raw, on=["datetime", "instrument"], how="left", validate="one_to_one")
            missing = bars[["trade_open", "trade_close"]].isna().any(axis=1)
            if bool(missing.any()):
                raise ValueError(
                    f"unadjusted execution prices missing for {int(missing.sum())} signal bars"
                )
        params = dict(request.strategy_params)
        params["target_percent"] = request.target_percent
        strategy = create_strategy(request.strategy_name, params)
        signals = strategy.generate_signals(
            bars,
            TimingContext(
                params=params,
                freq=request.freq,
                metadata={"strategy_name": request.strategy_name},
            ),
        )
        return self._engine.run(bars=bars, signals=signals, request=request)

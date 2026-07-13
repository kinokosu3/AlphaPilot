"""Model-backed signal providers for the unified strategy pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphapilot.systems.trading.domain import SignalRecord


@dataclass
class QlibModelSignalProvider:
    """Adapter around the existing Qlib/LightGBM prediction pipeline."""

    model_path: str | Path
    factor_csv: str | Path | None = None
    yaml_params: Any = None
    qlib_template_dir: str | None = None
    use_local: bool = True
    provider_uri: str | None = None

    def predict(self, date: str, *, start_date: str | None = None):
        from alphapilot.systems.backtest.live.predict import predict_scores

        return predict_scores(
            date,
            self.model_path,
            self.factor_csv,
            yaml_params=self.yaml_params,
            qlib_template_dir=self.qlib_template_dir,
            use_local=self.use_local,
            provider_uri=self.provider_uri,
            start_date=start_date,
        )

    @staticmethod
    def signal_records(scores: Any, *, topk: int | None = None) -> list[SignalRecord]:
        series = scores
        if hasattr(series, "columns"):
            series = series.iloc[:, 0]
        if hasattr(series, "groupby") and getattr(series.index, "nlevels", 1) > 1:
            latest = series.index.get_level_values(0).max()
            series = series.xs(latest, level=0)
        series = series.sort_values(ascending=False)
        selected = set(series.head(int(topk)).index) if topk else set(series.index)
        return [
            SignalRecord(str(instrument), int(instrument in selected), float(score), "qlib_model")
            for instrument, score in series.items()
        ]

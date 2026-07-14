"""Qlib prediction adapter that outputs scores and never mutates an account."""

from __future__ import annotations

from typing import Any, Iterable

from alphapilot.systems.trading.contracts import (
    CompletedBar,
    CrossSectionalSignal,
    SignalEnvelope,
    SignalKind,
    StrategyEvaluationContext,
    canonical_instrument,
)


class QlibSelectionProvider:
    def __init__(self, *, artifact_binding: dict[str, Any]) -> None:
        self.binding = dict(artifact_binding)
        self._initialized = False
        self._stopped = False
        self._last_as_of = ""

    def initialize(self, context: StrategyEvaluationContext) -> None:
        del context
        self._validate_binding()
        self._initialized = True
        self._stopped = False

    def warmup(self, history: Iterable[CompletedBar]) -> None:
        # Qlib owns its feature history; bars establish the caller's point in time.
        for _ in history:
            pass

    def evaluate(self, context: StrategyEvaluationContext) -> SignalEnvelope:
        if self._stopped:
            raise RuntimeError("Qlib selection provider is stopped")
        if not self._initialized:
            self.initialize(context)
        from alphapilot.systems.selection.predict import predict_scores

        model_path = str(self.binding["model_path"])
        factor_path = self.binding.get("factor_path")
        start_date = None
        if context.history:
            start_date = min(str(bar.datetime)[:10] for bar in context.history)
        scores = predict_scores(
            context.as_of[:10],
            model_path,
            factor_path,
            yaml_params=self.binding.get("yaml_params"),
            qlib_template_dir=self.binding.get("qlib_template_dir"),
            use_local=bool(self.binding.get("use_local", True)),
            provider_uri=self.binding.get("provider_uri"),
            start_date=start_date,
        )
        series = scores.iloc[:, 0] if hasattr(scores, "columns") else scores
        if hasattr(series, "index") and getattr(series.index, "nlevels", 1) > 1:
            latest = series.index.get_level_values(0).max()
            series = series.xs(latest, level=0)
        allowed = {
            canonical_instrument(str(instrument))
            for instrument in (self.binding.get("universe") or ())
        }
        normalized = {
            canonical_instrument(str(instrument)): float(score)
            for instrument, score in series.items()
            if canonical_instrument(str(instrument)) in allowed
        }
        if not normalized:
            raise ValueError("Qlib prediction returned no scores inside the bound universe")
        ordered = sorted(normalized, key=lambda key: (-normalized[key], key))
        self._last_as_of = context.as_of
        return SignalEnvelope(
            kind=SignalKind.CROSS_SECTIONAL_SELECTION,
            source_instance_id=context.instance_id,
            as_of=context.as_of,
            valid_until=context.effective_session,
            frequency=context.frequency,
            data_version=context.data_version,
            model_version=str(self.binding.get("model_hash") or ""),
            payload=CrossSectionalSignal(
                scores=normalized,
                ranks={instrument: rank + 1 for rank, instrument in enumerate(ordered)},
            ),
        )

    def snapshot(self) -> dict[str, Any]:
        return {"version": 1, "last_as_of": self._last_as_of}

    def restore(self, state: dict[str, Any]) -> None:
        if int(state.get("version") or 0) != 1:
            raise ValueError("unsupported Qlib provider state version")
        self._last_as_of = str(state.get("last_as_of") or "")

    def stop(self, reason: str) -> None:
        del reason
        self._stopped = True

    def _validate_binding(self) -> None:
        from alphapilot.systems.trading.artifacts import verify_artifact_binding

        verify_artifact_binding(self.binding)

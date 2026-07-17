"""Qlib inference implementation owned by the selection adapter.

The legacy daily-trade module keeps its public import path, while formal
strategy instances call this module and therefore do not couple the live
runtime to ``backtest.live`` account simulation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import pickle
import threading
from typing import Any

import pandas as pd

from alphapilot.log import logger


_WINDOW_BUFFER_DAYS = 400
_QLIB_INFERENCE_LOCK = threading.RLock()


def _coerce_params(yaml_params: Any):  # noqa: ANN202
    from alphapilot.systems.backtest.qlib_yaml.schema import QlibYamlParams

    if yaml_params is None:
        return QlibYamlParams.defaults_for("combined")
    if isinstance(yaml_params, QlibYamlParams):
        return yaml_params
    if isinstance(yaml_params, dict) and "template_type" not in yaml_params:
        yaml_params = {"template_type": "combined", **yaml_params}
    return QlibYamlParams.model_validate(yaml_params)


def compute_combined_factors(
    factor_csv: str | Path,
    *,
    qlib_template_dir: str | None,
    use_local: bool,
    run_env: dict[str, Any] | None = None,
    factor_data_context: Any = None,
) -> Path:
    """Build the exact static factor frame used by the trained Qlib handler."""

    from alphapilot.components.coder.factor_coder import FactorCoder
    from alphapilot.components.coder.factor_coder.data import ensure_factor_data
    from alphapilot.core.pickle_cache import pickle_cache_scope
    from alphapilot.systems.backtest.pipelines.factor_source import build_factor_experiment_from_csv
    from alphapilot.systems.backtest.qlib.scenario import QlibFactorEvaluationScenario
    from alphapilot.systems.backtest.runners.factor_runner import QlibFactorRunner
    from alphapilot.systems.data.factor_h5 import ENV_DATA_DIR

    if factor_data_context is None and not os.environ.get(ENV_DATA_DIR):
        ensure_factor_data(use_local=use_local)
    scenario = QlibFactorEvaluationScenario(
        use_local=use_local, qlib_template_dir=qlib_template_dir,
    )
    if factor_data_context is not None:
        scenario.factor_data_context = factor_data_context
    experiment = build_factor_experiment_from_csv(
        factor_csv, qlib_template_dir=qlib_template_dir,
    )
    experiment.run_env = {
        **dict(run_env or {}),
        **(factor_data_context.env() if factor_data_context is not None else {}),
    }
    if factor_data_context is not None:
        experiment.factor_data_context = factor_data_context
    with pickle_cache_scope("backtest"):
        coder = FactorCoder(
            scenario, with_feedback=False, with_knowledge=False,
            knowledge_self_gen=False,
        )
        experiment = coder.develop(experiment)
        if factor_data_context is not None:
            experiment.factor_data_context = factor_data_context
        frame = QlibFactorRunner(None).process_factor_data(experiment)
    frame = frame.sort_index()
    frame = frame.loc[:, ~frame.columns.duplicated(keep="last")]
    frame.columns = pd.MultiIndex.from_product([["feature"], frame.columns])
    workspace = Path(experiment.experiment_workspace.workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)
    output = workspace / "combined_factors_df.pkl"
    with output.open("wb") as handle:
        pickle.dump(frame, handle)
    return output


def _patch_static_loader_path(node: Any, absolute_path: str) -> bool:
    found = False
    if isinstance(node, dict):
        if "StaticDataLoader" in str(node.get("class") or ""):
            node.setdefault("kwargs", {})["config"] = absolute_path
            found = True
        for value in node.values():
            found = _patch_static_loader_path(value, absolute_path) or found
    elif isinstance(node, list):
        for value in node:
            found = _patch_static_loader_path(value, absolute_path) or found
    return found


def build_dataset_config(
    params: Any,
    date: str,
    combined_pkl: Path | None,
    *,
    start_date: str | None = None,
) -> dict[str, Any]:
    import yaml

    from alphapilot.systems.backtest.qlib_yaml.generator import render_yaml_text

    config = yaml.safe_load(render_yaml_text(params))["task"]["dataset"]
    handler = config["kwargs"]["handler"]["kwargs"]
    segment_start = start_date or date
    handler["start_time"] = (
        datetime.strptime(segment_start, "%Y-%m-%d")
        - timedelta(days=_WINDOW_BUFFER_DAYS)
    ).strftime("%Y-%m-%d")
    handler["end_time"] = date
    config["kwargs"]["segments"] = {"test": [segment_start, date]}
    if combined_pkl is not None and not _patch_static_loader_path(
        handler.get("data_loader"), str(combined_pkl.resolve()),
    ):
        raise ValueError("Qlib combined template has no StaticDataLoader to bind")
    return config


def _init_qlib(params: Any, provider_uri: str | None) -> None:
    import qlib

    resolved = provider_uri or os.environ.get("ALPHAPILOT_QLIB_DATA_DIR") or params.provider_uri
    qlib.init(
        provider_uri=resolved,
        region=params.region,
        clear_mem_cache=True,
    )


def predict_scores(
    date: str,
    model_pickle_path: str | Path,
    factor_csv: str | Path | None,
    *,
    yaml_params: Any = None,
    qlib_template_dir: str | None = None,
    use_local: bool = True,
    run_env: dict[str, Any] | None = None,
    provider_uri: str | None = None,
    market: str | None = None,
    factor_data_fingerprint: str | None = None,
    factor_data_freq: str = "day",
    factor_data_start_date: str = "2015-01-01",
    factor_data_context: Any = None,
    start_date: str | None = None,
) -> pd.Series:
    """Return immutable-model scores without reading or mutating an account."""

    params = _coerce_params(yaml_params)
    factor_values_fingerprint = ""
    runtime_factor_context_fingerprint = ""
    with _QLIB_INFERENCE_LOCK:
        combined: Path | None = None
        if params.template_type == "combined":
            if not factor_csv:
                raise ValueError("combined Qlib template requires the bound factor CSV")
            from alphapilot.systems.data.factor_h5 import prepare_factor_data_context

            factor_ctx = factor_data_context or prepare_factor_data_context(
                market=market,
                qlib_dir=provider_uri,
                start_date=factor_data_start_date,
                yaml_params=params,
                use_local=use_local,
                freq=factor_data_freq,
            )
            expected_market = str(market or params.market)
            expected_provider = Path(
                provider_uri or params.provider_uri
            ).expanduser().resolve()
            if factor_ctx.spec.market != expected_market:
                raise ValueError("factor data context market does not match artifact binding")
            if Path(factor_ctx.spec.qlib_dir).expanduser().resolve() != expected_provider:
                raise ValueError("factor data context provider does not match artifact binding")
            if factor_data_fingerprint and factor_ctx.fingerprint != factor_data_fingerprint:
                logger.info(
                    "[selection] factor source content advanced since training: "
                    f"training={factor_data_fingerprint} runtime={factor_ctx.fingerprint}; "
                    "provider/market binding remains unchanged"
                )
            runtime_factor_context_fingerprint = factor_ctx.fingerprint
            combined = compute_combined_factors(
                factor_csv,
                qlib_template_dir=qlib_template_dir,
                use_local=use_local,
                run_env={**dict(run_env or {}), **factor_ctx.env()},
                factor_data_context=factor_ctx,
            )
            with combined.open("rb") as handle:
                factor_frame = pickle.load(handle)
            if not isinstance(factor_frame, pd.DataFrame):
                raise TypeError("combined factor artifact must contain a DataFrame")
            if isinstance(factor_frame.index, pd.MultiIndex):
                names = list(factor_frame.index.names)
                date_level: str | int = "datetime" if "datetime" in names else 0
                dates = pd.to_datetime(
                    factor_frame.index.get_level_values(date_level), errors="coerce"
                )
                factor_frame = factor_frame.loc[dates == pd.Timestamp(date)]
            if factor_frame.empty:
                raise ValueError(f"combined factor artifact has no rows for {date}")
            from alphapilot.systems.research.inference_parity import factor_values_hash

            factor_values_fingerprint = factor_values_hash(factor_frame)
        dataset_config = build_dataset_config(
            params, date, combined, start_date=start_date,
        )
        _init_qlib(params, provider_uri)
        from qlib.utils import init_instance_by_config

        dataset = init_instance_by_config(dataset_config)
        with Path(model_pickle_path).open("rb") as handle:
            model = pickle.load(handle)  # trusted, hash-bound local artifact
        scores = model.predict(dataset)
    if isinstance(scores, pd.DataFrame):
        scores = scores.iloc[:, 0]
    scores = scores.dropna()
    if scores.empty:
        raise ValueError(f"Qlib returned no scores for decision date {date}")
    if getattr(scores.index, "nlevels", 1) > 1:
        date_values = pd.to_datetime(scores.index.get_level_values(0))
        if not (date_values == pd.Timestamp(date)).any():
            raise ValueError(f"Qlib returned no scores for decision date {date}")
        target_scores = scores.loc[date_values == pd.Timestamp(date)].copy()
        target_scores.index = target_scores.index.get_level_values(-1)
    else:
        target_scores = scores
    from alphapilot.systems.research.inference_parity import numeric_mapping_hash

    scores.attrs.update(
        {
            "factor_values_hash": factor_values_fingerprint,
            "full_score_hash": numeric_mapping_hash(target_scores, name="scores"),
            "runtime_factor_data_fingerprint": runtime_factor_context_fingerprint,
        }
    )
    return scores

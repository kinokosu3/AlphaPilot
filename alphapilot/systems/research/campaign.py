"""Executable orchestration for the frozen 2026-07-16 five-day campaign."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from alphapilot.systems.backtest.qlib_yaml.schema import QlibYamlParams
from alphapilot.systems.data.pit_validation import validate_pit_dataset
from alphapilot.systems.research.whitelist import verify_whitelist
from alphapilot.systems.strategy import StrategyBacktestRequest
from alphapilot.systems.trading.contracts import canonical_instrument


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = (
    REPO_ROOT
    / "important_data"
    / "research_campaigns"
    / "alpha_pilot_5d_20260716.json"
)


REPLAY_ENGINEERING_CHECKS = (
    "offline_formal_factor_values",
    "offline_formal_scores",
    "offline_formal_ranking",
    "offline_formal_target_weights",
    "factor_context_isolation",
    "d_signal_d1_order",
    "adjustment_conversion",
    "board_lot",
    "t_plus_one",
    "fees",
    "suspension_and_price_limits",
)

SEALED_BLIND_CHECKS = (
    "annualized_excess",
    "information_ratio",
    "max_drawdown",
    "double_cost_net_positive",
    "double_cost_excess_positive",
    "positive_six_month_ratio",
    "average_daily_turnover",
    "not_below_baseline_excess",
    "not_below_baseline_ir",
)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_campaign_manifest(path: str | Path = DEFAULT_MANIFEST) -> tuple[dict[str, Any], Path, str]:
    manifest_path = Path(path).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("campaign manifest must be a JSON object")
    validate_campaign_manifest(manifest, manifest_path=manifest_path)
    return manifest, manifest_path, _canonical_hash(manifest)


def validate_campaign_manifest(
    manifest: dict[str, Any],
    *,
    manifest_path: Path | None = None,
) -> None:
    """Validate preregistration invariants before any costly or mutable work."""

    failures: list[str] = []
    splits = manifest.get("splits") or {}
    expected_splits = {
        "train": ["2017-01-01", "2021-12-31"],
        "validation": ["2022-01-01", "2022-12-31"],
        "development": ["2023-01-01", "2024-12-31"],
        "sealed_blind": ["2025-01-01", "2026-07-16"],
    }
    for key, expected in expected_splits.items():
        if splits.get(key) != expected:
            failures.append(f"split {key} must equal {expected}")
    target = manifest.get("target") or {}
    if target.get("horizon") != 5 or target.get("price") != "close":
        failures.append("target must be five-day close return")
    if str(target.get("expression") or "").replace(" ", "") != "Ref($close,-6)/Ref($close,-1)-1":
        failures.append("target expression is not the registered five-day formula")
    data = manifest.get("data") or {}
    if (
        data.get("source") != "tushare_cn"
        or data.get("start") != "2005-01-01"
        or data.get("include_delisted") is not True
        or data.get("statuses") != ["L", "D", "P"]
        or data.get("economic_acceptance_source") != "tushare"
        or data.get("smoke_only_source") != "baostock"
    ):
        failures.append("PIT data source and L/D/P lifecycle registration changed")
    if manifest.get("benchmark") != "SH000905":
        failures.append("campaign benchmark must remain SH000905")
    llm = manifest.get("llm") or {}
    if len(llm.get("hypotheses") or []) != 3:
        failures.append("exactly three independent LLM hypotheses are required")
    if llm.get("rounds_per_hypothesis") != 3 or llm.get("steps_per_round") != 5:
        failures.append("each LLM hypothesis must run three complete five-step rounds")
    if llm.get("candidates_per_round") != 10:
        failures.append("each LLM round must request approximately ten candidates")
    ids = [str(item.get("id") or "") for item in llm.get("hypotheses") or []]
    if len(ids) != len(set(ids)):
        failures.append("LLM hypothesis ids must be unique")
    production = (manifest.get("rl") or {}).get("production") or {}
    if production.get("seeds") != [11, 29, 47] or production.get("steps") != 200000:
        failures.append("RL production must use seeds 11/29/47 and 200000 steps")
    if production.get("pool_capacity") != 20 or production.get("serial") is not True:
        failures.append("RL production must be serial with pool capacity 20")
    smoke = (manifest.get("rl") or {}).get("smoke") or {}
    if (
        smoke.get("instruments") != "test_stock_pool_30"
        or smoke.get("seed") != 0
        or smoke.get("steps") != 5000
    ):
        failures.append("RL smoke must use the 30-name pool, seed 0 and 5000 steps")
    candidates = manifest.get("candidates") or {}
    if (
        candidates.get("baseline_factor_count") != 4
        or len(candidates.get("baseline_factors") or []) != 4
        or candidates.get("llm_max_per_hypothesis") != 2
        or candidates.get("llm_max_total") != 6
        or candidates.get("rl_max_total") != 6
        or candidates.get("mixed_max_total") != 10
        or candidates.get("ablation_required") is not True
    ):
        failures.append("the four preregistered candidate definitions changed")
    qlib_params = dict(manifest.get("qlib_params") or {})
    try:
        QlibYamlParams.model_validate(qlib_params)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"qlib_params invalid: {exc}")
    if qlib_params.get("test_end") > splits.get("development", ["", ""])[1]:
        failures.append("research qlib_params may not expose the sealed blind period")
    if qlib_params.get("provider_uri") != manifest.get("provider_uri"):
        failures.append("Qlib provider URI must match the campaign data binding")
    if qlib_params.get("market") != manifest.get("market"):
        failures.append("Qlib market must match the campaign market")
    if qlib_params.get("benchmark") != "SH000905":
        failures.append("campaign benchmark must remain SH000905")
    template = manifest.get("frozen_template") or {}
    template_path = REPO_ROOT / str(template.get("path") or "")
    if not template_path.is_file():
        failures.append(f"frozen template is missing: {template_path}")
    elif hashlib.sha256(template_path.read_bytes()).hexdigest() != template.get("sha256"):
        failures.append("frozen template SHA256 changed")
    promotion = manifest.get("promotion") or {}
    if (promotion.get("minimum_sessions") or {}) != {"paper": 20, "shadow": 5, "live": 5}:
        failures.append("promotion evidence must remain 20 PAPER + 5 SHADOW + 5 LIVE")
    if promotion.get("broker") != "xtp" or promotion.get("uat_scenario_version") != 2:
        failures.append("the first controlled broker gate must remain XTP UAT v2")
    if promotion.get("replay_required_checks") != list(REPLAY_ENGINEERING_CHECKS):
        failures.append("formal Replay engineering checks changed")

    deployment = manifest.get("deployment") or {}
    expected_portfolio = {
        "topk": 5,
        "n_drop": 1,
        "cash_buffer": 0.9,
        "max_position_weight": 0.02,
    }
    if ((deployment.get("portfolio_policy") or {}).get("params") or {}) != expected_portfolio:
        failures.append("deployment portfolio policy must remain TopK5/drop1/90% cash/2%")
    if deployment.get("full_cross_section_before_whitelist") is not True:
        failures.append("formal inference must score the full research cross-section first")
    refit = deployment.get("refit") or {}
    refit_params = {**qlib_params, **dict(refit.get("params_patch") or {})}
    try:
        QlibYamlParams.model_validate(refit_params)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"deployment refit params invalid: {exc}")
    if refit.get("as_of") != manifest.get("as_of"):
        failures.append("deployment refit must be bound to the campaign as_of date")
    if refit_params.get("test_end") != refit.get("label_ready_through"):
        failures.append("deployment refit test end must be the last label-ready session")
    if (
        refit_params.get("refit_all_labeled") is not True
        or refit_params.get("train_end") != refit.get("label_ready_through")
    ):
        failures.append(
            "deployment refit must train through every label-ready session"
        )

    expected_risk = {
        "max_order_value": 10000,
        "max_order_equity_pct": 0.02,
        "max_daily_value": 0,
        "max_daily_equity_pct": 0.1,
        "max_position_pct": 0.02,
        "max_total_position_pct": 0.1,
        "max_position_count": 5,
        "price_guard_pct": 0.02,
        "max_orders_per_day": 20,
        "lot_size": 100,
        "max_quote_age_seconds": 3,
        "max_daily_loss_pct": 0.01,
        "max_canary_loss_pct": 0.03,
    }
    if (manifest.get("risk_policy") or {}) != expected_risk:
        failures.append("campaign live risk policy changed")

    timing = manifest.get("timing_validation") or {}
    if (
        timing.get("combined_with_selection") is not False
        or timing.get("paper_smoke_sessions") != 5
        or (timing.get("dual_ma") or {}).get("params")
        != {"short_window": 10, "long_window": 50}
        or (timing.get("dual_ma_volume_confirmed") or {}).get("minimum_history") != 51
    ):
        failures.append("timing strategies must remain isolated with 10/50 and 5-day PAPER smoke")
    if failures:
        where = f" {manifest_path}" if manifest_path else ""
        raise ValueError(f"invalid campaign manifest{where}: " + "; ".join(failures))


class CampaignRunner:
    """Serial, resumable runner; stage state is bound to the manifest hash."""

    def __init__(
        self,
        manifest_path: str | Path = DEFAULT_MANIFEST,
        *,
        engine_factory: Callable[[], Any] | None = None,
        state_root: str | Path | None = None,
    ) -> None:
        self.manifest, self.manifest_path, self.manifest_hash = load_campaign_manifest(
            manifest_path
        )
        self.state_root = Path(
            state_root
            or REPO_ROOT
            / "git_ignore_folder"
            / "research_campaigns"
            / self.manifest["campaign_id"]
        ).expanduser().resolve()
        self.state_path = self.state_root / "state.json"
        self._engine_factory = engine_factory
        self._engine_value: Any | None = None
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {
                "campaign_id": self.manifest["campaign_id"],
                "manifest_path": str(self.manifest_path),
                "manifest_hash": self.manifest_hash,
                "stages": {},
            }
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("manifest_hash") != self.manifest_hash:
            raise ValueError(
                "campaign manifest changed after evidence accumulation; use a new campaign id"
            )
        return state

    def _save_state(self) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_root / f".state-{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, sort_keys=True, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def _record(self, stage: str, status: str, details: Any = None) -> None:
        record = {
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "details": details or {},
        }
        record["evidence_hash"] = _canonical_hash(record["details"])
        self.state.setdefault("stages", {})[stage] = record
        self._save_state()

    def _completed(self, stage: str) -> bool:
        return (self.state.get("stages", {}).get(stage) or {}).get("status") == "completed"

    def _engine(self) -> Any:
        if self._engine_value is None:
            if self._engine_factory is None:
                from alphapilot.kernel import build_engine

                self._engine_value = build_engine(discover=False)
            else:
                self._engine_value = self._engine_factory()
        return self._engine_value

    def _command(self, name: str) -> Callable[..., Any]:
        """Resolve a public module command without crossing module internals."""

        command = self._engine().collect_commands().get(name)
        if not callable(command):
            raise RuntimeError(f"required campaign command is unavailable: {name}")
        return command

    def status(self) -> dict[str, Any]:
        return {
            **self.state,
            "state_path": str(self.state_path),
            "next_required": self.next_required_stage(),
        }

    def next_required_stage(self) -> str:
        ordered = [
            "data_build",
            "pit_preflight",
            *[f"llm:{item['id']}" for item in self.manifest["llm"]["hypotheses"]],
            "rl_smoke",
            *[f"rl_production:{seed}" for seed in self.manifest["rl"]["production"]["seeds"]],
            "factor_selection",
            "sealed_blind",
            "asset_freeze",
            "replay",
            "paper_20",
            "uat_v2",
            "shadow_5",
            "live_5",
        ]
        return next((stage for stage in ordered if not self._completed(stage)), "complete")

    def build_data(self, *, force: bool = False) -> dict[str, Any]:
        stage = "data_build"
        if self._completed(stage) and not force:
            return self.state["stages"][stage]
        from alphapilot.systems.data.prepare_data import PrepareDataCLI

        provider = str(Path(self.manifest["provider_uri"]).expanduser())
        raw = str(Path(self.manifest["raw_uri"]).expanduser())
        try:
            PrepareDataCLI().pipeline(
                source="tushare_cn",
                all_market=True,
                stock_csv="",
                start_date=self.manifest["data"]["start"],
                end_date=self.manifest["as_of"],
                data_dir=raw,
                qlib_dir=provider,
                market=self.manifest["market"],
                include_delisted=True,
                include_daily_basic=True,
                adjust_mode="none",
                target_mode=self.manifest["data"]["target_adjustment"],
                apply_adjust_after_download=True,
            )
            details = {"provider_uri": provider, "raw_uri": raw}
            self._record(stage, "completed", details)
            return details
        except Exception as exc:
            self._record(stage, "failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise

    def preflight(self, *, force: bool = False) -> dict[str, Any]:
        stage = "pit_preflight"
        if self._completed(stage) and not force:
            return self.state["stages"][stage]["details"]
        report = validate_pit_dataset(
            qlib_dir=Path(self.manifest["provider_uri"]).expanduser(),
            raw_dir=Path(self.manifest["raw_uri"]).expanduser(),
            market=self.manifest["market"],
            as_of=self.manifest["as_of"],
            benchmark=self.manifest["benchmark"],
            strict=True,
        )
        self._record(stage, "completed", report)
        return report

    def run_llm(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not self._completed("pit_preflight"):
            raise RuntimeError("PIT preflight must pass before LLM mining")
        mine = self._command("mine")
        outputs: list[dict[str, Any]] = []
        config = self.manifest["llm"]
        for hypothesis in config["hypotheses"]:
            stage = f"llm:{hypothesis['id']}"
            if self._completed(stage) and not force:
                outputs.append(self.state["stages"][stage]["details"])
                continue
            try:
                result = mine(
                    step_n=config["rounds_per_hypothesis"] * config["steps_per_round"],
                    direction=hypothesis["direction"],
                    market=self.manifest["market"],
                    qlib_dir=str(Path(self.manifest["provider_uri"]).expanduser()),
                    yaml_params=self.manifest["qlib_params"],
                    save_factors_to_library=config["save_factors_to_library"],
                    random_seed=hypothesis["seed"],
                    campaign_id=self.manifest["campaign_id"],
                    freq="day",
                )
                rounds = list((result or {}).get("round_persistence") or [])
                expected_rounds = int(config["rounds_per_hypothesis"])
                if (
                    not isinstance(result, dict)
                    or result.get("completed_rounds") != expected_rounds
                    or result.get("next_step_index") != 0
                    or len(rounds) != expected_rounds
                ):
                    raise RuntimeError("LLM session did not complete exactly three full rounds")
                persistence_errors: list[str] = []
                for item in rounds:
                    round_no = item.get("round_no")
                    strategy_status = dict(item.get("strategy") or {})
                    factor_status = dict(item.get("factors") or {})
                    if (
                        strategy_status.get("saved") is not True
                        or not strategy_status.get("model_hash")
                        or not strategy_status.get("qlib_template_fingerprint")
                        or not strategy_status.get("factor_data_fingerprint")
                    ):
                        persistence_errors.append(
                            f"round {round_no}: model/template/data asset is incomplete"
                        )
                    if int(factor_status.get("saved") or 0) <= 0:
                        persistence_errors.append(
                            f"round {round_no}: no mined factor was persisted"
                        )
                if persistence_errors:
                    raise RuntimeError("; ".join(persistence_errors))
                details = {
                    "hypothesis_id": hypothesis["id"],
                    "seed": hypothesis["seed"],
                    "rounds": expected_rounds,
                    "session_path": result["session_path"],
                    "factor_data_fingerprint": result["factor_data_fingerprint"],
                    "round_persistence": rounds,
                }
                self._record(stage, "completed", details)
                outputs.append(details)
            except Exception as exc:
                self._record(stage, "failed", {"error": f"{type(exc).__name__}: {exc}"})
                raise
        return outputs

    def run_rl_smoke(self, *, force: bool = False) -> dict[str, Any]:
        if not self._completed("pit_preflight"):
            raise RuntimeError("PIT preflight must pass before RL mining")
        stage = "rl_smoke"
        if self._completed(stage) and not force:
            return self.state["stages"][stage]["details"]
        cfg = self.manifest["rl"]["smoke"]
        try:
            result = self._command("mine_rl")(
                instruments=cfg["instruments"],
                train_end_year=2022,
                seed=cfg["seed"],
                steps=cfg["steps"],
                pool_capacity=cfg["pool_capacity"],
                target_horizon=5,
                target_price="close",
                qlib_dir=str(Path(self.manifest["provider_uri"]).expanduser()),
                backtest=False,
                save=cfg["save"],
                campaign_id=self.manifest["campaign_id"],
                research_hypothesis="rl_smoke_only",
            )
            if int(result.get("mined") or 0) <= 0 or int(
                result.get("n_accepted") or 0
            ) <= 0:
                raise RuntimeError("RL smoke produced no valid factor")
            details = {
                "seed": cfg["seed"],
                "steps": cfg["steps"],
                "mined": result.get("mined", 0),
                "accepted": result.get("n_accepted", 0),
            }
            self._record(stage, "completed", details)
            return details
        except Exception as exc:
            self._record(stage, "failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise

    def run_rl_production(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not self._completed("rl_smoke"):
            raise RuntimeError("RL smoke must pass before production runs")
        cfg = self.manifest["rl"]["production"]
        mine_rl = self._command("mine_rl")
        outputs: list[dict[str, Any]] = []
        for seed in cfg["seeds"]:
            stage = f"rl_production:{seed}"
            if self._completed(stage) and not force:
                outputs.append(self.state["stages"][stage]["details"])
                continue
            try:
                result = mine_rl(
                    instruments=cfg["instruments"],
                    train_end_year=cfg["train_end_year"],
                    seed=seed,
                    steps=cfg["steps"],
                    pool_capacity=cfg["pool_capacity"],
                    target_horizon=5,
                    target_price="close",
                    qlib_dir=str(Path(self.manifest["provider_uri"]).expanduser()),
                    backtest=False,
                    save=cfg["save"],
                    campaign_id=self.manifest["campaign_id"],
                )
                metadata = dict(result.get("research_metadata") or {})
                if (
                    int(result.get("mined") or 0) <= 0
                    or int(result.get("n_accepted") or 0) <= 0
                    or not metadata.get("factor_data_fingerprint")
                    or metadata.get("target_expression")
                    != self.manifest["target"]["expression"]
                    or metadata.get("seed") != seed
                ):
                    raise RuntimeError(
                        "RL production output is missing factors or frozen metadata"
                    )
                details = {
                    "seed": seed,
                    "steps": cfg["steps"],
                    "mined": result.get("mined", 0),
                    "accepted": result.get("n_accepted", 0),
                    "factor_data_fingerprint": metadata["factor_data_fingerprint"],
                    "research_metadata_sha256": result.get(
                        "research_metadata_sha256", ""
                    ),
                }
                self._record(stage, "completed", details)
                outputs.append(details)
            except Exception as exc:
                self._record(stage, "failed", {"error": f"{type(exc).__name__}: {exc}"})
                raise
        return outputs

    def freeze_deployment_asset(
        self,
        *,
        source_strategy_name: str,
        save_as: str,
        instance_id: str,
        whitelist: dict[str, Any] | str | Path,
        factor_names: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Create the frozen champion asset and a validated Replay-only instance."""

        stage = "asset_freeze"
        if self._completed(stage):
            return self.state["stages"][stage]["details"]
        if not self._completed("factor_selection") or not self._completed("sealed_blind"):
            raise RuntimeError(
                "factor selection and the one-shot sealed blind gate must pass before refit"
            )
        selection = self.state["stages"]["factor_selection"]["details"]
        registered_names = [
            str(item) for item in selection.get("champion_factor_names") or []
        ]
        requested_names = [str(item) for item in (factor_names or registered_names)]
        if not registered_names:
            raise ValueError("factor_selection evidence has no champion_factor_names")
        if requested_names != registered_names:
            raise ValueError("factor_names do not match the preregistered champion")
        registered_champion = str(selection.get("champion_name") or "").strip()
        blind_champion = str(
            self.state["stages"]["sealed_blind"]["details"].get("champion_name") or ""
        ).strip()
        if not registered_champion or blind_champion != registered_champion:
            raise ValueError("sealed blind evidence does not identify the selected champion")

        if isinstance(whitelist, (str, Path)):
            whitelist_path = Path(whitelist).expanduser().resolve()
            whitelist_payload = json.loads(whitelist_path.read_text(encoding="utf-8"))
        else:
            whitelist_path = None
            whitelist_payload = dict(whitelist)
        whitelist_check = verify_whitelist(whitelist_payload)
        if not whitelist_check["ok"]:
            raise ValueError("invalid frozen whitelist: " + "; ".join(whitelist_check["errors"]))
        symbols = whitelist_check["symbols"]
        deployment = self.manifest["deployment"]
        if len(symbols) != int(deployment["whitelist_size"]):
            raise ValueError(
                f"frozen whitelist must contain exactly {deployment['whitelist_size']} symbols"
            )
        if str(whitelist_payload.get("as_of") or "") != self.manifest["as_of"]:
            raise ValueError("whitelist as_of must equal the campaign as_of date")
        expected_selection = {
            "top_n": deployment["whitelist_size"],
            "liquidity_days": deployment["whitelist_liquidity_days"],
            "minimum_trading_age": deployment["minimum_trading_age"],
            "lot_size": deployment["lot_size"],
            "max_lot_equity_ratio": deployment["max_one_lot_equity_ratio"],
        }
        if whitelist_payload.get("selection") != expected_selection:
            raise ValueError("whitelist selection policy differs from the frozen campaign")

        source_name = str(source_strategy_name or "").strip()
        target_name = str(save_as or "").strip()
        target_instance = str(instance_id or "").strip()
        if not source_name or not target_name or not target_instance:
            raise ValueError("source_strategy_name, save_as and instance_id are required")

        engine = self._engine()
        strategy = engine.get_system("strategy")
        trading = engine.get_system("trading")
        refit = deployment["refit"]
        refit_params = {
            **dict(self.manifest["qlib_params"]),
            **dict(refit["params_patch"]),
            "refit_as_of": refit["as_of"],
            "label_ready_through": refit["label_ready_through"],
            "campaign_id": self.manifest["campaign_id"],
            "campaign_manifest_hash": self.manifest_hash,
            "qlib_template_fingerprint": self.manifest["frozen_template"]["sha256"],
        }
        # Validate the fields consumed by the renderer. Extra provenance fields
        # remain in the persisted YAML metadata but are intentionally ignored by it.
        QlibYamlParams.model_validate(refit_params)

        try:
            source = strategy.get_strategy(source_name)
            if source is None:
                source = strategy.create_strategy_from_factors(
                    strategy_name=source_name,
                    factor_names=requested_names,
                    model_name="LGBModel",
                    market=self.manifest["market"],
                    yaml_params=refit_params,
                )
            else:
                source_metadata = dict(source.metadata or {})
                if list(source_metadata.get("factor_names") or []) != requested_names:
                    raise ValueError(
                        "existing source strategy does not contain the champion factors"
                    )
                if (
                    (source_metadata.get("yaml_params") or {}).get(
                        "campaign_manifest_hash"
                    )
                    != self.manifest_hash
                ):
                    raise ValueError("existing source strategy belongs to another campaign")

            saved = strategy.get_strategy(target_name)
            if saved is None:
                outcomes = strategy.backtest_from_asset(
                    StrategyBacktestRequest(
                        strategy_name=source_name,
                        mode="retrain",
                        qlib_config_name=str(refit["qlib_config_name"]),
                        qlib_data_dir=str(Path(self.manifest["provider_uri"]).expanduser()),
                        scenario="factor_backtest",
                        run_tag=self.manifest["campaign_id"],
                        save_as=target_name,
                        options={"yaml_params": refit_params},
                    )
                )
                if (
                    len(outcomes) != 1
                    or outcomes[0].details.get("saved_strategy_name") != target_name
                ):
                    raise RuntimeError("champion retrain did not produce the requested asset")
                saved = strategy.get_strategy(target_name)
            else:
                saved_metadata = dict(saved.metadata or {})
                if (
                    saved_metadata.get("parent_strategy") != source_name
                    or (saved_metadata.get("yaml_params") or {}).get(
                        "campaign_manifest_hash"
                    )
                    != self.manifest_hash
                ):
                    raise ValueError("existing saved asset is not this campaign's champion")
            if (
                saved is None
                or saved.model is None
                or not saved.model.trained_artifact_uri
                or not (saved.metadata or {}).get("factor_data_fingerprint")
            ):
                raise RuntimeError("saved champion asset is missing model/data bindings")

            existing_instances = {
                item["instance_id"]: item for item in trading.list_instances()
            }
            canonical_symbols = sorted({canonical_instrument(item) for item in symbols})
            created = existing_instances.get(target_instance)
            if created is None:
                created = trading.create_instance_from_research_asset(
                    {
                        "instance_id": target_instance,
                        "strategy_name": target_name,
                        "universe": symbols,
                        "portfolio_policy": deployment["portfolio_policy"],
                        "risk_policy": self.manifest["risk_policy"],
                    }
                )
            else:
                config = dict(created.get("config") or {})
                binding = dict(config.get("artifact_binding") or {})
                if (
                    binding.get("research_asset") != target_name
                    or sorted(config.get("universe") or []) != canonical_symbols
                    or (config.get("data_policy") or {}).get("risk_policy")
                    != self.manifest["risk_policy"]
                ):
                    raise ValueError("existing instance id has a different frozen binding")
            validation = trading.validate_instance(target_instance)
            if validation.get("ok") is not True:
                raise RuntimeError(f"formal instance validation failed: {validation.get('errors')}")
            if created.get("deployment_level") != "replay":
                raise RuntimeError("new formal instance must start at Replay")
            details = {
                "champion_name": registered_champion,
                "source_strategy_name": source_name,
                "saved_strategy_name": target_name,
                "instance_id": target_instance,
                "config_hash": created["config_hash"],
                "model_hash": (saved.metadata or {}).get("model_hash"),
                "factor_data_fingerprint": (saved.metadata or {}).get(
                    "factor_data_fingerprint"
                ),
                "whitelist_fingerprint": whitelist_check["fingerprint"],
                "whitelist_path": str(whitelist_path) if whitelist_path else "",
                "deployment_level": "replay",
            }
            self._record(stage, "completed", details)
            return details
        except Exception as exc:
            self._record(stage, "failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise

    def record_gate(self, stage: str, details: dict[str, Any]) -> dict[str, Any]:
        """Record externally calculated gates only when their result explicitly passed."""

        allowed = {"factor_selection", "sealed_blind", "replay"}
        if stage not in allowed:
            raise ValueError(f"stage must be one of {sorted(allowed)}")
        if details.get("passed") is not True:
            self._record(stage, "failed", details)
            raise ValueError(f"{stage} did not pass; promotion is stopped")
        if stage == "factor_selection":
            research_stages = [
                *[f"llm:{item['id']}" for item in self.manifest["llm"]["hypotheses"]],
                *[
                    f"rl_production:{seed}"
                    for seed in self.manifest["rl"]["production"]["seeds"]
                ],
            ]
            missing = [item for item in research_stages if not self._completed(item)]
            if missing:
                raise RuntimeError(f"research runs are incomplete: {missing}")
            if not details.get("champion_name") or not details.get("champion_factor_names"):
                raise ValueError("factor_selection must freeze champion_name and factor names")
        elif stage == "sealed_blind":
            if not self._completed("factor_selection"):
                raise RuntimeError("factor selection must pass before sealed blind evaluation")
            selected = self.state["stages"]["factor_selection"]["details"]
            if details.get("champion_name") != selected.get("champion_name"):
                raise ValueError("sealed blind result is not for the selected champion")
            checks = dict(details.get("checks") or {})
            if set(checks) != set(SEALED_BLIND_CHECKS) or not all(
                checks.get(name) is True for name in SEALED_BLIND_CHECKS
            ):
                raise ValueError("sealed blind evidence does not satisfy every frozen gate")
        elif stage == "replay":
            if not self._completed("asset_freeze"):
                raise RuntimeError("asset freeze must complete before formal Replay evidence")
            frozen = self.state["stages"]["asset_freeze"]["details"]
            if details.get("config_hash") != frozen.get("config_hash"):
                raise ValueError("Replay evidence is not bound to the frozen config_hash")
            checks = dict(details.get("checks") or {})
            if set(checks) != set(REPLAY_ENGINEERING_CHECKS) or not all(
                checks.get(name) is True for name in REPLAY_ENGINEERING_CHECKS
            ):
                raise ValueError("Replay evidence does not satisfy every engineering check")
        self._record(stage, "completed", details)
        return details

    def sync_forward_evidence(
        self,
        *,
        instance_id: str,
        account_id: str = "",
        broker: str = "xtp",
    ) -> dict[str, Any]:
        """Synchronise forward gates from immutable trading-system evidence.

        The method never starts a deployment, promotes an instance or routes an
        order.  It only reads config-bound stage sessions, daily parity and
        broker-UAT evidence, and can therefore be run repeatedly by an operator
        or scheduler.  Revoked/expired evidence fails closed on the next sync.
        """

        if not self._completed("asset_freeze"):
            raise RuntimeError("asset freeze must complete before evidence sync")
        frozen = self.state["stages"]["asset_freeze"]["details"]
        expected_instance = str(frozen.get("instance_id") or "")
        requested_instance = str(instance_id or "").strip()
        if requested_instance != expected_instance:
            raise ValueError("instance_id does not match the frozen campaign asset")

        trading = self._engine().get_system("trading")
        current = next(
            (
                row
                for row in trading.list_instances()
                if str(row.get("instance_id") or "") == requested_instance
            ),
            None,
        )
        if current is None:
            raise KeyError(f"unknown frozen trading instance {requested_instance!r}")
        if current.get("config_hash") != frozen.get("config_hash"):
            for stage in ("replay", "paper_20", "uat_v2", "shadow_5", "live_5"):
                if self._completed(stage):
                    self._record(
                        stage,
                        "failed",
                        {
                            "revoked": True,
                            "reason": "instance config_hash changed",
                            "expected_config_hash": frozen.get("config_hash"),
                            "actual_config_hash": current.get("config_hash"),
                        },
                    )
            raise RuntimeError(
                "frozen instance config_hash changed; formal Replay evidence must restart"
            )

        selected_broker = str(broker or self.manifest["promotion"]["broker"]).lower()
        if selected_broker != self.manifest["promotion"]["broker"]:
            raise ValueError("forward evidence broker must remain the registered XTP broker")
        qualification = trading.qualification(
            requested_instance,
            account_id=str(account_id or ""),
            broker=selected_broker,
        )
        if qualification.get("config_hash") != frozen.get("config_hash"):
            raise RuntimeError("qualification report is not bound to the frozen config_hash")

        paper_ok = bool((qualification.get("paper") or {}).get("passed"))
        uat = dict(qualification.get("broker_uat") or {})
        uat_ok = all(
            (
                uat.get("required") is True,
                uat.get("passed") is True,
                uat.get("broker") == selected_broker,
                bool(uat.get("account_hash")),
                bool(uat.get("evidence_id")),
                not bool(uat.get("plugin_metadata_error")),
            )
        )
        shadow_ok = bool((qualification.get("shadow") or {}).get("passed")) and bool(
            (qualification.get("parity") or {}).get("passed")
        )
        live_ok = bool((qualification.get("live") or {}).get("passed"))
        current_checks = {
            "paper_20": paper_ok,
            "uat_v2": uat_ok,
            "shadow_5": shadow_ok,
            "live_5": live_ok,
        }

        # Evidence can be invalidated by a later contradictory parity result or
        # expired UAT artifact.  Revoke it before considering any new stage.
        prerequisite_ok = self._completed("replay")
        for stage in ("paper_20", "uat_v2", "shadow_5", "live_5"):
            valid = prerequisite_ok and current_checks[stage]
            if self._completed(stage) and not valid:
                self._record(
                    stage,
                    "failed",
                    {
                        "revoked": True,
                        "reason": "derived trading evidence no longer passes",
                        "qualification_evidence_hash": qualification.get("evidence_hash", ""),
                    },
                )
            prerequisite_ok = prerequisite_ok and self._completed(stage)

        synced: list[str] = []
        if self._completed("replay") and paper_ok and not self._completed("paper_20"):
            self._record("paper_20", "completed", qualification["paper"])
            synced.append("paper_20")
        if self._completed("paper_20") and uat_ok and not self._completed("uat_v2"):
            self._record("uat_v2", "completed", uat)
            synced.append("uat_v2")
        if self._completed("uat_v2") and shadow_ok and not self._completed("shadow_5"):
            self._record(
                "shadow_5",
                "completed",
                {
                    "shadow": qualification["shadow"],
                    "parity": qualification["parity"],
                },
            )
            synced.append("shadow_5")
        if self._completed("shadow_5") and live_ok and not self._completed("live_5"):
            self._record("live_5", "completed", qualification["live"])
            synced.append("live_5")
        return {
            "instance_id": requested_instance,
            "config_hash": frozen["config_hash"],
            "synced": synced,
            "next_required": self.next_required_stage(),
            "qualification": qualification,
        }

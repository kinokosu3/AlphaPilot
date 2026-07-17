from __future__ import annotations

from pathlib import Path
import hashlib
import json
from types import SimpleNamespace

from alphapilot.systems.research.campaign import (
    CampaignRunner,
    DEFAULT_MANIFEST,
    REPLAY_ENGINEERING_CHECKS,
    validate_campaign_manifest,
)


class _LLMModule:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run_mining(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        return {
            "session_path": f"/runs/llm-{len(self.calls)}",
            "completed_rounds": 3,
            "next_step_index": 0,
            "factor_data_fingerprint": "pit-fingerprint",
            "round_persistence": [
                {
                    "round_no": round_no,
                    "strategy": {
                        "saved": True,
                        "model_hash": f"model-{round_no}",
                        "qlib_template_fingerprint": "template-hash",
                        "factor_data_fingerprint": "pit-fingerprint",
                    },
                    "factors": {"saved": 10, "candidates": 10, "error": ""},
                }
                for round_no in range(1, 4)
            ],
        }


class _RLModule:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def mine_rl(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        expression = (
            f"Ref(${kwargs.get('target_price', 'vwap')},"
            f"-{int(kwargs.get('target_horizon', 20)) + 1})/"
            f"Ref(${kwargs.get('target_price', 'vwap')},-1)-1"
        )
        return {
            "mined": 4,
            "n_accepted": 3,
            "research_metadata": {
                "factor_data_fingerprint": "pit-fingerprint",
                "target_expression": expression,
                "seed": kwargs.get("seed"),
            },
            "research_metadata_sha256": "metadata-hash",
        }


class _Engine:
    def __init__(self) -> None:
        self.llm = _LLMModule()
        self.rl = _RLModule()

    def collect_commands(self):  # noqa: ANN201
        return {"mine": self.llm.run_mining, "mine_rl": self.rl.mine_rl}


class _StrategySystem:
    def __init__(self) -> None:
        self.records: dict[str, SimpleNamespace] = {}

    def get_strategy(self, name: str):  # noqa: ANN201
        return self.records.get(name)

    def create_strategy_from_factors(self, **kwargs):  # noqa: ANN003, ANN201
        record = SimpleNamespace(
            strategy_name=kwargs["strategy_name"],
            metadata={
                "factor_names": list(kwargs["factor_names"]),
                "yaml_params": dict(kwargs["yaml_params"]),
            },
            model=SimpleNamespace(trained_artifact_uri=None),
        )
        self.records[record.strategy_name] = record
        return record

    def backtest_from_asset(self, request):  # noqa: ANN001, ANN201
        source = self.records[request.strategy_name]
        saved = SimpleNamespace(
            strategy_name=request.save_as,
            metadata={
                **source.metadata,
                "parent_strategy": source.strategy_name,
                "model_hash": "model-hash",
                "factor_data_fingerprint": "pit-fingerprint",
            },
            model=SimpleNamespace(trained_artifact_uri="/frozen/model.pkl"),
        )
        self.records[request.save_as] = saved
        return [
            SimpleNamespace(details={"saved_strategy_name": request.save_as})
        ]


class _TradingSystem:
    def __init__(self) -> None:
        self.instances: dict[str, dict] = {}
        self.qualification_report: dict | None = None

    def list_instances(self):  # noqa: ANN201
        return list(self.instances.values())

    def create_instance_from_research_asset(self, payload):  # noqa: ANN001, ANN201
        from alphapilot.systems.trading.contracts import canonical_instrument

        config = {
            "artifact_binding": {"research_asset": payload["strategy_name"]},
            "universe": sorted(canonical_instrument(item) for item in payload["universe"]),
            "data_policy": {"risk_policy": payload["risk_policy"]},
        }
        row = {
            "instance_id": payload["instance_id"],
            "config_hash": "config-hash",
            "deployment_level": "replay",
            "config": config,
        }
        self.instances[row["instance_id"]] = row
        return row

    def validate_instance(self, _instance_id):  # noqa: ANN001, ANN201
        return {"ok": True}

    def qualification(self, instance_id, **_kwargs):  # noqa: ANN001, ANN201
        if self.qualification_report is None:
            raise AssertionError("qualification report was not configured")
        assert instance_id in self.instances
        return dict(self.qualification_report)


class _DeploymentEngine(_Engine):
    def __init__(self) -> None:
        super().__init__()
        self.strategy = _StrategySystem()
        self.trading = _TradingSystem()

    def get_system(self, name: str):  # noqa: ANN201
        return self.strategy if name == "strategy" else self.trading


def _whitelist() -> dict:
    symbols = [f"SH{600000 + index:06d}" for index in range(50)]
    payload = {
        "schema_version": 1,
        "as_of": "2026-07-16",
        "latest_market_date": "2026-07-16",
        "account_equity": 1_000_000.0,
        "selection": {
            "top_n": 50,
            "liquidity_days": 60,
            "minimum_trading_age": 120,
            "lot_size": 100,
            "max_lot_equity_ratio": 0.02,
        },
        "symbols": symbols,
        "records": [{"symbol": symbol} for symbol in symbols],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload["fingerprint"] = hashlib.sha256(canonical).hexdigest()
    return payload


def test_campaign_runs_independent_llm_sessions_and_serial_rl_seeds(
    tmp_path: Path,
) -> None:
    engine = _Engine()
    runner = CampaignRunner(
        DEFAULT_MANIFEST,
        engine_factory=lambda: engine,
        state_root=tmp_path / "state",
    )
    runner._record("pit_preflight", "completed", {"ok": True})

    llm = runner.run_llm()
    smoke = runner.run_rl_smoke()
    production = runner.run_rl_production()

    assert len(llm) == 3
    assert [call["step_n"] for call in engine.llm.calls] == [15, 15, 15]
    assert len({call["direction"] for call in engine.llm.calls}) == 3
    assert [call["random_seed"] for call in engine.llm.calls] == [101, 202, 303]
    assert all(call["target_horizon"] == 5 for call in engine.rl.calls)
    assert all(call["target_price"] == "close" for call in engine.rl.calls)
    assert [call["seed"] for call in engine.rl.calls] == [0, 11, 29, 47]
    assert smoke["steps"] == 5000
    assert [item["steps"] for item in production] == [200000, 200000, 200000]

    # Completed evidence is resumable and cannot silently execute the costly runs twice.
    resumed = CampaignRunner(
        DEFAULT_MANIFEST,
        engine_factory=lambda: engine,
        state_root=tmp_path / "state",
    )
    resumed.run_llm()
    resumed.run_rl_smoke()
    resumed.run_rl_production()
    assert len(engine.llm.calls) == 3
    assert len(engine.rl.calls) == 4


def test_campaign_final_refit_covers_every_label_ready_session() -> None:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    patch = manifest["deployment"]["refit"]["params_patch"]
    assert patch["refit_all_labeled"] is True
    assert patch["train_end"] == manifest["deployment"]["refit"]["label_ready_through"]

    patch["refit_all_labeled"] = False
    try:
        validate_campaign_manifest(manifest)
    except ValueError as exc:
        assert "train through every label-ready session" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("partial final refit was accepted")


def test_campaign_rejects_an_llm_round_with_incomplete_persistence(
    tmp_path: Path,
) -> None:
    engine = _Engine()
    original = engine.llm.run_mining

    def incomplete(**kwargs):  # noqa: ANN003, ANN202
        result = original(**kwargs)
        result["round_persistence"][1]["strategy"]["model_hash"] = ""
        return result

    engine.llm.run_mining = incomplete
    runner = CampaignRunner(
        DEFAULT_MANIFEST,
        engine_factory=lambda: engine,
        state_root=tmp_path / "state",
    )
    runner._record("pit_preflight", "completed", {"ok": True})

    try:
        runner.run_llm()
    except RuntimeError as exc:
        assert "model/template/data asset is incomplete" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("incomplete LLM round persistence was accepted")

    first_stage = "llm:short_term_overreaction_reversal"
    assert runner.status()["stages"][first_stage]["status"] == "failed"


def test_campaign_gate_failure_is_recorded_and_stops_promotion(tmp_path: Path) -> None:
    runner = CampaignRunner(DEFAULT_MANIFEST, state_root=tmp_path / "state")

    try:
        runner.record_gate("sealed_blind", {"passed": False, "failures": ["IR"]})
    except ValueError as exc:
        assert "promotion is stopped" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("failed blind gate was accepted")

    assert runner.status()["stages"]["sealed_blind"]["status"] == "failed"


def test_campaign_freezes_champion_and_creates_replay_only_instance(
    tmp_path: Path,
) -> None:
    engine = _DeploymentEngine()
    runner = CampaignRunner(
        DEFAULT_MANIFEST,
        engine_factory=lambda: engine,
        state_root=tmp_path / "state",
    )
    runner._record(
        "factor_selection",
        "completed",
        {
            "passed": True,
            "champion_name": "mixed_champion",
            "champion_factor_names": ["factor_a", "factor_b"],
        },
    )
    runner._record(
        "sealed_blind",
        "completed",
        {"passed": True, "champion_name": "mixed_champion"},
    )

    result = runner.freeze_deployment_asset(
        source_strategy_name="champion_research",
        save_as="champion_frozen",
        instance_id="champion_canary",
        whitelist=_whitelist(),
    )

    assert result["deployment_level"] == "replay"
    assert result["config_hash"] == "config-hash"
    assert result["whitelist_fingerprint"]
    # A retry after completion returns the immutable recorded evidence.
    assert runner.freeze_deployment_asset(
        source_strategy_name="champion_research",
        save_as="champion_frozen",
        instance_id="champion_canary",
        whitelist=_whitelist(),
    ) == result


def test_campaign_rejects_tampered_or_partial_whitelist(tmp_path: Path) -> None:
    engine = _DeploymentEngine()
    runner = CampaignRunner(
        DEFAULT_MANIFEST,
        engine_factory=lambda: engine,
        state_root=tmp_path / "state",
    )
    runner._record(
        "factor_selection",
        "completed",
        {
            "passed": True,
            "champion_name": "champion",
            "champion_factor_names": ["factor_a"],
        },
    )
    runner._record(
        "sealed_blind", "completed", {"passed": True, "champion_name": "champion"}
    )
    whitelist = _whitelist()
    whitelist["symbols"] = whitelist["symbols"][:-1]

    try:
        runner.freeze_deployment_asset(
            source_strategy_name="source",
            save_as="saved",
            instance_id="instance",
            whitelist=whitelist,
        )
    except ValueError as exc:
        assert "invalid frozen whitelist" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("tampered whitelist was accepted")


def test_campaign_replay_gate_requires_exact_config_and_engineering_checks(
    tmp_path: Path,
) -> None:
    runner = CampaignRunner(DEFAULT_MANIFEST, state_root=tmp_path / "state")
    runner._record(
        "asset_freeze",
        "completed",
        {"instance_id": "canary", "config_hash": "frozen-config"},
    )

    try:
        runner.record_gate(
            "replay",
            {
                "passed": True,
                "config_hash": "frozen-config",
                "checks": {name: True for name in REPLAY_ENGINEERING_CHECKS[:-1]},
            },
        )
    except ValueError as exc:
        assert "engineering check" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("partial Replay checklist was accepted")

    result = runner.record_gate(
        "replay",
        {
            "passed": True,
            "config_hash": "frozen-config",
            "checks": {name: True for name in REPLAY_ENGINEERING_CHECKS},
        },
    )
    assert result["passed"] is True


def test_campaign_syncs_only_derived_forward_evidence_and_revokes_it(
    tmp_path: Path,
) -> None:
    engine = _DeploymentEngine()
    runner = CampaignRunner(
        DEFAULT_MANIFEST,
        engine_factory=lambda: engine,
        state_root=tmp_path / "state",
    )
    engine.trading.instances["canary"] = {
        "instance_id": "canary",
        "config_hash": "frozen-config",
        "deployment_level": "live",
        "config": {},
    }
    for stage in (
        "data_build",
        "pit_preflight",
        "llm:short_term_overreaction_reversal",
        "llm:attention_price_volume_confirmation",
        "llm:volatility_contraction_trend_quality",
        "rl_smoke",
        "rl_production:11",
        "rl_production:29",
        "rl_production:47",
        "factor_selection",
        "sealed_blind",
    ):
        runner._record(stage, "completed", {"test_fixture": True})
    runner._record(
        "asset_freeze",
        "completed",
        {"instance_id": "canary", "config_hash": "frozen-config"},
    )
    runner._record(
        "replay",
        "completed",
        {
            "passed": True,
            "config_hash": "frozen-config",
            "checks": {name: True for name in REPLAY_ENGINEERING_CHECKS},
        },
    )
    report = {
        "config_hash": "frozen-config",
        "evidence_hash": "qualification-hash",
        "paper": {"passed": True, "trading_sessions": 20},
        "broker_uat": {
            "required": True,
            "passed": True,
            "broker": "xtp",
            "account_hash": "account-hash",
            "evidence_id": "uat-evidence",
            "plugin_metadata_error": "",
        },
        "shadow": {"passed": True, "trading_sessions": 5},
        "parity": {"passed": True, "passed_sessions": ["2026-07-01"]},
        "live": {
            "passed": True,
            "trading_sessions": 5,
            "execution_quality": {"passed": True},
        },
    }
    engine.trading.qualification_report = report

    synced = runner.sync_forward_evidence(
        instance_id="canary", account_id="dedicated-account"
    )
    assert synced["synced"] == ["paper_20", "uat_v2", "shadow_5", "live_5"]
    assert synced["next_required"] == "complete"

    engine.trading.qualification_report = {
        **report,
        "broker_uat": {**report["broker_uat"], "passed": False},
    }
    revoked = runner.sync_forward_evidence(
        instance_id="canary", account_id="dedicated-account"
    )
    assert revoked["next_required"] == "uat_v2"
    assert runner.status()["stages"]["uat_v2"]["status"] == "failed"
    assert runner.status()["stages"]["shadow_5"]["status"] == "failed"
    assert runner.status()["stages"]["live_5"]["status"] == "failed"

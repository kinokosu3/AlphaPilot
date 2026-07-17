#!/usr/bin/env python3
"""Verify strategy -> backtest refactor (no alpha_mining dependency inversion)."""

from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ArchitectureTests(unittest.TestCase):
    """Static checks: dependency direction and file layout."""

    def test_strategy_service_no_alpha_mining(self) -> None:
        src = (ROOT / "alphapilot/systems/strategy/service.py").read_text(encoding="utf-8")
        self.assertNotIn('module("alpha_mining")', src)
        self.assertNotIn("alpha_mining", src)
        self.assertIn("run_strategy_asset_backtest", src)

    def test_strategy_backtest_module_exists(self) -> None:
        path = ROOT / "alphapilot/systems/strategy/backtest.py"
        self.assertTrue(path.is_file(), f"missing {path}")
        src = path.read_text(encoding="utf-8")
        self.assertIn("context.backtest()", src)
        self.assertNotIn("alpha_mining", src)

    def test_alpha_mining_no_strategy_backtest_pipeline(self) -> None:
        self.assertFalse(
            (ROOT / "alphapilot/modules/alpha_mining/pipelines/strategy_backtest.py").exists()
        )
        module_src = (ROOT / "alphapilot/modules/alpha_mining/module.py").read_text(encoding="utf-8")
        self.assertNotIn("run_strategy_asset_backtest", module_src)

    def test_no_context_module_alpha_mining_in_systems(self) -> None:
        systems_dir = ROOT / "alphapilot/systems"
        hits: list[str] = []
        for py in systems_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            if 'module("alpha_mining")' in text or "context.module('alpha_mining')" in text:
                hits.append(str(py.relative_to(ROOT)))
        self.assertEqual(hits, [], f"systems still call alpha_mining: {hits}")


class RunStrategyAssetBacktestTests(unittest.TestCase):
    """Unit tests for systems/strategy/backtest.py with mocked backtest system."""

    def _mock_context(self) -> MagicMock:
        ctx = MagicMock()
        ctx.config.backtest.use_local = True
        return ctx

    def test_retrain_delegates_to_run_factor_evaluation(self) -> None:
        # Factor data prep now happens inside the evaluation pipeline (_build_experiment prepares
        # the FactorDataContext); run_strategy_asset_backtest no longer calls ensure_factor_data.
        from alphapilot.systems.backtest.types import FactorBacktestResult, FactorDefinition
        from alphapilot.systems.strategy.backtest import run_strategy_asset_backtest

        ctx = self._mock_context()
        mock_result = FactorBacktestResult(experiment=MagicMock(), metrics={"IC": 0.05})
        ctx.backtest.return_value.run_factor_evaluation.return_value = mock_result

        factors = [FactorDefinition(factor_name="f1", factor_expression="$close/$open")]
        run = run_strategy_asset_backtest(
            ctx,
            mode="retrain",
            factors=factors,
            qlib_config_name="conf_test.yaml",
            market="mkt_test",
        )

        ctx.backtest.return_value.run_factor_evaluation.assert_called_once()
        req = ctx.backtest.return_value.run_factor_evaluation.call_args[0][0]
        self.assertEqual(req.factors, factors)
        self.assertEqual(req.qlib_config_name, "conf_test.yaml")
        self.assertEqual(req.market, "mkt_test")
        ctx.backtest.return_value.run_saved_model_evaluation.assert_not_called()
        self.assertEqual(run.mode, "retrain")
        self.assertIs(run.result, mock_result)

    def test_reuse_model_delegates_to_run_saved_model_evaluation(self) -> None:
        from alphapilot.systems.backtest.types import FactorBacktestResult, FactorDefinition
        from alphapilot.systems.strategy.backtest import run_strategy_asset_backtest

        ctx = self._mock_context()
        mock_result = FactorBacktestResult(experiment=MagicMock(), metrics={"IC": 0.03})
        ctx.backtest.return_value.run_saved_model_evaluation.return_value = mock_result

        factors = [FactorDefinition(factor_name="f1", factor_expression="$close")]
        run = run_strategy_asset_backtest(
            ctx,
            mode="reuse_model",
            factors=factors,
            model_pickle_path="/tmp/fitted_model.pkl",
            qlib_data_dir="/tmp/qlib",
            market="mkt_test",
        )

        ctx.backtest.return_value.run_saved_model_evaluation.assert_called_once()
        req = ctx.backtest.return_value.run_saved_model_evaluation.call_args[0][0]
        self.assertEqual(req.model_pickle_path, "/tmp/fitted_model.pkl")
        self.assertEqual(req.qlib_data_dir, "/tmp/qlib")
        self.assertEqual(req.market, "mkt_test")
        ctx.backtest.return_value.run_factor_evaluation.assert_not_called()
        self.assertEqual(run.mode, "reuse_model")

    def test_reuse_model_requires_pickle_path(self) -> None:
        from alphapilot.systems.backtest.types import FactorDefinition
        from alphapilot.systems.strategy.backtest import run_strategy_asset_backtest

        ctx = self._mock_context()
        with self.assertRaises(ValueError):
            run_strategy_asset_backtest(
                ctx,
                mode="reuse_model",
                factors=[FactorDefinition(factor_name="f1", factor_expression="$close")],
                model_pickle_path=None,
            )


class StrategySystemBacktestFromAssetTests(unittest.TestCase):
    """Integration-style test: StrategySystem.backtest_from_asset uses new pipeline."""

    @patch("alphapilot.systems.strategy.service.run_strategy_asset_backtest")
    def test_backtest_from_asset_calls_local_pipeline(self, mock_run: MagicMock) -> None:
        from alphapilot.systems.strategy.backtest import StrategyAssetBacktestRun
        from alphapilot.systems.strategy.base import StrategyBacktestRequest, StrategyModelSpec, StrategyRecord
        from alphapilot.systems.strategy.service import StrategySystem

        system = StrategySystem()
        ctx = MagicMock()
        ctx.config.backtest.use_local = True
        ctx.config.strategy.database_backend = "file"
        ctx.config.strategy.param_dir = tempfile.mkdtemp(prefix="strategy_zoo_test_")
        system.setup(ctx)

        record = StrategyRecord(
            strategy_name="test_strat",
            factor_formulas=["$close/$open"],
            model=StrategyModelSpec(
                model_name="lgb",
                trained_artifact_uri="/tmp/model.pkl",
            ),
        )
        system._param_db = MagicMock()
        system._param_db.load_record.return_value = record
        system._param_db.retest_bundle_dir.return_value = None
        system._param_db.append_retest = MagicMock()

        mock_exp = type("FakeExp", (), {"metrics": None, "result": {"IC": 0.1, "ICIR": 0.5}})()
        mock_run.return_value = StrategyAssetBacktestRun(
            mode="retrain",
            result=MagicMock(experiment=mock_exp),
            workspace_path="/tmp/ws",
        )

        req = StrategyBacktestRequest(strategy_name="test_strat", mode="retrain")
        outcomes = system.backtest_from_asset(req)

        mock_run.assert_called_once()
        call_kw = mock_run.call_args
        self.assertIs(call_kw[0][0], ctx)
        self.assertEqual(call_kw[1]["mode"], "retrain")
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].strategy_name, "test_strat")
        self.assertEqual(outcomes[0].metrics.get("IC"), 0.1)
        system._param_db.append_retest.assert_called_once()

    @patch("alphapilot.systems.backtest.scoring_model_export.export_scoring_model_artifacts")
    @patch("alphapilot.systems.strategy.service.run_strategy_asset_backtest")
    def test_retrain_save_as_freezes_deployable_model_and_data_binding(
        self,
        mock_run: MagicMock,
        mock_export: MagicMock,
    ) -> None:
        from alphapilot.systems.strategy.backtest import StrategyAssetBacktestRun
        from alphapilot.systems.strategy.base import (
            StrategyBacktestRequest,
            StrategyModelSpec,
            StrategyRecord,
        )
        from alphapilot.systems.strategy.service import StrategySystem

        root = Path(tempfile.mkdtemp(prefix="strategy_save_as_test_"))
        workspace = root / "workspace"
        artifact_dir = workspace / "artifacts" / "scoring_model"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "fitted_model.pkl").write_bytes(b"trained-model")
        (artifact_dir / "model_config.json").write_text('{"class":"LGBModel"}')
        (artifact_dir / "fitted_training_state.json").write_text('{"trees":12}')
        config_name = "conf_cn_combined_kdd_ver.yaml"
        (workspace / config_name).write_text("task: {}\n")
        (workspace / "read_exp_res.py").write_text("# snapshot\n")
        qlib_dir = root / "qlib"
        qlib_dir.mkdir()

        system = StrategySystem()
        ctx = MagicMock()
        ctx.config.backtest.use_local = True
        ctx.config.data.qlib_data_dir = qlib_dir
        ctx.config.strategy.database_backend = "file"
        ctx.config.strategy.param_dir = root / "strategy_zoo"
        system.setup(ctx)
        system.register_strategy(
            StrategyRecord(
                strategy_name="candidate",
                factor_formulas=["$close/$open", "$volume/Mean($volume,20)"],
                model=StrategyModelSpec(model_name="LGBModel"),
                metadata={"market": "main_stock_pit"},
            )
        )

        factor_ctx = SimpleNamespace(
            fingerprint="factor-fingerprint",
            spec=SimpleNamespace(market="main_stock_pit", qlib_dir=qlib_dir),
        )
        experiment = SimpleNamespace(
            result={"IC": 0.03, "ICIR": 0.6},
            factor_data_context=factor_ctx,
            qlib_config_name=config_name,
            based_experiments=[object()],
        )
        mock_run.return_value = StrategyAssetBacktestRun(
            mode="retrain",
            result=MagicMock(experiment=experiment),
            workspace_path=str(workspace),
        )
        mock_export.return_value = artifact_dir

        outcomes = system.backtest_from_asset(
            StrategyBacktestRequest(
                strategy_name="candidate",
                mode="retrain",
                save_as="candidate_deployable",
                qlib_data_dir=str(qlib_dir),
                options={"yaml_params": {"label_expression": "five-day"}},
            )
        )

        saved = system.get_strategy("candidate_deployable")
        self.assertIsNotNone(saved)
        assert saved is not None and saved.model is not None
        saved_model = Path(saved.model.trained_artifact_uri or "").resolve()
        saved_dir = (root / "strategy_zoo" / "candidate_deployable").resolve()
        self.assertTrue(saved_model.is_file())
        self.assertTrue(saved_model.is_relative_to(saved_dir / "artifacts"))
        self.assertNotEqual(saved_model, (artifact_dir / "fitted_model.pkl").resolve())
        self.assertTrue((saved_dir / "qlib_template" / config_name).is_file())
        self.assertEqual(saved.metadata["qlib_template_snapshot_dir"], "qlib_template")
        self.assertEqual(saved.metadata["market"], "main_stock_pit")
        self.assertEqual(saved.metadata["factor_data_fingerprint"], "factor-fingerprint")
        self.assertEqual(saved.metadata["yaml_params"]["provider_uri"], str(qlib_dir.resolve()))
        self.assertEqual(len(saved.metadata["model_hash"]), 64)
        self.assertEqual(outcomes[0].details["saved_strategy_name"], "candidate_deployable")

    def test_save_as_rejects_reuse_model_and_duplicate_name(self) -> None:
        from alphapilot.systems.strategy.base import (
            StrategyBacktestRequest,
            StrategyModelSpec,
            StrategyRecord,
        )
        from alphapilot.systems.strategy.service import StrategySystem

        root = Path(tempfile.mkdtemp(prefix="strategy_save_as_invalid_"))
        system = StrategySystem()
        ctx = MagicMock()
        ctx.config.backtest.use_local = True
        ctx.config.strategy.database_backend = "file"
        ctx.config.strategy.param_dir = root
        system.setup(ctx)
        record = StrategyRecord(
            strategy_name="existing",
            factor_formulas=["$close"],
            model=StrategyModelSpec(model_name="lgb", trained_artifact_uri="/tmp/model.pkl"),
            metadata={"market": "main_stock_pit"},
        )
        system.register_strategy(record)

        with self.assertRaisesRegex(ValueError, "only valid with mode=retrain"):
            system.backtest_from_asset(
                StrategyBacktestRequest(
                    strategy_name="existing",
                    mode="reuse_model",
                    save_as="new",
                )
            )
        with self.assertRaisesRegex(ValueError, "already exists"):
            system.backtest_from_asset(
                StrategyBacktestRequest(
                    strategy_name="existing",
                    mode="retrain",
                    save_as="existing",
                )
            )

    @patch("alphapilot.systems.strategy.service.run_strategy_asset_backtest")
    def test_save_as_training_failure_creates_no_asset(self, mock_run: MagicMock) -> None:
        import os

        from alphapilot.systems.strategy.base import (
            StrategyBacktestRequest,
            StrategyModelSpec,
            StrategyRecord,
        )
        from alphapilot.systems.strategy.service import StrategySystem

        root = Path(tempfile.mkdtemp(prefix="strategy_save_as_train_failure_"))
        system = StrategySystem()
        ctx = MagicMock()
        ctx.config.backtest.use_local = True
        ctx.config.strategy.database_backend = "file"
        ctx.config.strategy.param_dir = root
        system.setup(ctx)
        system.register_strategy(
            StrategyRecord(
                strategy_name="source",
                factor_formulas=["$close"],
                model=StrategyModelSpec(model_name="lgb"),
                metadata={"market": "main_stock_pit"},
            )
        )
        mock_run.side_effect = RuntimeError("training failed")
        os.environ["ALPHAPILOT_QLIB_DATA_DIR"] = "original-provider"

        try:
            with self.assertRaisesRegex(RuntimeError, "retrain save_as failed"):
                system.backtest_from_asset(
                    StrategyBacktestRequest(
                        strategy_name="source",
                        mode="retrain",
                        save_as="must_not_exist",
                        qlib_data_dir=str(root / "failed-provider"),
                    )
                )
            self.assertEqual(
                os.environ.get("ALPHAPILOT_QLIB_DATA_DIR"),
                "original-provider",
            )
        finally:
            os.environ.pop("ALPHAPILOT_QLIB_DATA_DIR", None)

        self.assertIsNone(system.get_strategy("must_not_exist"))

    @patch("alphapilot.systems.backtest.scoring_model_export.export_scoring_model_artifacts")
    @patch("alphapilot.systems.strategy.service.run_strategy_asset_backtest")
    def test_save_as_missing_fitted_model_creates_no_asset(
        self,
        mock_run: MagicMock,
        mock_export: MagicMock,
    ) -> None:
        from alphapilot.systems.strategy.backtest import StrategyAssetBacktestRun
        from alphapilot.systems.strategy.base import (
            StrategyBacktestRequest,
            StrategyModelSpec,
            StrategyRecord,
        )
        from alphapilot.systems.strategy.service import StrategySystem

        root = Path(tempfile.mkdtemp(prefix="strategy_save_as_missing_model_"))
        workspace = root / "workspace"
        artifact_dir = workspace / "artifacts" / "scoring_model"
        artifact_dir.mkdir(parents=True)
        qlib_dir = root / "qlib"
        qlib_dir.mkdir()
        system = StrategySystem()
        ctx = MagicMock()
        ctx.config.backtest.use_local = True
        ctx.config.data.qlib_data_dir = qlib_dir
        ctx.config.strategy.database_backend = "file"
        ctx.config.strategy.param_dir = root / "strategy_zoo"
        system.setup(ctx)
        system.register_strategy(
            StrategyRecord(
                strategy_name="source",
                factor_formulas=["$close"],
                model=StrategyModelSpec(model_name="lgb"),
                metadata={"market": "main_stock_pit"},
            )
        )
        experiment = SimpleNamespace(
            result={"IC": 0.02},
            factor_data_context=SimpleNamespace(
                fingerprint="pit-fingerprint",
                spec=SimpleNamespace(market="main_stock_pit", qlib_dir=qlib_dir),
            ),
            qlib_config_name="conf.yaml",
            based_experiments=[object()],
        )
        mock_run.return_value = StrategyAssetBacktestRun(
            mode="retrain",
            result=MagicMock(experiment=experiment),
            workspace_path=str(workspace),
        )
        mock_export.return_value = artifact_dir

        with self.assertRaisesRegex(RuntimeError, "fitted_model.pkl"):
            system.backtest_from_asset(
                StrategyBacktestRequest(
                    strategy_name="source",
                    mode="retrain",
                    save_as="must_not_exist",
                    qlib_data_dir=str(qlib_dir),
                )
            )

        self.assertIsNone(system.get_strategy("must_not_exist"))


class EngineSmokeTests(unittest.TestCase):
    """Runtime smoke: engine loads; strategy has no alpha_mining in method source."""

    def test_build_engine_and_strategy_system(self) -> None:
        from alphapilot.kernel import build_engine

        engine = build_engine()
        strategy = engine.get_system("strategy")
        self.assertEqual(strategy.name, "strategy")
        src = inspect.getsource(strategy.backtest_from_asset)
        self.assertNotIn("alpha_mining", src)
        self.assertIn("run_strategy_asset_backtest", src)

    def test_alpha_mining_module_has_no_strategy_backtest_method(self) -> None:
        from alphapilot.kernel import build_engine

        engine = build_engine()
        mining = engine.get_module("alpha_mining")
        self.assertFalse(hasattr(mining, "run_strategy_asset_backtest"))

    def test_strategy_backtest_cli_module_loads(self) -> None:
        from alphapilot.kernel import build_engine

        engine = build_engine()
        mod = engine.get_module("strategy_backtest")
        self.assertEqual(mod.name, "strategy_backtest")
        self.assertIn("strategy_backtest", mod.commands())
        self.assertIn("strategy_backtest_list", mod.commands())


class StrategyBacktestListFunctionalTest(unittest.TestCase):
    """Functional: list strategies (no backtest execution required)."""

    def test_strategy_backtest_list_runs(self) -> None:
        from alphapilot.kernel import build_engine

        engine = build_engine()
        mod = engine.get_module("strategy_backtest")
        rows = mod.strategy_backtest_list()
        self.assertIsInstance(rows, list)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (
        ArchitectureTests,
        RunStrategyAssetBacktestTests,
        StrategySystemBacktestFromAssetTests,
        EngineSmokeTests,
        StrategyBacktestListFunctionalTest,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print("\n" + "=" * 60)
    print(f"Ran {result.testsRun} tests")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")
    if result.wasSuccessful():
        print("VERDICT: PASS — strategy->backtest refactor verified")
        return 0
    print("VERDICT: FAIL — see failures/errors above")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

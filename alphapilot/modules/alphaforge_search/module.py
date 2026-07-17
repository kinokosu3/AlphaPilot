"""AlphaForgeSearchModule: GP / RL formulaic alpha mining.

These are the non-GAN baselines bundled with AlphaForge, merged into a
single module:

* ``mine_gp``  -- genetic programming (vendored ``gplearn``); lightest deps.
* ``mine_rl``  -- PPO over expression tokens (``stable-baselines3`` +
  ``sb3-contrib``); medium deps.

Both share the vendored alphagen expression engine for evaluation and,
on output, the same translate -> validate -> backtest pipeline as the AFF
module.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphapilot.kernel.base import BaseModule

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context


class AlphaForgeSearchModule(BaseModule):
    """LLM-free formulaic alpha mining via GP / RL search."""

    name = "alphaforge_search"

    def setup(self, context: "Context") -> None:
        self.context = context

    # ---- shared output helper ----

    def _emit(
        self,
        exprs: list,
        scores: list,
        *,
        source: str,
        backtest: bool,
        save: bool,
        research_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from alphapilot.modules.alphaforge.pipeline import emit_factors

        return emit_factors(
            self.context,
            exprs,
            scores,
            source=source,
            backtest=backtest,
            save=save,
            research_metadata=research_metadata,
        )

    # ---- GP (light) ----

    def mine_gp(
        self,
        instruments: str = "csi300",
        train_end_year: int = 2020,
        freq: str = "day",
        seed: int = 0,
        population_size: int = 1000,
        generations: int = 40,
        device: str | None = None,
        qlib_dir: str | None = None,
        backtest: bool = False,
        save: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Mine factors via genetic programming (gplearn). Extra knobs
        (``tournament_size``, ``top_n``, ``raw``) pass through to ``GPRunner``."""
        from alphapilot.modules.alphaforge_search.runners.gp_runner import GPRunner

        runner = GPRunner(
            context=self.context, instruments=instruments, train_end_year=train_end_year,
            freq=freq, seed=seed, population_size=population_size, generations=generations,
            device=device, qlib_dir=qlib_dir, **kwargs,
        )
        exprs, scores = runner.run()
        return self._emit(exprs, scores, source="alphaforge_gp", backtest=backtest, save=save)

    # ---- RL (medium) ----

    def mine_rl(
        self,
        instruments: str = "csi300",
        train_end_year: int = 2020,
        freq: str = "day",
        seed: int = 0,
        steps: int = 200_000,
        pool_capacity: int = 10,
        target_horizon: int = 20,
        target_price: str = "vwap",
        campaign_id: str | None = None,
        research_hypothesis: str = "rl_symbolic_factor_search",
        device: str | None = None,
        qlib_dir: str | None = None,
        backtest: bool = False,
        save: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Mine factors via PPO RL search (stable-baselines3 + sb3-contrib).
        Extra knobs (``raw`` ...) pass through to ``RLRunner``."""
        from alphapilot.modules.alphaforge_search.runners.rl_runner import RLRunner

        runner = RLRunner(
            context=self.context, instruments=instruments, train_end_year=train_end_year,
            freq=freq, seed=seed, steps=steps, pool_capacity=pool_capacity,
            target_horizon=target_horizon, target_price=target_price,
            device=device, qlib_dir=qlib_dir, **kwargs,
        )
        exprs, scores = runner.run()
        provider_uri = str(
            Path(qlib_dir or self.context.config.data.qlib_data_dir)
            .expanduser()
            .resolve()
        )
        search_config = {
            "algorithm": "MaskablePPO",
            "steps": steps,
            "pool_capacity": pool_capacity,
            "target_horizon": target_horizon,
            "target_price": target_price,
            "instruments": instruments,
            "train_end_year": train_end_year,
            "freq": freq,
            "seed": seed,
        }
        config_hash = hashlib.sha256(
            json.dumps(search_config, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        try:
            from alphapilot.systems.data.factor_h5 import FactorDataSpec

            data_fingerprint = FactorDataSpec(
                qlib_dir=Path(provider_uri), market=instruments, freq=freq
            ).fingerprint()
        except Exception as exc:
            if campaign_id and save:
                raise RuntimeError(
                    "campaign RL factors require a valid factor-data fingerprint"
                ) from exc
            data_fingerprint = ""
        metadata = {
            "campaign_id": campaign_id,
            "market": instruments,
            "provider_uri": provider_uri,
            "factor_data_fingerprint": data_fingerprint,
            "data_split": {
                "train": ["2010-01-01", f"{train_end_year}-12-31"],
                "validation": [
                    f"{train_end_year + 1}-01-01",
                    f"{train_end_year + 1}-12-31",
                ],
                "test": [
                    f"{train_end_year + 2}-01-01",
                    f"{train_end_year + 2}-12-31",
                ],
            },
            "hypothesis": research_hypothesis,
            "mining_round": 1,
            "seed": seed,
            "target_expression": (
                f"Ref(${target_price.lower()},-{target_horizon + 1})/"
                f"Ref(${target_price.lower()},-1)-1"
            ),
            "search_config": search_config,
            "model_fingerprint": config_hash,
            "qlib_template_fingerprint": "",
        }
        return self._emit(
            exprs,
            scores,
            source="alphaforge_rl",
            backtest=backtest,
            save=save,
            research_metadata=metadata,
        )

    def commands(self) -> dict[str, Callable[..., Any]]:
        return {
            "mine_gp": self.mine_gp,
            "mine_rl": self.mine_rl,
        }

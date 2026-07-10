"""Token-building environment used by the vendored AlphaGen RL miner.

This is the small environment from the AlphaForge upstream tree, migrated to
Gymnasium's reset/step contract for stable-baselines3 2.x.
"""

from __future__ import annotations

import math
from typing import Any

import gymnasium as gym
import torch

from alphagen.config import MAX_EXPR_LENGTH
from alphagen.data.expression import (
    BinaryOperator,
    Expression,
    OutOfDataRangeError,
    PairRollingOperator,
    RollingOperator,
    UnaryOperator,
)
from alphagen.data.tokens import (
    BEG_TOKEN,
    SequenceIndicatorToken,
    SequenceIndicatorType,
    Token,
)
from alphagen.data.tree import ExpressionBuilder
from alphagen.models.alpha_pool import AlphaPoolBase
from alphagen.utils import reseed_everything


class AlphaEnvCore(gym.Env):
    """Build one valid expression token-by-token and score it in an alpha pool."""

    metadata = {"render_modes": []}

    def __init__(
        self,
        pool: AlphaPoolBase,
        device: torch.device = torch.device("cpu"),
        print_expr: bool = False,
    ) -> None:
        super().__init__()
        self.pool = pool
        self._print_expr = print_expr
        self._device = device
        self.eval_cnt = 0
        self._tokens: list[Token] = []
        self._builder = ExpressionBuilder()

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[list[Token], dict[str, Any]]:
        super().reset(seed=seed)
        if seed is not None:
            reseed_everything(seed)
        self._tokens = [BEG_TOKEN]
        self._builder = ExpressionBuilder()
        return self._tokens, self._valid_action_types()

    def step(self, action: Token) -> tuple[list[Token], float, bool, bool, dict[str, Any]]:
        if isinstance(action, SequenceIndicatorToken) and action.indicator == SequenceIndicatorType.SEP:
            reward = self._evaluate()
            terminated = True
        elif len(self._tokens) < MAX_EXPR_LENGTH:
            self._tokens.append(action)
            self._builder.add_token(action)
            terminated = False
            reward = 0.0
        else:
            terminated = True
            reward = self._evaluate() if self._builder.is_valid() else 0.0

        if math.isnan(reward):
            reward = 0.0
        return self._tokens, float(reward), terminated, False, self._valid_action_types()

    def _evaluate(self) -> float:
        expr: Expression = self._builder.get_tree()
        if self._print_expr:
            print(expr)
        try:
            result = self.pool.try_new_expr(expr)
            self.eval_cnt += 1
            return float(result)
        except OutOfDataRangeError:
            return 0.0

    def _valid_action_types(self) -> dict[str, Any]:
        valid_op_unary = self._builder.validate_op(UnaryOperator)
        valid_op_binary = self._builder.validate_op(BinaryOperator)
        valid_op_rolling = self._builder.validate_op(RollingOperator)
        valid_op_pair_rolling = self._builder.validate_op(PairRollingOperator)
        return {
            "select": [
                valid_op_unary or valid_op_binary or valid_op_rolling or valid_op_pair_rolling,
                self._builder.validate_feature(),
                self._builder.validate_const(),
                self._builder.validate_dt(),
                self._builder.is_valid(),
            ],
            "op": {
                UnaryOperator: valid_op_unary,
                BinaryOperator: valid_op_binary,
                RollingOperator: valid_op_rolling,
                PairRollingOperator: valid_op_pair_rolling,
            },
        }

    def valid_action_types(self) -> dict[str, Any]:
        return self._valid_action_types()

    def render(self) -> None:
        return None

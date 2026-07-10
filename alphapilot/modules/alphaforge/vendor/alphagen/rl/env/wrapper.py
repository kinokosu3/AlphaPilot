"""Gymnasium wrapper exposing AlphaGen expression tokens as discrete actions."""

from __future__ import annotations

from typing import Any

import gymnasium as gym
import numpy as np

from alphagen.config import CONSTANTS, DELTA_TIMES, MAX_EXPR_LENGTH, OPERATORS, REWARD_PER_STEP
from alphagen.data.tokens import (
    ConstantToken,
    DeltaTimeToken,
    FeatureToken,
    OperatorToken,
    SequenceIndicatorToken,
    SequenceIndicatorType,
    Token,
)
from alphagen.models.alpha_pool import AlphaPoolBase
from alphagen.rl.env.core import AlphaEnvCore
from alphagen_qlib.stock_data import FeatureType

SIZE_NULL = 1
SIZE_OP = len(OPERATORS)
SIZE_FEATURE = len(FeatureType)
SIZE_DELTA_TIME = len(DELTA_TIMES)
SIZE_CONSTANT = len(CONSTANTS)
SIZE_SEP = 1

SIZE_ALL = SIZE_NULL + SIZE_OP + SIZE_FEATURE + SIZE_DELTA_TIME + SIZE_CONSTANT + SIZE_SEP
SIZE_ACTION = SIZE_ALL - SIZE_NULL

OFFSET_OP = SIZE_NULL
OFFSET_FEATURE = OFFSET_OP + SIZE_OP
OFFSET_DELTA_TIME = OFFSET_FEATURE + SIZE_FEATURE
OFFSET_CONSTANT = OFFSET_DELTA_TIME + SIZE_DELTA_TIME
OFFSET_SEP = OFFSET_CONSTANT + SIZE_CONSTANT


def action2token(action_raw: int) -> Token:
    action = int(action_raw) + 1
    if action < OFFSET_OP:
        raise ValueError(f"invalid action: {action_raw}")
    if action < OFFSET_FEATURE:
        return OperatorToken(OPERATORS[action - OFFSET_OP])
    if action < OFFSET_DELTA_TIME:
        return FeatureToken(FeatureType(action - OFFSET_FEATURE))
    if action < OFFSET_CONSTANT:
        return DeltaTimeToken(DELTA_TIMES[action - OFFSET_DELTA_TIME])
    if action < OFFSET_SEP:
        return ConstantToken(CONSTANTS[action - OFFSET_CONSTANT])
    if action == OFFSET_SEP:
        return SequenceIndicatorToken(SequenceIndicatorType.SEP)
    raise ValueError(f"invalid action: {action_raw}")


class AlphaEnvWrapper(gym.Wrapper):
    """Stable-Baselines-compatible view of :class:`AlphaEnvCore`."""

    def __init__(self, env: AlphaEnvCore) -> None:
        super().__init__(env)
        self.action_space = gym.spaces.Discrete(SIZE_ACTION)
        self.observation_space = gym.spaces.Box(
            low=0,
            high=SIZE_ALL - 1,
            shape=(MAX_EXPR_LENGTH,),
            dtype=np.uint8,
        )
        self.counter = 0
        self.state = np.zeros(MAX_EXPR_LENGTH, dtype=np.uint8)

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        self.counter = 0
        self.state = np.zeros(MAX_EXPR_LENGTH, dtype=np.uint8)
        _, info = self.env.reset(seed=seed, options=options)
        return self.state.copy(), info

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        _, reward, terminated, truncated, info = self.env.step(self.action(action))
        if not terminated and not truncated:
            self.state[self.counter] = action
            self.counter += 1
        return self.state.copy(), self.reward(reward), terminated, truncated, info

    @staticmethod
    def action(action: int) -> Token:
        return action2token(action)

    @staticmethod
    def reward(reward: float) -> float:
        return float(reward + REWARD_PER_STEP)

    def action_masks(self) -> np.ndarray:
        mask = np.zeros(SIZE_ACTION, dtype=bool)
        valid = self.env.valid_action_types()
        for i in range(OFFSET_OP, OFFSET_OP + SIZE_OP):
            if valid["op"][OPERATORS[i - OFFSET_OP].category_type()]:
                mask[i - 1] = True
        if valid["select"][1]:
            mask[OFFSET_FEATURE - 1 : OFFSET_FEATURE + SIZE_FEATURE - 1] = True
        if valid["select"][2]:
            mask[OFFSET_CONSTANT - 1 : OFFSET_CONSTANT + SIZE_CONSTANT - 1] = True
        if valid["select"][3]:
            mask[OFFSET_DELTA_TIME - 1 : OFFSET_DELTA_TIME + SIZE_DELTA_TIME - 1] = True
        if valid["select"][4]:
            mask[OFFSET_SEP - 1] = True
        return mask


def AlphaEnv(pool: AlphaPoolBase, **kwargs: Any) -> AlphaEnvWrapper:
    return AlphaEnvWrapper(AlphaEnvCore(pool=pool, **kwargs))

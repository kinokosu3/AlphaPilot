"""Regression tests for factor-mining workflow snapshots."""

from __future__ import annotations

import pickle
import threading

from alphapilot.modules.alpha_mining.loops.alphapilot_loop import AlphaPilotLoop


class _RuntimeContext:
    """Representative process-local context that pickle cannot serialize."""

    def __init__(self) -> None:
        self.lock = threading.RLock()


def test_alphapilot_snapshot_excludes_runtime_context() -> None:
    loop = AlphaPilotLoop.__new__(AlphaPilotLoop)
    runtime_context = _RuntimeContext()
    loop.context = runtime_context
    loop.loop_idx = 2
    loop.step_idx = 3
    loop.loop_prev_out = {"factor_propose": "test hypothesis"}

    restored = pickle.loads(pickle.dumps(loop))

    # Snapshotting must not detach services from the actively running loop.
    assert loop.context is runtime_context
    # The current engine context is injected by AlphaMiningModule on resume.
    assert restored.context is None
    assert restored.loop_idx == 2
    assert restored.step_idx == 3
    assert restored.loop_prev_out == {"factor_propose": "test hypothesis"}

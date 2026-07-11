from __future__ import annotations

import pytest


def test_llm_external_attempts_are_bounded_to_initial_plus_two(monkeypatch) -> None:
    from alphapilot.oai import llm_utils

    backend = llm_utils.APIBackend.__new__(llm_utils.APIBackend)
    backend.retry_wait_seconds = 0
    calls = 0

    def fail(**kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("malformed provider response")

    monkeypatch.setattr(backend, "_create_chat_completion_auto_continue", fail)
    monkeypatch.setattr(llm_utils.LLM_SETTINGS, "max_retry", 3)

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        backend._try_create_chat_completion_or_embedding(
            chat_completion=True,
            messages=[],
        )

    assert calls == 3

"""LLM 返回脏 JSON 时的容错契约。

这两个测试来自一次真实崩溃：2026-09-02 的一轮 `mine` 在 5 分半后死于
``json.decoder.JSONDecodeError: Extra data: line 4 column 2``，代理在
``json_mode=True`` 下返回了**两个前后拼接的 JSON 对象**。

两个独立缺陷，各一个测试：

1. ``extract_and_validate_llm_json`` 用「首 ``{`` 到末 ``}``」切片，把 ``{...}{...}``
   整段装进一个字符串，于是 ``json.loads`` 抛 ``Extra data``。
2. ``FactorFinalDecisionEvaluator.evaluate`` 的 ``except json.JSONDecodeError``
   **立即 raise 且不计入 attempts**，所以 ``max_attempts=3`` 对 JSON 错误形同虚设；
   裸的 ``JSONDecodeError`` 逃出 ``LoopBase.run``（它只接 ``CoderError`` 与
   ``skip_loop_error``），整个 ``mine`` 进程退出。

都不需要 API key：第一个是纯函数，第二个把 LLM 换成 fake。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


class _FakeLLM:
    """只提供 evaluate() 用到的两个方法。"""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    def count_tokens(self, **kwargs) -> int:
        return 0          # 永远不超限，跳过那个 10 次折半循环

    def chat_completion(self, **kwargs) -> str:
        self.calls += 1
        return self._response


# --------------------------------------------------------------- 缺陷 1：提取

_TWO_OBJECTS = (
    '{"final_decision": true, "final_feedback": "first"}'
    '{"final_decision": false, "final_feedback": "second"}'
)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(_TWO_OBJECTS, id="two_concatenated_objects"),
        pytest.param(f"Here you go: {_TWO_OBJECTS}", id="prose_then_two_objects"),
        pytest.param('{"final_decision": true, "final_feedback": "first"} trailing junk',
                     id="object_then_prose"),
    ],
)
def test_extract_takes_the_first_complete_object(response: str) -> None:
    """两个拼接对象时取**第一个**，而不是把两个都塞进去然后 Extra data。"""
    from alphapilot.oai.llm_utils import extract_and_validate_llm_json

    parsed = json.loads(extract_and_validate_llm_json(response))
    assert parsed["final_feedback"] == "first"


def test_extract_still_repairs_a_single_object_with_trailing_commas() -> None:
    """尾随逗号的单对象仍走 _remove_trailing_commas 兜底路径。"""
    from alphapilot.oai.llm_utils import extract_and_validate_llm_json

    parsed = json.loads(
        extract_and_validate_llm_json('{"final_decision": true, "final_feedback": "ok",}')
    )
    assert parsed["final_decision"] is True


@pytest.mark.parametrize("response", ["", "no json here at all"])
def test_extract_still_rejects_a_response_with_no_object(response: str) -> None:
    from alphapilot.oai.llm_utils import extract_and_validate_llm_json

    with pytest.raises(json.JSONDecodeError):
        extract_and_validate_llm_json(response)


# ------------------------------------------------------- 缺陷 2：不杀死整轮

def _evaluator_with(monkeypatch, response: str) -> tuple[object, _FakeLLM]:
    from alphapilot.components.coder.factor_coder import eva_utils

    fake = _FakeLLM(response)
    monkeypatch.setattr(eva_utils, "get_llm", lambda **kwargs: fake)
    evaluator = eva_utils.FactorFinalDecisionEvaluator(scen=None)
    return evaluator, fake


def _evaluate(evaluator) -> tuple:
    return evaluator.evaluate(
        target_task=SimpleNamespace(get_task_information=lambda: "a factor"),
        execution_feedback="ran fine",
        value_feedback=None,
        code_feedback="looks fine",
    )


def test_undecodable_response_raises_coder_error_not_json_error(monkeypatch) -> None:
    """解析持续失败时抛 CoderError —— LoopBase.run 接得住它，轮次得以存活。

    裸 JSONDecodeError 会穿透 run() 一路到 fire 的 sys.exit，整个 mine 进程结束。
    """
    from alphapilot.core.exception import CoderError

    evaluator, fake = _evaluator_with(monkeypatch, "this is not json at all")

    with pytest.raises(CoderError):
        _evaluate(evaluator)
    # 三次都试过了，不是第一次就放弃。
    assert fake.calls == 3


def test_two_object_response_now_succeeds_instead_of_crashing(monkeypatch) -> None:
    """崩溃现场的那个 payload 现在应当正常解析出第一个对象。"""
    evaluator, fake = _evaluator_with(monkeypatch, _TWO_OBJECTS)

    decision, feedback = _evaluate(evaluator)
    assert decision is True
    assert feedback == "first"
    assert fake.calls == 1          # 一次就成功，不该重试

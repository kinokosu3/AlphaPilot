from __future__ import annotations

import json


def test_factor_proposals_are_registered_as_scalar_rows(monkeypatch) -> None:
    from alphapilot.modules.alpha_mining.qlib.proposal import factor_proposal

    factors = {
        "factor_a": {
            "description": "a",
            "variables": {"$close": "close"},
            "formulation": "a",
            "expression": "TS_MEAN($close, 10)/($close+1e-8)-1",
        },
        "factor_b": {
            "description": "b",
            "variables": {"$volume": "volume"},
            "formulation": "b",
            "expression": "TS_MEAN($volume, 20)/($volume+1e-8)-1",
        },
    }

    class FakeLLM:
        def chat_completion(self, *_args, **_kwargs):
            return json.dumps(factors)

    class FakeRegulator:
        def __init__(self):
            self.added = []

        def check_expression(self, expression):
            return True, {"expr": expression}, None

        def is_expression_acceptable(self, _evaluation):
            return True

        def add_factor(self, name, expression):
            self.added.append((name, expression))

    class FakeScenario:
        background = "daily equity factor research"
        qlib_template_dir = None

        def get_scenario_all_desc(self, **_kwargs):
            return self.background

    monkeypatch.setattr(factor_proposal, "get_llm", lambda: FakeLLM())
    constructor = factor_proposal.AlphaPilotHypothesis2FactorExpression()
    regulator = FakeRegulator()
    constructor.factor_regulator = regulator
    trace = factor_proposal.Trace(scen=FakeScenario())
    hypothesis = factor_proposal.AlphaPilotHypothesis(
        "test", "observation", "justification", "knowledge", "specification"
    )

    experiment = constructor.convert(hypothesis, trace)

    assert regulator.added == [
        ("factor_a", factors["factor_a"]["expression"]),
        ("factor_b", factors["factor_b"]["expression"]),
    ]
    assert len(experiment.sub_tasks) == 2

from __future__ import annotations

import json
from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined


def test_factor_prompt_examples_are_valid_json() -> None:
    from alphapilot.modules.alpha_mining.qlib.proposal.factor_proposal import (
        alphapilot_prompt_dict,
    )

    output_format = alphapilot_prompt_dict["factor_experiment_output_format"]
    schema_and_example = output_format.split("The schema is as follows:\n", 1)[1]
    schema, example = schema_and_example.split("\n\nHere is an example:\n", 1)

    assert len(json.loads(schema)) == 2
    assert len(json.loads(example)) == 2


def test_factor_prompt_renders_implementation_and_factor_history() -> None:
    from alphapilot.modules.alpha_mining.qlib.proposal.factor_proposal import (
        alphapilot_prompt_dict,
    )

    old_expression = "TS_MEAN($close, 20)/$close-1"
    old_factor = SimpleNamespace(
        factor_name="old_momentum",
        factor_expression=old_expression,
    )
    feedback = SimpleNamespace(
        observations="observation",
        hypothesis_evaluation="evaluation",
        new_hypothesis="new hypothesis",
        reason="reason",
        decision="decision",
    )
    experiment = SimpleNamespace(
        sub_workspace_list=[SimpleNamespace(code_dict={"factor.py": "expr = '$close'"})]
    )
    trace = SimpleNamespace(hist=[("old hypothesis", experiment, feedback)])

    rendered_history = (
        Environment(undefined=StrictUndefined)
        .from_string(alphapilot_prompt_dict["hypothesis_and_feedback"])
        .render(trace=trace)
    )
    rendered_proposal = (
        Environment(undefined=StrictUndefined)
        .from_string(alphapilot_prompt_dict["hypothesis2experiment"]["user_prompt"])
        .render(
            targets="factors",
            target_hypothesis="test",
            hypothesis_and_feedback=rendered_history,
            function_lib_description="allowed functions",
            target_list=[old_factor],
            RAG=None,
            expression_validation_errors=None,
            expression_duplication=None,
        )
    )

    assert "Corresponding factor implementation: expr = '$close'" in rendered_history
    assert f"old_momentum: {old_expression}" in rendered_proposal

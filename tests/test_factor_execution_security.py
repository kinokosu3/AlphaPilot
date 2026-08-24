from __future__ import annotations

import ast
from unittest.mock import Mock


def _literal_assignments(code: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for node in ast.walk(ast.parse(code)):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = node.value.value
    return assignments


def test_factor_template_json_escapes_expression_and_name() -> None:
    from alphapilot.components.coder.factor_coder.evolving_strategy import code_template

    expression = '$close"\nraise RuntimeError("expression injected")\n#'
    factor_name = 'factor"\nraise RuntimeError("name injected")\n#'
    code = code_template.render(expression=expression, factor_name=factor_name)
    tree = ast.parse(code)

    assignments = _literal_assignments(code)
    assert assignments["expr"] == expression
    assert assignments["name"] == factor_name
    assert not any(isinstance(node, ast.Raise) for node in ast.walk(tree))


def test_factor_execution_uses_argv_without_a_shell(tmp_path, monkeypatch) -> None:
    from alphapilot.components.coder.factor_coder import factor as factor_module

    monkeypatch.setenv("ALPHAPILOT_PICKLE_CACHE_ENABLED", "false")
    data_path = tmp_path / "factor_data"
    data_path.mkdir()
    task = factor_module.FactorTask(
        factor_name="argv_test",
        factor_description="test factor",
        factor_formulation="test formulation",
        version=1,
    )
    workspace = factor_module.FactorFBWorkspace(target_task=task)
    workspace.workspace_path = tmp_path / "workspace with spaces"
    workspace.inject_code(**{"factor.py": "pass\n"})

    python_bin = "/opt/factor python/bin/python"

    monkeypatch.setattr(
        factor_module,
        "resolve_factor_data_dir",
        lambda _workspace, _data_type: data_path,
    )
    monkeypatch.setattr(factor_module, "resolve_factor_python_bin", lambda: python_bin)
    check_output = Mock(return_value=b"")
    monkeypatch.setattr(factor_module.subprocess, "check_output", check_output)

    feedback, result = workspace.execute(data_type="Debug")

    positional, keywords = check_output.call_args
    assert positional == ([python_bin, str(workspace.workspace_path / "factor.py")],)
    assert isinstance(positional[0], list)
    assert keywords["cwd"] == workspace.workspace_path
    assert keywords.get("shell", False) is False
    assert result is None
    assert factor_module.FactorFBWorkspace.FB_OUTPUT_FILE_NOT_FOUND in feedback

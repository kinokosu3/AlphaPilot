"""Slow packaging test for real pip install/uninstall entry-point discovery."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import venv
from pathlib import Path

import pytest


@pytest.mark.slow
def test_pip_install_and_uninstall_changes_catalog_after_process_restart(tmp_path: Path) -> None:
    package = tmp_path / "alphapilot_broker_dummy"
    module = package / "alphapilot_broker_dummy"
    module.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [build-system]
            requires = ["setuptools>=68"]
            build-backend = "setuptools.build_meta"
            [project]
            name = "alphapilot-broker-dummy"
            version = "1.0.0"
            [project.entry-points."alphapilot.live.plugins"]
            dummy = "alphapilot_broker_dummy.plugin:get_plugin_spec"
            """
        ),
        encoding="utf-8",
    )
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "plugin.py").write_text(
        textwrap.dedent(
            """
            from alphapilot.systems.live.plugin import LivePluginSpec, ProviderSpec, TradeChannelSpec
            def get_plugin_spec():
                return LivePluginSpec(
                    plugin_id="dummy",
                    providers=(ProviderSpec(
                        name="dummy",
                        factory_path="alphapilot_broker_dummy.plugin:create_gateway",
                        gateway_name="DUMMY",
                        trade=TradeChannelSpec(),
                    ),),
                )
            def create_gateway(*, name, roles):
                raise RuntimeError("catalog test must not instantiate")
            """
        ),
        encoding="utf-8",
    )

    env_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(env_dir)
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(package)],
        check=True,
        capture_output=True,
        text=True,
    )
    probe = (
        "from alphapilot.kernel import build_engine; "
        "rows=build_engine().collect_commands()['live_brokers'](); "
        "print(','.join(x['name'] for x in rows))"
    )
    installed = subprocess.run(
        [str(python), "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "dummy" in installed.stdout.strip().split(",")

    subprocess.run(
        [str(python), "-m", "pip", "uninstall", "-y", "alphapilot-broker-dummy"],
        check=True,
        capture_output=True,
        text=True,
    )
    removed = subprocess.run(
        [str(python), "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "dummy" not in removed.stdout.strip().split(",")

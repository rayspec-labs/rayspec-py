"""A third-party package adds a command through the ``rayspec.cli_plugins`` entry point."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from .conftest import InstallPlugin

GOOD = """
import typer


def register(app: typer.Typer) -> None:
    @app.command("acme-lint")
    def acme_lint() -> None:
        \"\"\"Lint the tree the acme way.\"\"\"
        typer.echo("acme ok")
"""

SHADOWS_BUILTIN = """
import typer


def register(app: typer.Typer) -> None:
    @app.command("run")
    def run() -> None:
        \"\"\"Hijack the builtin.\"\"\"
        typer.echo("hijacked")
"""

RAISES_ON_IMPORT = """
raise RuntimeError("boom at import time")
"""

RAISES_IN_REGISTER = """
import typer


def register(app: typer.Typer) -> None:
    @app.command("acme-half")
    def acme_half() -> None:
        typer.echo("half")

    raise RuntimeError("boom during register")
"""

NOT_CALLABLE = """
register = 5
"""

REPLACES_CALLBACK = """
import typer


def register(app: typer.Typer) -> None:
    @app.callback()
    def _root() -> None:
        \"\"\"Hijacked root.\"\"\"

    @app.command("acme-cb")
    def acme_cb() -> None:
        typer.echo("cb")
"""


def _build():
    from rayspec.cli.app import build_app

    return build_app()


def test_plugin_command_is_registered_and_runs(install_plugin: InstallPlugin) -> None:
    install_plugin(
        "acme-rayspec",
        version="0.2.0",
        modules={"acme_plugin": GOOD},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    app = _build()
    runner = CliRunner()
    assert "acme-lint" in runner.invoke(app, ["--help"]).output
    result = runner.invoke(app, ["acme-lint"])
    assert result.exit_code == 0
    assert "acme ok" in result.output


def test_plugin_may_not_shadow_a_builtin_command(install_plugin: InstallPlugin) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": SHADOWS_BUILTIN},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    with pytest.warns(RuntimeWarning, match=r"acme.*'run'"):
        app = _build()
    result = CliRunner().invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "hijacked" not in result.output
    assert "--stubs" in result.output  # the builtin `run`, not the plugin's


def test_broken_plugin_never_breaks_the_cli(install_plugin: InstallPlugin) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": RAISES_ON_IMPORT},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    with pytest.warns(RuntimeWarning, match="boom at import time"):
        app = _build()
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "workflows" in result.output


def test_plugin_that_raises_while_registering_is_rolled_back(
    install_plugin: InstallPlugin,
) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": RAISES_IN_REGISTER},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    with pytest.warns(RuntimeWarning, match="boom during register"):
        app = _build()
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "acme-half" not in result.output


def test_entry_point_that_is_not_callable_is_skipped(install_plugin: InstallPlugin) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": NOT_CALLABLE},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    with pytest.warns(RuntimeWarning, match="not callable"):
        app = _build()
    assert CliRunner().invoke(app, ["--help"]).exit_code == 0


def test_plugin_can_not_replace_the_root_callback(install_plugin: InstallPlugin) -> None:
    from rayspec import __version__

    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": REPLACES_CALLBACK},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    with pytest.warns(RuntimeWarning, match="replaced the root callback"):
        app = _build()
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
    assert "acme-cb" in CliRunner().invoke(app, ["--help"]).output


def test_first_plugin_wins_a_collision_between_two_plugins(install_plugin: InstallPlugin) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": GOOD},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    install_plugin(
        "zeta-rayspec",
        modules={"zeta_plugin": GOOD.replace("acme ok", "zeta ok")},
        entry_points={"rayspec.cli_plugins": {"zeta": "zeta_plugin:register"}},
    )
    with pytest.warns(RuntimeWarning, match=r"zeta.*'acme-lint'"):
        app = _build()
    result = CliRunner().invoke(app, ["acme-lint"])
    assert result.exit_code == 0
    assert "acme ok" in result.output


def test_no_plugins_installed_changes_nothing() -> None:
    """With nothing installed the scan adds no command and imports no plugin module."""
    from rayspec.cli.plugins import command_names, loaded_cli_plugins, register_cli_plugins

    app = _build()
    before = command_names(app)
    assert {"workflows", "run", "runs", "plan"} <= before
    assert register_cli_plugins(app) == ()
    assert command_names(app) == before
    assert loaded_cli_plugins() == ()

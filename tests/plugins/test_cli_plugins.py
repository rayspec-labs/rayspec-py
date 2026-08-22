"""A third-party package adds a command through the ``rayspec.cli_plugins`` entry point."""

from __future__ import annotations

import warnings

import pytest
import typer
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


def _build_reporting(capsys: pytest.CaptureFixture[str]) -> tuple[typer.Typer, str]:
    """Build the app and return it with what the plugin scan printed on stderr.

    A plugin problem is a rayspec line, never a Python warning: Python renders one with the
    absolute path of rayspec's own ``plugins.py`` plus an echoed line of its source, which reads
    as a rayspec crash about somebody else's package. ``simplefilter("error")`` makes the old
    behaviour a test failure rather than something to notice later.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        app = _build()
    return app, capsys.readouterr().err


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


def test_plugin_may_not_shadow_a_builtin_command(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": SHADOWS_BUILTIN},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    app, notice = _build_reporting(capsys)
    assert "acme" in notice and "'run'" in notice
    result = CliRunner().invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "hijacked" not in result.output
    assert "--stubs" in result.output  # the builtin `run`, not the plugin's


def test_broken_plugin_never_breaks_the_cli(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": RAISES_ON_IMPORT},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    app, notice = _build_reporting(capsys)
    assert "boom at import time" in notice
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "workflows" in result.output


def test_plugin_that_raises_while_registering_is_rolled_back(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": RAISES_IN_REGISTER},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    app, notice = _build_reporting(capsys)
    assert "boom during register" in notice
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "acme-half" not in result.output


def test_entry_point_that_is_not_callable_is_skipped(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": NOT_CALLABLE},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    app, notice = _build_reporting(capsys)
    assert "not callable" in notice
    assert CliRunner().invoke(app, ["--help"]).exit_code == 0


def test_plugin_can_not_replace_the_root_callback(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    from rayspec import __version__

    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": REPLACES_CALLBACK},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    app, notice = _build_reporting(capsys)
    assert "replaced the root callback" in notice
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
    assert "acme-cb" in CliRunner().invoke(app, ["--help"]).output


def test_first_plugin_wins_a_collision_between_two_plugins(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
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
    app, notice = _build_reporting(capsys)
    assert "zeta" in notice and "'acme-lint'" in notice
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


REORDERS_THE_TABLE = """
import typer


def register(app: typer.Typer) -> None:
    @app.command("acme-first")
    def acme_first() -> None:
        typer.echo("first")

    # a legal Typer operation: "list my command first in --help"
    app.registered_commands.insert(0, app.registered_commands.pop())
"""

CLEARS_THE_TABLE = """
import typer


def register(app: typer.Typer) -> None:
    app.registered_commands.clear()

    @app.command("acme-only")
    def acme_only() -> None:
        typer.echo("only")
"""


def test_plugin_that_reorders_the_command_table_keeps_every_builtin(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    """Builtins are protected by identity, so moving an entry can not delete one."""
    from rayspec.cli.plugins import command_names, loaded_cli_plugins

    baseline = command_names(_build())  # before the plugin is on sys.path
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": REORDERS_THE_TABLE},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    app, notice = _build_reporting(capsys)
    assert notice == ""  # nothing was shadowed, so nothing is reported
    assert baseline <= command_names(app)
    assert "acme-first" in command_names(app)
    assert CliRunner().invoke(app, ["workflows", "--help"]).exit_code == 0
    (plugin,) = loaded_cli_plugins()
    assert plugin.ok
    assert plugin.commands == ("acme-first",)
    assert plugin.refused == ()


def test_plugin_that_drops_builtins_is_rolled_back_and_reported(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    """Clearing the table is undone: the plugin's own commands go with it."""
    from rayspec.cli.plugins import command_names, loaded_cli_plugins

    baseline = command_names(_build())  # before the plugin is on sys.path
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": CLEARS_THE_TABLE},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    app, notice = _build_reporting(capsys)
    assert "removed builtin commands" in notice
    assert baseline <= command_names(app)
    assert "acme-only" not in command_names(app)
    assert CliRunner().invoke(app, ["--help"]).exit_code == 0
    (plugin,) = loaded_cli_plugins()
    assert not plugin.ok


UNNAMED_CALLBACK = """
import typer


def register(app: typer.Typer) -> None:
    @app.command()
    def acme_named_by_typer() -> None:
        \"\"\"Named after the callback.\"\"\"
        typer.echo("derived")
"""


def test_a_command_without_a_name_is_matched_under_typers_own_name(
    install_plugin: InstallPlugin,
) -> None:
    """Collision detection has to reproduce Typer's ``do_thing`` -> ``do-thing`` rule."""
    from rayspec.cli.plugins import _default_command_name, command_names

    assert _default_command_name("acme_named_by_typer") == "acme-named-by-typer"
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": UNNAMED_CALLBACK},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )
    app = _build()
    assert "acme-named-by-typer" in command_names(app)
    assert CliRunner().invoke(app, ["acme-named-by-typer"]).output.strip() == "derived"


# --------------------------------------------------------------------------------------------
# how a problem reaches the user: one rayspec line, and not on an invocation that only reads
# --------------------------------------------------------------------------------------------


RAISES_A_MULTILINE_ERROR = """
raise RuntimeError("first line\\nsecond line\\nthird line")
"""


def _install_broken(install_plugin: InstallPlugin, source: str = RAISES_ON_IMPORT) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_plugin": source},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_plugin:register"}},
    )


def test_a_broken_plugin_is_one_rayspec_line_pointing_at_rayspec_plugins(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the user actually sees: not a `RuntimeWarning` with rayspec's own file path in it."""
    _install_broken(install_plugin)
    _, notice = _build_reporting(capsys)
    assert notice.count("\n") == 1  # exactly one line
    line = notice.strip()
    assert line.startswith("rayspec: ")
    assert "acme" in line and "boom at import time" in line
    assert "rayspec plugins" in line  # where the whole story is
    assert "RuntimeWarning" not in line
    assert "plugins.py" not in line and "site-packages" not in line
    assert "warnings.warn" not in line


def test_a_multiline_failure_is_still_one_line(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    """A plugin's exception message is arbitrary text; the notice is one bounded line."""
    _install_broken(install_plugin, RAISES_A_MULTILINE_ERROR)
    _, notice = _build_reporting(capsys)
    assert notice.count("\n") == 1
    assert "first line" in notice and "second line" not in notice


def test_two_broken_plugins_still_produce_one_line(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_broken(install_plugin)
    install_plugin(
        "zeta-rayspec",
        modules={"zeta_plugin": NOT_CALLABLE},
        entry_points={"rayspec.cli_plugins": {"zeta": "zeta_plugin:register"}},
    )
    _, notice = _build_reporting(capsys)
    assert notice.count("\n") == 1
    assert "and 1 more" in notice


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no-arguments"),
        pytest.param(["--help"], id="root-help"),
        pytest.param(["run", "--help"], id="command-help"),
        pytest.param(["completion", "zsh"], id="completion-script"),
    ],
)
def test_an_invocation_that_only_reads_is_not_interrupted(
    install_plugin: InstallPlugin,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    """A skipped plugin is a standing condition, not an answer to what was typed."""
    _install_broken(install_plugin)
    monkeypatch.setattr("sys.argv", ["rayspec", *argv])
    _, notice = _build_reporting(capsys)
    assert notice == ""


def test_a_completion_request_is_not_interrupted(
    install_plugin: InstallPlugin,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shell reads the completion protocol; a notice in the middle of it helps nobody."""
    from rayspec.cli.commands.completion import COMPLETE_VAR
    from rayspec.cli.plugins import COMPLETE_VAR as PLUGINS_COMPLETE_VAR

    assert PLUGINS_COMPLETE_VAR == COMPLETE_VAR  # spelled out in plugins.py; keep them in step
    _install_broken(install_plugin)
    monkeypatch.setenv(COMPLETE_VAR, "complete_zsh")
    monkeypatch.setattr("sys.argv", ["rayspec", "run"])
    _, notice = _build_reporting(capsys)
    assert notice == ""


def test_running_a_command_does_get_the_line(
    install_plugin: InstallPlugin,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_broken(install_plugin)
    monkeypatch.setattr("sys.argv", ["rayspec", "workflows"])
    _, notice = _build_reporting(capsys)
    assert notice.startswith("rayspec: ")


def test_the_problems_are_kept_for_rayspec_plugins(
    install_plugin: InstallPlugin, capsys: pytest.CaptureFixture[str]
) -> None:
    """The line is a pointer; `rayspec plugins` is the report, and it works when it is quiet."""
    from rayspec.cli.plugins import cli_plugin_problems

    _install_broken(install_plugin)
    _build_reporting(capsys)
    (problem,) = cli_plugin_problems()
    assert "boom at import time" in problem

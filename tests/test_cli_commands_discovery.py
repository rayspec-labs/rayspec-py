from typer.testing import CliRunner


def test_command_modules_are_auto_registered():
    """Any module in rayspec.cli.commands exposing register(app) is picked up without editing app.py."""
    from rayspec.cli import app as app_module

    # the built-in `version` command is registered via commands/version.py
    result = CliRunner().invoke(app_module.app, ["version"])
    assert result.exit_code == 0
    assert "rayspec.cli.commands.version" in app_module.discovered_command_modules()
    from rayspec import __version__

    assert result.output.strip() == f"rayspec {__version__}"

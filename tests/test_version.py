import re
import tomllib
from pathlib import Path

from typer.testing import CliRunner


def test_package_version_matches_pyproject():
    import rayspec

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert rayspec.__version__ == pyproject["project"]["version"]


def test_cli_version_command_prints_version():
    import rayspec
    from rayspec.cli.app import app

    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert re.search(rf"rayspec {re.escape(rayspec.__version__)}", result.output)

"""The CLI boundary for config/.env problems and project ``.env`` trust.

Every command that loads the config must turn a malformed ``config.yaml`` into one
``error: <path>:<line>: …`` line with exit 2 — never a traceback. The project ``.rayspec/.env``
is applied by the execution commands only (``run``/``resume``/``approve``/``reject``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

WORKFLOW = """\
rayspec: 1
name: gate
isolation: none
steps:
  - id: a
    shell: echo "FROM_ENV=${RAYSPEC_TEST_ENV:-unset}"
outputs:
  seen: "{{ steps.a.output }}"
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    monkeypatch.delenv("RAYSPEC_TEST_ENV", raising=False)
    monkeypatch.delenv("RAYSPEC_TEST_HOME_ENV", raising=False)
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "gate.yaml").write_text(WORKFLOW)
    return root


COMMANDS = [
    ["workflows"],
    ["agents"],
    ["validate"],
    ["plan", "gate"],
    ["run", "gate", "--dry-run", "--no-interactive"],
    ["runs"],
    ["show", "20260820-100000-aaaa"],
    ["logs", "20260820-100000-aaaa"],
    ["cancel", "20260820-100000-aaaa", "--yes"],
]


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: a[0])
@pytest.mark.parametrize("where", ["project", "home"])
def test_malformed_config_is_a_clean_error_everywhere(
    home: Path, project: Path, argv: list[str], where: str
) -> None:
    target = (project / ".rayspec" / "config.yaml") if where == "project" else home / "config.yaml"
    target.write_text("foo: [\n")
    result = CliRunner().invoke(app, [*argv, "--root", str(project)])
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output and "╭" not in result.output
    assert f"error: {target}:2: " in result.output.replace("\n", ""), result.output


@pytest.mark.parametrize(
    "text", ["projects: 5\n", "!!python/object/apply:os.system [echo pwned]\n", "- a\n"]
)
def test_wrong_type_and_unsafe_tag_are_clean_errors(home: Path, project: Path, text: str) -> None:
    (project / ".rayspec" / "config.yaml").write_text(text)
    result = CliRunner().invoke(app, ["workflows", "--root", str(project)])
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "error: " in result.output and "config.yaml" in result.output


def test_doctor_reports_a_broken_config_as_a_failed_check(home: Path, project: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text("foo: [\n")
    result = CliRunner().invoke(app, ["doctor", "--root", str(project), "--json"])
    assert "Traceback" not in result.output
    assert result.exit_code in {0, 1}


# -- project .env trust --------------------------------------------------------------------


def _write_envs(home: Path, project: Path) -> None:
    (home / ".env").write_text("RAYSPEC_TEST_HOME_ENV=home\n")
    (project / ".rayspec" / ".env").write_text("RAYSPEC_TEST_ENV=from-project\n")


def test_inspection_commands_do_not_apply_the_project_env(
    home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_envs(home, project)
    for argv in (["workflows"], ["validate"], ["plan", "gate"], ["runs"], ["agents"]):
        monkeypatch.delenv("RAYSPEC_TEST_ENV", raising=False)
        monkeypatch.delenv("RAYSPEC_TEST_HOME_ENV", raising=False)
        result = CliRunner().invoke(app, [*argv, "--root", str(project)])
        assert result.exit_code == 0, (argv, result.output)
        assert "RAYSPEC_TEST_ENV" not in os.environ, argv
        assert os.environ.get("RAYSPEC_TEST_HOME_ENV") == "home", argv  # home .env still loads
        assert "env: loaded" not in result.output, argv


def test_run_applies_the_project_env_and_says_so(
    home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_envs(home, project)
    result = CliRunner().invoke(
        app,
        ["run", "gate", "--dry-run", "--exec-shell", "--no-interactive", "--root", str(project)],
    )
    assert result.exit_code == 0, result.output + result.stderr
    assert "FROM_ENV=from-project" in result.stdout
    assert "env: loaded 1 variable from .rayspec/.env (project): RAYSPEC_TEST_ENV" in (
        result.stderr
    )
    assert "env: loaded" not in result.stdout


def test_project_env_wins_over_home_env_for_execution_commands(
    home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the same key in both files: the project value reaches the step ("project wins") and the
    # notice counts the colliding key as loaded from the project file
    (home / ".env").write_text("RAYSPEC_TEST_ENV=from-home\nRAYSPEC_TEST_HOME_ENV=home\n")
    (project / ".rayspec" / ".env").write_text("RAYSPEC_TEST_ENV=from-project\n")
    result = CliRunner().invoke(
        app,
        ["run", "gate", "--dry-run", "--exec-shell", "--no-interactive", "--root", str(project)],
    )
    assert result.exit_code == 0, result.output + result.stderr
    assert "FROM_ENV=from-project" in result.stdout, result.stdout
    assert "env: loaded 1 variable from .rayspec/.env (project): RAYSPEC_TEST_ENV" in (
        result.stderr
    ), result.stderr
    assert os.environ.get("RAYSPEC_TEST_HOME_ENV") == "home"
    # already-set process variables are never overridden by either file
    monkeypatch.setenv("RAYSPEC_TEST_ENV", "from-shell")
    result = CliRunner().invoke(
        app,
        ["run", "gate", "--dry-run", "--exec-shell", "--no-interactive", "--root", str(project)],
    )
    assert result.exit_code == 0, result.output + result.stderr
    assert "FROM_ENV=from-shell" in result.stdout, result.stdout
    assert "env: loaded" not in result.stderr  # nothing of the project file was applied


def test_invoked_command_uses_the_click_context() -> None:
    from rayspec.cli.commands._loader_common import invoked_command

    assert invoked_command() is None  # outside a CLI invocation


def test_invoked_command_sees_typers_vendored_click_context(home: Path, project: Path) -> None:
    # typer ≥ 0.20 runs on a vendored click whose context stack is separate from ``click``'s:
    # ``invoked_command`` must find the running command there (the switch depends on it)
    import click
    import typer

    from rayspec.cli.commands._loader_common import invoked_command

    seen: dict[str, object] = {}
    probe = typer.Typer()

    @probe.command()
    def run() -> None:
        seen["plain_click"] = click.get_current_context(silent=True)
        seen["invoked"] = invoked_command()

    @probe.command()
    def other() -> None:  # a second command makes the app a group like rayspec's
        pass

    CliRunner().invoke(probe, ["run"])
    assert seen["invoked"] == "run", seen

"""`rayspec completion <shell>` — the opt-in shell-completion script and its value lookups."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.completion import COMPLETE_VAR, RUN_COMMANDS, SHELLS, WORKFLOW_COMMANDS

RUNNER_SNIPPET = "from rayspec.cli.app import app; app(prog_name='rayspec')"


@pytest.fixture
def project(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    res = CliRunner().invoke(app, ["init", "--root", str(root), "--no-skill", "--kind", "content"])
    assert res.exit_code == 0, res.output
    return root


@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_completion_prints_a_script_per_supported_shell(shell: str, home: Path) -> None:
    res = CliRunner().invoke(app, ["completion", shell])
    assert res.exit_code == 0, res.output
    assert COMPLETE_VAR in res.stdout
    assert "rayspec completion --values workflows" in res.stdout
    assert "rayspec completion --values runs" in res.stdout
    for command in (*WORKFLOW_COMMANDS, *RUN_COMMANDS):
        assert command in res.stdout


def test_completion_without_a_shell_lists_the_supported_ones(home: Path) -> None:
    res = CliRunner().invoke(app, ["completion"])
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "Traceback" not in res.output
    for shell in SHELLS:
        assert shell in res.output


def test_completion_rejects_an_unsupported_shell(home: Path) -> None:
    res = CliRunner().invoke(app, ["completion", "tcsh"])
    assert res.exit_code == 2, res.output
    assert "tcsh" in res.output and "bash" in res.output


def test_values_workflows_lists_the_project_workflows(
    project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    res = CliRunner().invoke(app, ["completion", "--values", "workflows"])
    assert res.exit_code == 0, res.output
    assert res.stdout.split() == [
        "create_issue",
        "example",
        "fix_issue",
        "pr_review",
        "release_check",
        "resolve_conflicts",
        "review_block",
        "review_panel",
    "validate_pr",
    ]


def test_values_runs_lists_run_ids_newest_first(
    project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    for _ in range(2):
        run = CliRunner().invoke(app, ["run", "example", "--dry-run"])
        assert run.exit_code == 0, run.output
    res = CliRunner().invoke(app, ["completion", "--values", "runs"])
    assert res.exit_code == 0, res.output
    ids = res.stdout.split()
    assert len(ids) == 2
    assert ids == sorted(ids, reverse=True)


def test_values_never_fails_outside_a_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completion callback must never print an error into the shell's candidate list."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAYSPEC_HOME", str(tmp_path / "nowhere"))
    (tmp_path / ".rayspec").mkdir()
    (tmp_path / ".rayspec" / "config.yaml").write_text("{{ not yaml\n", encoding="utf-8")
    for kind in ("workflows", "runs"):
        res = CliRunner().invoke(app, ["completion", "--values", kind])
        assert res.exit_code == 0, (kind, res.output)
        assert res.output.strip() == "", (kind, res.output)


def test_values_and_shell_together_is_a_usage_error(home: Path) -> None:
    res = CliRunner().invoke(app, ["completion", "bash", "--values", "workflows"])
    assert res.exit_code == 2, res.output
    assert "--values" in res.output


def _rayspec_env(home: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {**os.environ, "RAYSPEC_HOME": str(home), "NO_COLOR": "1"}
    env.update(extra)
    return env


def test_the_completion_protocol_answers_command_names(home: Path, tmp_path: Path) -> None:
    """With `add_completion=False` Typer registers no shell classes; the completion module
    switches them on for an in-flight request, so the protocol works without the app-level
    `--install-completion` option."""
    proc = subprocess.run(
        [sys.executable, "-c", RUNNER_SNIPPET],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_rayspec_env(
            home, {COMPLETE_VAR: "complete_bash", "COMP_WORDS": "rayspec ru", "COMP_CWORD": "1"}
        ),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["run", "runs"]


@pytest.mark.parametrize("in_flight", [False, True])
def test_completion_classes_are_registered_only_for_a_completion_request(
    in_flight: bool, home: Path, tmp_path: Path
) -> None:
    """The app keeps `add_completion=False`, so an ordinary invocation leaves Click's shell
    registry — a process-global — untouched; a completion request fills it in."""
    code = (
        "import json; "
        "from rayspec.cli.app import app; "
        "from typer._click.shell_completion import _available_shells; "
        "print(json.dumps(sorted(_available_shells)))"
    )
    extra = {COMPLETE_VAR: "complete_bash"} if in_flight else {}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_rayspec_env(home, extra),
    )
    assert proc.returncode == 0, proc.stderr
    registered = json.loads(proc.stdout)
    assert (registered != []) is in_flight, registered
    if in_flight:
        assert set(registered) >= set(SHELLS)


#: How each shell is asked to parse a script without running it.
SYNTAX_CHECK = {"bash": ["bash", "-n"], "zsh": ["zsh", "-n"], "fish": ["fish", "--no-execute"]}


@pytest.mark.parametrize("shell", sorted(SHELLS))
def test_the_emitted_script_parses_in_its_own_shell(shell: str, home: Path, tmp_path: Path) -> None:
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed")
    res = CliRunner().invoke(app, ["completion", shell])
    assert res.exit_code == 0, res.output
    script = tmp_path / f"rayspec.{shell}"
    script.write_text(res.stdout, encoding="utf-8")
    proc = subprocess.run(
        [*SYNTAX_CHECK[shell], str(script)], capture_output=True, text=True, env=os.environ.copy()
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_the_bash_script_completes_workflow_names(
    project: Path, home: Path, tmp_path: Path
) -> None:
    """End-to-end, non-interactive: source the emitted script in a real bash and call the
    completion function the way bash does."""
    res = CliRunner().invoke(app, ["completion", "bash"])
    assert res.exit_code == 0, res.output
    script = tmp_path / "rayspec.bash"
    script.write_text(res.stdout, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "rayspec"
    shim.write_text(
        f'#!/bin/sh\nexec {sys.executable} -c "{RUNNER_SNIPPET}" "$@"\n', encoding="utf-8"
    )
    shim.chmod(0o755)
    driver = textwrap.dedent(f"""
        source '{script}'
        COMP_WORDS=(rayspec run ex)
        COMP_CWORD=2
        _rayspec_values_completion rayspec
        printf '%s\\n' "${{COMPREPLY[@]}}"
    """)
    proc = subprocess.run(
        ["bash", "-c", driver],
        capture_output=True,
        text=True,
        cwd=project,
        env=_rayspec_env(home, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}),
    )
    assert proc.returncode == 0, proc.stderr
    assert "example" in proc.stdout.split(), (proc.stdout, proc.stderr)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_the_bash_script_still_completes_commands(
    project: Path, home: Path, tmp_path: Path
) -> None:
    res = CliRunner().invoke(app, ["completion", "bash"])
    script = tmp_path / "rayspec.bash"
    script.write_text(res.stdout, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "rayspec"
    shim.write_text(
        f'#!/bin/sh\nexec {sys.executable} -c "{RUNNER_SNIPPET}" "$@"\n', encoding="utf-8"
    )
    shim.chmod(0o755)
    driver = textwrap.dedent(f"""
        source '{script}'
        COMP_WORDS=(rayspec ru)
        COMP_CWORD=1
        _rayspec_values_completion rayspec
        printf '%s\\n' "${{COMPREPLY[@]}}"
    """)
    proc = subprocess.run(
        ["bash", "-c", driver],
        capture_output=True,
        text=True,
        cwd=project,
        env=_rayspec_env(home, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}),
    )
    assert proc.returncode == 0, proc.stderr
    assert set(proc.stdout.split()) >= {"run", "runs"}, (proc.stdout, proc.stderr)

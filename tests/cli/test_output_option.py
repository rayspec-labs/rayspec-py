"""`--output table|json` — the uniform presentation flag, and `--json` as its documented alias."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands._loader_common import OutputFormat, resolve_output

#: Commands that still take `--json` alone. Empty, and meant to stay that way: the rule is that
#: `--output` is accepted wherever `--json` is, and a new command appearing here means one was
#: added without it. `show` was the last exception and adopted the flag once the gap was noticed.
KNOWN_GAPS: set[str] = set()

#: What a command has to add: `output: OutputOption = None` plus one `resolve_output` line.
ADOPT = (
    "add `output: OutputOption = None` and `json_ = resolve_output(output, json_)`, "
    "or extend KNOWN_GAPS"
)


def _leaf_commands() -> dict[str, Any]:
    root = get_command(app)
    found: dict[str, Any] = {}

    def walk(group: Any, prefix: str) -> None:
        for name in sorted(group.commands):
            cmd = group.commands[name]
            full = f"{prefix}{name}"
            if hasattr(cmd, "commands"):
                if getattr(cmd, "invoke_without_command", False):
                    found[full] = cmd
                walk(cmd, f"{full} ")
            else:
                found[full] = cmd

    walk(root, "")
    return found


def _flags(cmd: Any) -> set[str]:
    return {opt for param in cmd.params for opt in getattr(param, "opts", [])}


def _with(flag: str) -> set[str]:
    return {name for name, cmd in _leaf_commands().items() if flag in _flags(cmd)}


def test_every_json_command_also_takes_output() -> None:
    missing = _with("--json") - _with("--output")
    assert missing <= KNOWN_GAPS, f"{sorted(missing)}: {ADOPT}"


def test_output_is_never_added_without_json() -> None:
    """`--output` is the new spelling of `--json`, not a second meaning of the word: the only
    command that may take `--output` for something else is the one that had it first."""
    extra = _with("--output") - _with("--json")
    assert extra == {"runs stubs"}, sorted(extra)


def test_resolve_output_prefers_the_explicit_format() -> None:
    assert resolve_output(None, False) is False
    assert resolve_output(None, True) is True
    assert resolve_output(OutputFormat.table, False) is False
    assert resolve_output(OutputFormat.json, False) is True
    assert resolve_output(OutputFormat.json, True) is True  # both given, and they agree


@pytest.fixture
def project(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    res = CliRunner().invoke(app, ["init", "--root", str(root), "--no-skill"])
    assert res.exit_code == 0, res.output
    return root


#: ``(command, extra arguments)`` — one representative invocation per shape of `--json` output.
EQUIVALENT = [
    ("workflows", []),
    ("agents", []),
    ("validate", []),
    ("plan", ["example"]),
    ("providers", []),
    ("projects list", []),
    ("worktrees list", []),
    ("skill show", []),
    ("runs", []),
    # deterministic with these flags: nothing is asked, nothing is written, nothing is run
    ("quickstart", ["--no-interactive", "--no-init", "--no-run"]),
]


def _argv(command: str, extra: list[str], project: Path) -> list[str]:
    """``command`` + ``extra``, plus ``--root <project>`` when the command reads a project."""
    argv = [*command.split(), *extra]
    if "--root" in _flags(_leaf_commands()[command]):
        argv += ["--root", str(project)]
    return argv


@pytest.mark.parametrize(("command", "extra"), EQUIVALENT, ids=lambda case: str(case))
def test_output_json_is_the_same_as_json(
    command: str, extra: list[str], project: Path, home: Path
) -> None:
    base = _argv(command, extra, project)
    legacy = CliRunner().invoke(app, [*base, "--json"])
    modern = CliRunner().invoke(app, [*base, "--output", "json"])
    assert legacy.exit_code == modern.exit_code, (legacy.output, modern.output)
    assert legacy.stdout == modern.stdout, (legacy.stdout, modern.stdout)


@pytest.mark.parametrize(("command", "extra"), EQUIVALENT, ids=lambda case: str(case))
def test_output_table_is_the_default(
    command: str, extra: list[str], project: Path, home: Path
) -> None:
    base = _argv(command, extra, project)
    default = CliRunner().invoke(app, base)
    explicit = CliRunner().invoke(app, [*base, "--output", "table"])
    assert default.exit_code == explicit.exit_code
    assert default.stdout == explicit.stdout


@pytest.mark.parametrize(("command", "extra"), EQUIVALENT, ids=lambda case: str(case))
def test_json_and_output_table_conflict_is_a_usage_error(
    command: str, extra: list[str], project: Path, home: Path
) -> None:
    res = CliRunner().invoke(app, [*_argv(command, extra, project), "--json", "--output", "table"])
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "Traceback" not in res.output
    assert "error:" in res.output and "--json" in res.output and "--output" in res.output


def test_json_and_output_json_agree(project: Path, home: Path) -> None:
    res = CliRunner().invoke(
        app, ["workflows", "--root", str(project), "--json", "--output", "json"]
    )
    assert res.exit_code == 0, res.output
    assert res.stdout.lstrip().startswith("[")


def test_runs_rejects_a_listing_flag_before_a_subcommand(project: Path, home: Path) -> None:
    """`--output` belongs to the listing, exactly like `--json`: silently dropping it before a
    subcommand would print a table where the caller asked for JSON."""
    res = CliRunner().invoke(
        app, ["runs", "--output", "json", "--root", str(project), "stubs", "x"]
    )
    assert res.exit_code == 2, res.output
    assert "--output" in res.output and "listing" in res.output


def test_run_accepts_output_json(
    project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    stubs = ".rayspec/stubs/example.yaml"
    legacy = CliRunner().invoke(app, ["run", "example", "--dry-run", "--stubs", stubs, "--json"])
    modern = CliRunner().invoke(
        app, ["run", "example", "--dry-run", "--stubs", stubs, "--output", "json"]
    )
    assert legacy.exit_code == 0 and modern.exit_code == 0, (legacy.output, modern.output)
    assert modern.stdout.splitlines()[-1].startswith("{")
    assert len(legacy.stdout.splitlines()) == len(modern.stdout.splitlines())

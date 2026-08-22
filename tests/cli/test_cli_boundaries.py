"""The two CLI-wide boundaries: what an escaped error looks like, and the ``--root`` rule.

Both exist because the same decision used to be made per command. ``rayspec run`` mapped a store
error to ``error: …`` + exit 2 on the path that takes a lock and let the ``--dry-run`` path — the
one ``rayspec init`` prints first for a brand-new project — end in a traceback and exit 1; and the
``--root`` rule was enforced by :func:`~rayspec.cli.commands._loader_common.make_context`, so the
commands that resolve their root without it (``init``, ``skill install``, ``doctor``) did not
honour it. These tests are written against the boundaries themselves, so a command added later is
covered without anybody having to remember either rule.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest
import typer
import typer.main
from typer.core import TyperGroup
from typer.testing import CliRunner

from rayspec.cli.app import app, build_app
from rayspec.cli.commands._loader_common import filesystem_failure
from rayspec.errors import RayspecError

DRY = """
rayspec: 1
name: dry
isolation: none
steps:
  - {id: a, shell: echo a}
"""

GATED = """
rayspec: 1
name: gated
isolation: none
steps:
  - {id: a, shell: echo a}
  - {id: ok, needs: [a], approve: "ship?"}
  - {id: b, needs: [ok], shell: echo b}
"""

#: Marks of the failure mode this module exists to keep out of the CLI.
TRACEBACK_MARKS = ("Traceback (most recent call last)", "PermissionError", "NotADirectoryError")


@pytest.fixture
def wf_project(home: Path, project: Path) -> Path:
    wfs = project / ".rayspec" / "workflows"
    (wfs / "dry.yaml").write_text(DRY, encoding="utf-8")
    (wfs / "gated.yaml").write_text(GATED, encoding="utf-8")
    return project


@pytest.fixture
def unusable_store(home: Path) -> Path:
    """A ``RAYSPEC_HOME`` whose **run store** cannot be created: a regular file at ``projects/``.

    The reported repro is a ``chmod 500`` directory above ``RAYSPEC_HOME`` (a read-only mount, a
    home somebody else owns), but a mode is not a way a *test* can close a directory: root
    ignores it, so the suite would pass on exactly the machines where it proves the least. A file
    where a directory has to go is refused by the kernel for everybody — and it fails in the one
    place the traceback came from (``store.create`` → ``secure_mkdir``) while leaving config,
    ``.env`` and the policy file readable, so nothing else can answer first.
    """
    projects = home / "projects"
    projects.write_text("", encoding="utf-8")
    return projects


# --------------------------------------------------------------------------------------------------
# the error boundary: no command ends in a traceback
# --------------------------------------------------------------------------------------------------


def test_a_dry_run_reports_an_unusable_home_instead_of_a_traceback(
    cli: CliRunner, wf_project: Path, unusable_store: Path
) -> None:
    result = cli.invoke(app, ["run", "dry", "--dry-run", "--root", str(wf_project)])
    assert result.exit_code == 2, result.output
    assert "error:" in result.stderr
    assert str(unusable_store) in result.stderr
    for mark in TRACEBACK_MARKS:
        assert mark not in result.output


def test_a_dry_run_and_a_real_run_answer_the_same_way(
    cli: CliRunner, wf_project: Path, unusable_store: Path
) -> None:
    """The rehearsal path and the executing path must agree: the gap was that they did not."""
    dry = cli.invoke(app, ["run", "dry", "--dry-run", "--root", str(wf_project)])
    real = cli.invoke(app, ["run", "dry", "--root", str(wf_project)])
    assert dry.exit_code == real.exit_code == 2, (dry.output, real.output)
    assert dry.stderr.strip().startswith(("policy:", "error:"))
    assert "error:" in dry.stderr and "error:" in real.stderr


def test_a_dry_run_with_json_does_not_fail_silently(
    cli: CliRunner, wf_project: Path, unusable_store: Path
) -> None:
    """``--json`` must not swallow the refusal: exit 2 and a readable line, not exit 1 and air."""
    result = cli.invoke(app, ["run", "dry", "--dry-run", "--json", "--root", str(wf_project)])
    assert result.exit_code == 2, result.output
    assert "error:" in result.stderr
    for line in result.stdout.splitlines():  # whatever stdout carries stays parseable
        json.loads(line)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the directory mode")
def test_a_home_that_cannot_be_written_is_named_as_the_home(
    cli: CliRunner, wf_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported repro, verbatim: ``chmod 500`` above ``RAYSPEC_HOME``."""
    parent = tmp_path / "read-only"
    parent.mkdir(mode=0o500)
    try:
        monkeypatch.setenv("RAYSPEC_HOME", str(parent / "home"))
        result = cli.invoke(app, ["run", "dry", "--dry-run", "--root", str(wf_project)])
        assert result.exit_code == 2, result.output
        assert "cannot use the rayspec home" in result.stderr
        assert "RAYSPEC_HOME" in result.stderr
        for mark in TRACEBACK_MARKS:
            assert mark not in result.output
    finally:
        parent.chmod(0o700)


def test_the_boundary_covers_a_command_it_has_never_heard_of(cli: CliRunner) -> None:
    """A command registered after the fact — a plugin, a command added next month."""
    fresh = build_app()

    @fresh.command("boom")
    def boom() -> None:
        raise PermissionError(errno.EACCES, "Permission denied", "/nowhere/at/all")

    result = cli.invoke(fresh, ["boom"])
    assert result.exit_code == 2, result.output
    assert "error: Permission denied: /nowhere/at/all" in result.stderr
    assert "Traceback" not in result.output


def test_the_boundary_covers_a_sub_group(cli: CliRunner) -> None:
    """``rayspec new workflow``, ``rayspec runs diff`` … are invoked from inside the root group."""
    fresh = build_app()
    nested = typer.Typer()

    @nested.command("boom")
    def boom() -> None:
        raise RayspecError("the store said no", hint="check the store")

    fresh.add_typer(nested, name="deep")
    result = cli.invoke(fresh, ["deep", "boom"])
    assert result.exit_code == 2, result.output
    assert "error: the store said no" in result.stderr
    assert "hint: check the store" in result.stderr


def test_a_broken_pipe_is_not_reported_as_an_error(cli: CliRunner) -> None:
    """``rayspec runs | head -1``: the reader left, which is not the command failing."""
    fresh = build_app()

    @fresh.command("pipe")
    def pipe_() -> None:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    result = cli.invoke(fresh, ["pipe"])
    assert result.exit_code == 1, result.output
    assert "error:" not in result.stderr


def test_an_os_error_outside_the_home_is_reported_as_itself(tmp_path: Path) -> None:
    message, hint = filesystem_failure(
        PermissionError(errno.EACCES, "Permission denied", str(tmp_path / "elsewhere"))
    )
    assert message == f"Permission denied: {tmp_path / 'elsewhere'}"
    assert hint is not None and "read and write" in hint


# --------------------------------------------------------------------------------------------------
# the --root rule: one decision, every command that takes the option
# --------------------------------------------------------------------------------------------------

#: ``rayspec completion`` is the one exception and a deliberate one: its ``--root`` only feeds the
#: candidate lookup a shell calls, which is silent by contract — an ``error:`` line there would be
#: offered to the user as a completion candidate.
ROOT_RULE_EXEMPT: frozenset[tuple[str, ...]] = frozenset({("completion",)})


def commands_taking_root() -> list[tuple[tuple[str, ...], int]]:
    """Every ``rayspec`` command with a ``--root`` option, with its count of required arguments."""

    def walk(command: object, prefix: tuple[str, ...] = ()) -> object:
        if isinstance(command, TyperGroup):
            for name, sub in sorted(command.commands.items()):
                yield from walk(sub, (*prefix, name))  # type: ignore[misc]
            return
        params = getattr(command, "params", [])
        if any("--root" in getattr(p, "opts", []) for p in params):
            required = sum(1 for p in params if p.param_type_name == "argument" and p.required)
            yield prefix, required  # type: ignore[misc]

    found = [pair for pair in walk(typer.main.get_command(build_app()))]  # type: ignore[misc]
    return [pair for pair in found if pair[0] not in ROOT_RULE_EXEMPT]


ROOT_COMMANDS = commands_taking_root()


def test_every_command_taking_root_was_found() -> None:
    """A guard on the guard: an empty list would make the rule test below pass by doing nothing."""
    names = {path for path, _ in ROOT_COMMANDS}
    assert {("init",), ("skill", "install"), ("doctor",), ("run",), ("validate",)} <= names
    assert len(ROOT_COMMANDS) > 25


@pytest.mark.parametrize(
    ("path", "required"), ROOT_COMMANDS, ids=[" ".join(p) for p, _ in ROOT_COMMANDS]
)
def test_a_root_that_is_not_a_directory_is_a_usage_error(
    cli: CliRunner, home: Path, tmp_path: Path, path: tuple[str, ...], required: int
) -> None:
    missing = tmp_path / "typo"
    result = cli.invoke(app, [*path, *(["x"] * required), "--root", str(missing)])
    assert result.exit_code == 2, result.output
    assert f"--root '{missing}' is not a directory" in result.output
    # the writers are the point: a mistyped path must not BECOME the project it named
    assert not missing.exists()


def test_a_root_that_is_a_file_is_a_usage_error(cli: CliRunner, home: Path, tmp_path: Path) -> None:
    a_file = tmp_path / "file"
    a_file.write_text("", encoding="utf-8")
    for path in (["init"], ["skill", "install"], ["workflows"]):
        result = cli.invoke(app, [*path, "--root", str(a_file)])
        assert result.exit_code == 2, result.output
        assert "is not a directory" in result.output
    assert a_file.read_text(encoding="utf-8") == ""


def test_a_file_that_is_not_utf8_is_a_usage_error_not_a_traceback(
    cli: CliRunner, home: Path, tmp_path: Path
) -> None:
    """A stray byte in a checked-in file must read as "your file is wrong", not as a crash.

    ``UnicodeDecodeError`` is a ``ValueError``, so it escaped a boundary that named ``RayspecError``
    and ``OSError`` — and `docs/cli.md` promises in bold that no rayspec command ends in a
    traceback. A workflow with a stray byte is an ordinary thing to find in a repository; a
    traceback tells the reader rayspec is broken rather than their file.
    """
    project = tmp_path / "proj"
    (project / ".rayspec" / "workflows").mkdir(parents=True)
    (project / ".rayspec" / "workflows" / "binary.yaml").write_bytes(b"\xff\xfe\x00\x01")

    result = cli.invoke(app, ["validate", "--root", str(project)])

    assert result.exit_code == 2, result.output
    assert "not valid UTF-8" in result.output
    assert "Traceback" not in result.output
    # the bytes themselves are never echoed back into a terminal
    assert "\\xff" not in result.output and "\xff" not in result.output

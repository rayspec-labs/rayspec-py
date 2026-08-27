# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R1: the detached child's argv is rebuilt from the run command's PARSED parameters, so
a future ``run`` option cannot be silently dropped from a detached run. The introspection test
below fails the moment ``run`` grows an option that ``child_run_argv`` neither threads through
nor is on the explicit drop list — forcing a decision instead of a silent omission.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from typer.main import get_command

from rayspec.cli._detach import DETACHED_CHILD_OPT, child_run_argv
from rayspec.cli.app import app

#: ``run`` options the launcher owns rather than passing to the child, each with the reason:
#: the launcher forbids it with ``--detach`` (resume/stubs_init/force), owns the mode itself
#: (detach), owns its own console (json_/output/verbose), or forces its own value on the child
#: (quiet + no_interactive are always added, never mirrored from the parent).
LAUNCHER_OWNED = frozenset(
    {
        "detach",  # the launcher IS this flag; the child never re-detaches
        "resume",  # refused with --detach (a resume is not launched detached)
        "stubs_init",  # refused with --detach (writes a scaffold and exits)
        "force",  # only meaningful with resume / stubs_init, both refused
        "json_",  # the launcher owns stdout (prints the run id / a JSON object)
        "output",  # ditto — the child never re-parses --output
        "verbose",  # the child always runs --quiet
        "quiet",  # forced on for the child regardless of the parent
        "no_interactive",  # forced on for the child regardless of the parent
        "detached_child",  # the launcher INJECTS this (via child_run_argv's own tail), never mirrors it
    }
)


def _run_command() -> object:
    """The click command object behind ``rayspec run`` (its params carry the option names)."""
    root = get_command(app)
    return root.commands["run"]  # type: ignore[attr-defined]


def _run_option_names() -> set[str]:
    run = _run_command()
    return {p.name for p in run.params if p.param_type_name == "option"}


def _run_param_names() -> set[str]:
    """Every run parameter — options AND the ``workflow`` argument."""
    run = _run_command()
    return {p.name for p in run.params}


def _child_argv_params() -> set[str]:
    sig = inspect.signature(child_run_argv)
    # run_dir is injected by the launcher, not a mirror of a run option
    return {name for name in sig.parameters if name != "run_dir"}


def test_every_run_option_is_threaded_or_explicitly_owned() -> None:
    options = _run_option_names()
    threaded = _child_argv_params() & options
    owned = LAUNCHER_OWNED & options
    unclassified = options - threaded - owned
    assert not unclassified, (
        f"run gained option(s) {sorted(unclassified)} that child_run_argv neither threads "
        f"through nor lists as launcher-owned — decide which and update _detach.py / this test"
    )


def test_threaded_and_owned_do_not_overlap() -> None:
    assert not (_child_argv_params() & LAUNCHER_OWNED)


def test_launcher_owned_are_real_run_options() -> None:
    assert _run_option_names() >= LAUNCHER_OWNED


def test_child_argv_params_are_all_run_options_or_run_dir() -> None:
    """child_run_argv must not invent a parameter that is not a run option (typo guard)."""
    sig = inspect.signature(child_run_argv)
    stray = {n for n in sig.parameters if n != "run_dir"} - _run_param_names()
    assert not stray, f"child_run_argv has parameter(s) with no matching run option: {stray}"


def _full_argv(run_dir: Path) -> list[str]:
    return child_run_argv(
        workflow="wf.yaml",
        inputs=["a=1", "b=2"],
        inputs_file="ins.yaml",
        root="/proj",
        dry_run=True,
        stubs="stubs.yaml",
        stubs_from="20260101-000000-old",
        exec_shell=True,
        yes=True,
        approve_class=["chore", "scope"],
        allow_unsupported=True,
        fail_fast=True,
        worktree=True,
        base="main",
        locked=True,
        wait_slot="30m",
        repo="/repo",
        run_dir=run_dir,
    )


def test_full_option_set_round_trips_into_argv() -> None:
    argv = _full_argv(Path("/runs/r1"))
    assert argv[:2] == ["run", "wf.yaml"]
    # every value present, repeatables repeated
    assert argv.count("--input") == 2 and "a=1" in argv and "b=2" in argv
    i = argv.index("--inputs-file")
    assert argv[i : i + 2] == ["--inputs-file", "ins.yaml"]
    assert "--dry-run" in argv and "--exec-shell" in argv and "--yes" in argv
    assert argv.count("--approve-class") == 2 and "chore" in argv and "scope" in argv
    assert "--allow-unsupported" in argv and "--fail-fast" in argv
    assert "--worktree" in argv and "--no-worktree" not in argv
    b = argv.index("--base")
    assert argv[b : b + 2] == ["--base", "main"]
    assert "--locked" in argv and "--no-locked" not in argv
    w = argv.index("--wait-slot")
    assert argv[w : w + 2] == ["--wait-slot", "30m"]
    r = argv.index("--repo")
    assert argv[r : r + 2] == ["--repo", "/repo"]
    s = argv.index("--stubs")
    assert argv[s : s + 2] == ["--stubs", "stubs.yaml"]
    sf = argv.index("--stubs-from")
    assert argv[sf : sf + 2] == ["--stubs-from", "20260101-000000-old"]


def test_child_is_always_quiet_non_interactive_and_carries_the_run_dir() -> None:
    argv = child_run_argv(
        workflow="wf.yaml",
        inputs=None,
        inputs_file=None,
        root=None,
        dry_run=False,
        stubs=None,
        stubs_from=None,
        exec_shell=False,
        yes=False,
        approve_class=None,
        allow_unsupported=False,
        fail_fast=False,
        worktree=None,
        base=None,
        locked=None,
        wait_slot=None,
        repo=None,
        run_dir=Path("/runs/r2"),
    )
    assert argv == ["run", "wf.yaml", "--quiet", "--no-interactive", DETACHED_CHILD_OPT, "/runs/r2"]


def test_tri_state_flags_emit_the_negative_form() -> None:
    argv = child_run_argv(
        workflow="w",
        inputs=None,
        inputs_file=None,
        root=None,
        dry_run=False,
        stubs=None,
        stubs_from=None,
        exec_shell=False,
        yes=False,
        approve_class=None,
        allow_unsupported=False,
        fail_fast=False,
        worktree=False,
        base=None,
        locked=False,
        wait_slot=None,
        repo=None,
        run_dir=Path("/r"),
    )
    assert "--no-worktree" in argv and "--worktree" not in argv
    assert "--no-locked" in argv and "--locked" not in argv


def test_none_options_are_omitted_entirely() -> None:
    argv = child_run_argv(
        workflow="w",
        inputs=None,
        inputs_file=None,
        root=None,
        dry_run=False,
        stubs=None,
        stubs_from=None,
        exec_shell=False,
        yes=False,
        approve_class=None,
        allow_unsupported=False,
        fail_fast=False,
        worktree=None,
        base=None,
        locked=None,
        wait_slot=None,
        repo=None,
        run_dir=Path("/r"),
    )
    for absent in (
        "--input",
        "--inputs-file",
        "--root",
        "--stubs",
        "--stubs-from",
        "--base",
        "--wait-slot",
        "--repo",
        "--worktree",
        "--no-worktree",
        "--locked",
        "--no-locked",
        "--dry-run",
        "--exec-shell",
        "--yes",
        "--approve-class",
        "--allow-unsupported",
        "--fail-fast",
    ):
        assert absent not in argv, absent

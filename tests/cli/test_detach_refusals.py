# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R1: ``rayspec run --detach`` refuses the combinations a detached run cannot honour,
synchronously (exit 2) — before anything is backgrounded. These are the run-command guards, as
opposed to the launcher protocol unit-tested in ``test_detach_launcher.py``."""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

from .conftest import Seeded

pytestmark = pytest.mark.skipif(os.name != "posix", reason="--detach is POSIX-only")


def test_detach_with_resume_is_refused(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(
        app,
        [
            "run",
            "wf",
            "--detach",
            "--resume",
            "20260101-000000-fake",
            "--root",
            str(seeded.project),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--detach cannot be combined with --resume" in result.output


def test_detach_with_stubs_init_is_refused(cli: CliRunner, seeded: Seeded, tmp_path) -> None:
    result = cli.invoke(
        app,
        [
            "run",
            "wf",
            "--detach",
            "--stubs-init",
            str(tmp_path / "s.yaml"),
            "--root",
            str(seeded.project),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--detach cannot be combined with --stubs-init" in result.output


def test_detach_off_posix_is_refused(seeded: Seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    """The POSIX guard fires before any path work, so it is exercised directly (monkeypatching
    the global ``os.name`` to a non-POSIX value would otherwise switch pathlib to Windows
    semantics and break the harness itself)."""
    import typer

    from rayspec.cli.commands import run as run_mod
    from rayspec.cli.commands._loader_common import make_context

    ctx = make_context(seeded.project)  # built while os.name is still posix
    monkeypatch.setattr(run_mod.os, "name", "nt")
    with pytest.raises(typer.Exit) as exc:
        run_mod._launch_detached_run(
            workflow="wf",
            inputs=None,
            inputs_file=None,
            root=None,
            dry_run=False,
            stubs=None,
            stubs_from=None,
            stubs_init=None,
            exec_shell=False,
            yes=False,
            approve_class=None,
            allow_unsupported=False,
            fail_fast=False,
            resume=None,
            worktree=None,
            base=None,
            locked=None,
            wait_slot=None,
            repo=None,
            json_=False,
            run_id="20260827-120000-abcd",
            ctx=ctx,
            project_root=seeded.project,
            pure_dry_run=False,
        )
    assert exc.value.exit_code == 2

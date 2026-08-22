# SPDX-License-Identifier: Apache-2.0
"""Seam guard: ``rayspec runs`` is a Typer sub-app whose *bare* invocation never changed.

``runs stubs`` / ``runs diff`` hang off the same group; the flat listing (table, ``--all``,
``-n/--limit``, ``--json``, exit codes) must keep behaving exactly as it did when ``runs`` was a
plain ``@app.command``.
"""

from __future__ import annotations

import json
import re

import typer.core
import typer.main
from typer.testing import CliRunner

from rayspec.cli.app import app

from .conftest import FAILED_ID, OTHER_ID, OTHER_SLUG, PAUSED_ID, SUCCEEDED_ID, Seeded


def test_runs_is_a_group_with_subcommands() -> None:
    command = typer.main.get_command(app)
    assert isinstance(command, typer.core.TyperGroup)
    runs = command.commands["runs"]
    assert isinstance(runs, typer.core.TyperGroup), "runs must be a sub-app"


def test_bare_runs_table_is_unchanged(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["runs", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if "2026" in line]
    assert [line.split()[0] for line in lines] == [PAUSED_ID, FAILED_ID, SUCCEEDED_ID]
    # columns are separated by the table's padding, so a header cell may contain a space
    header = re.split(r"\s{2,}", result.output.splitlines()[0].strip())
    assert header == [
        "run",
        "workflow",
        "status",
        "started (UTC)",
        "duration",
        "steps",
        "tokens",
        "cost",
    ]
    assert "1m35s" in lines[2] and "5/5" in lines[2] and "~$0.26" in lines[2]
    assert OTHER_ID not in result.output


def test_bare_runs_flags_are_unchanged(cli: CliRunner, seeded: Seeded) -> None:
    all_ = cli.invoke(app, ["runs", "--all", "--root", str(seeded.project)])
    assert all_.exit_code == 0 and OTHER_SLUG in all_.output
    short = cli.invoke(app, ["runs", "-a", "--root", str(seeded.project)])
    assert short.output == all_.output
    limited = cli.invoke(app, ["runs", "-n", "2", "--root", str(seeded.project)])
    assert PAUSED_ID in limited.output and FAILED_ID in limited.output
    assert SUCCEEDED_ID not in limited.output
    rows = json.loads(cli.invoke(app, ["runs", "--json", "--root", str(seeded.project)]).output)
    assert [r["run_id"] for r in rows] == [PAUSED_ID, FAILED_ID, SUCCEEDED_ID]


def test_bare_runs_outside_a_project_still_notices(cli: CliRunner, tmp_path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = cli.invoke(app, ["runs", "--root", str(outside)])
    assert result.exit_code == 0
    assert "not inside a rayspec project" in result.output


def test_group_flags_before_a_subcommand_are_refused(cli: CliRunner, seeded: Seeded) -> None:
    """`--json`/`--all`/`-n` belong to the LISTING; before a subcommand they were parsed and then
    silently dropped, so `rayspec runs --json diff a b | jq` got a Rich table and exit 0."""
    result = cli.invoke(
        app, ["runs", "--json", "diff", SUCCEEDED_ID, SUCCEEDED_ID, "--root", str(seeded.project)]
    )
    assert result.exit_code == 2, result.output
    assert "--json" in result.output
    assert "rayspec runs diff" in result.output

    for flag in (["--all"], ["-n", "2"]):
        out = cli.invoke(app, ["runs", *flag, "stubs", SUCCEEDED_ID, "--root", str(seeded.project)])
        assert out.exit_code == 2, out.output


def test_root_before_a_subcommand_still_works(cli: CliRunner, seeded: Seeded) -> None:
    """The one group option a subcommand honours (it is stashed in ``ctx.obj``)."""
    result = cli.invoke(app, ["runs", "--root", str(seeded.project), "stubs", SUCCEEDED_ID])
    assert result.exit_code == 0, result.output

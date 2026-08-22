"""`rayspec runs [--all] [--limit N] [--json]`."""

from __future__ import annotations

import io
import json

from rich.console import Console
from typer.testing import CliRunner

from rayspec.cli.app import app

from .conftest import FAILED_ID, OTHER_ID, OTHER_SLUG, PAUSED_ID, SUCCEEDED_ID, Seeded


def test_runs_table_newest_first_current_project(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["runs", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if "2026" in line]
    assert [line.split()[0] for line in lines] == [PAUSED_ID, FAILED_ID, SUCCEEDED_ID]
    assert OTHER_ID not in result.output
    succeeded_line = lines[2]
    assert "fixit" in succeeded_line and "succeeded" in succeeded_line
    assert "1m35s" in succeeded_line and "5/5" in succeeded_line and "$0.26" in succeeded_line
    assert "paused" in lines[0] and "1/2" in lines[0]
    assert "failed" in lines[1]


def test_runs_table_header_is_one_line_at_eighty_columns(seeded: Seeded) -> None:
    """80 columns is the terminal this listing has to work on, in its widest form (``--all``).

    A column label wide enough to fold puts half the header on a second physical line, which
    costs `head -1`, `grep` and `awk` the one-line shape they read the listing by, and costs the
    run column — the cell a reader copies into the next command — the characters the second line
    took. Rich still shortens a label that does not fit; what it must not do is wrap one.
    """
    from rayspec.cli.commands.runs import runs_table

    records = sorted(seeded.store.list_runs(), key=lambda r: r.run_id, reverse=True)
    assert records, "the seeded project has runs"
    buffer = io.StringIO()
    Console(file=buffer, width=80, highlight=False).print(
        runs_table(records, show_project=True, planned={})
    )
    rendered = buffer.getvalue()
    lines = [line for line in rendered.splitlines() if line.strip()]
    columns = ["run", "workflow", "project", "status", "started", "duration", "steps", "tokens"]
    columns.append("cost")
    header = lines[0].split()
    assert len(header) == len(columns), f"the header folded onto a second line:\n{rendered}"
    for shown, name in zip(header, columns, strict=True):
        assert name.startswith(shown.rstrip("…")), f"{shown!r} is not the {name!r} column"
    assert lines[1].split()[0][:8].isdigit(), (
        f"the first run does not follow the header:\n{rendered}"
    )


def test_runs_all_and_limit(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["runs", "--all", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert OTHER_ID in result.output and OTHER_SLUG in result.output
    limited = cli.invoke(app, ["runs", "--all", "--limit", "2", "--root", str(seeded.project)])
    assert PAUSED_ID in limited.output and FAILED_ID in limited.output
    assert SUCCEEDED_ID not in limited.output and OTHER_ID not in limited.output


def test_runs_json_shape(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["runs", "--json", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [r["run_id"] for r in rows] == [PAUSED_ID, FAILED_ID, SUCCEEDED_ID]
    row = rows[2]
    assert row["workflow"] == "fixit" and row["status"] == "succeeded"
    assert row["duration_ms"] == 95_000
    assert row["steps_done"] == 5 and row["steps_total"] == 5
    assert row["tokens"] == 8500 and row["cost_usd"] == 0.2556
    assert row["project_slug"] == seeded.slug
    assert row["started_at"].startswith("2026-08-20T10:00:00")
    assert rows[0]["pause"]["step"] == "ok"


def test_runs_empty_project(cli: CliRunner, home, project) -> None:
    result = cli.invoke(app, ["runs", "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert "no runs" in result.output
    assert json.loads(cli.invoke(app, ["runs", "--json", "--root", str(project)]).output) == []


def test_runs_table_is_safe_against_rich_markup(cli: CliRunner, seeded: Seeded) -> None:
    run = seeded.store.load(FAILED_ID)
    run.workflow_name = "deploy[/bold]"
    run.project_slug = "local/[red]x"
    seeded.store.save(run)
    result = cli.invoke(app, ["runs", "--all", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "deploy[/bold]" in result.output and "local/[red]x" in result.output

"""Every resume entry point (``resume`` with/without ``--no-interactive``/``--yes``,
``approve``, ``reject``, ``run --resume``) refuses a changed workflow with exit 2 and the
``--force`` hint BEFORE reporting "paused awaiting approval" (exit 3) or touching the run."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.store.file import FileRunStore

GATE = """
rayspec: 1
name: gate
isolation: none
steps:
  - {id: a, shell: echo a}
  - {id: ok, needs: [a], approve: "ship?"}
  - {id: b, needs: [ok], shell: "echo {{ steps.ok.output }}"}
"""


@pytest.fixture
def paused(cli: CliRunner, home: Path, project: Path) -> tuple[str, FileRunStore, Path]:
    wf = project / ".rayspec" / "workflows" / "gate.yaml"
    wf.write_text(GATE, encoding="utf-8")
    result = cli.invoke(app, ["run", "gate", "--root", str(project), "--no-interactive"])
    assert result.exit_code == 3, result.output
    (slug_dir,) = [p for p in (home / "projects").glob("*/*") if (p / "runs").is_dir()]
    store = FileRunStore(slug_dir)
    (run_id,) = store.list_run_ids()
    # the workflow drifts while the run is paused
    wf.write_text(GATE + "  - {id: c, needs: [b], shell: echo c}\n", encoding="utf-8")
    return run_id, store, project


@pytest.mark.parametrize(
    "argv",
    [
        ["resume", "{run}", "--no-interactive"],
        ["resume", "{run}"],  # non-TTY under the CliRunner ⇒ same short-circuit as above
        ["resume", "{run}", "--yes"],
        ["approve", "{run}", "looks good"],
        ["reject", "{run}", "nope"],
        ["run", "gate", "--resume", "{run}", "--no-interactive"],
    ],
    ids=[
        "resume-no-interactive",
        "resume-non-tty",
        "resume-yes",
        "approve",
        "reject",
        "run-resume",
    ],
)
def test_every_resume_entry_point_refuses_a_changed_workflow_first(
    cli: CliRunner, paused: tuple[str, FileRunStore, Path], argv: list[str]
) -> None:
    run_id, store, project = paused
    args = [a.format(run=run_id) for a in argv] + ["--root", str(project)]
    result = cli.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert "changed since run" in result.output and "--force" in result.output
    assert "awaiting approval" not in result.output
    run = store.load(run_id)
    # nothing touched: still paused, no decision recorded, no resume counted
    assert run.status.value == "paused" and run.pause is not None
    assert run.pause.decision is None and run.resume_count == 0


def test_force_still_reports_the_pending_gate(
    cli: CliRunner, paused: tuple[str, FileRunStore, Path]
) -> None:
    run_id, store, project = paused
    result = cli.invoke(
        app, ["resume", run_id, "--no-interactive", "--force", "--root", str(project)]
    )
    assert result.exit_code == 3, result.output
    assert "awaiting approval" in result.output and f"rayspec approve {run_id}" in result.output
    assert store.load(run_id).status.value == "paused"

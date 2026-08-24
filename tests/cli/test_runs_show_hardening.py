"""`rayspec runs` / `rayspec show`: steps column, cost markers, warnings
block, escape neutralisation and the outside-a-project behaviour of `runs`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli import _runs_common as common
from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.providers.base import Usage
from rayspec.schema import RunStatus, StepStatus
from rayspec.store.model import RunRecord, StepRecord

from .conftest import FAILED_ID, PAUSED_ID, SUCCEEDED_ID, Seeded

ESC = "\x1b"
NASTY = f"before{ESC}]0;PWNED\x07{ESC}[31mRED{ESC}[0m [bold red]MARKUP[/] {ESC}[2J after"
CLEAN = "beforeRED [bold red]MARKUP[/]  after"

GATED = """\
rayspec: 1
name: gate
isolation: none
steps:
  - id: a
    shell: echo prepared
  - id: ok
    needs: [a]
    approve: "Continue?"
  - id: deploy
    needs: [ok]
    shell: echo deployed
"""


def _row(cli: CliRunner, seeded: Seeded, run_id: str) -> dict:
    rows = json.loads(cli.invoke(app, ["runs", "--json", "--root", str(seeded.project)]).output)
    return next(r for r in rows if r["run_id"] == run_id)


# -- steps column ------------------------------------------------------------------------


def test_skipped_steps_count_as_done(cli: CliRunner, seeded: Seeded) -> None:
    run = seeded.store.load(SUCCEEDED_ID)
    run.steps["notify"] = StepRecord(
        path="notify",
        id="notify",
        kind="shell",
        status=StepStatus.SKIPPED,
        skip_reason="when_false",
    )
    seeded.store.save(run)
    result = cli.invoke(app, ["runs", "--root", str(seeded.project)])
    line = next(ln for ln in result.output.splitlines() if SUCCEEDED_ID in ln)
    assert "6/6" in line, line
    row = _row(cli, seeded, SUCCEEDED_ID)
    assert row["steps_done"] == 6 and row["steps_total"] == 6
    assert row["steps_ok"] == 5 and row["steps_skipped"] == 1
    shown = cli.invoke(app, ["show", SUCCEEDED_ID, "--root", str(seeded.project)]).output
    assert "steps:      6/6 done (5 ok · 1 skipped)" in shown, shown


def test_paused_run_counts_planned_steps(cli: CliRunner, seeded: Seeded) -> None:
    (seeded.project / ".rayspec" / "workflows" / "gate.yaml").write_text(GATED)
    result = cli.invoke(app, ["runs", "--root", str(seeded.project)])
    line = next(ln for ln in result.output.splitlines() if PAUSED_ID in ln)
    assert "1/3" in line, line
    row = _row(cli, seeded, PAUSED_ID)
    assert row["steps_done"] == 1 and row["steps_total"] == 3
    shown = cli.invoke(app, ["show", PAUSED_ID, "--root", str(seeded.project)]).output
    assert "steps:      1/3 done" in shown, shown


def test_interrupted_run_counts_planned_steps_and_falls_back_without_workflow(
    cli: CliRunner, seeded: Seeded
) -> None:
    (seeded.project / ".rayspec" / "workflows" / "gate.yaml").write_text(GATED)
    run = seeded.store.load(PAUSED_ID)
    run.status = RunStatus.INTERRUPTED
    run.steps["ok"].status = StepStatus.INTERRUPTED
    seeded.store.save(run)
    assert "1/3" in cli.invoke(app, ["runs", "--root", str(seeded.project)]).output
    # an old record whose workflow is gone: recorded steps only (never an error)
    (seeded.project / ".rayspec" / "workflows" / "gate.yaml").unlink()
    result = cli.invoke(app, ["runs", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "1/2" in next(ln for ln in result.output.splitlines() if PAUSED_ID in ln)


DEPLOY = """\
rayspec: 1
name: deploy
isolation: none
steps:
  - id: a
    shell: echo a
  - id: b
    needs: [a]
    shell: exit 1
  - id: c
    needs: [b]
    shell: echo c
  - id: d
    needs: [c]
    shell: echo d
"""


def test_failed_run_keeps_recorded_total_without_workflow(cli: CliRunner, seeded: Seeded) -> None:
    # a: ok, b: failed, c: skipped(upstream_failed) → 2/3 (skipped counts as done); the
    # workflow file is not in the seeded project, so the total is the recorded steps
    assert "2/3" in next(
        ln
        for ln in cli.invoke(app, ["runs", "--root", str(seeded.project)]).output.splitlines()
        if FAILED_ID in ln
    )


def test_failed_and_cancelled_runs_count_planned_steps(cli: CliRunner, seeded: Seeded) -> None:
    # failed and cancelled runs are resumable, so the reader must see how much is left
    (seeded.project / ".rayspec" / "workflows" / "deploy.yaml").write_text(DEPLOY)
    (seeded.project / ".rayspec" / "workflows" / "gate.yaml").write_text(GATED)
    marked = cli.invoke(app, ["cancel", PAUSED_ID, "--mark", "--root", str(seeded.project)])
    assert marked.exit_code == 0, marked.output
    out = cli.invoke(app, ["runs", "--root", str(seeded.project)]).output
    assert "2/4" in next(ln for ln in out.splitlines() if FAILED_ID in ln), out
    assert "1/3" in next(ln for ln in out.splitlines() if PAUSED_ID in ln), out
    assert _row(cli, seeded, FAILED_ID)["steps_total"] == 4
    assert _row(cli, seeded, PAUSED_ID)["steps_total"] == 3
    shown = cli.invoke(app, ["show", FAILED_ID, "--root", str(seeded.project)]).output
    assert "steps:      2/4 done" in shown, shown
    # a succeeded run never consults the workflow (nothing is left)
    assert "5/5" in next(ln for ln in out.splitlines() if SUCCEEDED_ID in ln), out


def test_planned_steps_never_break_the_listing(
    cli: CliRunner, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    # any loader failure (not only RayspecError/OSError) falls back to the recorded steps
    def boom(ctx: object, run: object) -> object:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(common, "load_resolved_for", boom)
    result = cli.invoke(app, ["runs", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "1/2" in next(ln for ln in result.output.splitlines() if PAUSED_ID in ln)
    shown = cli.invoke(app, ["show", PAUSED_ID, "--root", str(seeded.project)])
    assert shown.exit_code == 0, shown.output


def test_planned_steps_load_each_workflow_once_per_listing(
    cli: CliRunner, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    (seeded.project / ".rayspec" / "workflows" / "gate.yaml").write_text(GATED)
    # three unfinished records of the same workflow
    for i in range(3):
        run = seeded.store.load(PAUSED_ID)
        run.run_id = f"20260820-13000{i}-ca{i}e"
        seeded.store.create(run)
    calls: list[str] = []
    real = common.load_resolved_for

    def counting(ctx: common.RunsContext, run: RunRecord) -> object:
        calls.append(run.workflow_name)
        return real(ctx, run)

    monkeypatch.setattr(common, "load_resolved_for", counting)
    result = cli.invoke(app, ["runs", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert result.output.count("1/3") >= 4
    assert calls.count("gate") == 1, calls  # one load per (project, workflow), not per run


def test_steps_progress_helper() -> None:
    run = RunRecord(
        run_id="20260820-100000-zzzz",
        workflow_name="w",
        workflow_path="w.yaml",
        workflow_hash="x" * 64,
        project_slug="s",
        project_root="/p",
        status=RunStatus.RUNNING,
    )
    run.steps["a"] = StepRecord(path="a", id="a", kind="shell", status=StepStatus.SUCCEEDED)
    run.steps["b"] = StepRecord(
        path="b", id="b", kind="shell", status=StepStatus.FAILED, tolerated=True
    )
    run.steps["c"] = StepRecord(path="c", id="c", kind="shell", status=StepStatus.SKIPPED)
    run.steps["d"] = StepRecord(path="d", id="d", kind="shell", status=StepStatus.RUNNING)
    assert common.steps_progress(run) == (3, 4)
    assert common.steps_progress(run, planned=["a", "b", "c", "d", "e", "f"]) == (3, 6)
    assert common.steps_detail(run) == "2 ok · 1 skipped"
    run.steps.pop("c")
    assert common.steps_detail(run) == "2 ok"


# -- cost markers -----------------------------------------------------------------------


def test_runs_and_show_mark_table_and_partial_costs(cli: CliRunner, seeded: Seeded) -> None:
    # fixit has a provider-priced step and a table-priced step → table
    assert common.run_cost_source(seeded.store.load(SUCCEEDED_ID)) == "table"
    line = next(
        ln
        for ln in cli.invoke(app, ["runs", "--root", str(seeded.project)]).output.splitlines()
        if SUCCEEDED_ID in ln
    )
    assert "~$0.26" in line
    # add an unpriced step with tokens → partial
    run = seeded.store.load(SUCCEEDED_ID)
    run.steps["implement2"] = StepRecord(
        path="implement2",
        id="implement2",
        kind="prompt",
        status=StepStatus.SUCCEEDED,
        usage=Usage(input=50_000, output=200),
        cost_usd=None,
        cost_source="none",
    )
    seeded.store.save(run)
    assert common.run_cost_source(run) == "partial"
    assert _row(cli, seeded, SUCCEEDED_ID)["cost_source"] == "partial"
    listing = cli.invoke(app, ["runs", "--root", str(seeded.project)]).output
    assert "≥$0.26" in next(ln for ln in listing.splitlines() if SUCCEEDED_ID in ln)
    shown = cli.invoke(app, ["show", SUCCEEDED_ID, "--root", str(seeded.project)]).output
    assert "cost: ≥$0.26 (1 step unpriced)" in shown, shown


def _priced(**steps: StepRecord) -> RunRecord:
    run = RunRecord(
        run_id="20260820-100000-cccc",
        workflow_name="w",
        workflow_path="w.yaml",
        workflow_hash="x" * 64,
        project_slug="s",
        project_root="/p",
        status=RunStatus.SUCCEEDED,
    )
    run.steps.update(steps)
    return run


def test_a_cost_without_a_named_source_is_a_provider_cost() -> None:
    """A step that reported a cost but no source is `provider`, never `none`.

    A cost that came back from the provider is not an estimate, and the listing must say so:
    the alternative is a run that spent money showing no cost source at all.
    """
    run = _priced(
        a=StepRecord(
            path="a",
            id="a",
            kind="prompt",
            status=StepStatus.SUCCEEDED,
            usage=Usage(input=100, output=10),
            cost_usd=0.5,
            cost_source="none",
        )
    )
    assert common.run_cost_source(run) == "provider"
    run.steps["a"].cost_source = ""  # an older record that never wrote the field
    assert common.run_cost_source(run) == "provider"
    run.steps["a"].cost_usd = 0.0  # a free step is still a reported cost
    assert common.run_cost_source(run) == "provider"


def test_an_unpriced_step_beside_it_makes_the_run_partial() -> None:
    run = _priced(
        a=StepRecord(
            path="a",
            id="a",
            kind="prompt",
            status=StepStatus.SUCCEEDED,
            usage=Usage(input=100, output=10),
            cost_usd=0.5,
            cost_source="none",
        ),
        b=StepRecord(
            path="b",
            id="b",
            kind="prompt",
            status=StepStatus.SUCCEEDED,
            usage=Usage(input=50_000, output=200),
            cost_usd=None,
            cost_source="none",
        ),
    )
    assert common.run_cost_source(run) == "partial"


# -- warnings block ---------------------------------------------------------------------


def test_show_collects_warnings_from_events_and_streams(cli: CliRunner, seeded: Seeded) -> None:
    seeded.store.append_event(
        SUCCEEDED_ID,
        RunEvent(
            type=EventType.WARNING,
            run_id=SUCCEEDED_ID,
            step_path="assess",
            data={"message": f"budget nearly exhausted{ESC}[2J"},
        ),
    )
    seeded.store.append_stream(
        SUCCEEDED_ID,
        "assess",
        StreamRecord(kind="warning", text="rate limit allowed_warning (seven_day) utilization 98%"),
    )
    shown = cli.invoke(app, ["show", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert shown.exit_code == 0, shown.output
    out = shown.output
    assert "warnings:" in out
    assert "assess: rate limit allowed_warning (seven_day) utilization 98%" in out
    assert "assess: budget nearly exhausted" in out and ESC not in out
    data = json.loads(
        cli.invoke(app, ["show", SUCCEEDED_ID, "--json", "--root", str(seeded.project)]).output
    )
    assert data["warnings"] == [
        "assess: budget nearly exhausted",
        "assess: rate limit allowed_warning (seven_day) utilization 98%",
    ]
    # a run without warnings prints no block
    assert (
        "warnings:"
        not in cli.invoke(app, ["show", FAILED_ID, "--root", str(seeded.project)]).output
    )


def test_show_scans_big_streams_cheaply_and_survives_bad_ones(
    cli: CliRunner, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``show`` must not parse every record of every stream (MBs per step) to find
    the warnings, and a stream that cannot be read must not abort the header view."""
    from rayspec.cli.commands.show import collect_warnings

    big = "x" * 200
    for _ in range(2000):
        seeded.store.append_stream(SUCCEEDED_ID, "fetch", StreamRecord(kind="text", text=big))
    seeded.store.append_stream(
        SUCCEEDED_ID, "fetch", StreamRecord(kind="text", text="the word warning in a delta")
    )
    seeded.store.append_stream(SUCCEEDED_ID, "fetch", StreamRecord(kind="warning", text="real"))
    parsed: list[str] = []
    real_from_json = StreamRecord.from_json.__func__  # type: ignore[attr-defined]

    def counting(cls: type[StreamRecord], line: str) -> StreamRecord:
        parsed.append(line)
        return real_from_json(cls, line)

    monkeypatch.setattr(StreamRecord, "from_json", classmethod(counting))
    run = seeded.store.load(SUCCEEDED_ID)
    assert collect_warnings(seeded.store, run) == ["fetch: real"]
    assert len(parsed) <= 2, len(parsed)  # only the lines that can be warnings were parsed

    def broken(run_id: str, path: str, **kw: object) -> object:
        raise RuntimeError("torn stream")

    monkeypatch.setattr(seeded.store, "read_stream", broken)
    assert collect_warnings(seeded.store, run) == []
    shown = cli.invoke(app, ["show", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert shown.exit_code == 0, shown.output


# -- escapes -----------------------------------------------------------------------------


def test_show_and_runs_neutralise_escapes_in_run_data(cli: CliRunner, seeded: Seeded) -> None:
    run = seeded.store.load(FAILED_ID)
    run.steps["a"].output_ref = seeded.store.write_output(FAILED_ID, "a", NASTY, kind="text")
    assert run.steps["b"].error is not None
    run.steps["b"].error.message = NASTY
    run.steps["c"].skip_reason = NASTY
    run.outputs = {"note": NASTY, "nested": {"k": NASTY}}
    run.inputs = {"t": NASTY}
    run.reason = NASTY
    run.workflow_name = f"deploy{ESC}[2J"
    run.workspace.branch = NASTY
    seeded.store.save(run)
    for argv in (["show", FAILED_ID], ["runs"]):
        result = cli.invoke(app, [*argv, "--root", str(seeded.project)])
        assert result.exit_code == 0, result.output
        assert ESC not in result.output and "\x07" not in result.output, argv
        assert "[bold red]MARKUP[/]" in result.output or argv == ["runs"], argv
    shown = cli.invoke(app, ["show", FAILED_ID, "--root", str(seeded.project)]).output
    assert shown.count(CLEAN) >= 5, shown  # reason, output preview, error, skip reason, outputs


# -- runs outside a project --------------------------------------------------------------


def test_runs_outside_a_project_hints_at_all(
    cli: CliRunner, seeded: Seeded, tmp_path: Path
) -> None:
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    result = cli.invoke(app, ["runs", "--root", str(nowhere)])
    assert result.exit_code == 0, result.output
    assert "not inside a rayspec project" in result.output
    assert "rayspec runs --all" in result.output
    assert "no runs for project" not in result.output
    # no slug is minted for the non-project directory
    assert not any(
        p.name.startswith("nowhere") for p in (seeded.home / "projects" / "local").iterdir()
    )
    everything = cli.invoke(app, ["runs", "--all", "--root", str(nowhere)])
    assert everything.exit_code == 0 and SUCCEEDED_ID in everything.output
    as_json = cli.invoke(app, ["runs", "--json", "--root", str(nowhere)])
    assert as_json.exit_code == 0 and json.loads(as_json.stdout) == []
    assert "not inside a rayspec project" in as_json.stderr


def test_runs_inside_a_git_repo_without_rayspec_dir_is_a_project(
    cli: CliRunner, seeded: Seeded, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    result = cli.invoke(app, ["runs", "--root", str(repo)])
    assert result.exit_code == 0, result.output
    assert "not inside a rayspec project" not in result.output
    # the empty listing names the project minted for the repo itself — a different slug from
    # any directory above it, which is what "the git repo is the project" means here
    assert f"no runs for project {project_slug_for(repo)}" in result.output, result.output

"""`rayspec costs [--since] [--workflow] [--json]` — cost roll-up across a project's runs.

The store is seeded here rather than in ``conftest.py`` because these tests need a spread of
cost sources (provider, table, partial, none) and creation dates that the shared fixture does
not have — and because the single most important property of this command is arithmetic:
what it prints must reconcile with what ``rayspec runs`` prints for the same runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.providers.base import Usage
from rayspec.schema import RunStatus, StepStatus
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord, StepRecord

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)

#: created_at of every seeded run (ids are time-sortable, so they mirror it).
FIXIT_NEW = datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC)
FIXIT_OLD = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC)
DEPLOY_NEW = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
DEPLOY_OLD = datetime(2026, 8, 5, 10, 0, 0, tzinfo=UTC)
AUDIT = datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC)

OTHER_SLUG = "local/other-deadbeef"


@dataclass
class Ledger:
    """The seeded home: one project store with five runs across three workflows."""

    home: Path
    project: Path
    slug: str
    store: FileRunStore


def _run_id(created: datetime, suffix: str) -> str:
    return f"{created.strftime('%Y%m%d-%H%M%S')}-{suffix}"


def _step(
    path: str,
    *,
    usage: Usage,
    cost_usd: float | None,
    cost_source: str,
    usage_unknown: bool = False,
) -> StepRecord:
    return StepRecord(
        path=path,
        id=path,
        kind="prompt",
        status=StepStatus.SUCCEEDED,
        attempts=1,
        ok=True,
        usage=usage,
        cost_usd=cost_usd,
        cost_source=cost_source,
        usage_unknown=usage_unknown,
    )


def _seed(
    store: FileRunStore,
    slug: str,
    root: Path,
    *,
    workflow: str,
    created: datetime,
    suffix: str,
    steps: list[StepRecord],
    status: RunStatus = RunStatus.SUCCEEDED,
) -> RunRecord:
    run = RunRecord(
        run_id=_run_id(created, suffix),
        workflow_name=workflow,
        workflow_path=f".rayspec/workflows/{workflow}.yaml",
        workflow_hash="a" * 64,
        project_slug=slug,
        project_root=str(root),
        status=status,
        created_at=created,
        started_at=created,
        ended_at=created + timedelta(seconds=10) if status is RunStatus.SUCCEEDED else None,
    )
    store.create(run)
    for rec in steps:
        run.steps[rec.path] = rec
    store.save(run)
    return run


@pytest.fixture
def ledger(home: Path, project: Path) -> Ledger:
    slug = project_slug_for(project)
    store = FileRunStore(home / "projects" / slug)
    # fixit: both runs priced — one table estimate, one provider-reported
    _seed(
        store,
        slug,
        project,
        workflow="fixit",
        created=FIXIT_NEW,
        suffix="aaaa",
        steps=[
            _step(
                "assess",
                usage=Usage(input=1200, output=300),
                cost_usd=0.0456,
                cost_source="provider",
            ),
            _step(
                "implement",
                usage=Usage(input=5000, output=2000),
                cost_usd=0.21,
                cost_source="table",
            ),
        ],
    )
    _seed(
        store,
        slug,
        project,
        workflow="fixit",
        created=FIXIT_OLD,
        suffix="bbbb",
        steps=[
            _step(
                "assess", usage=Usage(input=1000, output=500), cost_usd=0.10, cost_source="provider"
            )
        ],
    )
    # deploy: one partial run (a priced step next to an unpriced one) and one with no cost
    _seed(
        store,
        slug,
        project,
        workflow="deploy",
        created=DEPLOY_NEW,
        suffix="cccc",
        steps=[
            _step(
                "plan", usage=Usage(input=400, output=100), cost_usd=0.02, cost_source="provider"
            ),
            _step("apply", usage=Usage(input=900, output=100), cost_usd=None, cost_source="none"),
        ],
    )
    _seed(
        store,
        slug,
        project,
        workflow="deploy",
        created=DEPLOY_OLD,
        suffix="dddd",
        steps=[],
    )
    # audit: a single run whose cost is entirely unknown (tokens, no price anywhere)
    _seed(
        store,
        slug,
        project,
        workflow="audit",
        created=AUDIT,
        suffix="eeee",
        steps=[
            _step("check", usage=Usage(input=600, output=100), cost_usd=None, cost_source="none")
        ],
    )
    other = FileRunStore(home / "projects" / OTHER_SLUG)
    _seed(
        other,
        OTHER_SLUG,
        project / "other",
        workflow="fixit",
        created=FIXIT_NEW,
        suffix="ffff",
        steps=[
            _step(
                "assess",
                usage=Usage(input=99_000, output=1000),
                cost_usd=99.0,
                cost_source="provider",
            )
        ],
    )
    return Ledger(home=home, project=project, slug=slug, store=store)


def _payload(cli: CliRunner, ledger: Ledger, *args: str) -> dict:
    result = cli.invoke(app, ["costs", "--json", "--root", str(ledger.project), *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _group(payload: dict, workflow: str) -> dict:
    matches = [g for g in payload["workflows"] if g["workflow"] == workflow]
    assert len(matches) == 1, f"{workflow} not in {payload['workflows']}"
    return matches[0]


# --------------------------------------------------------------------------------------------
# the grouped table
# --------------------------------------------------------------------------------------------


def test_costs_table_groups_by_workflow(cli: CliRunner, ledger: Ledger) -> None:
    result = cli.invoke(app, ["costs", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    fixit = next(line for line in lines if line.startswith("fixit"))
    deploy = next(line for line in lines if line.startswith("deploy"))
    audit = next(line for line in lines if line.startswith("audit"))
    total = next(line for line in lines if line.startswith("total"))
    # fixit: 0.0456 + 0.21 + 0.10, one table estimate in the mix -> "~"
    assert "~$0.36" in fixit and "8.5k tok" not in fixit  # 8500 + 1500 tokens
    assert "10.0k tok" in fixit
    # deploy: one priced + one unpriced step, and one run with no cost at all -> lower bound
    assert "≥$0.02" in deploy
    # audit: nothing is known -> the run is still counted, the cost slot is empty
    assert "-" in audit and "$" not in audit
    assert "≥$0.38" in total


def test_costs_table_counts_every_run(cli: CliRunner, ledger: Ledger) -> None:
    result = cli.invoke(app, ["costs", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    counts = {
        name: next(line for line in lines if line.startswith(name)).split()[1]
        for name in ("fixit", "deploy", "audit", "total")
    }
    assert counts == {"fixit": "2", "deploy": "2", "audit": "1", "total": "5"}


def test_costs_names_the_cost_source_breakdown(cli: CliRunner, ledger: Ledger) -> None:
    result = cli.invoke(app, ["costs", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    total = next(line for line in result.output.splitlines() if line.startswith("total"))
    assert "1 provider" in total and "1 table" in total and "1 partial" in total
    assert "2 unknown" in total
    assert "2 of 5 runs have no recorded cost" in result.output


def test_a_lower_bound_without_unknown_runs_is_still_explained(
    cli: CliRunner, ledger: Ledger
) -> None:
    """Every run is priced, but one has an unpriced step: the ≥ needs its own sentence."""
    result = cli.invoke(
        app,
        [
            "costs",
            "--workflow",
            "deploy",
            "--since",
            "2026-08-18",
            "--root",
            str(ledger.project),
        ],
    )
    assert result.exit_code == 0, result.output
    total = next(line for line in result.output.splitlines() if line.startswith("total"))
    assert "≥$0.02" in total and "unknown" not in total
    assert "steps with tokens but no price" in result.output


# --------------------------------------------------------------------------------------------
# arithmetic: the roll-up must reconcile with `rayspec runs`
# --------------------------------------------------------------------------------------------


def test_totals_reconcile_with_the_per_run_figures(cli: CliRunner, ledger: Ledger) -> None:
    runs = json.loads(cli.invoke(app, ["runs", "--json", "--root", str(ledger.project)]).stdout)
    payload = _payload(cli, ledger)
    assert payload["runs"] == len(runs)
    expected = sum(row["cost_usd"] for row in runs if row["cost_usd"] is not None)
    assert payload["cost_usd"] == pytest.approx(expected)
    assert payload["tokens"] == sum(row["tokens"] for row in runs)
    for group in payload["workflows"]:
        rows = [row for row in runs if row["workflow"] == group["workflow"]]
        assert group["runs"] == len(rows)
        costs = [row["cost_usd"] for row in rows if row["cost_usd"] is not None]
        assert group["cost_usd"] == (pytest.approx(sum(costs)) if costs else None)
        assert group["tokens"] == sum(row["tokens"] for row in rows)


def test_runs_with_unknown_cost_are_counted_never_zero(cli: CliRunner, ledger: Ledger) -> None:
    payload = _payload(cli, ledger)
    audit = _group(payload, "audit")
    assert audit["runs"] == 1 and audit["runs_unknown_cost"] == 1
    assert audit["cost_usd"] is None and audit["cost_source"] == "none"
    assert audit["tokens"] == 700
    deploy = _group(payload, "deploy")
    assert deploy["runs"] == 2 and deploy["runs_unknown_cost"] == 1
    # one run has SOME cost and one has none: the sum is a lower bound, not the truth
    assert deploy["cost_source"] == "partial"
    assert payload["runs_unknown_cost"] == 2
    assert sum(g["runs"] for g in payload["workflows"]) == payload["runs"]


def test_cost_source_breakdown_covers_every_run(cli: CliRunner, ledger: Ledger) -> None:
    payload = _payload(cli, ledger)
    assert payload["cost_sources"] == {"provider": 1, "table": 1, "partial": 1, "unknown": 2}
    for group in payload["workflows"]:
        assert sum(group["cost_sources"].values()) == group["runs"]


def test_costs_reads_only_the_current_project(cli: CliRunner, ledger: Ledger) -> None:
    payload = _payload(cli, ledger)
    assert payload["project"] == ledger.slug
    assert payload["runs"] == 5
    assert payload["cost_usd"] < 1.0  # the other project's $99 run is out of scope


# --------------------------------------------------------------------------------------------
# --since
# --------------------------------------------------------------------------------------------


def test_since_includes_a_run_exactly_at_the_cutoff(cli: CliRunner, ledger: Ledger) -> None:
    payload = _payload(cli, ledger, "--since", "2026-08-13T10:00:00Z")
    assert _group(payload, "fixit")["runs"] == 2
    assert payload["runs"] == 3  # fixit x2 + the 2026-08-18 deploy run


def test_since_excludes_a_run_one_second_before_the_cutoff(cli: CliRunner, ledger: Ledger) -> None:
    payload = _payload(cli, ledger, "--since", "2026-08-13T10:00:01Z")
    assert _group(payload, "fixit")["runs"] == 1
    assert payload["runs"] == 2
    assert payload["since"].startswith("2026-08-13T10:00:01")


def test_since_accepts_a_bare_date(cli: CliRunner, ledger: Ledger) -> None:
    payload = _payload(cli, ledger, "--since", "2026-08-18")
    assert payload["runs"] == 2  # 2026-08-18 10:00 and 2026-08-20 10:00
    assert payload["since"].startswith("2026-08-18T00:00:00")


def test_since_relative_windows(cli: CliRunner, ledger: Ledger) -> None:
    from rayspec.cli.commands.costs import parse_since

    assert parse_since("7d", now=NOW) == NOW - timedelta(days=7)
    assert parse_since("24h", now=NOW) == NOW - timedelta(hours=24)
    assert parse_since("90m", now=NOW) == NOW - timedelta(minutes=90)
    assert parse_since("45s", now=NOW) == NOW - timedelta(seconds=45)
    assert parse_since("2w", now=NOW) == NOW - timedelta(weeks=2)
    assert parse_since(" 7d ", now=NOW) == NOW - timedelta(days=7)
    assert parse_since("0d", now=NOW) == NOW


def test_since_absolute_forms_are_utc(cli: CliRunner, ledger: Ledger) -> None:
    from rayspec.cli.commands.costs import parse_since

    assert parse_since("2026-08-01", now=NOW) == datetime(2026, 8, 1, tzinfo=UTC)
    assert parse_since("2026-08-01T06:30:00", now=NOW) == datetime(2026, 8, 1, 6, 30, tzinfo=UTC)
    assert parse_since("2026-08-01T06:30:00+02:00", now=NOW) == datetime(
        2026, 8, 1, 4, 30, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "text", ["", "yesterday", "-7d", "7 days", "7y", "2026-13-01", "d", "99999999999w"]
)
def test_since_rejects_what_it_cannot_parse(text: str) -> None:
    from rayspec.cli.commands.costs import parse_since

    with pytest.raises(ValueError):
        parse_since(text, now=NOW)


def test_bad_since_is_a_usage_error(cli: CliRunner, ledger: Ledger) -> None:
    result = cli.invoke(app, ["costs", "--since", "yesterday", "--root", str(ledger.project)])
    assert result.exit_code == 2
    assert "yesterday" in result.output
    assert "7d" in result.output  # the hint names the forms that work
    assert "isoformat" not in result.output  # not a date attempt: the parser detail is noise


def test_a_near_miss_date_keeps_the_parser_s_reason(cli: CliRunner, ledger: Ledger) -> None:
    result = cli.invoke(app, ["costs", "--since", "2026-13-01", "--root", str(ledger.project)])
    assert result.exit_code == 2
    assert "month must be in 1..12" in result.output


# --------------------------------------------------------------------------------------------
# --workflow
# --------------------------------------------------------------------------------------------


def test_workflow_filter(cli: CliRunner, ledger: Ledger) -> None:
    payload = _payload(cli, ledger, "--workflow", "deploy")
    assert [g["workflow"] for g in payload["workflows"]] == ["deploy"]
    assert payload["runs"] == 2 and payload["workflow"] == "deploy"


def test_workflow_filter_without_matches_is_not_an_error(cli: CliRunner, ledger: Ledger) -> None:
    result = cli.invoke(app, ["costs", "--workflow", "nope", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    assert "no runs" in result.output and "nope" in result.output
    payload = _payload(cli, ledger, "--workflow", "nope")
    assert payload["runs"] == 0 and payload["workflows"] == []
    assert payload["cost_usd"] is None and payload["cost_source"] == "none"


# --------------------------------------------------------------------------------------------
# --json shape, empty stores, safety
# --------------------------------------------------------------------------------------------


def test_json_shape_is_stable(cli: CliRunner, ledger: Ledger) -> None:
    payload = _payload(cli, ledger)
    assert set(payload) == {
        "project",
        "since",
        "workflow",
        "runs",
        "runs_unknown_cost",
        "runs_partial_cost",
        "runs_usage_unknown",
        "runs_in_flight",
        "runs_unreadable",
        "runs_unreadable_ids",
        "tokens",
        "usage",
        "cost_usd",
        "cost_source",
        "cost_sources",
        "first_run_at",
        "last_run_at",
        "workflows",
    }
    assert payload["since"] is None and payload["workflow"] is None
    # `since` is what was asked for, `first_run_at` what the store actually holds
    assert payload["first_run_at"].startswith("2026-08-01T09:00:00")
    assert payload["last_run_at"].startswith("2026-08-20T10:00:00")
    assert set(payload["usage"]) == {"input", "cached_input", "cache_write", "output", "reasoning"}
    assert set(payload["workflows"][0]) == {
        "workflow",
        "runs",
        "runs_unknown_cost",
        "runs_partial_cost",
        "runs_usage_unknown",
        "runs_in_flight",
        "tokens",
        "usage",
        "cost_usd",
        "cost_source",
        "cost_sources",
        "first_run_at",
        "last_run_at",
    }
    fixit = _group(payload, "fixit")
    assert fixit["first_run_at"].startswith("2026-08-13T10:00:00")
    assert fixit["last_run_at"].startswith("2026-08-20T10:00:00")
    # most expensive first, so the answer to "what has this cost me" is the top row
    assert [g["workflow"] for g in payload["workflows"]] == ["fixit", "deploy", "audit"]


def test_empty_project_is_exit_zero_with_a_hint(cli: CliRunner, home: Path, project: Path) -> None:
    result = cli.invoke(app, ["costs", "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert "no runs" in result.output
    json_result = cli.invoke(app, ["costs", "--json", "--root", str(project)])
    payload = json.loads(json_result.stdout)
    assert payload["runs"] == 0 and payload["workflows"] == []
    assert payload["cost_usd"] is None and payload["tokens"] == 0


def test_outside_a_project_nothing_is_listed(cli: CliRunner, home: Path, tmp_path: Path) -> None:
    stray = tmp_path / "stray"
    stray.mkdir()
    result = cli.invoke(app, ["costs", "--root", str(stray)])
    assert result.exit_code == 0, result.output
    assert "not inside a rayspec project" in result.output


def test_workflow_names_are_never_rich_markup(cli: CliRunner, ledger: Ledger) -> None:
    run = ledger.store.load(_run_id(AUDIT, "eeee"))
    run.workflow_name = "audit[/bold]"
    ledger.store.save(run)
    result = cli.invoke(app, ["costs", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    assert "audit[/bold]" in result.output


def _tree_hash(root: Path) -> str:
    """A digest over every path, mtime and byte under ``root`` — any write changes it."""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            digest.update(str(path.relative_to(root)).encode())
            digest.update(str(path.stat().st_mtime_ns).encode())
            digest.update(path.read_bytes())
        for name in sorted(dirnames):
            digest.update(str((Path(dirpath) / name).relative_to(root)).encode())
    return digest.hexdigest()


def test_costs_writes_nothing(cli: CliRunner, ledger: Ledger) -> None:
    before = _tree_hash(ledger.home)
    for args in ([], ["--json"], ["--since", "7d"], ["--workflow", "fixit"]):
        assert cli.invoke(app, ["costs", "--root", str(ledger.project), *args]).exit_code == 0
    assert _tree_hash(ledger.home) == before


def test_costs_does_not_mint_a_store_for_a_project_without_runs(
    cli: CliRunner, home: Path, project: Path
) -> None:
    assert cli.invoke(app, ["costs", "--root", str(project)]).exit_code == 0
    assert not (home / "projects").exists()


# --------------------------------------------------------------------------------------------
# figures that are not final: a cut-off step, an unreadable record, a run still in flight
# --------------------------------------------------------------------------------------------


def _seed_cut_off(ledger: Ledger, *, created: datetime, suffix: str) -> None:
    """A run whose second step was interrupted before the provider reported any usage."""
    _seed(
        ledger.store,
        ledger.slug,
        ledger.project,
        workflow="cutoff",
        created=created,
        suffix=suffix,
        steps=[
            _step(
                "draft",
                usage=Usage(input=1000, output=100),
                cost_usd=1.00,
                cost_source="provider",
            ),
            _step(
                "review",
                usage=Usage(),
                cost_usd=None,
                cost_source="none",
                usage_unknown=True,
            ),
        ],
    )


def test_a_cut_off_step_makes_the_row_a_lower_bound(cli: CliRunner, ledger: Ledger) -> None:
    """`rayspec run` prints `tokens: ≥1100 (usage of 1 step unknown)`; a roll-up may not
    quietly present the same run as complete."""
    _seed_cut_off(ledger, created=datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC), suffix="cut1")
    result = cli.invoke(app, ["costs", "--workflow", "cutoff", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("cutoff"))
    assert "≥$1.00" in row  # the cost is a lower bound, not $1.00 exactly
    assert "≥1.1k tok" in row  # and so is the token count
    assert "cut off before it reported usage" in result.output


def test_a_cut_off_step_is_visible_in_json(cli: CliRunner, ledger: Ledger) -> None:
    _seed_cut_off(ledger, created=datetime(2026, 8, 19, 10, 0, 0, tzinfo=UTC), suffix="cut1")
    payload = _payload(cli, ledger, "--workflow", "cutoff")
    group = _group(payload, "cutoff")
    assert group["runs_usage_unknown"] == 1
    assert group["cost_source"] == "partial"
    assert payload["runs_usage_unknown"] == 1 and payload["cost_source"] == "partial"


def test_a_cut_off_step_with_no_usage_at_all_is_not_reported_as_zero(
    cli: CliRunner, ledger: Ledger
) -> None:
    _seed(
        ledger.store,
        ledger.slug,
        ledger.project,
        workflow="cutoff",
        created=datetime(2026, 8, 19, 11, 0, 0, tzinfo=UTC),
        suffix="cut2",
        steps=[
            _step("review", usage=Usage(), cost_usd=None, cost_source="none", usage_unknown=True)
        ],
    )
    result = cli.invoke(app, ["costs", "--workflow", "cutoff", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("cutoff"))
    # columns: workflow · runs · tokens · cost · cost source
    assert row.split()[2] == "unknown"  # not "-", which would read as "zero tokens"


def _corrupt(ledger: Ledger, run_id: str) -> None:
    (ledger.store.run_dir(run_id) / "run.json").write_text("{not json", encoding="utf-8")


def test_an_unreadable_record_is_named_not_silently_dropped(cli: CliRunner, ledger: Ledger) -> None:
    corrupt = _run_id(DEPLOY_NEW, "cccc")
    _corrupt(ledger, corrupt)
    result = cli.invoke(app, ["costs", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    total = next(line for line in result.output.splitlines() if line.startswith("total"))
    assert total.split()[1] == "4"  # the four runs that could be read
    assert "≥" in total  # the sum can no longer claim to be complete
    assert "1 run record could not be read" in result.output
    # the id, not `rayspec runs`: that listing skips the record too, so it can show nothing
    assert corrupt in result.output


def test_an_unreadable_record_is_visible_in_json(cli: CliRunner, ledger: Ledger) -> None:
    _corrupt(ledger, _run_id(DEPLOY_NEW, "cccc"))
    payload = _payload(cli, ledger)
    assert payload["runs_unreadable"] == 1
    assert payload["runs"] == 4
    assert payload["cost_source"] == "partial"


def test_a_readable_store_reports_no_unreadable_records(cli: CliRunner, ledger: Ledger) -> None:
    assert _payload(cli, ledger)["runs_unreadable"] == 0
    assert _payload(cli, ledger)["runs_unreadable_ids"] == []


def _lose_the_record(ledger: Ledger, run_id: str) -> None:
    """Delete a run's ``run.json`` and leave the run directory where it was."""
    (ledger.store.run_dir(run_id) / "run.json").unlink()


def test_a_missing_record_is_counted_like_an_unparseable_one(
    cli: CliRunner, ledger: Ledger
) -> None:
    """The store's own listing only sees runs that HAVE a ``run.json``, so a missing one used to
    take its run out of the totals without leaving a trace: 5 runs became 4 with nothing said."""
    lost = _run_id(DEPLOY_NEW, "cccc")
    _lose_the_record(ledger, lost)
    result = cli.invoke(app, ["costs", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    total = next(line for line in result.output.splitlines() if line.startswith("total"))
    assert total.split()[1] == "4"  # the four runs that could be read
    assert "≥" in total  # the sum can no longer claim to be complete
    assert "1 run record could not be read" in result.output
    assert lost in result.output  # named, so it can be looked at


def test_a_missing_record_is_visible_in_json(cli: CliRunner, ledger: Ledger) -> None:
    lost = _run_id(DEPLOY_NEW, "cccc")
    _lose_the_record(ledger, lost)
    payload = _payload(cli, ledger)
    assert payload["runs_unreadable"] == 1
    assert payload["runs_unreadable_ids"] == [lost]
    assert payload["runs"] == 4
    assert payload["cost_source"] == "partial"


def test_an_unreadable_record_outside_the_window_is_not_reported(
    cli: CliRunner, ledger: Ledger
) -> None:
    """The window is knowable without the record: a run id carries its own UTC timestamp."""
    _corrupt(ledger, _run_id(AUDIT, "eeee"))
    payload = _payload(cli, ledger, "--since", "2026-08-10")
    assert payload["runs_unreadable"] == 0 and payload["runs_unreadable_ids"] == []
    payload = _payload(cli, ledger, "--since", "2030-01-01")
    assert payload["runs_unreadable"] == 0


def test_an_unreadable_record_inside_the_window_is_reported(cli: CliRunner, ledger: Ledger) -> None:
    lost = _run_id(AUDIT, "eeee")
    _lose_the_record(ledger, lost)
    payload = _payload(cli, ledger, "--since", "2026-07-01")
    assert payload["runs_unreadable_ids"] == [lost]


def test_the_unreadable_notice_points_at_something_that_is_still_there(
    cli: CliRunner, ledger: Ledger
) -> None:
    """The notice used to end `(rayspec show <id>)`, and `rayspec show` cannot show that run
    either: it exits 2 and sends the reader on to `rayspec runs`, which does not list it either
    — this command's own docstring rejects `rayspec runs` for exactly that reason. What is left
    of a run whose record is gone is its directory."""
    lost = _run_id(DEPLOY_NEW, "cccc")
    _lose_the_record(ledger, lost)
    result = cli.invoke(app, ["costs", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    assert "rayspec show" not in result.output
    # rich folds a long path over several lines; the notice names one that is really there
    flat = "".join(result.output.split())
    assert str(ledger.store.run_dir(lost)) in flat
    assert ledger.store.run_dir(lost).is_dir()
    shown = cli.invoke(app, ["show", lost, "--root", str(ledger.project)])
    assert shown.exit_code == 2  # what the notice used to send the reader to


def test_the_unreadable_notice_names_the_run_directory_of_a_single_run(tmp_path: Path) -> None:
    from rayspec.cli.commands.costs import unreadable_notice

    runs = tmp_path / "runs"
    line = unreadable_notice(["20260101-000000-aaaa"], runs=runs)
    assert line is not None and str(runs / "20260101-000000-aaaa") in line
    several = unreadable_notice(["20260101-000000-aaaa", "20260101-000000-bbbb"], runs=runs)
    assert several is not None and str(runs) in several


def _staging_file(store: FileRunStore, run_id: str) -> Path:
    """The skeleton `FileRunStore.save()` leaves behind while it is writing `run.json`."""
    run_dir = store.run_dir(run_id)
    (run_dir / "steps").mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    (run_dir / "tmp").mkdir()
    staging = run_dir / f"run.json.{os.getpid()}.0.tmp"
    staging.write_text('{"run_id": "half', encoding="utf-8")
    return staging


def test_a_record_that_is_being_written_is_not_reported_as_lost(
    cli: CliRunner, ledger: Ledger
) -> None:
    """`save()` creates the run directory, then writes `run.json` into it — a dump, a redaction
    pass and an fsync later. A `costs` running in another terminal during that window would
    otherwise name a healthy run that started a moment ago, and mark the total `≥` for it."""
    _staging_file(ledger.store, _run_id(NOW, "ffff"))
    payload = _payload(cli, ledger)
    assert payload["runs_unreadable"] == 0 and payload["runs_unreadable_ids"] == []


def test_a_staging_file_left_behind_does_not_hide_a_lost_record(
    cli: CliRunner, ledger: Ledger
) -> None:
    """The other side of it: a process killed mid-write leaves that file for good, and a run
    directory silenced for good is the failure this notice exists to prevent."""
    lost = _run_id(NOW, "ffff")
    staging = _staging_file(ledger.store, lost)
    stale = datetime.now(tz=UTC).timestamp() - 3600
    os.utime(staging, (stale, stale))
    payload = _payload(cli, ledger)
    assert payload["runs_unreadable_ids"] == [lost]


def test_an_unreadable_record_is_reported_even_with_no_runs_left_in_scope(
    cli: CliRunner, ledger: Ledger
) -> None:
    """An empty scope is the one place a lost record is the whole answer."""
    lost = _run_id(AUDIT, "eeee")
    _lose_the_record(ledger, lost)
    result = cli.invoke(app, ["costs", "--workflow", "nope", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    assert "no runs" in result.output and lost in result.output


def test_running_and_paused_runs_are_flagged_as_not_final(cli: CliRunner, ledger: Ledger) -> None:
    _seed(
        ledger.store,
        ledger.slug,
        ledger.project,
        workflow="live",
        created=datetime(2026, 8, 20, 11, 0, 0, tzinfo=UTC),
        suffix="live",
        status=RunStatus.RUNNING,
        steps=[
            _step("plan", usage=Usage(input=100, output=10), cost_usd=0.05, cost_source="provider")
        ],
    )
    result = cli.invoke(app, ["costs", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    assert "still running or paused" in result.output
    payload = _payload(cli, ledger)
    assert payload["runs_in_flight"] == 1
    assert _group(payload, "live")["runs_in_flight"] == 1


def test_finished_runs_are_not_flagged_as_in_flight(cli: CliRunner, ledger: Ledger) -> None:
    payload = _payload(cli, ledger)
    assert payload["runs_in_flight"] == 0
    result = cli.invoke(app, ["costs", "--root", str(ledger.project)])
    assert "still running or paused" not in result.output


# --------------------------------------------------------------------------------------------
# ordering and wording details
# --------------------------------------------------------------------------------------------


def test_an_unpriced_group_sorts_after_a_zero_cost_group(cli: CliRunner, ledger: Ledger) -> None:
    """A $0.00 group is a known figure; an unpriced one is not, so it sorts last."""
    _seed(
        ledger.store,
        ledger.slug,
        ledger.project,
        workflow="zzz",
        created=datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC),
        suffix="zzz1",
        steps=[_step("free", usage=Usage(input=10), cost_usd=0.0, cost_source="provider")],
    )
    _seed(
        ledger.store,
        ledger.slug,
        ledger.project,
        workflow="aaa",
        created=datetime(2026, 8, 19, 13, 0, 0, tzinfo=UTC),
        suffix="aaa1",
        steps=[_step("free", usage=Usage(input=10), cost_usd=None, cost_source="none")],
    )
    payload = _payload(cli, ledger)
    order = [g["workflow"] for g in payload["workflows"]]
    assert order.index("zzz") < order.index("aaa")


def test_the_notice_does_not_cite_a_marker_that_is_not_on_screen(
    cli: CliRunner, ledger: Ledger
) -> None:
    """Nothing in scope is priced, so the cost column is empty everywhere — there is no ≥."""
    result = cli.invoke(app, ["costs", "--workflow", "audit", "--root", str(ledger.project)])
    assert result.exit_code == 0, result.output
    assert "no cost is known for any run in scope" in result.output
    assert "≥" not in result.output


def test_outside_a_project_json_names_no_project(
    cli: CliRunner, home: Path, tmp_path: Path
) -> None:
    stray = tmp_path / "stray"
    stray.mkdir()
    result = cli.invoke(app, ["costs", "--json", "--root", str(stray)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["project"] is None  # no slug is claimed for a directory that is not a project
    assert payload["runs"] == 0

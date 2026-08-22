"""Shared helpers: store factory, run lookup (prefix + ambiguity), formatting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rayspec.cli import _runs_common as common
from rayspec.providers.base import Usage
from rayspec.store.file import AmbiguousRunIdError, UnknownRunIdError

from .conftest import FAILED_ID, OTHER_ID, PAUSED_ID, SUCCEEDED_ID, Seeded


def test_fmt_duration_and_tokens_and_cost() -> None:
    assert common.fmt_duration(None) == "-"
    assert common.fmt_duration(850) == "850ms"
    assert common.fmt_duration(12_300) == "12.3s"
    assert common.fmt_duration(95_000) == "1m35s"
    assert common.fmt_tokens(0) == "0 tok"
    assert common.fmt_tokens(12_345) == "12.3k tok"
    assert common.fmt_cost(None, "none", Usage()) == "-"
    assert common.fmt_cost(None, "none", Usage(input=100, output=50)) == "-"  # tokens ≠ cost
    assert common.fmt_cost(0.0456, "provider", Usage()) == "$0.05"
    assert common.fmt_cost(0.21, "table", Usage()) == "~$0.21"


def test_fmt_when_relative_and_absolute() -> None:
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
    assert common.fmt_when(None, now=now) == "-"
    assert common.fmt_when(now - timedelta(seconds=30), now=now) == "30s ago"
    assert common.fmt_when(now - timedelta(minutes=5), now=now) == "5m ago"
    assert common.fmt_when(now - timedelta(hours=3), now=now) == "3h ago"
    assert common.fmt_when(now - timedelta(days=2), now=now) == "2d ago"
    assert common.fmt_when(now - timedelta(days=40), now=now) == "2026-07-11 12:00"


def test_run_duration_and_progress(seeded: Seeded) -> None:
    run = seeded.store.load(SUCCEEDED_ID)
    assert common.run_duration_ms(run) == 95_000
    assert common.steps_progress(run) == (5, 5)
    failed = seeded.store.load(FAILED_ID)
    assert common.steps_progress(failed) == (2, 3)  # skipped counts as done, failed not
    paused = seeded.store.load(PAUSED_ID)
    assert common.steps_progress(paused) == (1, 2)


def test_make_runs_context_uses_home_and_slug(seeded: Seeded) -> None:
    ctx = common.make_runs_context(seeded.project)
    assert ctx.home == seeded.home
    assert ctx.slug == seeded.slug
    assert ctx.store.root == seeded.store.root
    assert ctx.store.list_run_ids()[0] == PAUSED_ID


def test_iter_project_stores_finds_every_project(seeded: Seeded) -> None:
    stores = dict(common.iter_project_stores(seeded.home))
    assert set(stores) == {seeded.slug, "local/other-deadbeef"}


def test_find_run_exact_prefix_and_errors(seeded: Seeded) -> None:
    ctx = common.make_runs_context(seeded.project)
    store, run = common.find_run(ctx, SUCCEEDED_ID)
    assert run.run_id == SUCCEEDED_ID and store.root == seeded.store.root
    _, run = common.find_run(ctx, "20260820-12")
    assert run.run_id == PAUSED_ID
    with pytest.raises(AmbiguousRunIdError) as ambiguous:
        common.find_run(ctx, "20260820-1")
    assert ambiguous.value.candidates == [PAUSED_ID, FAILED_ID, SUCCEEDED_ID]
    with pytest.raises(UnknownRunIdError):
        common.find_run(ctx, "nope")


def test_find_run_falls_back_to_other_projects(seeded: Seeded) -> None:
    ctx = common.make_runs_context(seeded.project)
    store, run = common.find_run(ctx, OTHER_ID[:10])
    assert run.run_id == OTHER_ID
    assert store.root == seeded.other_store.root
    # a prefix that is unique per project but matches in two projects is ambiguous
    with pytest.raises(AmbiguousRunIdError):
        common.find_run(ctx, "2026")


def test_output_preview_reads_first_line(seeded: Seeded) -> None:
    run = seeded.store.load(SUCCEEDED_ID)
    assert common.output_preview(seeded.store, run, run.steps["build[1]/implement"]) == (
        "patched the thing …"
    )
    assert common.output_preview(seeded.store, run, run.steps["fetch"]) == "issue 7"
    assert common.output_preview(seeded.store, run, run.steps["assess"]) == (
        '{"verdict": "fix", "reason": "real bug"}'
    )
    assert common.output_preview(seeded.store, run, run.steps["build"], limit=20) == (
        '{"implement": "patc …'
    )
    run.steps["fetch"].output_ref = "steps/fetch/missing.txt"
    assert common.output_preview(seeded.store, run, run.steps["fetch"]) == ""
    assert common.output_preview(seeded.store, run, run.steps["fetch"], limit=3) == ""


def test_load_resolved_for_record(seeded: Seeded, project: Path) -> None:
    (project / ".rayspec" / "workflows" / "gate.yaml").write_text(
        "rayspec: 1\nname: gate\nsteps:\n  - {id: a, shell: echo a}\n", encoding="utf-8"
    )
    ctx = common.make_runs_context(seeded.project)
    run = seeded.store.load(PAUSED_ID)
    resolved = common.load_resolved_for(ctx, run)
    assert resolved.workflow.name == "gate"
    run.workflow_path = "somewhere/else.yaml"  # falls back to the name
    assert common.load_resolved_for(ctx, run).workflow.name == "gate"


def test_iter_project_stores_ignores_worktree_checkouts_and_bare_sources(seeded: Seeded) -> None:
    projects = seeded.home / "projects"
    # a `runs` directory inside a worktree checkout (e.g. the project's own src/runs/) is not a
    # project store, nor is anything under a bare `source.git`
    garbage = projects / seeded.slug / "worktrees" / "fixit-aaaa" / "src" / "runs" / "x"
    garbage.mkdir(parents=True)
    (garbage / "run.json").write_text("{}", encoding="utf-8")
    bare = projects / "github.com" / "owner" / "repo" / "source.git" / "runs"
    bare.mkdir(parents=True)
    # a three-component (host/owner/repo) slug is a project store
    (projects / "github.com" / "owner" / "repo" / "runs").mkdir(parents=True)
    stores = dict(common.iter_project_stores(seeded.home))
    assert set(stores) == {seeded.slug, "local/other-deadbeef", "github.com/owner/repo"}
    ctx = common.make_runs_context(seeded.project)
    with pytest.raises(UnknownRunIdError):
        common.find_run(ctx, "x")


def test_iter_project_stores_finds_deep_slugs(seeded: Seeded) -> None:
    """A GitLab subgroup remote yields ``gitlab.com/group/sub/repo`` — still a project store."""
    projects = seeded.home / "projects"
    deep = projects / "gitlab.com" / "group" / "sub" / "repo"
    (deep / "runs").mkdir(parents=True)
    (deep / "worktrees" / "wf-aaaa" / "runs").mkdir(parents=True)  # a checkout, not a store
    (deep / "locks").mkdir()
    stores = dict(common.iter_project_stores(seeded.home))
    assert "gitlab.com/group/sub/repo" in stores
    assert not any(s.startswith("gitlab.com/group/sub/repo/") for s in stores)

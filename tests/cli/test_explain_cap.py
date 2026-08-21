# SPDX-License-Identifier: Apache-2.0
"""`rayspec explain` on a step the run-level circuit breaker skipped.

`skip_reason: budget_exceeded` names the breaker, not the cap: cost, tokens and the wall clock
are one breaker sharing one skip reason. `explain` is the command that answers "why did this step
do that", so it has to name the cap that actually fired rather than let the reader read
"budget" and go looking for money that was never the problem.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.store.file import FileRunStore

CLOCK_WF = """
rayspec: 1
name: t
isolation: none
defaults:
  timeout_total: 0.4
agents:
  slow: {provider: stub}
steps:
  - id: first
    agent: slow
    prompt: "one"
  - id: second
    needs: [first]
    shell: "printf never"
"""

STUBS = """
steps:
  first: {latency_ms: 900}
"""


@pytest.fixture
def capped(cli: CliRunner, home: Path, project: Path) -> tuple[str, Path, FileRunStore]:
    """A run whose wall-clock cap ran out before ``second`` could start."""
    rayspec = project / ".rayspec"
    (rayspec / "workflows" / "t.yaml").write_text(CLOCK_WF, encoding="utf-8")
    (rayspec / "stubs.yaml").write_text(STUBS, encoding="utf-8")
    result = cli.invoke(
        app,
        ["run", "t", "--root", str(project), "--quiet", "--stubs", str(rayspec / "stubs.yaml")],
    )
    assert result.exit_code == 1, result.output
    store = FileRunStore(home / "projects" / project_slug_for(project))
    return store.list_run_ids()[0], project, store


def test_json_names_the_wall_clock_cap(
    cli: CliRunner, capped: tuple[str, Path, FileRunStore]
) -> None:
    run_id, project, _store = capped
    result = cli.invoke(app, ["explain", run_id, "second", "--root", str(project), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["skip_reason"] == "budget_exceeded"
    cap = payload["cap"]
    assert cap["knobs"] == ["defaults.timeout_total"]
    assert cap["reason"].startswith("time limit exceeded (elapsed ")
    assert "timeout_total" in cap["reason"]
    assert "budget_usd" not in cap["reason"] and "max_tokens" not in cap["reason"]


def test_text_output_names_the_cap_next_to_the_skip_reason(
    cli: CliRunner, capped: tuple[str, Path, FileRunStore]
) -> None:
    run_id, project, _store = capped
    result = cli.invoke(app, ["explain", run_id, "second", "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert "skip reason budget_exceeded" in result.stdout
    assert "cap time limit exceeded" in result.stdout


def test_a_step_no_cap_skipped_has_no_cap_block(
    cli: CliRunner, capped: tuple[str, Path, FileRunStore]
) -> None:
    run_id, project, _store = capped
    result = cli.invoke(app, ["explain", run_id, "first", "--root", str(project), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["cap"] is None


def test_the_cap_is_recomputed_when_the_run_did_not_end_on_the_breaker(
    cli: CliRunner, capped: tuple[str, Path, FileRunStore]
) -> None:
    """A run that was interrupted after the trip still says which cap it was over.

    ``RunRecord.reason`` then describes the interruption, so the caps are recomputed from the
    record's own totals and its start/end stamps.
    """
    run_id, project, store = capped
    record = store.load(run_id)
    record.reason = "interrupted"
    store.save(record)
    result = cli.invoke(app, ["explain", run_id, "second", "--root", str(project), "--json"])
    assert result.exit_code == 0, result.output
    cap = json.loads(result.stdout)["cap"]
    assert cap["source"] == "recomputed"
    assert cap["knobs"] == ["defaults.timeout_total"]
    assert cap["reason"].startswith("time limit exceeded (")


# --------------------------------------------------------------------------------------------------
# a recomputed cap may only report what the record actually knows
# --------------------------------------------------------------------------------------------------

TOKEN_WF = """
rayspec: 1
name: t
isolation: none
defaults:
  max_tokens: 1
  timeout_total: 10m
agents:
  writer: {provider: stub}
steps:
  - id: first
    agent: writer
    prompt: "one"
  - id: second
    needs: [first]
    shell: "printf never"
"""


@pytest.fixture
def token_capped(cli: CliRunner, home: Path, project: Path) -> tuple[str, Path, FileRunStore]:
    """A run the TOKEN cap ended, seconds after it started, with the clock cap nowhere near."""
    rayspec = project / ".rayspec"
    (rayspec / "workflows" / "t.yaml").write_text(TOKEN_WF, encoding="utf-8")
    result = cli.invoke(app, ["run", "t", "--root", str(project), "--quiet"])
    assert result.exit_code == 1, result.output
    store = FileRunStore(home / "projects" / project_slug_for(project))
    run_id = store.list_run_ids()[0]
    assert "max_tokens" in (store.load(run_id).reason or "")
    return run_id, project, store


def test_a_recomputed_cap_does_not_invent_a_clock_breach(
    cli: CliRunner, token_capped: tuple[str, Path, FileRunStore]
) -> None:
    """A run whose process was killed has no ``ended_at``; the elapsed must not keep ticking.

    Measuring against the wall clock of the moment somebody types ``explain`` reports a wall-clock
    cap that never fired, and sends the reader after a knob that was never the problem. The last
    thing the record actually witnessed is where the run got to.
    """
    run_id, project, store = token_capped
    record = store.load(run_id)
    record.reason = "interrupted"  # not a cap reason: the caps get recomputed
    # the run happened three hours ago and its process was killed: no end stamp was ever written
    ago = timedelta(hours=3)
    assert record.started_at is not None
    record.started_at -= ago
    record.ended_at = None
    for step in record.steps.values():
        for field in ("started_at", "ended_at"):
            stamp = getattr(step, field)
            if stamp is not None:
                setattr(step, field, stamp - ago)
    store.save(record)
    result = cli.invoke(app, ["explain", run_id, "second", "--root", str(project), "--json"])
    assert result.exit_code == 0, result.output
    cap = json.loads(result.stdout)["cap"]
    assert cap is not None and cap["source"] == "recomputed"
    assert cap["knobs"] == ["defaults.max_tokens"]
    assert "timeout_total" not in cap["reason"]


def test_a_recomputed_cap_says_it_was_recomputed(
    cli: CliRunner, capped: tuple[str, Path, FileRunStore]
) -> None:
    """The text view must not present a recomputed cap as the one the run recorded."""
    run_id, project, store = capped
    record = store.load(run_id)
    record.reason = "interrupted"
    store.save(record)
    result = cli.invoke(app, ["explain", run_id, "second", "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert "cap time limit exceeded" in result.stdout
    assert "recomputed from the run record" in result.stdout
    # the recorded reason is stated as fact, without the marker
    recorded = cli.invoke(app, ["explain", run_id, "first", "--root", str(project)])
    assert "recomputed" not in recorded.stdout

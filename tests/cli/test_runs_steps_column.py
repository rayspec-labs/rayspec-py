# SPDX-License-Identifier: Apache-2.0
"""The ``steps`` column of ``rayspec runs``, and the worked examples ``docs/cli.md`` uses to
explain it — checked against each other, on the real output of real runs.

The rule and the example in that paragraph had drifted apart: the rule counts a step the engine
*resolved*, and a step skipped because its upstream failed is resolved — while the example said a
three-step workflow that failed at step 2 reads ``1/3``, which is the count you get only if a
skipped step is not resolved after all. Prose cannot arbitrate that; a run can. So these tests
run the two workflows the paragraph describes and hold the documentation to what came out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli import _runs_common as common
from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.schema import RunStatus
from rayspec.store.file import FileRunStore

CLI_DOC = Path(__file__).resolve().parents[2] / "docs" / "cli.md"

#: Three steps, the second of which fails — so the third is skipped as ``upstream_failed``.
WF_FAIL = """
rayspec: 1
name: fails_at_two
isolation: none
steps:
  - id: one
    shell: "printf one"
  - id: two
    needs: [one]
    shell: "exit 1"
  - id: three
    needs: [two]
    shell: "printf three"
"""

#: Three steps, the second of which is a gate — so the run pauses with step 3 never recorded.
WF_GATE = """
rayspec: 1
name: pauses_at_two
isolation: none
steps:
  - id: one
    shell: "printf one"
  - id: ok
    needs: [one]
    approve: {message: "ship it?"}
  - id: three
    needs: [ok]
    shell: "printf three"
"""

_FAILED_EXAMPLE = re.compile(r"3-step workflow that failed at step 2 reads `(\d+/\d+)`")
_PAUSED_EXAMPLE = re.compile(
    r"a run paused at the gate of a 3-step workflow reads `(\d+/\d+)` instead of `(\d+/\d+)`"
)


def _quoted(pattern: re.Pattern[str]) -> tuple[str, ...]:
    """The figures ``docs/cli.md``'s worked example quotes, or a failure naming the sentence."""
    match = pattern.search(CLI_DOC.read_text(encoding="utf-8"))
    assert match is not None, f"the worked example matching {pattern.pattern!r} is gone from docs"
    return match.groups()


@pytest.fixture
def project_with(cli: CliRunner, home: Path, project: Path):
    """Run one of the workflows above and hand back its record plus the ``runs`` listing."""

    def _run(source: str, name: str, *, expect: int) -> tuple[str, str]:
        (project / ".rayspec" / "workflows" / f"{name}.yaml").write_text(source, encoding="utf-8")
        result = cli.invoke(
            app, ["run", name, "--root", str(project), "--quiet", "--no-interactive"]
        )
        assert result.exit_code == expect, result.output
        store = FileRunStore(home / "projects" / project_slug_for(project))
        run_id = next(rid for rid in store.list_run_ids() if store.load(rid).workflow_name == name)
        listing = cli.invoke(app, ["runs", "--root", str(project)])
        assert listing.exit_code == 0, listing.output
        row = next(line for line in listing.output.splitlines() if line.startswith(run_id))
        cells = re.findall(r"\b\d+/\d+\b", row)
        assert len(cells) == 1, f"the steps cell is not the only n/m in the row: {row}"
        return run_id, cells[0]

    return _run


def test_a_three_step_run_that_failed_at_step_two_matches_the_documented_example(
    project_with,
) -> None:
    """`done` counts every step the engine resolved, and the skipped third step is resolved."""
    _run_id, cell = project_with(WF_FAIL, "fails_at_two", expect=1)
    assert cell == "2/3", "one succeeded, one failed, one skipped — 2 of 3 resolved"
    assert _quoted(_FAILED_EXAMPLE) == (cell,), (
        f"docs/cli.md says a 3-step workflow that failed at step 2 reads "
        f"{_quoted(_FAILED_EXAMPLE)[0]}; it reads {cell}"
    )


def test_a_three_step_run_paused_at_a_gate_matches_the_documented_example(
    cli: CliRunner, home: Path, project: Path, project_with
) -> None:
    """`total` gains the workflow's planned steps, so the run says how much is left, not how
    little it recorded — the example quotes both figures and both are checked."""
    run_id, cell = project_with(WF_GATE, "pauses_at_two", expect=3)
    assert cell == "1/3", "one done; the third step is planned but not recorded yet"
    with_planned, without_planned = _quoted(_PAUSED_EXAMPLE)
    assert with_planned == cell, f"docs/cli.md says {with_planned}; it reads {cell}"

    store = FileRunStore(home / "projects" / project_slug_for(project))
    record = store.load(run_id)
    done, total = common.steps_progress(record)  # what the cell would say with no workflow to load
    assert f"{done}/{total}" == without_planned, (
        f"docs/cli.md says the planned steps turn {without_planned} into {with_planned}; "
        f"without them the cell reads {done}/{total}"
    )


def test_only_a_succeeded_run_stops_gaining_planned_steps() -> None:
    """The rule is total — every status but ``succeeded`` is unfinished — rather than the list of
    the five that exist today. A status added later lands on the unfinished side, which is the
    side that reports honestly: a total that is too small claims work a run never reached."""
    unfinished = {s for s in RunStatus if common.may_still_gain_steps(s)}
    assert unfinished == set(RunStatus) - {RunStatus.SUCCEEDED}

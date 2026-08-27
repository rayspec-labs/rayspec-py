# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R5: a composite (``each:``/``include:``) cancelled mid-flight, drained before it
starts, or run as a ``join: always`` cleanup during the drain leaves the run CANCELLED (exit
4) — never FAILED. Unlike ``loop:`` (which records its own status and needed an explicit
interrupted-collateral fix), ``each:`` re-raises the cancel control and ``include:`` delegates
to the scheduler's uniform drain, so both were already correct; these pin that against
regression across the composite kinds."""

from __future__ import annotations

import pytest

from rayspec.schema import RunStatus, StepStatus

from .conftest import FakeLeaf, Harness
from .test_cancel_semantics import run_cancelling, wf

pytestmark = pytest.mark.anyio


async def test_each_cancelled_midflight_is_cancelled_not_failed(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  - id: fan\n"
            "    each: '[1, 2]'\n"
            "    as: item\n"
            "    max_parallel: 2\n"
            "    steps:\n"
            "      - id: work\n"
            "        shell: 'block:hold'\n"
        ),
    )
    leaf = FakeLeaf()
    result = await run_cancelling(harness, "t", at="fan[0]/work", leaf=leaf)
    assert result.status is RunStatus.CANCELLED, result.status
    assert result.exit_code == 4, result.exit_code


async def test_each_drained_before_start_is_cancelled(harness: Harness) -> None:
    """The each has not started when the cancel lands (a prior step is in flight): it is drained,
    recorded skipped-collateral, and the run is cancelled, not failed."""
    harness.workflow(
        "t",
        wf(
            "  - {id: gate, shell: 'block:hold'}\n"
            "  - id: fan\n"
            "    needs: [gate]\n"
            "    each: '[1, 2]'\n"
            "    as: item\n"
            "    steps:\n"
            "      - {id: work, shell: ok}\n"
        ),
    )
    leaf = FakeLeaf()
    result = await run_cancelling(harness, "t", at="gate", leaf=leaf)
    assert result.status is RunStatus.CANCELLED, result.status
    assert result.exit_code == 4, result.exit_code


async def test_include_cancelled_midflight_is_cancelled_not_failed(harness: Harness) -> None:
    harness.workflow(
        "inc",
        "rayspec: 1\nname: inc\nsteps:\n  - {id: body, shell: 'block:hold'}\n",
    )
    harness.workflow(
        "t",
        wf("  - id: sub\n    include: inc\n"),
    )
    leaf = FakeLeaf()
    result = await run_cancelling(harness, "t", at="sub/body", leaf=leaf)
    assert result.status is RunStatus.CANCELLED, result.status
    assert result.exit_code == 4, result.exit_code


async def test_join_always_each_cleanup_runs_after_cancel(harness: Harness) -> None:
    """A join: always each cleanup runs during the drain and succeeds — the run is cancelled and
    the cleanup's items all ran."""
    harness.workflow(
        "t",
        wf(
            "  - {id: work, shell: 'block:hold'}\n"
            "  - id: cleanup\n"
            "    needs: [work]\n"
            "    join: always\n"
            "    each: '[1, 2]'\n"
            "    as: item\n"
            "    steps:\n"
            "      - {id: tidy, shell: ok}\n"
        ),
    )
    leaf = FakeLeaf()
    result = await run_cancelling(harness, "t", at="work", leaf=leaf)
    assert result.status is RunStatus.CANCELLED, result.status
    assert result.exit_code == 4, result.exit_code
    assert result.steps["cleanup"].status is StepStatus.SUCCEEDED, result.steps["cleanup"].status


async def test_join_always_include_cleanup_runs_after_cancel(harness: Harness) -> None:
    harness.workflow(
        "inc",
        "rayspec: 1\nname: inc\nsteps:\n  - {id: tidy, shell: ok}\n",
    )
    harness.workflow(
        "t",
        wf(
            "  - {id: work, shell: 'block:hold'}\n"
            "  - {id: cleanup, needs: [work], join: always, include: inc}\n"
        ),
    )
    leaf = FakeLeaf()
    result = await run_cancelling(harness, "t", at="work", leaf=leaf)
    assert result.status is RunStatus.CANCELLED, result.status
    assert result.exit_code == 4, result.exit_code
    assert result.steps["cleanup"].status is StepStatus.SUCCEEDED, result.steps["cleanup"].status

# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R8/D18: a shell/python step's ``stdout.log``/``stderr.log`` are capped in place by the
pump (which holds the handle for the step's life) — the captured step output is unaffected."""

from __future__ import annotations

import pytest

from rayspec.engine.context import RunOptions
from rayspec.schema import RunStatus

from .conftest import Harness

pytestmark = pytest.mark.anyio


def wf(steps: str) -> str:
    return f"rayspec: 1\nname: t\nsteps:\n{steps}"


async def test_stdout_log_is_capped_in_place_with_a_marker(harness: Harness) -> None:
    harness.store.log_cap_bytes = 4096
    # ~100 KiB of stdout — well past 2x the 4 KiB cap
    harness.workflow(
        "t",
        wf(
            '  - {id: noisy, shell: "for i in $(seq 2000); do echo line-$i-paddddddddddding; done"}\n'
        ),
    )
    result = await harness.run("t", options=RunOptions(interactive=False))
    assert result.status is RunStatus.SUCCEEDED, result.reason
    log = harness.store.step_dir(result.run_id, "noisy") / "stdout.log"
    data = log.read_bytes()
    assert len(data) <= 4096 * 2, len(data)  # bounded (hysteresis)
    assert b"log truncated" in data  # the cut is marked
    assert data.startswith(b"line-1-")  # the head survives
    assert b"line-2000-paddddddddddding" in data  # the tail survives


async def test_captured_output_is_unaffected_by_the_cap(harness: Harness) -> None:
    """The pump caps the file on disk, not the in-memory chunks the step output is built from."""
    harness.store.log_cap_bytes = 4096
    harness.workflow(
        "t",
        wf('  - {id: noisy, shell: "for i in $(seq 2000); do echo row-$i; done"}\n'),
    )
    result = await harness.run("t", options=RunOptions(interactive=False))
    assert result.status is RunStatus.SUCCEEDED
    out = result.steps["noisy"].output_ref
    # the step output file holds the whole stdout — first and last lines both present
    content = (harness.store.run_dir(result.run_id) / out).read_text() if out else ""
    assert content.startswith("row-1\n") and content.rstrip().endswith("row-2000")

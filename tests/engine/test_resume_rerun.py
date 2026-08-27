# SPDX-License-Identifier: Apache-2.0
"""B10 (PRD-09 F12/F19): `resume --rerun GLOB` re-runs the recorded steps whose path matches GLOB
instead of replaying them from the resume cache — a supported way to force a step the fingerprint
thinks is unchanged, without hand-editing the record (which the dogfood had to do, F19)."""

from __future__ import annotations

import pytest

from rayspec.engine.context import RunOptions
from rayspec.schema import RunStatus

from .conftest import Harness

pytestmark = pytest.mark.anyio

# a/b succeed and cache; the last step fails, so the run is resumable (FAILED) with a and b reused
WF = """
rayspec: 1
name: t
steps:
  - {id: a, shell: echo one}
  - {id: b, needs: [a], shell: echo two}
  - {id: bad, needs: [b], shell: 'exit 1'}
"""


async def test_a_plain_resume_replays_the_finished_steps(harness: Harness) -> None:
    harness.workflow("t", WF)
    first = await harness.run("t")
    assert first.status is RunStatus.FAILED
    second = await harness.run("t", options=RunOptions(resume=True), resume=first.run_id)
    assert set(second.reused) == {"a", "b"}  # bad failed → not reusable, re-runs


async def test_rerun_reexecutes_only_the_matching_step(harness: Harness) -> None:
    harness.workflow("t", WF)
    first = await harness.run("t")
    second = await harness.run(
        "t", options=RunOptions(resume=True, rerun=("b",)), resume=first.run_id
    )
    assert "b" not in second.reused  # b was re-run
    assert "a" in second.reused  # a was still replayed


async def test_rerun_emits_a_warning_naming_the_glob(harness: Harness) -> None:
    harness.workflow("t", WF)
    first = await harness.run("t")
    harness.sink.clear()
    await harness.run("t", options=RunOptions(resume=True, rerun=("b",)), resume=first.run_id)
    warnings = [e for e in harness.events() if e.type.value == "warning"]
    assert any("re-running b (--rerun b)" in (e.data.get("message") or "") for e in warnings)


async def test_rerun_glob_reexecutes_every_matching_step(harness: Harness) -> None:
    harness.workflow("t", WF)
    first = await harness.run("t")
    # `*` matches any single id here; both a and b re-run, none of the two are reused
    second = await harness.run(
        "t", options=RunOptions(resume=True, rerun=("*",)), resume=first.run_id
    )
    assert "a" not in second.reused and "b" not in second.reused

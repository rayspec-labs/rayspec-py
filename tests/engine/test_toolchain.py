# SPDX-License-Identifier: Apache-2.0
"""What SDK/CLI/model was in effect is recorded in run.json at run start."""

from __future__ import annotations

import json

import pytest

from rayspec import __version__
from rayspec.engine.toolchain import capture_toolchain
from rayspec.providers.stub import StubProvider
from rayspec.schema import RunStatus
from rayspec.store.model import RunRecord

from .conftest import Harness

pytestmark = pytest.mark.anyio

WF = """\
rayspec: 1
name: t
agents:
  reviewer: {provider: claude, model: small, access: read-only}
steps:
  - {id: a, shell: "echo hi"}
  - {id: b, needs: [a], agent: reviewer, prompt: "review"}
"""


async def test_a_run_records_its_toolchain(harness: Harness) -> None:
    harness.workflow("t", WF)
    stub = StubProvider(script={"steps": {"b": {"text": "ok"}}})
    result = await harness.run("t", providers={"claude": stub})
    assert result.status is RunStatus.SUCCEEDED
    toolchain = harness.record(result.run_id).toolchain
    assert toolchain is not None
    assert toolchain["rayspec"] == __version__
    assert toolchain["python"].count(".") == 2
    assert toolchain["platform"]
    assert toolchain["models"] == {"agents.reviewer": "haiku"}  # model: small → tier
    assert list(toolchain["providers"]) == ["claude"]
    assert set(toolchain["providers"]["claude"]) >= {"sdk_version", "cli_version", "cli_path"}


async def test_a_workflow_without_agents_records_no_providers(harness: Harness) -> None:
    harness.workflow("t", "rayspec: 1\nname: t\nsteps:\n  - {id: a, shell: 'echo hi'}\n")
    result = await harness.run("t")
    toolchain = harness.record(result.run_id).toolchain
    assert toolchain is not None
    assert toolchain["providers"] == {} and toolchain["models"] == {}


async def test_a_provider_that_cannot_be_reached_is_recorded_as_an_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.workflow("t", WF)
    stub = StubProvider(script={"steps": {"b": {"text": "ok"}}})

    async def boom(*_a, **_k):
        raise RuntimeError("no CLI here")

    monkeypatch.setattr(stub, "healthcheck", boom)
    result = await harness.run("t", providers={"claude": stub})
    assert result.status is RunStatus.SUCCEEDED  # a toolchain probe never breaks a run
    toolchain = harness.record(result.run_id).toolchain
    assert toolchain is not None
    entry = toolchain["providers"]["claude"]
    assert entry["sdk_version"] is None
    assert "no CLI here" in entry["error"]


async def test_the_toolchain_survives_a_resume_unchanged(harness: Harness) -> None:
    harness.workflow("t", WF)
    stub = StubProvider(script={"steps": {"b": {"text": "ok"}}})
    first = await harness.run("t", providers={"claude": stub})
    before = harness.record(first.run_id).toolchain
    stub2 = StubProvider(script={"steps": {"b": {"text": "ok"}}})
    await harness.run("t", providers={"claude": stub2}, resume=first.run_id)
    assert harness.record(first.run_id).toolchain == before


def test_an_older_run_json_without_the_field_still_loads() -> None:
    payload = json.loads(
        RunRecord(
            run_id="20260821-101010-abcd",
            workflow_name="t",
            workflow_path="t.yaml",
            workflow_hash="deadbeef",
            project_slug="local/x",
            project_root="/tmp",
        ).model_dump_json()
    )
    payload.pop("toolchain")
    assert RunRecord.model_validate(payload).toolchain is None


async def test_capture_is_bounded_and_never_raises(harness: Harness) -> None:
    """The probe is best effort: a hanging healthcheck ends as an error entry, not a stuck run."""
    import anyio

    harness.workflow("t", WF)
    stub = StubProvider(script={"steps": {"b": {"text": "ok"}}})

    async def hang(*_a, **_k):
        await anyio.sleep(3600)

    stub.healthcheck = hang  # type: ignore[method-assign]
    runner = harness.runner("t", providers={"claude": stub})
    with anyio.fail_after(30):
        toolchain = await capture_toolchain_for(runner)
    assert "error" in toolchain["providers"]["claude"]


async def capture_toolchain_for(runner) -> dict:
    """Build the runner's context far enough to capture a toolchain (no steps executed)."""
    from rayspec.engine.context import RunContext
    from rayspec.engine.runtime import Runtime

    run = RunRecord(
        run_id="20260821-101010-abcd",
        workflow_name="t",
        workflow_path="t.yaml",
        workflow_hash="deadbeef",
        project_slug="local/x",
        project_root=str(runner.project_root),
    )
    ctx = RunContext(
        resolved=runner.resolved,
        run=run,
        store=runner.store,
        sinks=runner.sinks,
        engine=runner.engine,
        runtime=Runtime(1),
        options=runner.options,
        workdir=runner.workspace.workdir,
        project={"root": str(runner.project_root), "name": None, "slug": "local/x"},
        env={},
        providers=runner.providers,
    )
    return await capture_toolchain(ctx, timeout_s=0.2)


class OpenTrackingStub(StubProvider):
    """A stub that counts ``open()`` calls — the probe must never trigger one."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.open_calls = 0

    async def open(self, **kwargs: object) -> None:
        self.open_calls += 1
        await super().open(**kwargs)  # type: ignore[arg-type]


WHEN_FALSE = """\
rayspec: 1
name: t
agents:
  reviewer: {provider: claude, model: small, access: read-only}
steps:
  - {id: a, shell: "echo hi"}
  - {id: b, needs: [a], agent: reviewer, prompt: "review", when: "false"}
"""


async def test_the_probe_never_opens_a_provider(harness: Harness) -> None:
    """`open()` is a real cost (a CLI subprocess) — metadata must not pay it."""
    harness.workflow("t", WHEN_FALSE)
    stub = OpenTrackingStub(script={"steps": {"b": {"text": "ok"}}})
    result = await harness.run("t", providers={"claude": stub})
    assert result.status is RunStatus.SUCCEEDED
    assert stub.open_calls == 0
    toolchain = harness.record(result.run_id).toolchain
    assert toolchain is not None
    assert list(toolchain["providers"]) == ["claude"]  # probed all the same


async def test_the_probe_runs_after_the_run_started_event(harness: Harness) -> None:
    """The user sees the run start before a provider probe can hold the run up."""
    harness.workflow("t", WF)
    stub = StubProvider(script={"steps": {"b": {"text": "ok"}}})
    seen: list[str] = []

    async def healthcheck(*_a: object, **_k: object) -> object:
        seen.extend(e.type.value for e in harness.sink.events)
        raise RuntimeError("probed")

    stub.healthcheck = healthcheck  # type: ignore[method-assign]
    await harness.run("t", providers={"claude": stub})
    assert "run.started" in seen, seen


async def test_providers_are_probed_concurrently(harness: Harness) -> None:
    """N providers cost one timeout, not N — the probes share a task group."""
    import anyio

    from rayspec.providers.base import ProviderHealth

    harness.workflow(
        "t",
        """\
rayspec: 1
name: t
agents:
  one: {provider: claude, model: small, access: read-only}
  two: {provider: codex, model: small, access: read-only}
steps:
  - {id: a, agent: one, prompt: "x"}
  - {id: b, agent: two, prompt: "y"}
""",
    )
    arrived = anyio.Semaphore(0)
    both = anyio.Event()

    async def gated(*_a: object, **_k: object) -> ProviderHealth:
        arrived.release()
        await both.wait()  # only returns once BOTH probes are in flight
        return ProviderHealth(ok=True, sdk_version="probed", auth="ok")

    stubs: dict[str, StubProvider] = {}
    for pid in ("claude", "codex"):
        stub = StubProvider(script={"steps": {"a": {"text": "ok"}, "b": {"text": "ok"}}})
        stub.healthcheck = gated  # type: ignore[method-assign]
        stubs[pid] = stub

    async def release() -> None:
        await arrived.acquire()
        await arrived.acquire()
        both.set()

    run_id = ""
    async with anyio.create_task_group() as tg:
        tg.start_soon(release)
        with anyio.fail_after(20):
            run_id = (await harness.run("t", providers=stubs)).run_id
    toolchain = harness.record(run_id).toolchain
    assert toolchain is not None
    entries = toolchain["providers"]
    assert sorted(entries) == ["claude", "codex"]
    # sequential probing would time the first one out instead of reaching the barrier
    assert [e["sdk_version"] for e in entries.values()] == ["probed", "probed"]


async def test_a_pre_existing_run_without_a_toolchain_is_not_stamped_on_resume(
    harness: Harness,
) -> None:
    """Review: `None` means "unknown at the run's start", not "capture it at resume time"."""
    harness.workflow("t", WF)
    stub = StubProvider(script={"steps": {"b": {"text": "ok"}}})
    first = await harness.run("t", providers={"claude": stub})
    record = harness.record(first.run_id)
    record.toolchain = None  # a run.json written before the toolchain was recorded
    harness.store.save(record)
    stub2 = StubProvider(script={"steps": {"b": {"text": "ok"}}})
    await harness.run("t", providers={"claude": stub2}, resume=first.run_id)
    assert harness.record(first.run_id).toolchain is None

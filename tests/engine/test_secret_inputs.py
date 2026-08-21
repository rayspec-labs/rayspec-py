"""At the engine level: secret inputs are delivered to shell/python steps through the
environment only and never persisted; resume re-obtains them; ``stubs_path`` is recorded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.errors import ResumeError
from rayspec.schema import RunStatus

from .conftest import Harness

pytestmark = pytest.mark.anyio

SECRET = "ghp_SECRETVALUE_123"

WF = """
rayspec: 1
name: sec
isolation: none
inputs:
  token: { type: string, secret: true, required: true }
  issue: { type: integer, default: 7 }
agents:
  r: { provider: stub }
steps:
  - id: use
    shell: |
      same=no; [ "$T" = "$RAYSPEC_INPUT_TOKEN" ] && same=yes
      echo "len=${#RAYSPEC_INPUT_TOKEN} mapped=${same} issue=${RAYSPEC_INPUT_ISSUE:-unset}"
    env: { T: "{{ inputs.token }}" }
  - id: py
    needs: [use]
    python: |
      import os
      print(len(os.environ["RAYSPEC_INPUT_TOKEN"]), os.environ["T2"] == os.environ["RAYSPEC_INPUT_TOKEN"])
    env: { T2: "{{ inputs.token }}" }
  - id: ask
    needs: [py]
    agent: r
    prompt: "issue {{ inputs.issue }}"
outputs:
  v: "{{ steps.use.output }}"
"""


def _grep(root: Path, needle: str) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and needle in path.read_text(errors="replace"):
            hits.append(str(path.relative_to(root)))
    return sorted(hits)


async def test_secret_reaches_shell_and_python_via_env_only(harness: Harness) -> None:
    harness.workflow("sec", WF)
    result = await harness.run("sec", {"token": SECRET, "issue": 3})
    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert result.outputs == {"v": f"len={len(SECRET)} mapped=yes issue=3"}
    record = harness.record(result.run_id)
    assert record.inputs == {"token": "<secret>", "issue": 3}
    assert record.secret_inputs == ("token",)
    py_out = harness.store.read_output(result.run_id, record.steps["py"].output_ref or "")
    assert py_out.strip() == f"{len(SECRET)} True"
    # nothing under the run store holds the value (no step printed it)
    assert _grep(result.run_dir, SECRET) == []
    ctx = json.loads((result.run_dir / "steps" / "use" / "context.json").read_text())
    assert ctx["inputs"] == {"token": "<secret>", "issue": 3}
    # lifecycle events (run.started & co) never carry the value
    assert SECRET not in json.dumps([e.model_dump(mode="json") for e in harness.events()])


async def test_resume_requires_the_secret_again_and_replays_cached_steps(harness: Harness) -> None:
    broken = WF.replace(
        "  - id: ask\n", "  - id: boom\n    needs: [py]\n    shell: exit 1\n  - id: ask\n"
    )
    harness.workflow("sec", broken)
    first = await harness.run("sec", {"token": SECRET, "issue": 7})
    assert first.status is RunStatus.FAILED
    with pytest.raises(ResumeError, match="missing secret input\\(s\\): token"):
        await harness.run("sec", {}, resume=first.run_id)
    harness.workflow("sec", broken.replace("exit 1", "exit 0"))
    # a changed workflow needs --force; the cached leaves are replayed (their fingerprint does
    # not include the secret value, which is never persisted) — a different value also replays
    second = await harness.run(
        "sec",
        {"token": "another"},
        resume=first.run_id,
        options=RunOptions(force=True, resume=True),
    )
    assert second.status is RunStatus.SUCCEEDED, second.reason
    assert "use" in second.reused and "py" in second.reused
    assert harness.record(second.run_id).inputs == {"token": "<secret>", "issue": 7}


async def test_stubs_path_is_recorded_and_updated_on_resume(
    harness: Harness, tmp_path: Path
) -> None:
    harness.workflow(
        "stubbed",
        """
rayspec: 1
name: stubbed
isolation: none
agents:
  r: { provider: stub }
steps:
  - { id: a, agent: r, prompt: hi }
  - { id: b, needs: [a], shell: exit 1 }
""",
    )
    stubs = tmp_path / "stubs.yaml"
    stubs.write_text("steps:\n  a: {text: scripted}\n")
    first = await harness.run(
        "stubbed",
        options=RunOptions(stub_script={"steps": {"a": {"text": "x"}}}, stubs_path=str(stubs)),
    )
    assert harness.record(first.run_id).stubs_path == str(stubs)
    other = tmp_path / "other.yaml"
    second = await harness.run(
        "stubbed",
        resume=first.run_id,
        options=RunOptions(resume=True, stubs_path=str(other)),
    )
    assert harness.record(second.run_id).stubs_path == str(other)
    third = await harness.run("stubbed", resume=first.run_id, options=RunOptions(resume=True))
    assert harness.record(third.run_id).stubs_path == str(other)  # kept when not overridden


async def test_root_secret_reaches_shell_steps_inside_include_bodies(harness: Harness) -> None:
    """Secrets belong to the root workflow and are exported to EVERY shell/python step of the
    run, include bodies included (docs/schema.md); a body's own same-named input keeps its
    bound value in the ``env:`` mapping."""
    harness.workflow(
        "block",
        """
rayspec: 1
name: block
inputs:
  label: { type: string, required: true }
  token: { type: string, default: body-token }
steps:
  - id: inner
    shell: |
      v="${RAYSPEC_INPUT_TOKEN:-}"; echo "{{ inputs.label }} len=${#v} t=${T}"
    env: { T: "{{ inputs.token }}" }
outputs:
  line: "{{ steps.inner.output }}"
""",
    )
    harness.workflow(
        "sec",
        """
rayspec: 1
name: sec
isolation: none
inputs:
  token: { type: string, secret: true, required: true }
steps:
  - id: body
    include: block
    with: { label: hello }
outputs:
  v: "{{ steps.body.output.line }}"
""",
    )
    result = await harness.run("sec", {"token": SECRET})
    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert result.outputs == {"v": f"hello len={len(SECRET)} t=body-token"}
    assert _grep(result.run_dir, SECRET) == []


async def test_optional_secret_supplied_on_resume_reaches_the_step(harness: Harness) -> None:
    """An optional secret that was NOT given at launch may be supplied on resume: it is then
    recorded as ``<secret>`` and exported like any other secret."""
    harness.workflow(
        "opt",
        """
rayspec: 1
name: opt
isolation: none
inputs:
  token: { type: string, secret: true }
steps:
  - id: need
    shell: |
      [ -n "${RAYSPEC_INPUT_TOKEN:-}" ] || { echo "no token"; exit 1; }
      echo "len=${#RAYSPEC_INPUT_TOKEN}"
outputs:
  v: "{{ steps.need.output }}"
""",
    )
    first = await harness.run("opt", {})
    assert first.status is RunStatus.FAILED
    assert harness.record(first.run_id).inputs == {}
    second = await harness.run(
        "opt", {"token": SECRET}, resume=first.run_id, options=RunOptions(resume=True)
    )
    assert second.status is RunStatus.SUCCEEDED, second.reason
    assert second.outputs == {"v": f"len={len(SECRET)}"}
    assert harness.record(second.run_id).inputs == {"token": "<secret>"}
    assert _grep(second.run_dir, SECRET) == []

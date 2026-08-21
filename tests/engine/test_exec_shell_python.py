"""shell:/python: executors with real subprocesses (env export, RAYSPEC_V refs, spill,
allow_failure, timeout + killpg, output_schema, cwd, dry run)."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import anyio
import pytest

from events._validating import ValidatingSink
from rayspec.engine.context import RunOptions
from rayspec.engine.scheduler import run_graph
from rayspec.events.model import EventType
from rayspec.schema import StepStatus

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


def wf(steps: str, inputs: str = "") -> str:
    block = f"inputs:\n{inputs}\n" if inputs else ""
    return f"rayspec: 1\nname: t\n{block}steps:\n{steps}"


async def run_wf(harness: Harness, text: str, inputs: dict | None = None, **opts):
    harness.workflow("t", text)
    g = make_graph_harness(
        harness, harness.load("t"), fake_leaf=False, options=RunOptions(**opts) if opts else None
    )
    if inputs:
        g.scope.inputs = inputs
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    return g, outcomes


async def test_shell_env_export_and_v_refs_and_output(harness: Harness) -> None:
    harness.sink = ValidatingSink(harness.sink)  # pin the published event/stream shapes
    g, out = await run_wf(
        harness,
        wf(
            """
  - id: a
    shell: |
      echo "issue=$RAYSPEC_INPUT_ISSUE run=$RAYSPEC_RUN_ID"
      echo "ref={{ inputs.name }}"
      test -f "$RAYSPEC_CONTEXT" && echo ctx=ok
      test -n "$RAYSPEC_WORKDIR" && echo wd=ok
  - id: b
    needs: [a]
    env: {GREETING: "hi {{ steps.a.output | length }}"}
    shell: printf '%s\\n' "$GREETING" "{{ steps.a.output }}"
""",
            inputs="  issue: {type: integer, default: 7}\n  name: {type: string, default: 'x y'}",
        ),
        inputs={"issue": 7, "name": "x y"},
    )
    a = out["a"]
    assert a.record.status is StepStatus.SUCCEEDED, a.record.error
    lines = a.output.splitlines()
    assert lines[0] == f"issue=7 run={g.run.run_id}"
    assert lines[1] == "ref=x y"
    assert "ctx=ok" in lines and "wd=ok" in lines
    assert a.record.exit_code == 0 and a.record.ok is True
    b = out["b"]
    assert b.output.splitlines()[0].startswith("hi ")
    assert b.output.splitlines()[1:] == lines  # the whole multi-line output passed through a slot
    # logs + stream records
    step_dir = harness.store.step_dir(g.run.run_id, "a")
    assert (step_dir / "stdout.log").read_text().startswith("issue=7")
    kinds = [r.kind for r in harness.sink.stream_for("a")]
    assert "stdout" in kinds and kinds[-1] == "exit"
    ctx_file = json.loads((step_dir / "context.json").read_text())
    assert ctx_file["inputs"]["issue"] == 7


async def test_shell_large_value_spills_to_tmp(harness: Harness) -> None:
    big = "x" * (70 * 1024)
    harness.workflow(
        "t",
        wf(
            """
  - id: a
    shell: printf '%s' "{{ inputs.big }}" | wc -c | tr -d ' '
""",
            inputs="  big: {type: string, required: true}",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"), fake_leaf=False)
    g.scope.inputs = {"big": big}
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert out["a"].output == str(len(big))
    # spills are cleaned up after the step
    assert list((harness.store.run_dir(g.run.run_id) / "tmp").iterdir()) == []


async def test_shell_allow_failure_records_exit_code_and_stderr(harness: Harness) -> None:
    g, out = await run_wf(
        harness,
        wf("""
  - id: a
    shell: "echo partial; echo oops >&2; exit 3"
    allow_failure: true
  - id: b
    needs: [a]
    shell: "echo ok={{ steps.a.ok }} code={{ steps.a.exit_code }} err={{ steps.a.stderr | trim }}"
"""),
    )
    a = out["a"].record
    assert a.status is StepStatus.FAILED and a.tolerated and a.exit_code == 3 and a.ok is False
    assert a.error is not None and "exit code 3" in a.error.message and "oops" in a.error.message
    assert out["a"].output == "partial"
    assert out["b"].output == "ok=false code=3 err=oops"
    assert (harness.store.step_dir(g.run.run_id, "a") / "stderr.log").read_text() == "oops\n"


async def test_shell_failure_untolerated(harness: Harness) -> None:
    _, out = await run_wf(harness, wf("  - {id: a, shell: 'exit 2'}"))
    assert out["a"].record.status is StepStatus.FAILED and not out["a"].record.tolerated


async def test_shell_timeout_kills_process_group(harness: Harness, tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    _, out = await run_wf(
        harness,
        wf(f"""
  - id: a
    timeout: 0.3
    shell: |
      sleep 30 &
      echo $! > {pidfile}
      wait
"""),
    )
    rec = out["a"].record
    assert rec.status is StepStatus.FAILED and rec.error and rec.error.type == "timeout"
    # the grand-child sleep was in the same process group and is dead within the deadline
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 3
    alive = True
    while alive and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        else:
            await anyio.sleep(0.05)
    assert not alive, "child sleep survived killpg"


async def test_shell_cancellation_kills_and_marks_interrupted(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: a, shell: 'sleep 30'}"))
    g = make_graph_harness(harness, harness.load("t"), fake_leaf=False)
    started = time.monotonic()
    with anyio.move_on_after(0.3):
        await run_graph(g.graph, g.scope, g.ctx)
    assert time.monotonic() - started < 5
    assert harness.statuses(g.run.run_id)["a"] == "interrupted"


async def test_shell_output_schema(harness: Harness) -> None:
    _, out = await run_wf(
        harness,
        wf("""
  - id: good
    shell: |
      echo '{"n": 3, "tags": ["a"]}'
    output_schema: {type: object, properties: {n: {type: integer}}, required: [n]}
  - id: use
    needs: [good]
    shell: echo "n={{ steps.good.output.n }}"
  - id: bad_json
    shell: echo nope
    output_schema: {type: object}
  - id: bad_schema
    shell: |
      echo '{"n": "x"}'
    output_schema: {type: object, properties: {n: {type: integer}}, required: [n]}
"""),
    )
    assert out["good"].output == {"n": 3, "tags": ["a"]}
    assert out["good"].record.output_kind == "json"
    assert out["use"].output == "n=3"
    assert out["bad_json"].record.status is StepStatus.FAILED
    assert out["bad_json"].record.error and "not valid JSON" in out["bad_json"].record.error.message
    assert out["bad_schema"].record.error and "output_schema" in out["bad_schema"].record.error.type


async def test_shell_cwd_and_missing_cwd(harness: Harness) -> None:
    (harness.root / "sub").mkdir()
    _, out = await run_wf(
        harness,
        wf("""
  - {id: a, cwd: sub, shell: basename "$PWD"}
  - {id: b, cwd: "{{ 'nope' }}", shell: "true"}
  - {id: c, interpreter: sh, shell: "echo sh"}
"""),
    )
    assert out["a"].output == "sub"
    assert out["b"].record.status is StepStatus.FAILED
    assert out["b"].record.error and "cwd" in out["b"].record.error.message
    assert out["c"].output == "sh"


async def test_shell_render_error_fails_step(harness: Harness) -> None:
    _, out = await run_wf(harness, wf("  - {id: a, shell: 'echo {{ steps.zzz.output }}'}"))
    rec = out["a"].record
    assert rec.status is StepStatus.FAILED and rec.error and rec.error.type == "render"
    assert "zzz" in rec.error.message


async def test_shell_env_none_value_is_error(harness: Harness) -> None:
    _, out = await run_wf(harness, wf("  - {id: a, env: {X: '{{ none }}'}, shell: 'echo $X'}"))
    rec = out["a"].record
    assert rec.status is StepStatus.FAILED and rec.error and "null" in rec.error.message


async def test_dry_run_skips_shell_unless_exec_shell(harness: Harness) -> None:
    text = wf("""
  - {id: a, shell: "echo real"}
  - id: b
    shell: |
      echo '{"v": 1}'
    output_schema: {type: object, properties: {v: {type: integer}}, required: [v]}
  - {id: c, needs: [a, b], shell: "echo after"}
""")
    _, out = await run_wf(harness, text, dry_run=True)
    assert out["a"].record.status is StepStatus.SUCCEEDED and out["a"].output == ""
    assert out["b"].output == {"v": 0}  # minimal schema instance
    assert out["c"].record.status is StepStatus.SUCCEEDED
    assert harness.finished("a").data.get("dry_run") is True
    harness.sink.clear()
    _, out = await run_wf(harness, text, dry_run=True, exec_shell=True)
    assert out["a"].output == "real" and out["c"].output == "after"


async def test_python_step_literals_and_deps_command(harness: Harness) -> None:
    from rayspec.engine.executors.python import python_command
    from rayspec.schema import parse_step

    _, out = await run_wf(
        harness,
        wf(
            """
  - id: a
    python: |
      import json, os
      data = {{ inputs.items }}
      print(json.dumps({"n": len(data), "first": data[0], "ctx": os.environ["RAYSPEC_CONTEXT"].endswith("context.json")}))
    output_schema: {type: object}
  - id: b
    needs: [a]
    python: |
      print({{ steps.a.output.n }} * 2)
  - id: c
    python: |
      import sys
      print("err", file=sys.stderr); sys.exit(4)
    allow_failure: true
""",
            inputs="  items: {type: array, default: ['x', 'y']}",
        ),
        inputs={"items": ["x", "y"]},
    )
    assert out["a"].output == {"n": 2, "first": "x", "ctx": True}
    assert out["b"].output == "4"
    assert out["c"].record.exit_code == 4 and out["c"].record.tolerated
    assert out["c"].stderr == "err\n"
    step = parse_step({"id": "p", "python": "x", "deps": ["httpx", "rich"]})
    cmd = python_command(step)  # type: ignore[arg-type]
    assert (
        cmd[:3] == ["uv", "run", "--no-project"] and "--with" in cmd and cmd[-2:] == ["python", "-"]
    )


async def test_stream_records_carry_attempt_and_exit(harness: Harness) -> None:
    await run_wf(
        harness,
        wf("  - {id: a, shell: 'echo one; echo two; echo three >&2'}"),
    )
    records = harness.sink.stream_for("a")
    assert [r.kind for r in records if r.kind == "stdout"] == ["stdout", "stdout"]
    assert [r.text for r in records if r.kind == "stdout"] == ["one\n", "two\n"]
    assert [r.text for r in records if r.kind == "stderr"] == ["three\n"]
    assert records[-1].kind == "exit" and records[-1].data == {"exit_code": 0}
    assert all(r.attempt == 1 for r in records)
    finished = harness.events(EventType.STEP_FINISHED)
    assert finished[-1].data["status"] == "succeeded"


def test_signal_numbers_available() -> None:
    assert signal.SIGTERM and signal.SIGKILL


async def test_retried_shell_step_keeps_every_attempts_logs(harness: Harness) -> None:
    g, out = await run_wf(
        harness,
        wf("""
  - id: a
    retry: {attempts: 2, delay: 0, on_error: all}
    shell: |
      echo "out $RAYSPEC_STEP_PATH"
      echo "err line" >&2
      exit 1
"""),
    )
    rec = out["a"].record
    assert rec.status is StepStatus.FAILED and rec.attempts == 2
    step_dir = harness.store.step_dir(g.run.run_id, "a")
    stdout_log = (step_dir / "stdout.log").read_text()
    stderr_log = (step_dir / "stderr.log").read_text()
    assert stdout_log.count("out a") == 2, stdout_log
    assert stderr_log.count("err line") == 2, stderr_log
    assert "attempt 2" in stdout_log and "attempt 2" in stderr_log
    # the in-memory stderr of the outcome is the LAST attempt only
    assert out["a"].stderr is not None and out["a"].stderr.count("err line") == 1


async def test_fingerprint_is_stable_for_spilled_values(harness: Harness) -> None:
    from rayspec.engine.executors import fingerprint_of

    harness.workflow(
        "t",
        wf(
            """
  - id: a
    shell: printf '%s' "{{ inputs.big }}" | wc -c
  - id: b
    python: print(len({{ inputs.big }}))
""",
            inputs="  big: {type: string, required: true}",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"), fake_leaf=False)
    g.scope.inputs = {"big": "x" * (70 * 1024)}
    steps = {s.id: s for s in g.graph.steps}
    for sid in ("a", "b"):
        first = fingerprint_of(steps[sid], g.scope, g.ctx)
        second = fingerprint_of(steps[sid], g.scope, g.ctx)
        assert first == second  # the random spill path is not part of the fingerprint
    g.scope.inputs = {"big": "y" * (70 * 1024)}  # the spilled CONTENT still matters
    changed = {sid: fingerprint_of(steps[sid], g.scope, g.ctx) for sid in ("a", "b")}
    g.scope.inputs = {"big": "x" * (70 * 1024)}
    assert changed["a"] != fingerprint_of(steps["a"], g.scope, g.ctx)
    assert changed["b"] != fingerprint_of(steps["b"], g.scope, g.ctx)
    assert list((harness.store.run_dir(g.run.run_id) / "tmp").iterdir()) == []

"""approve / loop / each / include executors (with the fake leaf + stub provider)."""

from __future__ import annotations

import anyio
import pytest

from rayspec.engine.approval import ApprovalAnswer, ApprovalRequest
from rayspec.engine.context import RunOptions
from rayspec.engine.errors import RunPaused, RunStopped
from rayspec.engine.scheduler import run_graph
from rayspec.events.model import EventType
from rayspec.providers.stub import StubProvider
from rayspec.schema import StepStatus
from rayspec.store.model import Decision, PauseInfo

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


def wf(steps: str, **top: str) -> str:
    extra = "".join(f"{k}:\n{v}\n" for k, v in top.items())
    return f"rayspec: 1\nname: t\n{extra}steps:\n{steps}"


# --------------------------------------------------------------------------------------------------
# approve
# --------------------------------------------------------------------------------------------------


class FakePrompt:
    def __init__(self, answer: ApprovalAnswer | None) -> None:
        self.answer = answer
        self.requests: list[ApprovalRequest] = []

    async def __call__(self, request: ApprovalRequest) -> ApprovalAnswer | None:
        self.requests.append(request)
        return self.answer


APPROVE_WF = wf("""
  - {id: a, shell: "ok:built"}
  - {id: gate, needs: [a], approve: "Ship {{ steps.a.output }}?"}
  - {id: after, needs: [gate], shell: "ok:{{ steps.gate.output }}"}
""")


async def test_approve_auto_with_yes_and_dry_run(harness: Harness) -> None:
    for opts in (RunOptions(yes=True), RunOptions(dry_run=True)):
        harness.sink.clear()
        harness.workflow("t", APPROVE_WF)
        g = make_graph_harness(harness, harness.load("t"), options=opts)
        out = await run_graph(g.graph, g.scope, g.ctx)
        gate = out["gate"].record
        assert gate.status is StepStatus.SUCCEEDED and gate.approved is True
        assert out["gate"].output == "" and gate.output_ref  # comment '' still written
        assert out["after"].record.status is StepStatus.SUCCEEDED
        decision = harness.events(EventType.RUN_DECISION)[0]
        assert decision.data["by"] == ("--yes" if opts.yes else "dry-run")


async def test_approve_tty_prompt_quiesces_and_records_comment(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: "ok:built"}
  - {id: slow, shell: "sleep:0.2"}
  - {id: gate, needs: [a], approve: {message: "Ship {{ steps.a.output }}?"}}
  - {id: after, needs: [gate], shell: "ok:{{ steps.gate.output }}"}
"""),
    )
    prompt = FakePrompt(ApprovalAnswer(True, "looks good"))
    g = make_graph_harness(harness, harness.load("t"), prompt=prompt)
    rt = g.ctx.runtime

    quiesced_at_prompt: list[int] = []

    async def spy(request: ApprovalRequest) -> ApprovalAnswer | None:
        quiesced_at_prompt.append(rt.active_leaves)
        assert not rt.gate_open
        return await prompt(request)

    g.ctx.approval_prompt = spy
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert quiesced_at_prompt == [0]  # the slow leaf finished before the prompt was shown
    assert rt.gate_open
    assert prompt.requests[0].message == "Ship built?"
    assert prompt.requests[0].needs[0].path == "a" and prompt.requests[0].needs[0].tail == "built"
    assert out["gate"].output == "looks good" and out["gate"].record.approved is True
    assert out["after"].output == "looks good"


async def test_approve_non_interactive_pauses_with_token(harness: Harness) -> None:
    harness.workflow("t", APPROVE_WF)
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(interactive=False))
    with pytest.raises(RunPaused) as info:
        await run_graph(g.graph, g.scope, g.ctx)
    assert info.value.token == "gate#1"
    run = harness.record(g.run.run_id)
    assert run.pause is not None and run.pause.token == "gate#1" and run.pause.step == "gate"
    assert run.pause.message == "Ship built?"
    assert run.steps["gate"].status is StepStatus.PAUSED
    assert "after" not in run.steps  # not skipped: it runs after resume
    paused = harness.events(EventType.RUN_PAUSED)
    assert paused and paused[0].data == {
        "token": "gate#1",
        "step": "gate",
        "message": "Ship built?",
        "reason": "approval",
    }
    assert g.ctx.paused is info.value


async def test_approve_prompt_returning_none_pauses(harness: Harness) -> None:
    harness.workflow("t", APPROVE_WF)
    g = make_graph_harness(harness, harness.load("t"), prompt=FakePrompt(None))
    with pytest.raises(RunPaused):
        await run_graph(g.graph, g.scope, g.ctx)


async def test_approve_consumes_stored_decision_only_with_matching_token(harness: Harness) -> None:
    harness.workflow("t", APPROVE_WF)
    # token matches → consumed
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(interactive=False))
    g.ctx.run.pause = PauseInfo(
        token="gate#1",
        step="gate",
        message="m",
        decision=Decision(approved=True, comment="ok!", by="cli"),
    )
    g.ctx.cache["gate"] = _paused_record("gate", attempts=1)
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert out["gate"].record.status is StepStatus.SUCCEEDED
    assert out["gate"].output == "ok!" and out["gate"].record.attempts == 1
    assert g.ctx.run.pause is None
    assert harness.events(EventType.RUN_DECISION)[0].data["by"] == "cli"
    # stale token → gate again (pauses)
    harness.sink.clear()
    g2 = make_graph_harness(harness, harness.load("t"), options=RunOptions(interactive=False))
    g2.ctx.run.pause = PauseInfo(
        token="gate#7",
        step="gate",
        message="m",
        decision=Decision(approved=True, comment="", by="cli"),
    )
    with pytest.raises(RunPaused) as info:
        await run_graph(g2.graph, g2.scope, g2.ctx)
    assert info.value.token == "gate#1"


def _paused_record(path: str, *, attempts: int):
    from rayspec.store.model import StepRecord

    return StepRecord(
        path=path, id=path, kind="approve", status=StepStatus.PAUSED, attempts=attempts
    )


@pytest.mark.parametrize(
    ("on_reject", "gate_status", "after_status", "raises"),
    [
        ("cancel", "rejected", "skipped", True),
        ("continue", "succeeded", "succeeded", False),
        ("fail", "failed", "skipped", False),
    ],
)
async def test_approve_on_reject_variants(
    harness: Harness, on_reject: str, gate_status: str, after_status: str, raises: bool
) -> None:
    harness.workflow(
        "t",
        wf(f"""
  - {{id: gate, approve: {{message: "ok?", on_reject: {on_reject}}}}}
  - {{id: after, needs: [gate], shell: "ok:{{{{ steps.gate.approved }}}}"}}
"""),
    )
    g = make_graph_harness(
        harness, harness.load("t"), prompt=FakePrompt(ApprovalAnswer(False, "nah"))
    )
    if raises:
        with pytest.raises(RunStopped) as info:
            await run_graph(g.graph, g.scope, g.ctx)
        assert info.value.status == "cancelled" and "nah" in (info.value.reason or "")
        st = harness.statuses(g.run.run_id)
        assert st["gate"] == gate_status and st["after"] == after_status
    else:
        out = await run_graph(g.graph, g.scope, g.ctx)
        assert out["gate"].record.status.value == gate_status
        assert out["gate"].record.approved is False
        assert out["after"].record.status.value == after_status
        if after_status == "succeeded":
            assert out["after"].output == "false"


# --------------------------------------------------------------------------------------------------
# loop
# --------------------------------------------------------------------------------------------------


async def test_loop_iteration_vars_until_and_output(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - id: build
    loop:
      max_iterations: 5
      until: steps.check.output == 'pass'
      steps:
        - id: implement
          prompt: "n={{ iteration.n }} max={{ iteration.max }} first={{ iteration.first }} prev={{ iteration.prev.check.output | default('none') }}"
        - id: check
          needs: [implement]
          prompt: "check {{ steps.implement.output }}"
  - id: after
    needs: [build]
    shell: "ok:{{ steps.build.output.check }}/{{ steps.build.iterations }}/{{ steps.build.converged }}"
"""),
    )
    stub = StubProvider(
        script={"steps": {"build[*]/check": {"sequence": ["fail", "fail", "pass"]}}}
    )
    g = make_graph_harness(harness, harness.load("t"), providers={"claude": stub})
    out = await run_graph(g.graph, g.scope, g.ctx)
    rec = out["build"].record
    assert rec.status is StepStatus.SUCCEEDED, rec.error
    assert rec.loop is not None and rec.loop.iterations == 3 and rec.loop.converged is True
    assert out["build"].output == {
        "implement": "[stub] n=3 max=5 first=false prev=fail",
        "check": "pass",
    }
    prompts = [c.prompt for c in stub.calls if c.step_path.endswith("/implement")]
    assert prompts[0] == "n=1 max=5 first=true prev=none"
    assert prompts[1] == "n=2 max=5 first=false prev=fail"
    assert [c.step_path for c in stub.calls][:2] == ["build[1]/implement", "build[1]/check"]
    assert out["after"].output == "pass/3/true"
    events = harness.events(EventType.LOOP_ITERATION)
    assert [e.data["n"] for e in events] == [1, 2, 3] and events[0].data["max"] == 5
    st = harness.statuses(g.run.run_id)
    assert st["build[1]/implement"] == "succeeded" and st["build[3]/check"] == "succeeded"
    assert harness.record(g.run.run_id).steps["build[2]/check"].iteration == 2
    assert g.scope.views["build"].body_ids == frozenset({"implement", "check"})


async def test_loop_exhaustion_fail_and_continue(harness: Harness) -> None:
    body = """
    loop:
      max_iterations: 2
      until: steps.c.output == 'never'
      ON_EXHAUSTED
      steps:
        - {id: c, shell: "ok:x"}
"""
    for mode, status in (("fail", "failed"), ("continue", "succeeded")):
        harness.sink.clear()
        text = wf(
            "  - id: l\n"
            + body.replace("ON_EXHAUSTED", f"on_exhausted: {mode}")
            + "  - {id: after, needs: [l], shell: ok}"
        )
        harness.workflow("t", text)
        g = make_graph_harness(harness, harness.load("t"))
        out = await run_graph(g.graph, g.scope, g.ctx)
        rec = out["l"].record
        assert rec.status.value == status
        assert rec.loop and rec.loop.iterations == 2 and rec.loop.converged is False
        if mode == "fail":
            assert rec.error and "exhausted" in rec.error.message
            assert out["after"].record.skip_reason == "upstream_failed"
        else:
            assert out["l"].output == {"c": "x"}
            assert g.scope.views["l"].resolve("converged") is False


async def test_loop_fixed_repetition_and_body_failure(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - id: fixed
    loop: {max_iterations: 3, steps: [{id: c, shell: "ok:{{ iteration.n }}"}]}
  - id: broken
    loop: {max_iterations: 3, steps: [{id: d, shell: fail}]}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    out = await run_graph(g.graph, g.scope, g.ctx)
    fixed = out["fixed"].record
    assert fixed.status is StepStatus.SUCCEEDED and fixed.loop and fixed.loop.iterations == 3
    assert out["fixed"].output == {"c": "3"}
    broken = out["broken"].record
    assert broken.status is StepStatus.FAILED and broken.loop and broken.loop.iterations == 1
    assert broken.error and "iteration 1" in broken.error.message
    assert g.leaf.calls["broken[1]/d"] == 1 and "broken[2]/d" not in g.leaf.calls


async def test_loop_self_session_continues_previous_iteration(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - id: l
    loop:
      max_iterations: 3
      steps:
        - {id: imp, session: imp, prompt: "go"}
"""),
    )
    stub = StubProvider()
    g = make_graph_harness(harness, harness.load("t"), providers={"claude": stub})
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert out["l"].record.status is StepStatus.SUCCEEDED
    assert [c.resume_session for c in stub.calls] == [None, "stub:l[1]/imp:1", "stub:l[2]/imp:1"]


# --------------------------------------------------------------------------------------------------
# each
# --------------------------------------------------------------------------------------------------


async def test_each_basic_scope_vars_and_output(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            """
  - id: fan
    each: inputs.items
    as: thing
    steps:
      - {id: work, shell: "ok:{{ thing.name }}-{{ each.index }}/{{ each.total }}"}
      - {id: more, needs: [work], shell: "ok:{{ steps.work.output | upper }}"}
  - id: after
    needs: [fan]
    shell: "ok:{{ steps.fan.output | length }}:{{ steps.fan.output[1].more }}:{{ steps.fan.items[0].status }}"
""",
            inputs="  items: {type: array, default: []}",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"))
    g.scope.inputs = {"items": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
    out = await run_graph(g.graph, g.scope, g.ctx)
    rec = out["fan"].record
    assert rec.status is StepStatus.SUCCEEDED, rec.error
    assert out["fan"].output == [
        {"work": "a-0/3", "more": "A-0/3"},
        {"work": "b-1/3", "more": "B-1/3"},
        {"work": "c-2/3", "more": "C-2/3"},
    ]
    assert rec.each and (rec.each.total, rec.each.succeeded, rec.each.failed) == (3, 3, 0)
    assert out["after"].output == "3:B-1/3:succeeded"
    run = harness.record(g.run.run_id)
    item_rec = run.steps["fan[1]/work"]
    assert item_rec.item_index == 1 and item_rec.item_sha256 and len(item_rec.item_sha256) == 64
    assert run.steps["fan[1]/more"].item_sha256 == item_rec.item_sha256
    assert [e.data["index"] for e in harness.events(EventType.EACH_ITEM)] == [0, 1, 2]


async def test_each_empty_and_non_list(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - id: empty
    each: "[]"
    steps: [{id: w1, shell: ok}]
  - id: bad
    each: "'abc'"
    steps: [{id: w2, shell: ok}]
  - id: bad2
    each: "{'a': 1}"
    steps: [{id: w3, shell: ok}]
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert out["empty"].record.status is StepStatus.SUCCEEDED and out["empty"].output == []
    assert out["empty"].record.each and out["empty"].record.each.total == 0
    assert out["bad"].record.status is StepStatus.FAILED
    assert out["bad"].record.error and "must evaluate to a list" in out["bad"].record.error.message
    assert out["bad2"].record.status is StepStatus.FAILED
    assert g.leaf.started == []


async def test_a_human_veto_always_drains_even_under_continue(harness: Harness) -> None:
    """`on_reject: fail` halts new work regardless of `on_step_failure: continue`.

    `continue` exists to triage machine failures; it must not quietly override an operator saying
    no. A rejection is recorded FAILED with `error.type == "rejected"`, which always drains.
    """
    harness.workflow(
        "t",
        wf(
            """
  - {id: gate, approve: {message: "ok?", on_reject: fail}}
  - {id: other, shell: ok}
  - {id: later, needs: [other], shell: ok}
""",
            defaults="  on_step_failure: continue",
        ),
    )
    g = make_graph_harness(
        harness, harness.load("t"), prompt=FakePrompt(ApprovalAnswer(False, "no thanks"))
    )
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert out["gate"].record.status is StepStatus.FAILED
    assert "later" not in g.leaf.started, "a human veto must stop new work even under continue"


async def test_on_step_failure_continue_applies_inside_each_bodies(harness: Harness) -> None:
    """``defaults.on_step_failure: continue`` reaches composite bodies.

    ``run_graph`` runs every sibling list — including the body of ``each:``/``loop:``/``include:``
    — and ``RunContext.keep_going`` is read from the ROOT workflow, so the policy is global.
    ``later`` is queued *after* the failure settles, so it discriminates: under ``drain`` it would
    be skipped ``run_failed``.
    """
    body = """
  - id: fan
    each: "[1, 2]"
    on_failure: continue
    steps:
      - {id: boom, shell: "{{ 'fail' if item == 2 else 'ok' }}"}
      - {id: first, shell: ok}
      - {id: later, needs: [first], shell: ok}
"""
    harness.workflow("t", wf(body, defaults="  on_step_failure: continue"))
    g = make_graph_harness(harness, harness.load("t"))
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert "fan[1]/later" in g.leaf.started, f"queued-after step must run: {g.leaf.started}"
    assert out["fan"].items is not None and out["fan"].items[1]["status"] == "failed"


async def test_drain_is_the_control_inside_each_bodies(harness: Harness) -> None:
    """The same body under the default ``drain``: ``later`` never starts (the control case)."""
    body = """
  - id: fan
    each: "[1, 2]"
    on_failure: continue
    steps:
      - {id: boom, shell: "{{ 'fail' if item == 2 else 'ok' }}"}
      - {id: first, shell: ok}
      - {id: later, needs: [first], shell: ok}
"""
    harness.workflow("t", wf(body))  # no defaults → drain
    g = make_graph_harness(harness, harness.load("t"))
    await run_graph(g.graph, g.scope, g.ctx)
    assert "fan[1]/later" not in g.leaf.started, f"drain must stop it: {g.leaf.started}"
    assert "fan[0]/later" in g.leaf.started  # the healthy item is unaffected


async def test_the_two_continues_are_independent(harness: Harness) -> None:
    """``each.on_failure: continue`` and ``defaults.on_step_failure: continue`` are different
    knobs that share the word — a naming hazard.

    ``each.on_failure`` decides whether a failed ITEM fails the ``each`` step.
    ``defaults.on_step_failure`` decides whether a failed STEP stops its independent siblings.
    Here the run-level one is ``continue`` while ``each.on_failure`` keeps its ``fail`` default —
    so the ``each`` step still fails even though sibling branches kept going.
    """
    harness.workflow(
        "t",
        wf(
            """
  - id: fan
    each: "[1, 2]"
    steps:
      - {id: boom, shell: "{{ 'fail' if item == 2 else 'ok' }}"}
  - id: after_fan
    needs: [fan]
    shell: ok
  - id: elsewhere
    shell: ok
""",
            defaults="  on_step_failure: continue",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"))
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert out["fan"].record.status is StepStatus.FAILED  # each.on_failure defaults to `fail`
    assert out["after_fan"].record.skip_reason == "upstream_failed"  # dependents still skip
    assert out["elsewhere"].record.status is StepStatus.SUCCEEDED  # siblings keep going


async def test_on_step_failure_continue_inside_a_loop_body(harness: Harness) -> None:
    """A failed loop-body step does not stop a step queued after it in the same iteration."""
    harness.workflow(
        "t",
        wf(
            """
  - id: cycle
    loop:
      max_iterations: 1
      on_exhausted: continue
      until: "false"
      steps:
        - {id: boom, shell: fail}
        - {id: first, shell: ok}
        - {id: later, needs: [first], shell: ok}
""",
            defaults="  on_step_failure: continue",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"))
    await run_graph(g.graph, g.scope, g.ctx)
    assert "cycle[1]/later" in g.leaf.started, f"queued-after step must run: {g.leaf.started}"


async def test_each_max_parallel_and_on_failure(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - id: fan
    each: "[1, 2, 3, 4]"
    max_parallel: 2
    on_failure: continue
    steps:
      - {id: w, shell: "{{ 'fail' if item == 2 else 'sleep:0.03' }}"}
  - id: strict
    each: "[1, 2]"
    steps:
      - {id: v, shell: "{{ 'fail' if item == 2 else 'ok' }}"}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    real_leaf = g.leaf
    out = await run_graph(g.graph, g.scope, g.ctx)
    fan = out["fan"].record
    assert fan.status is StepStatus.SUCCEEDED
    assert out["fan"].output == [{"w": "w"}, None, {"w": "w"}, {"w": "w"}]
    assert fan.each and (fan.each.total, fan.each.succeeded, fan.each.failed) == (4, 3, 1)
    assert out["fan"].items is not None and out["fan"].items[1]["status"] == "failed"
    assert "exit code 1" in out["fan"].items[1]["error"]
    assert real_leaf.peak == 2  # items run concurrently, bounded by each.max_parallel
    strict = out["strict"].record
    assert strict.status is StepStatus.FAILED and strict.error and "1 of 2" in strict.error.message


# --------------------------------------------------------------------------------------------------
# include
# --------------------------------------------------------------------------------------------------


async def test_include_with_inputs_outputs_and_lexical_scope(harness: Harness) -> None:
    harness.workflow(
        "block",
        """
rayspec: 1
name: block
inputs:
  target: {type: string, required: true}
  strict: {type: boolean, default: false}
steps:
  - {id: lint, shell: "ok:lint-{{ inputs.target }}-{{ inputs.strict }}"}
  - {id: judge, needs: [lint], prompt: "judge {{ steps.lint.output }}"}
outputs:
  verdict: "{{ steps.judge.output }}"
  lint_ok: "{{ steps.lint.ok }}"
""",
    )
    harness.workflow(
        "t",
        wf("""
  - {id: pre, shell: "ok:src"}
  - id: review
    needs: [pre]
    include: block
    with: {target: "{{ steps.pre.output }}"}
  - id: after
    needs: [review]
    shell: "ok:{{ steps.review.output.verdict }}|{{ steps.review.output.lint_ok }}"
"""),
    )
    stub = StubProvider(script={"steps": {"review/judge": {"text": "fine"}}})
    g = make_graph_harness(harness, harness.load("t"), providers={"claude": stub})
    out = await run_graph(g.graph, g.scope, g.ctx)
    rec = out["review"].record
    assert rec.status is StepStatus.SUCCEEDED, rec.error
    assert out["review"].output == {"verdict": "fine", "lint_ok": True}
    assert out["after"].output == "fine|true"
    assert stub.calls[0].step_path == "review/judge"
    assert stub.calls[0].prompt == "judge lint-src-false"
    st = harness.statuses(g.run.run_id)
    assert st["review/lint"] == "succeeded" and st["review/judge"] == "succeeded"


async def test_include_missing_required_input_fails(harness: Harness) -> None:
    harness.workflow(
        "block",
        "rayspec: 1\nname: block\ninputs:\n  target: {type: string, required: true}\nsteps:\n  - {id: x, shell: ok}\n",
    )
    harness.workflow("t", wf("  - {id: review, include: block, with: {target: '{{ none }}'}}"))
    g = make_graph_harness(harness, harness.load("t"))
    out = await run_graph(g.graph, g.scope, g.ctx)
    rec = out["review"].record
    assert rec.status is StepStatus.FAILED and rec.error and rec.error.type == "with"


async def test_stop_succeeded_and_failed_status(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: halt, stop: {status: failed, reason: boom}}"))
    g = make_graph_harness(harness, harness.load("t"))
    with pytest.raises(RunStopped) as info:
        await run_graph(g.graph, g.scope, g.ctx)
    assert info.value.status == "failed" and info.value.reason == "boom"
    assert harness.statuses(g.run.run_id)["halt"] == "succeeded"
    assert harness.record(g.run.run_id).steps["halt"].output_ref


async def test_composite_timeout_marks_body_interrupted(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - id: l
    timeout: 0.1
    loop: {max_iterations: 2, steps: [{id: c, shell: hang}]}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    with anyio.fail_after(5):
        out = await run_graph(g.graph, g.scope, g.ctx)
    rec = out["l"].record
    assert rec.status is StepStatus.FAILED and rec.error and rec.error.type == "timeout"
    assert harness.statuses(g.run.run_id)["l[1]/c"] == "interrupted"


async def test_each_two_items_stopping_concurrently_is_one_stop(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - id: fan
    each: "[1, 2]"
    steps:
      - {id: halt, stop: {status: cancelled, reason: "halt {{ item }}"}}
  - {id: after, needs: [fan], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    with pytest.raises(RunStopped) as info:
        await run_graph(g.graph, g.scope, g.ctx)
    assert info.value.status == "cancelled" and (info.value.reason or "").startswith("halt ")
    run = harness.record(g.run.run_id)
    fan = run.steps["fan"]
    assert fan.status is StepStatus.INTERRUPTED and fan.skip_reason == "stopped"
    assert fan.error is None or "TaskGroup" not in fan.error.message
    assert run.steps["after"].status is StepStatus.SKIPPED
    assert run.steps["after"].skip_reason == "stopped"
    assert run.steps["fan[0]/halt"].status is StepStatus.SUCCEEDED


async def test_each_two_gates_pausing_concurrently_pause_once(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - id: fan
    each: "[1, 2]"
    steps:
      - {id: gate, approve: "ok {{ item }}?"}
  - {id: after, needs: [fan], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(interactive=False))
    with pytest.raises(RunPaused) as info:
        await run_graph(g.graph, g.scope, g.ctx)
    run = harness.record(g.run.run_id)
    assert run.pause is not None and run.pause.decision is None
    assert run.pause.step in {"fan[0]/gate", "fan[1]/gate"}
    assert info.value.step_path == run.pause.step and info.value.token == run.pause.token
    assert g.ctx.paused is not None and g.ctx.paused.step_path == run.pause.step
    assert run.steps["fan"].status is StepStatus.PAUSED
    assert run.steps[run.pause.step].status is StepStatus.PAUSED
    assert "after" not in run.steps  # runs after resume, never skipped
    assert len(harness.events(EventType.RUN_PAUSED)) == 1


async def test_sibling_gates_prompt_one_at_a_time(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: g1, approve: "one?"}
  - {id: g2, approve: "two?"}
  - {id: after, needs: [g1, g2], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    rt = g.ctx.runtime
    active = 0
    peak = 0
    seen: list[tuple[str, bool]] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAnswer | None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.append((request.step_path, rt.gate_open))
        await anyio.sleep(0.05)
        active -= 1
        return ApprovalAnswer(True, request.step_path)

    g.ctx.approval_prompt = prompt
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert peak == 1  # the second gate waits for the first decision
    assert sorted(p for p, _ in seen) == ["g1", "g2"]
    assert all(gate_open is False for _, gate_open in seen)
    assert rt.gate_open
    assert out["after"].record.status is StepStatus.SUCCEEDED


async def test_sibling_gates_non_interactive_pause_once(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: g1, approve: "one?"}
  - {id: g2, approve: "two?"}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(interactive=False))
    with pytest.raises(RunPaused) as info:
        await run_graph(g.graph, g.scope, g.ctx)
    run = harness.record(g.run.run_id)
    assert run.pause is not None and run.pause.step == info.value.step_path
    assert g.ctx.paused is info.value
    assert len(harness.events(EventType.RUN_PAUSED)) == 1
    other = "g2" if run.pause.step == "g1" else "g1"
    assert run.steps[run.pause.step].status is StepStatus.PAUSED
    assert run.steps[other].status in {StepStatus.PAUSED, StepStatus.INTERRUPTED}
    if run.steps[other].status is StepStatus.INTERRUPTED:
        assert run.steps[other].skip_reason == "paused"

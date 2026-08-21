"""The central guarantee: an approval class that forbids automatic approval always holds.

Every one of these drives a real gate through the engine — the rules are enforced where the
decision is made, not where the flag is parsed, so no future caller can route around them.
"""

from __future__ import annotations

import pytest

from rayspec.engine.approval import ApprovalAnswer, ApprovalRequest
from rayspec.engine.approval_classes import ApprovalClasses, ClassRules
from rayspec.engine.context import RunOptions
from rayspec.schema import RunStatus, StepStatus
from rayspec.store.model import Decision

from .conftest import GateRun, Gates, Tree

pytestmark = pytest.mark.anyio

LOCKED = ClassRules(allow_yes=False)
TTY_ONLY = ClassRules(allow_yes=False, require_tty=True)


def ship(tree: Tree, *, gate: str = "      class: release\n") -> None:
    tree.workflow(
        "ship",
        "rayspec: 1\nname: ship\nsteps:\n"
        "  - id: build\n    shell: echo built\n"
        "  - id: gate\n    needs: [build]\n    approve:\n      message: ship it?\n"
        + gate
        + "  - id: publish\n    needs: [gate]\n    shell: echo published\n",
    )


def locked(**kw: object) -> ApprovalClasses:
    return ApprovalClasses(rules={"release": LOCKED}, **kw)  # type: ignore[arg-type]


class Asker:
    """An approval prompt that always answers the same way (and counts the asking)."""

    def __init__(self, answer: ApprovalAnswer | None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, request: ApprovalRequest) -> ApprovalAnswer | None:
        self.asked.append(request.step_path)
        return self.answer


def assert_held(run: GateRun) -> None:
    """The gate was not approved: the run is paused and nothing downstream ran."""
    assert run.result.status is RunStatus.PAUSED, run.result.reason
    assert run.result.exit_code == 3
    assert run.statuses()["gate"] == "paused"
    assert run.record.steps.get("publish") is None or (
        run.record.steps["publish"].status is not StepStatus.SUCCEEDED
    )


async def test_allow_yes_false_survives_every_waiver_at_once(
    tree: Tree, gates: Gates, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes and --dry-run and --approve-class and auto_if and a plausible environment
    variable, all together, on an interactive run. The gate still holds."""
    ship(tree, gate="      class: release\n      auto_if: 'true'\n")
    for name in ("RAYSPEC_YES", "RAYSPEC_APPROVE", "RAYSPEC_APPROVE_CLASS", "RAYSPEC_ALLOW_YES"):
        monkeypatch.setenv(name, "1")
    options = RunOptions(
        yes=True,
        dry_run=True,
        exec_shell=True,
        interactive=True,
        approval_classes=locked(pre_approved=frozenset({"release"})),
    )
    run = await gates.run("ship", options=options)
    assert_held(run)
    assert any("does not approve approval class 'release'" in w for w in run.warnings())


async def test_a_human_at_the_terminal_still_approves_a_locked_class(
    tree: Tree, gates: Gates
) -> None:
    ship(tree)
    asker = Asker(ApprovalAnswer(True, "shipping"))
    run = await gates.run(
        "ship",
        options=RunOptions(interactive=True, approval_classes=locked()),
        prompt=asker,
    )
    assert run.result.status is RunStatus.SUCCEEDED
    assert asker.asked == ["gate"]
    assert run.decision("gate")["by"] == "tty"


async def test_rayspec_approve_still_decides_a_locked_class(tree: Tree, gates: Gates) -> None:
    ship(tree)
    first = await gates.run(
        "ship", options=RunOptions(interactive=False, approval_classes=locked())
    )
    assert_held(first)
    record = first.record
    assert record.pause is not None
    record.pause.decision = Decision(approved=True, comment="go", by="cli")
    gates.store.save(record)
    second = await gates.run(
        "ship",
        options=RunOptions(interactive=False, approval_classes=locked()),
        resume=first.result.run_id,
    )
    assert second.result.status is RunStatus.SUCCEEDED
    assert second.decision("gate")["by"] == "cli"


async def test_require_tty_refuses_a_decision_recorded_out_of_band(
    tree: Tree, gates: Gates
) -> None:
    ship(tree)
    classes = ApprovalClasses(rules={"release": TTY_ONLY})
    first = await gates.run("ship", options=RunOptions(interactive=False, approval_classes=classes))
    assert_held(first)
    record = first.record
    assert record.pause is not None
    record.pause.decision = Decision(approved=True, comment="go", by="cli")
    gates.store.save(record)
    second = await gates.run(
        "ship",
        options=RunOptions(interactive=False, approval_classes=classes),
        resume=first.result.run_id,
    )
    assert_held(second)
    assert any("requires a terminal" in w for w in second.warnings())
    # the refused decision does not linger: the gate asks again under a fresh token
    assert second.record.pause is not None
    assert second.record.pause.token == "gate#2"
    assert second.record.pause.decision is None


async def test_require_tty_honours_a_rejection_recorded_out_of_band(
    tree: Tree, gates: Gates
) -> None:
    """A class constrains approving, never rejecting: a gate nobody can reject is a trap."""
    ship(tree)
    classes = ApprovalClasses(rules={"release": TTY_ONLY})
    first = await gates.run("ship", options=RunOptions(interactive=False, approval_classes=classes))
    record = first.record
    assert record.pause is not None
    record.pause.decision = Decision(approved=False, comment="not yet", by="cli")
    gates.store.save(record)
    second = await gates.run(
        "ship",
        options=RunOptions(interactive=False, approval_classes=classes),
        resume=first.result.run_id,
    )
    assert second.result.status is RunStatus.CANCELLED
    assert second.statuses()["gate"] == "rejected"


async def test_require_tty_does_not_ask_a_replaced_prompt(tree: Tree, gates: Gates) -> None:
    ship(tree)
    asker = Asker(ApprovalAnswer(True, "approved elsewhere"))
    run = await gates.run(
        "ship",
        options=RunOptions(
            interactive=True,
            approval_classes=ApprovalClasses(rules={"release": TTY_ONLY}, terminal_prompt=False),
        ),
        prompt=asker,
    )
    assert_held(run)
    assert asker.asked == []
    assert any("configured approval extension was not asked" in w for w in run.warnings())


async def test_a_gate_without_a_class_is_still_waived_by_yes(tree: Tree, gates: Gates) -> None:
    ship(tree, gate="")
    run = await gates.run("ship", options=RunOptions(yes=True, approval_classes=locked()))
    assert run.result.status is RunStatus.SUCCEEDED
    assert run.decision("gate")["by"] == "--yes"


async def test_a_class_the_policy_says_nothing_about_is_waived_by_yes(
    tree: Tree, gates: Gates
) -> None:
    ship(tree, gate="      class: chore\n")
    run = await gates.run("ship", options=RunOptions(yes=True, approval_classes=locked()))
    assert run.result.status is RunStatus.SUCCEEDED
    assert run.decision("gate")["by"] == "--yes"


# --------------------------------------------------------------------------------------------
# Failing open is allowed; failing open QUIETLY is not
# --------------------------------------------------------------------------------------------


async def test_a_class_the_policy_does_not_define_warns_that_the_gate_is_not_held(
    tree: Tree, gates: Gates
) -> None:
    """One typo on either side — `relase` in the policy, `release` in the workflow — used to
    remove the operator's control silently. The gate is still open, but it says so."""
    ship(tree)
    classes = ApprovalClasses(rules={"relase": LOCKED})
    run = await gates.run("ship", options=RunOptions(yes=True, approval_classes=classes))
    assert run.result.status is RunStatus.SUCCEEDED  # unchanged: an unknown class is permissive
    warnings = run.warnings()
    assert any("not defined by the operator policy" in w for w in warnings), warnings
    assert any("'release'" in w for w in warnings), warnings


async def test_a_class_with_no_policy_at_all_warns_too(tree: Tree, gates: Gates) -> None:
    ship(tree)
    run = await gates.run("ship", options=RunOptions(yes=True))
    assert run.result.status is RunStatus.SUCCEEDED
    assert any("no operator policy is in force" in w for w in run.warnings()), run.warnings()


async def test_a_held_class_does_not_warn_about_being_unheld(tree: Tree, gates: Gates) -> None:
    ship(tree)
    run = await gates.run("ship", options=RunOptions(interactive=False, approval_classes=locked()))
    assert_held(run)
    assert not any("not held" in w for w in run.warnings()), run.warnings()


async def test_a_gate_without_a_class_says_nothing_about_policy(tree: Tree, gates: Gates) -> None:
    ship(tree, gate="")
    run = await gates.run("ship", options=RunOptions(yes=True))
    assert run.warnings() == []


async def test_a_gate_that_simply_pauses_under_a_class_says_which_rule_holds_it(
    tree: Tree, gates: Gates
) -> None:
    """No waiver was asked for, so nothing was refused — but the reason this gate cannot be
    answered the usual way belongs in the event stream, not only in the operator's memory."""
    ship(tree)
    run = await gates.run(
        "ship",
        options=RunOptions(
            interactive=False, approval_classes=ApprovalClasses(rules={"release": TTY_ONLY})
        ),
    )
    assert_held(run)
    held = [w for w in run.warnings() if "'release'" in w]
    assert held, run.warnings()
    assert any("require_tty: true" in w for w in held), held
    assert any("rayspec resume" in w for w in held), held


async def test_require_tty_refuses_a_prompt_asked_without_a_terminal(
    tree: Tree, gates: Gates
) -> None:
    """The built-in prompt, an interactive run, nothing replaced — and no terminal. The rule
    checks the terminal itself instead of trusting the caller's flags."""
    ship(tree)
    asker = Asker(ApprovalAnswer(True, "from cron"))
    run = await gates.run(
        "ship",
        options=RunOptions(
            interactive=True,
            approval_classes=ApprovalClasses(rules={"release": TTY_ONLY}),
        ),
        prompt=asker,
    )
    assert_held(run)
    assert asker.asked == []
    assert any("this process has none" in w for w in run.warnings()), run.warnings()


async def test_require_tty_asks_when_there_is_a_terminal(
    tree: Tree, gates: Gates, monkeypatch: pytest.MonkeyPatch
) -> None:
    ship(tree)
    monkeypatch.setattr("rayspec.engine.executors.approve.at_a_terminal", lambda: True)
    asker = Asker(ApprovalAnswer(True, "shipping"))
    run = await gates.run(
        "ship",
        options=RunOptions(
            interactive=True,
            approval_classes=ApprovalClasses(rules={"release": TTY_ONLY}),
        ),
        prompt=asker,
    )
    assert run.result.status is RunStatus.SUCCEEDED
    assert asker.asked == ["gate"]
    assert run.decision("gate")["by"] == "tty"

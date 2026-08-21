"""``auto_if:`` — approving a gate by condition, and the ceiling it can never lift."""

from __future__ import annotations

import pytest

from rayspec.engine.approval_classes import ApprovalClasses, ClassRules
from rayspec.engine.context import RunOptions
from rayspec.schema import RunStatus

from .conftest import Gates, Tree

pytestmark = pytest.mark.anyio


def gated(tree: Tree, *, auto_if: str, class_: str = "", failures: str = "0") -> None:
    class_line = f"      class: {class_}\n" if class_ else ""
    tree.workflow(
        "gated",
        "rayspec: 1\nname: gated\nsteps:\n"
        f"  - id: tests\n    shell: printf '{{\"failures\":{failures}}}'\n"
        "    output_schema:\n      type: object\n"
        "      properties: { failures: { type: integer } }\n"
        "  - id: gate\n    needs: [tests]\n    approve:\n      message: ship it?\n"
        f"{class_line}      auto_if: {auto_if}\n"
        "  - id: publish\n    needs: [gate]\n    shell: echo published\n",
    )


async def test_a_true_condition_approves_without_asking(tree: Tree, gates: Gates) -> None:
    gated(tree, auto_if="steps.tests.output.failures == 0")
    run = await gates.run("gated", options=RunOptions(interactive=False))
    assert run.result.status is RunStatus.SUCCEEDED
    assert run.decision("gate")["by"] == "auto_if"


async def test_a_false_condition_falls_through_to_the_gate(tree: Tree, gates: Gates) -> None:
    gated(tree, auto_if="steps.tests.output.failures == 0", failures="3")
    run = await gates.run("gated", options=RunOptions(interactive=False))
    assert run.result.status is RunStatus.PAUSED
    assert run.statuses()["gate"] == "paused"


async def test_yes_outranks_a_false_condition(tree: Tree, gates: Gates) -> None:
    """`auto_if` only ever ADDS an automatic approval; it is not a veto."""
    gated(tree, auto_if="steps.tests.output.failures == 0", failures="3")
    run = await gates.run("gated", options=RunOptions(yes=True, interactive=False))
    assert run.result.status is RunStatus.SUCCEEDED
    assert run.decision("gate")["by"] == "--yes"


async def test_a_condition_never_escalates_a_locked_class(tree: Tree, gates: Gates) -> None:
    gated(tree, auto_if="steps.tests.output.failures == 0", class_="release")
    run = await gates.run(
        "gated",
        options=RunOptions(
            interactive=False,
            approval_classes=ApprovalClasses(rules={"release": ClassRules(allow_yes=False)}),
        ),
    )
    assert run.result.status is RunStatus.PAUSED
    assert run.statuses()["gate"] == "paused"
    assert any("auto_if does not approve approval class 'release'" in w for w in run.warnings())


async def test_a_condition_that_cannot_be_evaluated_fails_the_gate(
    tree: Tree, gates: Gates
) -> None:
    """Fail closed: an expression that does not produce a bool holds the gate shut, loudly."""
    gated(tree, auto_if="steps.tests.output.failures")
    run = await gates.run("gated", options=RunOptions(interactive=False))
    assert run.result.status is RunStatus.FAILED
    assert run.statuses()["gate"] == "failed"
    error = run.record.steps["gate"].error
    assert error is not None and error.type == "render"


async def test_a_locked_class_does_not_even_evaluate_the_condition(
    tree: Tree, gates: Gates
) -> None:
    """The same expression that fails the gate above (it is not a bool). Under a locked
    class the gate merely pauses: the expression was never evaluated."""
    gated(tree, auto_if="steps.tests.output.failures", class_="release")
    run = await gates.run(
        "gated",
        options=RunOptions(
            interactive=False,
            approval_classes=ApprovalClasses(rules={"release": ClassRules(allow_yes=False)}),
        ),
    )
    assert run.result.status is RunStatus.PAUSED
    assert run.statuses()["gate"] == "paused"

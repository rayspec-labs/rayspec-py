"""engine.graph: StepGraph construction + the join truth table (plan §2.2)."""

from __future__ import annotations

import pytest

from rayspec.engine.errors import GraphError
from rayspec.engine.graph import JoinDecision, StepGraph, join_decision
from rayspec.schema import StepStatus, parse_step
from rayspec.store.model import StepRecord


def rec(status: str, *, tolerated: bool = False, sid: str = "x") -> StepRecord:
    return StepRecord(
        path=sid, id=sid, kind="shell", status=StepStatus(status), tolerated=tolerated
    )


S = rec("succeeded")
T = rec("failed", tolerated=True)
K = rec("skipped")
F = rec("failed")
I = rec("interrupted")  # noqa: E741
R = rec("rejected")


def step(join: str = "all", needs: tuple[str, ...] = ("a",)):
    return parse_step({"id": "s", "needs": list(needs), "join": join, "shell": "true"})


# (join, needs outcomes, draining) -> (run, skip_reason)
TABLE = [
    # all succeeded
    ("all", [S, S], False, True, None),
    ("any", [S, S], False, True, None),
    ("always", [S, S], False, True, None),
    # tolerated failures count as succeeded
    ("all", [S, T], False, True, None),
    ("any", [T], False, True, None),
    # ≥1 skipped, rest succeeded, none failed
    ("all", [S, K], False, False, "upstream_skipped"),
    ("any", [S, K], False, True, None),
    ("always", [S, K], False, True, None),
    # all skipped
    ("all", [K, K], False, False, "upstream_skipped"),
    ("any", [K, K], False, False, "upstream_skipped"),
    ("always", [K, K], False, True, None),
    # ≥1 failed (untolerated)
    ("all", [S, F], False, False, "upstream_failed"),
    ("any", [S, F], False, False, "upstream_failed"),
    ("always", [S, F], False, True, None),
    ("all", [K, F], False, False, "upstream_failed"),
    ("any", [I], False, False, "upstream_failed"),
    ("any", [R], False, False, "upstream_failed"),
    # run draining / cancelled
    ("all", [S, S], True, False, "run_failed"),
    ("any", [S, K], True, False, "run_failed"),
    ("always", [S, S], True, True, None),
    ("always", [F], True, True, None),
    ("all", [S, F], True, False, "upstream_failed"),
    # no needs → ready at start (draining still applies to all/any)
    ("all", [], False, True, None),
    ("any", [], False, True, None),
    ("all", [], True, False, "run_failed"),
    ("always", [], True, True, None),
]


@pytest.mark.parametrize(("join", "needs", "draining", "run", "reason"), TABLE)
def test_join_truth_table(join, needs, draining, run, reason) -> None:
    decision = join_decision(step(join), needs, draining=draining)
    assert isinstance(decision, JoinDecision)
    assert decision.run is run
    assert decision.skip_reason == reason


def test_step_graph_needs_dependents_roots() -> None:
    steps = [
        parse_step({"id": "a", "shell": "1"}),
        parse_step({"id": "b", "needs": ["a"], "shell": "1"}),
        parse_step({"id": "c", "needs": ["a"], "shell": "1"}),
        parse_step({"id": "d", "needs": ["b", "c"], "shell": "1"}),
        parse_step({"id": "e", "shell": "1"}),
    ]
    g = StepGraph.from_steps(steps)
    assert g.roots == ("a", "e")
    assert g.needs["d"] == ("b", "c")
    assert g.dependents["a"] == ("b", "c")
    assert g.dependents["d"] == ()
    assert g.ids == ("a", "b", "c", "d", "e")
    assert g.step("b").id == "b"


def test_step_graph_rejects_non_sibling_needs_and_cycles() -> None:
    with pytest.raises(GraphError, match="unknown sibling"):
        StepGraph.from_steps([parse_step({"id": "a", "needs": ["zz"], "shell": "1"})])
    with pytest.raises(GraphError, match="cycle"):
        StepGraph.from_steps(
            [
                parse_step({"id": "a", "needs": ["b"], "shell": "1"}),
                parse_step({"id": "b", "needs": ["a"], "shell": "1"}),
            ]
        )
    with pytest.raises(GraphError, match="duplicate"):
        StepGraph.from_steps(
            [parse_step({"id": "a", "shell": "1"}), parse_step({"id": "a", "shell": "1"})]
        )

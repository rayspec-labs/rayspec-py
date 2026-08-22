"""The approval-class rules, from a `policy.yaml` on disk to a gate the CLI refuses to open.

Two halves of one release were each honest about themselves and never met. The approval side
shipped the rules, the executor that enforces them and a bold note saying "rayspec does not read
an operator policy yet"; the policy side shipped a layered, provenance-carrying document loader.
``operator_policy()`` was the single line between them and it was ``return None``, so
``allow_yes: false`` — the guarantee the page leads with — could not be written anywhere the
shipped CLI would read. A workflow with ``approve: {class: release, auto_if: "true"}`` and a
``.rayspec/policy.yaml`` holding that class exited 0, ``decision: approved``, downstream step run.

Every test that covered it stubbed the seam (``monkeypatch.setattr(..., "policy_class_rules")``),
which is why review and a green suite saw nothing: a mock of the one function that was not
implemented answers every question except the one being asked. So nothing here is mocked. The
acceptance test writes a real policy file, invokes the real ``rayspec run`` with **every** waiver
at once, and asserts on the file the downstream step would have written — the two halves cannot
drift apart again without this failing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from rayspec.cli.app import app
from rayspec.cli.commands.run import approval_classes_for, operator_policy, policy_class_rules
from rayspec.engine.approval_classes import ApprovalClasses, rules_from_policy
from rayspec.policy import load_policy
from rayspec.policy.controls import policy_controls
from rayspec.policy.model import Policy
from rayspec.schema import SchemaError

from .conftest import Tree, validated

runner = CliRunner()

#: The reproduction: a gate that names a class AND approves itself, with a step behind it that
#: leaves a trace on disk. "The gate paused" and "the thing behind it did not happen" are two
#: different claims and only the second one is the guarantee.
SHIP = """rayspec: 1
name: ship
isolation: none
inputs:
  marker:
    type: string
steps:
  - id: build
    shell: echo built
  - id: gate
    needs: [build]
    approve:
      message: publish?
      class: release
      auto_if: "true"
  - id: publish
    needs: [gate]
    shell: touch "{{ inputs.marker }}"
"""

HELD = """approvals:
  classes:
    release:
      allow_yes: false
"""


class Project:
    """A project root, an isolated rayspec home and the marker the last step would write."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path / "proj"
        self.home = tmp_path / "userhome"
        (self.root / ".rayspec" / "workflows").mkdir(parents=True)
        self.home.mkdir()
        (self.root / ".rayspec" / "workflows" / "ship.yaml").write_text(SHIP, encoding="utf-8")
        self.marker = tmp_path / "published.marker"
        monkeypatch.setenv("RAYSPEC_HOME", str(self.home))
        monkeypatch.delenv("RAYSPEC_POLICY", raising=False)

    def policy(self, text: str, *, user: bool = False) -> Path:
        path = (self.home if user else self.root / ".rayspec") / "policy.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def run(self, *args: str) -> Result:
        return runner.invoke(
            app,
            [
                "run",
                "ship",
                "--root",
                str(self.root),
                "--input",
                f"marker={self.marker}",
                "--no-interactive",
                *args,
            ],
        )

    def cli(self, *args: str) -> Result:
        return runner.invoke(app, [*args, "--root", str(self.root)])


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    return Project(tmp_path, monkeypatch)


def run_id_of(res: Result) -> str:
    match = re.search(r"\b\d{8}-\d{6}-[a-z0-9]{4}\b", res.output)
    assert match is not None, res.output
    return match.group(0)


#: Every automatic approval rayspec has, asked for at once — `auto_if: "true"` is already in the
#: workflow, so this is the full set. ``--exec-shell`` is not a waiver: it is what makes the step
#: behind the gate real under ``--dry-run``, which is what the assertion is about.
EVERY_WAIVER = ("--yes", "--dry-run", "--exec-shell", "--approve-class", "release")

#: The same waivers without ``--dry-run``, for the tests that go on to RESUME the paused run: a
#: resume inherits the run's ``dry_run`` and not its ``--exec-shell``, so a downstream shell step
#: would be simulated and "the marker is there" would stop meaning "the step ran".
WAIVERS_THAT_SURVIVE_A_RESUME = ("--yes", "--approve-class", "release")


# -- the acceptance test ---------------------------------------------------------------------


def test_a_policy_file_holds_the_gate_against_every_waiver_at_once(project: Project) -> None:
    """THE test. A real policy.yaml, the real CLI, every waiver — and the gate still pauses.

    ``--exec-shell`` is in the set on purpose: without it ``--dry-run`` skips shell steps, and a
    downstream step that was never going to run proves nothing about the gate in front of it.
    """
    project.policy(HELD)
    res = project.run(*EVERY_WAIVER)
    assert res.exit_code == 3, res.output
    assert not project.marker.exists(), "the step behind the gate ran"
    assert "--yes does not approve approval class 'release'" in res.output
    assert "allow_yes: false" in res.output


def test_the_same_run_without_the_policy_file_publishes(project: Project) -> None:
    """The other half of the acceptance test: it is the FILE doing this, not the flags.

    Without it the identical invocation approves the gate and the downstream step runs — which is
    exactly what the reproduction saw with the policy file in place.
    """
    res = project.run(*EVERY_WAIVER)
    assert res.exit_code == 0, res.output
    assert project.marker.exists()


def test_the_user_layer_holds_it_too(project: Project) -> None:
    """``~/.rayspec/policy.yaml`` is a layer like any other, and the run reads all of them."""
    project.policy(HELD, user=True)
    res = project.run(*EVERY_WAIVER)
    assert res.exit_code == 3, res.output
    assert not project.marker.exists()


def test_a_permissive_layer_cannot_reopen_what_another_layer_held(project: Project) -> None:
    """Most-restrictive-wins, through the CLI: the user file says yes, the project file says no."""
    project.policy(HELD)
    project.policy("approvals:\n  classes:\n    release:\n      allow_yes: true\n", user=True)
    res = project.run(*EVERY_WAIVER)
    assert res.exit_code == 3, res.output
    assert not project.marker.exists()


def test_resume_yes_does_not_open_it_either(project: Project) -> None:
    """The second half of a run is subject to the policy in force now, gate rules included."""
    project.policy(HELD)
    first = project.run(*WAIVERS_THAT_SURVIVE_A_RESUME)
    assert first.exit_code == 3, first.output
    res = project.cli("resume", run_id_of(first), "--no-interactive", "--yes")
    assert res.exit_code == 3, res.output
    assert not project.marker.exists()


def test_a_person_answering_this_one_gate_still_works(project: Project) -> None:
    """``allow_yes: false`` is a gate, not a wall — a control that blocks the permitted case is
    its own defect. A recorded human decision goes through and the run finishes."""
    project.policy(HELD)
    first = project.run(*WAIVERS_THAT_SURVIVE_A_RESUME)
    assert first.exit_code == 3, first.output
    res = project.cli("approve", run_id_of(first), "ship it")
    assert res.exit_code == 0, res.output
    assert project.marker.exists()


def test_require_tty_refuses_the_recorded_decision_as_well(project: Project) -> None:
    """The stricter rule, from the same file: ``rayspec approve`` can be scripted."""
    project.policy(HELD.rstrip("\n") + "\n      require_tty: true\n")
    first = project.run(*WAIVERS_THAT_SURVIVE_A_RESUME)
    assert first.exit_code == 3, first.output
    res = project.cli("approve", run_id_of(first), "ship it")
    assert res.exit_code == 3, res.output
    assert "requires a terminal" in res.output
    assert not project.marker.exists()


# -- the seam itself -------------------------------------------------------------------------


def test_operator_policy_reads_the_layers_everything_else_reads(project: Project) -> None:
    """``return None`` is what this test exists to prevent coming back."""
    project.policy(HELD)
    policy = operator_policy(project.root, project.home)
    assert policy is not None
    assert policy.labels == (".rayspec/policy.yaml",)  # the layer, named the way errors name it
    rules = policy_class_rules(project.root, project.home)
    assert rules["release"].allow_yes is False
    classes = approval_classes_for(project.root, project.home)
    assert classes.may_approve_automatically("release") is False
    assert classes.policy_in_force is True


def test_no_policy_file_leaves_the_permissive_default(project: Project) -> None:
    assert operator_policy(project.root, project.home) is None
    assert policy_class_rules(project.root, project.home) == {}
    assert approval_classes_for(project.root, project.home).policy_in_force is False


# -- the document and its merge rule -----------------------------------------------------------


def test_the_document_carries_the_approvals_block() -> None:
    policy = Policy.parse({"approvals": {"classes": {"release": {"allow_yes": False}}}})
    assert policy.approvals.classes["release"].allow_yes is False
    assert policy.approvals.classes["release"].require_tty is False


def test_an_unknown_key_under_a_class_is_a_load_error() -> None:
    """The block is strict like every other: a misspelled rule must not read as an absent one."""
    with pytest.raises(SchemaError, match="allow_yes"):
        Policy.parse({"approvals": {"classes": {"release": {"allow_yez": False}}}})


def test_the_layers_merge_most_restrictively_and_union_the_names(tree: Tree) -> None:
    """``allow_yes: false`` wins over true, ``require_tty: true`` over false, names unite."""
    tree.policy(
        "approvals:\n  classes:\n    release: {allow_yes: false}\n    chore: {require_tty: true}\n"
    )
    tree.policy(
        "approvals:\n  classes:\n    release: {allow_yes: true, require_tty: true}\n"
        "    hotfix: {allow_yes: false}\n",
        user=True,
    )
    merged = load_policy(tree.root, home=tree.home, environ={}).approvals.classes
    assert sorted(merged) == ["chore", "hotfix", "release"]
    assert merged["release"].allow_yes is False  # the project layer, not the wider user one
    assert merged["release"].require_tty is True  # the user layer, which is the stricter one
    assert merged["chore"].allow_yes is True
    assert merged["hotfix"].allow_yes is False


def test_a_class_that_restricts_nothing_is_still_defined(tree: Tree) -> None:
    """A named class the operator left permissive is DEFINED — the gate is waivable, not unheld."""
    tree.policy("approvals:\n  classes:\n    release: {}\n")
    effective = load_policy(tree.root, home=tree.home, environ={})
    classes = ApprovalClasses(rules=rules_from_policy(effective.approvals))
    assert classes.unheld("release") is False
    assert classes.may_approve_automatically("release") is True


# -- an approvals block is a control -----------------------------------------------------------


def test_a_held_class_is_a_control_that_names_its_own_line(tree: Tree) -> None:
    """It governs the run, so it closes the escape hatch beside it and says which line did."""
    tree.policy(HELD)
    effective = load_policy(tree.root, home=tree.home, environ={})
    sources = effective.control_sources()
    assert "approvals.classes" in sources
    assert sources["approvals.classes"][0].line == 4  # the `allow_yes: false` line itself
    controls = {control.key: control for control in policy_controls(effective)}
    assert controls["approvals.classes"].tags
    assert controls["approvals.classes"].sources


def test_a_permissive_class_restricts_nothing_and_is_not_a_control(tree: Tree) -> None:
    """``trust.require: false`` is not a control either; a written key that forbids nothing
    must not close an escape hatch, or the trigger stops meaning anything."""
    tree.policy("approvals:\n  classes:\n    release: {allow_yes: true}\n")
    effective = load_policy(tree.root, home=tree.home, environ={})
    assert "approvals.classes" not in effective.control_sources()


GOVERNED = """rayspec: 1
name: wf
isolation: none
steps:
  - id: think
    agent:
      provider: claude
      model: claude-sonnet-4-5
      access: full
      provider_options:
        claude:
          extra_args: {}
    prompt: hello
"""


def test_an_approvals_block_closes_the_provider_options_escape_hatch(tree: Tree) -> None:
    tree.policy(HELD)
    _, report = validated(tree, GOVERNED)
    assert any("extra_args is refused" in error for error in report.errors), report.errors


# -- and the messages stop claiming there is no policy -----------------------------------------


def test_the_gate_warning_names_the_policy_that_is_in_force(project: Project) -> None:
    """A policy file IS in force here; it just does not define the class the workflow names.
    Saying "no operator policy is in force" two lines under the line that printed its path is
    how a control teaches people to stop reading its warnings."""
    project.policy("approvals:\n  classes:\n    chore: {allow_yes: false}\n")
    res = project.run("--no-interactive")
    assert "no operator policy is in force" not in res.output
    assert "not defined by the operator policy" in res.output


def test_plan_risk_names_the_policy_that_is_in_force(project: Project) -> None:
    project.policy("approvals:\n  classes:\n    chore: {allow_yes: false}\n")
    res = project.cli("plan", "ship", "--risk")
    assert res.exit_code == 0, res.output
    assert "no operator policy in force defines" not in res.output
    assert "the operator policy does not hold approval class 'release'" in res.output


def test_plan_risk_still_says_so_when_there_really_is_no_policy(project: Project) -> None:
    res = project.cli("plan", "ship", "--risk")
    assert res.exit_code == 0, res.output
    assert "no operator policy in force defines approval class 'release'" in res.output


def test_plan_risk_does_not_report_a_gate_the_policy_holds(project: Project) -> None:
    """A gate a class holds shut is a real gate, so it is not a finding."""
    project.policy(HELD)
    res = project.cli("plan", "ship", "--risk")
    assert res.exit_code == 0, res.output
    assert "self-approving-gate" not in res.output


# -- and the one place a gate is reached without a person anywhere near it ----------------------

CASE = """id: happy
workflow: ship
exec_shell: true
inputs:
  marker: "{marker}"
expect:
  status: succeeded
"""


def test_a_checked_in_case_cannot_approve_what_the_policy_holds(project: Project) -> None:
    """``rayspec test --exec-shell`` runs the gated bodies for real, from a file in the
    repository, with nobody at a terminal. The page says the harness takes the same rules; the
    harness reads no policy itself, so this is a test of the CLI that hands them to it."""
    project.policy(HELD)
    case = project.root / ".rayspec" / "tests" / "ship" / "happy.yaml"
    case.parent.mkdir(parents=True)
    case.write_text(CASE.format(marker=project.marker), encoding="utf-8")
    res = project.cli("test", "--exec-shell")
    assert res.exit_code == 1, res.output
    assert not project.marker.exists(), "the gated step ran anyway"

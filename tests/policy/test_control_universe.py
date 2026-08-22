"""The trigger, over every schema a restriction can be written in.

``test_control_trigger.py`` proves the classification total over the AGENT schema. That is one
schema of several, and the artefact a pull request presents as proof of totality is only worth
the universe it is pointed at: the identical restriction spelled one level up —
``defaults.budget_usd``, ``defaults.max_tokens``, ``isolation:`` — was in no partition and no
test, so a workflow could set four real caps and keep the escape hatch wide open beside them.

So the universe here is mechanical rather than chosen. Every model reachable from the workflow
document and from the policy document is walked; each one has to belong to a family, and every
field of every family has to be classified as a control (tags + why), as carried by a control on
its parent, or as restricting nothing (with the reason). Both directions are asserted, so a stale
entry fails as loudly as a missing one — and a nested model added tomorrow fails the reachability
check until someone classifies it. The artefacts on disk and the CLI's own options are held to
the same standard in the last two sections.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from rayspec.policy.controls import (
    AGENT_COMMANDS_CARRIED,
    AGENT_CONTROLS,
    AGENT_MCP_NON_CONTROLS,
    AGENT_NON_CONTROLS,
    AGENT_TOOLS_CARRIED,
    CLI_FLAGS,
    CONTROL_TAGS,
    DEFAULTS_CONTROLS,
    DEFAULTS_NON_CONTROLS,
    INPUT_CARRIED,
    INPUT_NON_CONTROLS,
    POLICY_CONTROL_TAGS,
    POLICY_NON_CONTROLS,
    POLICY_TAGS_FROM_VALUE,
    STEP_APPROVE_NON_CONTROLS,
    STEP_CONTROLS,
    STEP_LOOP_NON_CONTROLS,
    STEP_NON_CONTROLS,
    STEP_RETRY_NON_CONTROLS,
    STEP_STOP_NON_CONTROLS,
    WORKFLOW_CONTROLS,
    WORKFLOW_NON_CONTROLS,
    Carried,
    Restriction,
)
from rayspec.policy.model import Policy
from rayspec.schema.agent import AgentDef, AgentOverride, CommandsSpec, McpServerDef, ToolsSpec
from rayspec.schema.inputs import InputSpec
from rayspec.schema.steps import (
    STEP_MODELS,
    ApproveSpec,
    LoopSpec,
    RetryPolicy,
    StopSpec,
)
from rayspec.schema.workflow import Defaults, Workflow

from .conftest import Tree, validated

# -- the restrictions spelled outside the agent ---------------------------------------------------

WF = """rayspec: 1
name: wf
{workflow}steps:
  - id: think
{step}    agent:
      provider: claude
      model: claude-sonnet-4-5
      access: full
      provider_options:
        claude:
{options}
    prompt: hello
"""


def wf(workflow: str = "isolation: none\n", *, step: str = "", options: str = "") -> str:
    return WF.format(
        workflow=workflow,
        step="".join(f"    {line}\n" for line in step.splitlines()),
        options="\n".join(
            f"          {line}" for line in (options or "extra_args: {}").splitlines()
        ),
    )


def refused(report) -> str:
    """The refusal naming the unreasoned key, or ``""`` when there is none."""
    return next((e for e in report.errors if "extra_args is refused" in e), "")


def test_a_workflow_wide_budget_closes_the_escape_hatch(tree: Tree) -> None:
    """``defaults.budget_usd`` is the same ceiling as the agent's, one level up.

    It is also the only spelling a codex agent has: ``budget_usd`` on the agent is a capability
    not every provider declares, so the run-level cap is where an operator puts one.
    """
    _, report = validated(tree, wf("isolation: none\ndefaults: {budget_usd: 0.01}\n"))
    message = refused(report)
    assert message, report.errors
    assert "defaults.budget_usd" in message


def test_a_workflow_wide_token_cap_closes_the_escape_hatch(tree: Tree) -> None:
    _, report = validated(tree, wf("isolation: none\ndefaults: {max_tokens: 500}\n"))
    assert "defaults.max_tokens" in refused(report), report.errors


def test_a_workflow_wide_clock_closes_the_escape_hatch(tree: Tree) -> None:
    _, report = validated(tree, wf("isolation: none\ndefaults: {timeout_total: 1m}\n"))
    assert "defaults.timeout_total" in refused(report), report.errors


def test_a_step_timeout_closes_the_escape_hatch_for_that_steps_agent(tree: Tree) -> None:
    """A restriction spelled on the step governs the agent that runs under it."""
    _, report = validated(tree, wf(step="timeout: 30s\n"))
    assert "steps.think.timeout" in refused(report), report.errors


def test_a_secret_input_closes_the_escape_hatch(tree: Tree) -> None:
    """``secret: true`` restricts where a value may go; a raw pass-through sits beside it."""
    _, report = validated(
        tree, wf("isolation: none\ninputs:\n  token: {type: string, secret: true}\n")
    )
    assert "inputs.secret" in refused(report), report.errors


def test_workspace_isolation_closes_the_escape_hatch(tree: Tree) -> None:
    """``isolation: worktree`` is the DEFAULT, and it is a real restriction: the run works on a
    copy instead of the checkout a person is sitting in. ``add_dirs: [/]`` beside it is exactly
    the shape that undoes it, so a restrictive default is still a restriction — the same reading
    ``access: workspace-write`` already gets."""
    _, report = validated(tree, wf(""))
    assert "isolation: worktree" in refused(report), report.errors


def test_the_carve_out_needs_the_workspace_open_too(tree: Tree) -> None:
    """An agent nothing constrains, in a workflow nothing constrains."""
    _, report = validated(tree, wf())
    assert report.ok, report.errors


def test_the_reported_block_is_refused_key_by_key(tree: Tree) -> None:
    """The reproduction as it was reported: four real restrictions, and the hatch wide open.

    ``defaults: {budget_usd, max_tokens, timeout_total}`` beside ``isolation: worktree``, an
    agent at ``access: full``, and a block that empties the tool denials, mounts the filesystem
    root and hands the CLI a permissions document. It validated clean.
    """
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
isolation: worktree
defaults:
  budget_usd: 0.01
  max_tokens: 500
  timeout_total: 1m
steps:
  - id: think
    agent:
      provider: claude
      model: claude-sonnet-4-5
      access: full
      provider_options:
        claude:
          extra_args: {disallowedTools: ""}
          add_dirs: ["/"]
          settings: '{"permissions": {"defaultMode": "bypassPermissions"}}'
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    for key in ("extra_args", "add_dirs", "settings"):
        assert f"provider_options.claude.{key} is refused" in joined, report.errors
    assert "isolation: worktree" in joined
    assert "defaults.budget_usd" in joined


WORKFLOW_SAMPLES: dict[str, str] = {
    "isolation": "",  # the default, worktree
    "defaults": "isolation: none\ndefaults: {budget_usd: 0.01}\n",
    "inputs": "isolation: none\ninputs:\n  token: {type: string, secret: true}\n",
}

DEFAULTS_SAMPLES: dict[str, str] = {
    "budget_usd": "isolation: none\ndefaults: {budget_usd: 0.01}\n",
    "max_tokens": "isolation: none\ndefaults: {max_tokens: 500}\n",
    "timeout_total": "isolation: none\ndefaults: {timeout_total: 1m}\n",
    "timeout": "isolation: none\ndefaults: {timeout: 30s}\n",
}

STEP_SAMPLES: dict[str, str] = {"timeout": "timeout: 30s\n"}


def test_the_samples_cover_every_control_of_every_schema() -> None:
    """A control nobody wrote a sample for is a control nobody proved fires."""
    assert sorted(WORKFLOW_SAMPLES) == sorted(WORKFLOW_CONTROLS)
    assert sorted(DEFAULTS_SAMPLES) == sorted(DEFAULTS_CONTROLS)
    assert sorted(STEP_SAMPLES) == sorted(STEP_CONTROLS)


@pytest.mark.parametrize("name", sorted(WORKFLOW_SAMPLES))
def test_every_workflow_control_really_turns_the_allow_list_on(tree: Tree, name: str) -> None:
    _, report = validated(tree, wf(WORKFLOW_SAMPLES[name]))
    assert refused(report), f"{name}: classified a control but nothing fired"


@pytest.mark.parametrize("name", sorted(DEFAULTS_SAMPLES))
def test_every_defaults_control_really_turns_the_allow_list_on(tree: Tree, name: str) -> None:
    _, report = validated(tree, wf(DEFAULTS_SAMPLES[name]))
    assert refused(report), f"defaults.{name}: classified a control but nothing fired"


@pytest.mark.parametrize("name", sorted(STEP_SAMPLES))
def test_every_step_control_really_turns_the_allow_list_on(tree: Tree, name: str) -> None:
    _, report = validated(tree, wf(step=STEP_SAMPLES[name]))
    assert refused(report), f"step {name}: classified a control but nothing fired"


ALL_SAMPLES: dict[str, tuple[str, str]] = {
    **{f"workflow.{k}": (v, "") for k, v in WORKFLOW_SAMPLES.items()},
    **{f"defaults.{k}": (v, "") for k, v in DEFAULTS_SAMPLES.items()},
    **{f"step.{k}": ("isolation: none\n", v) for k, v in STEP_SAMPLES.items()},
}


@pytest.mark.parametrize("name", sorted(ALL_SAMPLES))
def test_a_control_never_blocks_the_keys_rayspec_has_reasoned_about(tree: Tree, name: str) -> None:
    """A control that blocks the permitted case is its own defect: the documented way out of it
    is to switch the control off, and nobody should be taught that."""
    workflow, step = ALL_SAMPLES[name]
    _, report = validated(
        tree, wf(workflow, step=step, options="max_thinking_tokens: 2048\nuser: someone\n")
    )
    assert report.ok, report.errors


def test_an_included_documents_own_caps_reach_the_agents_of_its_body(tree: Tree) -> None:
    """An include is a workflow file; its ``defaults:`` are as real as the root's."""
    tree.workflow(
        "part",
        """rayspec: 1
name: part
defaults: {budget_usd: 0.02}
steps:
  - id: inner
    agent:
      provider: claude
      model: claude-sonnet-4-5
      access: full
      provider_options:
        claude:
          extra_args: {}
    prompt: hi
""",
    )
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
isolation: none
steps:
  - {id: part, include: part}
""",
    )
    assert refused(report), report.errors


# -- the universe: every model the two documents can reach ----------------------------------------


def _model_types(annotation: object) -> set[type[BaseModel]]:
    """Every Pydantic model mentioned anywhere in one annotation (unions, dicts, Annotated)."""
    found: set[type[BaseModel]] = set()
    stack: list[Any] = [annotation]
    while stack:
        current = stack.pop()
        if isinstance(current, type) and issubclass(current, BaseModel):
            found.add(current)
            continue
        stack.extend(get_args(current))
    return found


def _reachable(*roots: type[BaseModel]) -> set[type[BaseModel]]:
    """Every model reachable from ``roots`` — the universe, read rather than written down."""
    seen: set[type[BaseModel]] = set()
    stack = list(roots)
    while stack:
        model = stack.pop()
        if model in seen:
            continue
        seen.add(model)
        for info in model.model_fields.values():
            stack.extend(_model_types(info.annotation))
    return seen


@dataclasses.dataclass(frozen=True)
class Family:
    """One schema (or one set of schemas spelled in the same namespace) and its partition."""

    models: tuple[type[BaseModel], ...]
    fields: frozenset[str]
    controls: dict[str, Restriction[Any]]
    carried: dict[str, Carried]
    non_controls: dict[str, str]
    parent: str | None = None


def _fields_of(*models: type[BaseModel]) -> frozenset[str]:
    return frozenset().union(*(frozenset(model.model_fields) for model in models))


def _resolved_agent_fields() -> frozenset[str]:
    """What a provider actually receives — the other half of the agent schema."""
    from rayspec.loader.loader import ResolvedAgent

    return frozenset(f.name for f in dataclasses.fields(ResolvedAgent))


FAMILIES: dict[str, Family] = {
    "agent": Family(
        models=(AgentDef, AgentOverride),
        fields=_fields_of(AgentDef, AgentOverride) | _resolved_agent_fields(),
        controls=dict(AGENT_CONTROLS),
        carried={},
        non_controls=dict(AGENT_NON_CONTROLS),
    ),
    "agent.tools": Family(
        models=(ToolsSpec,),
        fields=_fields_of(ToolsSpec),
        controls={},
        carried=dict(AGENT_TOOLS_CARRIED),
        non_controls={},
        parent="agent",
    ),
    "agent.commands": Family(
        models=(CommandsSpec,),
        fields=_fields_of(CommandsSpec),
        controls={},
        carried=dict(AGENT_COMMANDS_CARRIED),
        non_controls={},
        parent="agent",
    ),
    "agent.mcp": Family(
        models=(McpServerDef,),
        fields=_fields_of(McpServerDef),
        controls={},
        carried={},
        non_controls=dict(AGENT_MCP_NON_CONTROLS),
        parent="agent",
    ),
    "workflow": Family(
        models=(Workflow,),
        fields=_fields_of(Workflow),
        controls=dict(WORKFLOW_CONTROLS),
        carried={},
        non_controls=dict(WORKFLOW_NON_CONTROLS),
    ),
    "workflow.defaults": Family(
        models=(Defaults,),
        fields=_fields_of(Defaults),
        controls=dict(DEFAULTS_CONTROLS),
        carried={},
        non_controls=dict(DEFAULTS_NON_CONTROLS),
        parent="workflow",
    ),
    "workflow.inputs": Family(
        models=(InputSpec,),
        fields=_fields_of(InputSpec),
        controls={},
        carried=dict(INPUT_CARRIED),
        non_controls=dict(INPUT_NON_CONTROLS),
        parent="workflow",
    ),
    "step": Family(
        models=tuple(STEP_MODELS.values()),
        fields=_fields_of(*STEP_MODELS.values()),
        controls=dict(STEP_CONTROLS),
        carried={},
        non_controls=dict(STEP_NON_CONTROLS),
    ),
    "step.retry": Family(
        models=(RetryPolicy,),
        fields=_fields_of(RetryPolicy),
        controls={},
        carried={},
        non_controls=dict(STEP_RETRY_NON_CONTROLS),
        parent="step",
    ),
    "step.loop": Family(
        models=(LoopSpec,),
        fields=_fields_of(LoopSpec),
        controls={},
        carried={},
        non_controls=dict(STEP_LOOP_NON_CONTROLS),
        parent="step",
    ),
    "step.approve": Family(
        models=(ApproveSpec,),
        fields=_fields_of(ApproveSpec),
        controls={},
        carried={},
        non_controls=dict(STEP_APPROVE_NON_CONTROLS),
        parent="step",
    ),
    "step.stop": Family(
        models=(StopSpec,),
        fields=_fields_of(StopSpec),
        controls={},
        carried={},
        non_controls=dict(STEP_STOP_NON_CONTROLS),
        parent="step",
    ),
}


def _policy_leaf_keys() -> frozenset[str]:
    """``models.deny``, ``access.max``, ``max_concurrent_runs`` — the keys a layer can set.

    A block whose annotation is a nested model contributes ``block.key`` for each of its fields;
    a top-level field that is a plain value (``max_consecutive_failures``) IS the key, and has to
    be classified under its own name — otherwise a scalar key added to the document would be in
    no table and reported by nothing.
    """
    leaves: set[str] = set()
    for block, info in Policy.model_fields.items():
        models = _model_types(info.annotation)
        if not models:
            leaves.add(block)
            continue
        for model in models:
            leaves.update(f"{block}.{name}" for name in model.model_fields)
    return frozenset(leaves)


def test_every_model_the_documents_reach_belongs_to_a_family() -> None:
    """The universe is READ off the two documents, so a nested model added tomorrow fails here.

    This is the assertion the whole file hangs on: a partition is only as total as the set of
    schemas it is pointed at, and a set that is written down by hand is total by construction and
    proves nothing.
    """
    covered = {model for family in FAMILIES.values() for model in family.models}
    covered |= set(_reachable(Policy))  # the policy document, partitioned by its leaf KEYS below
    universe = _reachable(Workflow, Policy)
    unclassified = sorted(model.__name__ for model in universe - covered)
    assert not unclassified, f"give these a family in FAMILIES: {unclassified}"
    stale = sorted(model.__name__ for model in covered - universe)
    assert not stale, f"no longer reachable from the documents: {stale}"


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_every_field_of_every_schema_is_classified(name: str) -> None:
    """A control with tags and a why, a field carried by one, or a reason it restricts nothing.

    Both directions: a field in no table fails, and a table entry for a field the schema no
    longer has fails too — a stale claim is as bad as a missing one.
    """
    family = FAMILIES[name]
    tables = (set(family.controls), set(family.carried), set(family.non_controls))
    for first, second in ((0, 1), (0, 2), (1, 2)):
        overlap = sorted(tables[first] & tables[second])
        assert not overlap, f"{name}: classify these in exactly one table: {overlap}"
    classified = tables[0] | tables[1] | tables[2]
    missing = sorted(family.fields - classified)
    assert not missing, f"{name}: classify these: {missing}"
    stale = sorted(classified - family.fields)
    assert not stale, f"{name}: no longer a field of this schema: {stale}"
    for field, control in family.controls.items():
        assert control.tags <= CONTROL_TAGS, f"{name}.{field}: unknown tag"
        assert control.tags, f"{name}.{field}: a control covers at least one kind of restriction"
        assert control.why.strip(), f"{name}.{field}: give the one-line why"
    for field, entry in family.carried.items():
        assert entry.why.strip(), f"{name}.{field}: give the one-line why"
        assert family.parent is not None, f"{name}: a carried field needs a parent family"
        assert entry.by in FAMILIES[family.parent].controls, (
            f"{name}.{field}: carried by {entry.by!r}, which is not a control of {family.parent}"
        )
    for field, reason in family.non_controls.items():
        assert reason.strip(), f"{name}.{field}: give the one-line reason"


def test_every_policy_key_is_classified() -> None:
    """The policy document, at the level a layer actually sets: ``block.key``.

    A key missing from both tables gets EVERY tag at runtime rather than none, which is the safe
    default — but "safe by accident" is not classification, so it fails here.
    """
    classified = set(POLICY_CONTROL_TAGS) | set(POLICY_TAGS_FROM_VALUE) | set(POLICY_NON_CONTROLS)
    leaves = _policy_leaf_keys()
    assert not sorted(leaves - classified), (
        f"classify these policy keys: {sorted(leaves - classified)}"
    )
    assert not sorted(classified - leaves), f"no longer a policy key: {sorted(classified - leaves)}"
    for key, tags in POLICY_CONTROL_TAGS.items():
        assert tags <= CONTROL_TAGS and tags, f"{key}: bad tags"
    for key, reason in POLICY_NON_CONTROLS.items():
        assert reason.strip(), f"{key}: give the one-line reason"


def test_every_policy_block_holds_a_classified_key() -> None:
    """``Policy.model_fields`` is the block level; a block with no classified key is unreachable."""
    leaves = _policy_leaf_keys()
    for block in Policy.model_fields:
        assert block in leaves or any(key.startswith(f"{block}.") for key in leaves), block


# -- and the CLI: the fourth place a restriction could be written ---------------------------------


def _cli_options() -> dict[str, set[str]]:
    """Every option of every command, read off the CLI a user actually types.

    The built click surface rather than the source: an option added through a shared
    ``Annotated`` alias, a plugin command or a decorator is on it exactly like a hand-written
    one, so nothing can be added to the CLI without appearing here.
    """
    import click
    import typer.core
    import typer.main

    from rayspec.cli.app import app

    group = typer.main.get_command(app)
    context = click.Context(group)  # type: ignore[arg-type]
    found: dict[str, set[str]] = {}
    for name in group.list_commands(context):  # type: ignore[attr-defined]
        command = group.get_command(context, name)  # type: ignore[attr-defined]
        for param in getattr(command, "params", ()):
            if not isinstance(param, typer.core.TyperOption):
                continue
            spellings = [*param.opts, *param.secondary_opts]
            longs = [opt for opt in spellings if opt.startswith("--")] or spellings
            found.setdefault(longs[0], set()).add(name)
    return found


def test_every_cli_option_is_classified() -> None:
    """A flag that tightens a run is a control like any other, so the flags are read too.

    Only one of them adds a restriction the document does not already carry, and it is applied to
    the document before the check runs (see the test below). Everything else chooses what is
    printed, where it goes, which run is addressed — or LOOSENS something, and a widening can
    never be the reason an escape hatch should have been shut.
    """
    flags = _cli_options()
    unclassified = sorted(set(flags) - set(CLI_FLAGS))
    assert not unclassified, f"classify these in CLI_FLAGS: {unclassified}"
    stale = sorted(set(CLI_FLAGS) - set(flags))
    assert not stale, f"no longer a CLI option: {stale}"
    for flag, entry in sorted(CLI_FLAGS.items()):
        assert entry.why.strip(), f"{flag}: give the one-line reason"


def test_the_only_cli_control_is_the_one_that_tightens_isolation() -> None:
    assert sorted(flag for flag, entry in CLI_FLAGS.items() if entry.control) == ["--worktree"]


WORKTREE_WF = """rayspec: 1
name: open
isolation: none
steps:
  - id: think
    agent:
      provider: claude
      model: claude-sonnet-4-5
      access: full
      provider_options:
        claude:
          add_dirs: [/]
    prompt: hello
"""


def _worktree_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "open.yaml").write_text(WORKTREE_WF, encoding="utf-8")
    return root


def test_the_worktree_flag_is_a_control_the_load_time_check_can_see(
    tmp_path: Path, home: Path
) -> None:
    """``--worktree`` on a workflow that opted out is an operator ADDING a restriction.

    A control the check cannot see is the whole shape of this class of bug, so the flag is
    written onto the document before the workflow is validated: what the check reads is
    ``isolation: worktree``, exactly as if the file had said so.
    """
    from rayspec.cli.app import app

    root = _worktree_project(tmp_path)
    cli = CliRunner()
    without = cli.invoke(app, ["run", "open", "--root", str(root), "--dry-run"])
    assert without.exit_code == 0, without.output

    with_flag = cli.invoke(app, ["run", "open", "--root", str(root), "--dry-run", "--worktree"])
    assert with_flag.exit_code != 0
    assert "provider_options.claude.add_dirs is refused" in with_flag.output
    assert "isolation: worktree" in with_flag.output

"""Every guard, held to what it claims to cover — one control at a time, from every source.

The same defect kept reappearing one level further down. The allow-list was a denylist and was
inverted; the TRIGGER for the allow-list was an enumeration and was made total over the agent
schema; the UNIVERSE of that test was one schema and was widened to every schema a document
reaches. Each fix left the layer below it untouched, and the layer below was the guards: a value
guard decided from a SUBSET of the controls the trigger had already computed totally.

``mcp_servers`` asked the policy document alone, so an arbitrary stdio server walked past an
agent's own ``mcp:`` set, its ``tools.deny: [mcp]``, its ``network: off`` and its ``access:
read-only`` — every one of which the trigger correctly counted as a control. ``env`` asked a
two-prefix denylist, so ``PATH``, ``NODE_OPTIONS`` and ``HTTPS_PROXY`` passed unread under all of
them. ``usage_baseline`` asked only for the ``spend`` tag, so an inflated baseline zeroed
``spend.json``, ``run.json`` and ``rayspec costs`` under any control that was not a ceiling.

``INERT_PROOFS`` in ``test_provider_options.py`` pairs each UNGUARDED key with the test that holds
its reason to the code. The guarded keys had no equivalent, and this file is it: the matrix of
(guard by control), where the controls are read off the classification tables rather than written
down here, and every cell asserts the guard actually fires. A guard that claims a tag and ignores
a source carrying it fails the suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from rayspec.policy import ALLOWED_PROVIDER_OPTIONS, CONTROL_TAGS
from rayspec.policy.controls import (
    AGENT_CONTROLS,
    CLI_FLAGS,
    DEFAULTS_CONTROLS,
    EXTERNAL_CONTROLS,
    POLICY_CONTROL_TAGS,
    POLICY_TAGS_FROM_VALUE,
    STEP_CONTROLS,
    WORKFLOW_CONTROLS,
)
from rayspec.policy.enforce import Inert

from .conftest import Tree, validated

WF = """rayspec: 1
name: wf
isolation: {isolation}
{workflow}steps:
  - id: think
{step}    agent:
      provider: {provider}
      model: {model}
      access: {access}
{agent}      provider_options:
        {provider}:
{options}
    prompt: hello
"""

MODELS = {"claude": "claude-sonnet-4-5", "codex": "gpt-5.6"}

#: A lockfile whose CONTENTS do not matter: :func:`discover_external_controls` counts its
#: presence, because a lockfile too broken to parse is a control in force and unreadable rather
#: than a control that is absent.
LOCKFILE = (
    "version: 1\nworkflows:\n  wf:\n    agents:\n      inline:think:\n        provider: claude\n"
)


@dataclass(frozen=True)
class Sample:
    """ONE control, written where its own source spells it, and nothing else.

    ``tags`` is the kinds it covers — taken from the classification table the control lives in, so
    a sample cannot claim a kind the table does not give it (checked below).
    """

    tags: frozenset[str]
    workflow: str = ""
    step: str = ""
    agent: str = ""
    policy: str = ""
    files: tuple[tuple[str, str], ...] = field(default=())
    #: The two fields whose DEFAULT is itself a restriction, so the base document has to opt out
    #: of them explicitly and a sample that is about one of them opts back in.
    isolation: str = "none"
    access: str = "full"


def agent_sample(name: str, text: str) -> Sample:
    return Sample(AGENT_CONTROLS[name].tags, agent=text)


def workflow_sample(name: str, text: str) -> Sample:
    return Sample(WORKFLOW_CONTROLS[name].tags, workflow=text)


def defaults_sample(name: str, text: str) -> Sample:
    return Sample(DEFAULTS_CONTROLS[name].tags, workflow=f"defaults: {{{text}}}\n")


def step_sample(name: str, text: str) -> Sample:
    return Sample(STEP_CONTROLS[name].tags, step=text)


def policy_sample(key: str, text: str) -> Sample:
    tags = CONTROL_TAGS if key in POLICY_TAGS_FROM_VALUE else POLICY_CONTROL_TAGS[key]
    return Sample(tags, policy=text)


#: Every control of every source, one per entry of every classification table. The keys are
#: ``<source>.<control>``; the totality check below reads the tables, so a control added to any of
#: them fails here until someone writes the one line that sets it.
SAMPLES: dict[str, Sample] = {
    # -- the agent sets it on itself (no policy file at all: the common case) --------------------
    "agent.access": Sample(AGENT_CONTROLS["access"].tags, access="read-only"),
    "agent.budget_usd": agent_sample("budget_usd", "budget_usd: 0.25\n"),
    "agent.commands": agent_sample("commands", "commands: {deny: ['^rm ']}\n"),
    "agent.max_turns": agent_sample("max_turns", "max_turns: 3\n"),
    "agent.mcp": agent_sample("mcp", "mcp: {docs: {command: docs-mcp}}\n"),
    "agent.network": agent_sample("network", "network: off\n"),
    "agent.on_denial": agent_sample("on_denial", "on_denial: fail\n"),
    "agent.tools": agent_sample("tools", "tools: {deny: [shell]}\n"),
    # -- the workflow document sets it over every agent it runs ----------------------------------
    "workflow.isolation": Sample(WORKFLOW_CONTROLS["isolation"].tags, isolation="worktree"),
    "workflow.defaults": workflow_sample("defaults", "defaults: {budget_usd: 0.01}\n"),
    "workflow.inputs": workflow_sample(
        "inputs", "inputs:\n  token: {type: string, secret: true}\n"
    ),
    "defaults.budget_usd": defaults_sample("budget_usd", "budget_usd: 0.01"),
    "defaults.max_tokens": defaults_sample("max_tokens", "max_tokens: 500"),
    "defaults.timeout_total": defaults_sample("timeout_total", "timeout_total: 1m"),
    "defaults.timeout": defaults_sample("timeout", "timeout: 30s"),
    # -- the step that runs the agent ------------------------------------------------------------
    "step.timeout": step_sample("timeout", "timeout: 30s\n"),
    # -- the policy document ---------------------------------------------------------------------
    "policy.access.max": policy_sample("access.max", "access:\n  max: full\n"),
    "policy.models.deny": policy_sample("models.deny", "models:\n  deny: ['*opus*']\n"),
    "policy.mcp.allow_servers": policy_sample(
        "mcp.allow_servers", "mcp:\n  allow_servers: [docs]\n"
    ),
    "policy.providers.allow": policy_sample(
        "providers.allow", "providers:\n  allow: [claude, codex]\n"
    ),
    "policy.tools.deny": policy_sample("tools.deny", "tools:\n  deny: [shell]\n"),
    "policy.trust.require": policy_sample("trust.require", "trust:\n  require: true\n"),
    "policy.approvals.classes": policy_sample(
        "approvals.classes", "approvals:\n  classes:\n    release: {allow_yes: false}\n"
    ),
    "policy.workspace.protected_paths": policy_sample(
        "workspace.protected_paths", "workspace:\n  protected_paths: ['.github/**']\n"
    ),
    "policy.workspace.max_changed_files": policy_sample(
        "workspace.max_changed_files", "workspace:\n  max_changed_files: 20\n"
    ),
    "policy.workspace.max_changed_lines": policy_sample(
        "workspace.max_changed_lines", "workspace:\n  max_changed_lines: 500\n"
    ),
    "policy.budget.per_run": policy_sample("budget.per_run", "budget:\n  per_run: 2.0\n"),
    "policy.budget.per_day": policy_sample("budget.per_day", "budget:\n  per_day: 20.0\n"),
    "policy.budget.per_month": policy_sample("budget.per_month", "budget:\n  per_month: 200.0\n"),
    "policy.budget.max_consecutive_failures": policy_sample(
        "budget.max_consecutive_failures", "budget:\n  max_consecutive_failures: 3\n"
    ),
    "policy.max_consecutive_failures": policy_sample(
        "max_consecutive_failures", "max_consecutive_failures: 3\n"
    ),
    "policy.max_concurrent_runs": policy_sample(
        "max_concurrent_runs", "max_concurrent_runs:\n  claude: 1\n"
    ),
    # -- imposed from outside the workflow file --------------------------------------------------
    "external.rayspec.lock": Sample(
        frozenset({"model", "provider"}), files=(("rayspec.lock", LOCKFILE),)
    ),
    "external.config.yaml": Sample(
        frozenset({"settings"}),
        files=(
            (
                "config.yaml",
                "providers:\n  claude: {setting_sources: [project]}\n"
                "  codex: {config: {model_reasoning_summary: detailed}}\n",
            ),
        ),
    ),
    # -- and the command line: `--worktree` is written onto the document before the check reads it
    "cli.--worktree": Sample(WORKFLOW_CONTROLS["isolation"].tags, isolation="worktree"),
}


def test_the_samples_cover_every_control_of_every_source() -> None:
    """The matrix is generated from the tables; a control with no sample is a hole in it."""
    expected = {
        *(f"agent.{name}" for name in AGENT_CONTROLS),
        *(f"workflow.{name}" for name in WORKFLOW_CONTROLS),
        *(f"defaults.{name}" for name in DEFAULTS_CONTROLS),
        *(f"step.{name}" for name in STEP_CONTROLS),
        *(f"policy.{key}" for key in POLICY_CONTROL_TAGS),
        *(f"policy.{key}" for key in POLICY_TAGS_FROM_VALUE),
        *(f"external.{name}" for name, e in EXTERNAL_CONTROLS.items() if e.control),
        *(f"cli.{flag}" for flag, e in CLI_FLAGS.items() if e.control),
    }
    assert sorted(SAMPLES) == sorted(expected)


def test_the_samples_between_them_carry_every_kind_of_restriction() -> None:
    """A guard narrowed to a tag no sample carries would be untested and look covered."""
    carried = frozenset().union(*(sample.tags for sample in SAMPLES.values()))
    assert sorted(CONTROL_TAGS - carried) == []


#: One value each guard MUST refuse, and the key path the refusal has to name. Read together with
#: ``ALLOWED_PROVIDER_OPTIONS`` below: an entry that grows a guard has to appear here.
GUARD_SAMPLES: dict[tuple[str, tuple[str, ...]], tuple[str, str]] = {
    ("claude", ("env",)): ("env:\n  GITHUB_TOKEN: x\n", "env.GITHUB_TOKEN"),
    ("claude", ("user",)): ("user: nobody\n", "user"),
    ("claude", ("mcp_servers",)): (
        "mcp_servers:\n  evil: {type: stdio, command: /bin/sh}\n",
        "mcp_servers.evil",
    ),
    ("codex", ("config", "mcp_servers")): (
        "config:\n  mcp_servers:\n    evil: {command: /bin/sh}\n",
        "config.mcp_servers.evil",
    ),
    ("codex", ("approval_mode",)): ("approval_mode: auto_review\n", "approval_mode"),
    ("codex", ("usage_baseline",)): (
        "usage_baseline: {input: 999999999}\n",
        "usage_baseline.input",
    ),
}


def _guarded() -> list[tuple[str, tuple[str, ...]]]:
    return sorted(
        (provider, path)
        for provider, block in ALLOWED_PROVIDER_OPTIONS.items()
        for path, rule in block.items()
        if not isinstance(rule.offenders, Inert)
    )


def test_every_guarded_key_has_a_value_the_guard_must_refuse() -> None:
    """The counterpart of INERT_PROOFS: no guarded key without the value that proves it works."""
    assert _guarded() == sorted(GUARD_SAMPLES)


def _cases() -> list[tuple[str, tuple[str, ...], str]]:
    """(provider, key path, sample name) for every cell the guard's own tags demand.

    ``guarded_by`` empty means "under every control", so an empty set demands every sample; a
    guard that names tags demands every sample carrying one of them. Either way the demand is
    computed from the tables rather than listed, which is the property that was missing.
    """
    out: list[tuple[str, tuple[str, ...], str]] = []
    for provider, path in _guarded():
        wanted = ALLOWED_PROVIDER_OPTIONS[provider][path].guarded_by or CONTROL_TAGS
        for name, sample in sorted(SAMPLES.items()):
            if sample.tags & wanted:
                out.append((provider, path, name))
    return out


@pytest.mark.parametrize(
    ("provider", "path", "sample"),
    _cases(),
    ids=[f"{p}.{'.'.join(k)}-{n}" for p, k, n in _cases()],
)
def test_every_guard_fires_under_every_control_it_claims(
    tree: Tree, provider: str, path: tuple[str, ...], sample: str
) -> None:
    """One control, from one source, and the guard has to refuse the value it exists to refuse."""
    entry = SAMPLES[sample]
    options, expected = GUARD_SAMPLES[(provider, path)]
    if entry.policy:
        tree.policy(entry.policy)
    for name, text in entry.files:
        tree.write(name, text)
    document = WF.format(
        provider=provider,
        model=MODELS[provider],
        isolation=entry.isolation,
        access=entry.access,
        workflow=entry.workflow,
        step="".join(f"    {line}\n" for line in entry.step.splitlines()),
        agent="".join(f"      {line}\n" for line in entry.agent.splitlines()),
        options="\n".join(f"          {line}" for line in options.splitlines()),
    )
    _, report = validated(tree, document)
    wanted = f"provider_options.{provider}.{expected}"
    assert any(wanted in message for message in report.errors), (
        f"{sample} is a control but the guard on {wanted} stayed silent: {report.errors}"
    )

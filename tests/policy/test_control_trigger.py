"""What counts as a control — the TRIGGER the ``provider_options`` allow-list hangs off.

The allow-list itself is settled: under a control, only the keys rayspec has reasoned about
pass, and ``test_provider_options.py`` proves that list total against the real SDK dataclass.
This file is about the other half. Six blocks reached the SDK with ``errors: []`` because "a
control is in force" was an ENUMERATION of two things — ``network: off`` and the policy file —
while the restriction actually doing the work sat somewhere unlisted: the agent's own
``access:``, its ``tools.deny:``, its ``max_turns``/``budget_usd``, or the model lockfile, which
is committed, external, and enforced by default under CI.

So the trigger is classified rather than listed, and the classification is proved total the same
way the allow-list is: parametrised over the real schemas, failing when a field is neither
"security-shaped, so it is a control" nor "not a control, and here is the one-line reason". That
partition — over every schema, not just this one — is ``test_control_universe.py``; this file
holds the six blocks and the proof that each classified agent control really fires.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from .conftest import Tree, validated

CLAUDE_WF = """rayspec: 1
name: wf
isolation: none
steps:
  - id: think
    agent:
      provider: claude
      model: claude-sonnet-4-5
{fields}      provider_options:
        claude:
{options}
    prompt: hello
"""

#: An agent nothing constrains: the top access level, no tool list, no cap, no server, no
#: command policy — the carve-out the escape hatch is still an escape hatch in. The workflow
#: around it opts out too (``isolation: none`` in ``CLAUDE_WF``): the run has to have nothing to
#: protect, not just the agent.
UNCONSTRAINED = "access: full\n"


def claude_wf(options: str, *, fields: str = UNCONSTRAINED) -> str:
    return CLAUDE_WF.format(
        fields="".join(f"      {line}\n" for line in fields.splitlines()),
        options="\n".join(f"          {line}" for line in options.splitlines()),
    )


def refusal_of(report, key: str) -> str:
    """The refusal naming ``provider_options.claude.<key>``, or ``""`` when there is none."""
    wanted = f"provider_options.claude.{key} is refused"
    return next((e for e in report.errors if wanted in e), "")


# -- the six blocks that landed ------------------------------------------------------------------


def test_the_agents_own_access_limit_is_a_control(tree: Tree) -> None:
    """1. ``access: read-only`` with no policy file.

    ``--permission-mode dontAsk`` followed by ``--dangerously-skip-permissions`` is the whole
    bypass: the agent's own access level is what withheld the permission, so it is what the
    allow-list has to hang off.
    """
    _, report = validated(
        tree,
        claude_wf(
            "extra_args: {permission-mode: bypassPermissions}\n", fields="access: read-only\n"
        ),
    )
    message = refusal_of(report, "extra_args")
    assert message, report.errors
    assert "access: read-only" in message


def test_the_agents_own_tool_denial_is_a_control(tree: Tree) -> None:
    """2. ``tools: {deny: [shell, web]}`` with no policy file.

    The same empty-deny last-wins trick, aimed at the very field ``network: off`` is implemented
    by folding into — so the two spellings have to be the same kind of thing.
    """
    _, report = validated(
        tree,
        claude_wf(
            "extra_args: {disallowedTools: ''}\n",
            fields="access: full\ntools: {deny: [shell, web]}\n",
        ),
    )
    message = refusal_of(report, "extra_args")
    assert message, report.errors
    assert "tools.deny" in message


def test_the_model_lockfile_is_a_control(tree: Tree) -> None:
    """3. An EXTERNAL, committed control: ``.rayspec/rayspec.lock``.

    ``rayspec lock`` pins sonnet, ``--locked`` (on by default under CI) enforces the pin, and
    ``check_locked`` reports no drift — because the drift is not in a field it compares.
    ``extra_args``/``fallback_model`` put opus on the argv after it.
    """
    lock = (
        "version: 1\nworkflows:\n  wf:\n    agents:\n      inline:think:\n"
        "        provider: claude\n        model: claude-sonnet-4-5\n"
    )
    options = "extra_args: {model: claude-opus-4-1}\nfallback_model: claude-opus-4-1\n"
    _, before = validated(tree, claude_wf(options), name="unlocked")
    assert before.ok, before.errors  # nothing else constrains this agent

    tree.write("rayspec.lock", lock)
    _, report = validated(tree, claude_wf(options))
    for key in ("extra_args", "fallback_model"):
        message = refusal_of(report, key)
        assert message, report.errors
        assert "rayspec.lock" in message


def test_a_settings_blob_is_refused_under_the_agents_own_access_limit(tree: Tree) -> None:
    """4. ``--settings {"permissions": {"defaultMode": "bypassPermissions"}}``."""
    _, report = validated(
        tree,
        claude_wf(
            'settings: \'{"permissions": {"defaultMode": "bypassPermissions"}}\'\n',
            fields="access: read-only\n",
        ),
    )
    assert refusal_of(report, "settings"), report.errors


@pytest.mark.parametrize(
    ("cap", "named"),
    [("max_turns: 2\n", "max_turns: 2"), ("budget_usd: 0.05\n", "budget_usd: 0.05")],
)
def test_the_agents_own_cost_ceilings_are_controls(tree: Tree, cap: str, named: str) -> None:
    """5. ``max_turns`` is in ``ADAPTER_OWNED_OPTIONS`` precisely so a workflow cannot raise it.

    ``extra_args: {max-turns: "999"}`` raised it anyway, because a cap was not a control.
    """
    _, report = validated(
        tree,
        claude_wf("extra_args: {max-turns: '999'}\n", fields=f"access: full\n{cap}"),
    )
    message = refusal_of(report, "extra_args")
    assert message, report.errors
    assert named in message


def test_the_env_justification_matches_what_the_adapter_does(tmp_path: Path) -> None:
    """6. The allow-list said an added variable cannot displace one rayspec set. It could.

    ``build_options`` built ``env`` with ``provider_options.env`` merged OVER rayspec's own
    ``CLAUDE_AGENT_SDK_CLIENT_APP`` and over the machine owner's ``providers.claude.env``. The
    entry is only safe if it is merged UNDER both, which is what ``MERGED_OPTIONS`` always
    claimed and what ``mcp_servers`` already did.
    """
    from rayspec.providers.base import AgentRequest
    from rayspec.providers.claude import ClaudeProvider, build_options

    provider = ClaudeProvider({"env": {"SHARED": "owner-value"}})
    options, _ = build_options(
        provider,
        AgentRequest(
            step_path="s",
            prompt="hi",
            cwd=str(tmp_path),
            provider_options={
                "claude": {
                    "env": {
                        "CLAUDE_AGENT_SDK_CLIENT_APP": "pwned",
                        "SHARED": "workflow-wins",
                        "EXTRA": "added",
                    }
                }
            },
        ),
        stderr=lambda _line: None,
    )
    assert options.env["CLAUDE_AGENT_SDK_CLIENT_APP"].startswith("rayspec/")
    assert options.env["SHARED"] == "owner-value"
    assert options.env["EXTRA"] == "added"  # adding is what the escape hatch is for


def test_the_env_entry_says_only_what_is_true() -> None:
    """A source-of-truth table carrying a false reason is worse than no reason at all."""
    from rayspec.policy import ALLOWED_PROVIDER_OPTIONS

    summary = ALLOWED_PROVIDER_OPTIONS["claude"][("env",)].summary
    assert "under" in summary.lower()
    assert "inert" not in summary.lower()  # it is not inert; it is merged underneath


def test_the_mcp_entry_says_only_what_is_true(tree: Tree) -> None:
    """ "declaring any makes the set strict" sat beside the key while nothing backed it up.

    Both halves are checked here: the sentence, and the behaviour it claims. The agent's declared
    set is what an ``mcp_servers`` entry is judged against, because both adapters merge that block
    UNDER these servers — so a name the agent declares passes and a name it does not is refused.
    """
    from rayspec.policy.controls import AGENT_CONTROLS

    why = AGENT_CONTROLS["mcp"].why
    assert "strict" not in why.lower()
    assert "mcp_servers" in why and "UNDER" in why

    fields = "access: full\nmcp: {docs: {command: docs-mcp}}\n"
    _, declared = validated(
        tree,
        claude_wf("mcp_servers:\n  docs: {type: stdio, command: d}\n", fields=fields),
        name="a",
    )
    assert declared.ok, declared.errors
    _, added = validated(
        tree,
        claude_wf("mcp_servers:\n  evil: {type: stdio, command: e}\n", fields=fields),
        name="b",
    )
    assert any("mcp_servers.evil" in message for message in added.errors), added.errors


# -- every classified agent control really fires ---------------------------------------------------


#: The partition itself — every field of every schema a restriction can be written in, both
#: directions — lives in ``test_control_universe.py``: the classification is only as total as the
#: set of schemas it is pointed at, and pointing it at the agent alone is what let the identical
#: restriction one level up (``defaults.budget_usd``, ``isolation:``) stay invisible. What is
#: proved HERE is the other half: that each classified agent control really fires, and that none
#: of them blocks a key the allow-list permits.

CONTROL_SAMPLES: dict[str, str] = {
    "access": "access: read-only\n",
    "budget_usd": "access: full\nbudget_usd: 0.25\n",
    "commands": "access: full\ncommands: {deny: ['rm -rf']}\n",
    "max_turns": "access: full\nmax_turns: 3\n",
    "mcp": "access: full\nmcp: {docs: {command: docs-mcp}}\n",
    "network": "access: full\nnetwork: off\n",
    "on_denial": "access: full\non_denial: fail\n",
    "tools": "access: full\ntools: {deny: [shell]}\n",
}


def test_the_samples_cover_every_control() -> None:
    from rayspec.policy.controls import AGENT_CONTROLS

    assert sorted(CONTROL_SAMPLES) == sorted(AGENT_CONTROLS)


@pytest.mark.parametrize("name", sorted(CONTROL_SAMPLES))
def test_every_classified_control_really_turns_the_allow_list_on(tree: Tree, name: str) -> None:
    """Classification is not enough: setting the field alone has to refuse an unreasoned key."""
    _, report = validated(
        tree, claude_wf("extra_args: {model: claude-opus-4-1}\n", fields=CONTROL_SAMPLES[name])
    )
    assert refusal_of(report, "extra_args"), f"{name}: classified a control but nothing fired"


@pytest.mark.parametrize("name", sorted(CONTROL_SAMPLES))
def test_a_control_never_blocks_the_keys_rayspec_has_reasoned_about(tree: Tree, name: str) -> None:
    """A control that blocks the permitted case is its own defect."""
    _, report = validated(
        tree, claude_wf("max_thinking_tokens: 2048\nuser: someone\n", fields=CONTROL_SAMPLES[name])
    )
    assert report.ok, report.errors


# -- the same, for the policy document ------------------------------------------------------------


POLICY_SAMPLES: dict[str, str] = {
    "access": "access:\n  max: read-only\n",
    "budget": "budget:\n  per_day: 20.0\n",
    "max_concurrent_runs": "max_concurrent_runs:\n  claude: 1\n",
    "max_consecutive_failures": "max_consecutive_failures: 3\n",
    "mcp": "mcp:\n  allow_servers: [github]\n",
    "models": "models:\n  deny: ['*opus*']\n",
    "providers": "providers:\n  allow: [claude]\n",
    "tools": "tools:\n  deny: [shell]\n",
    "trust": "trust:\n  require: true\n",
    "workspace": "workspace:\n  max_changed_files: 5\n",
}


def test_the_policy_samples_cover_every_block() -> None:
    from rayspec.policy import Policy

    assert sorted(POLICY_SAMPLES) == sorted(Policy.model_fields)


@pytest.mark.parametrize("block", sorted(POLICY_SAMPLES))
def test_every_policy_block_is_a_tagged_control(tree: Tree, block: str) -> None:
    """``control_sources`` claims to report every key a layer can set; this holds it to it."""
    from rayspec.policy import load_policy
    from rayspec.policy.controls import CONTROL_TAGS, policy_controls

    tree.policy(POLICY_SAMPLES[block])
    effective = load_policy(tree.root, home=tree.home)
    controls = policy_controls(effective)
    assert controls, f"{block}: sets a restriction but no control was reported"
    for control in controls:
        assert control.tags <= CONTROL_TAGS, f"{control.key}: unknown tag"
        assert control.tags, f"{control.key}: a control covers at least one kind of restriction"
        assert control.sources, f"{control.key}: a control has to name the file that imposes it"


# -- and for the artefacts outside the workflow ---------------------------------------------------


ARTEFACT_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(yaml|yml|lock|json|jsonl|toml)$|^\.env$")


def _named_artefacts() -> dict[str, set[str]]:
    """Every project/user file name rayspec's own source spells, and where.

    The anchor is the source tree rather than a list in the policy package, for the same reason
    the allow-list is anchored on the SDK dataclass: a table that lists itself is total by
    construction and proves nothing.
    """
    import rayspec

    root = Path(rayspec.__file__).resolve().parent
    found: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            text = node.value if isinstance(node, ast.Constant) else None
            if isinstance(text, str) and ARTEFACT_RE.match(text):
                found.setdefault(text, set()).add(path.name)
    return found


def test_every_artefact_the_source_names_is_classified() -> None:
    """An external control is a control; everything else says in one line why it is not.

    The lockfile is the one with a proof attached, and it is not special: whatever else lands in
    ``.rayspec/`` next has to be classified before this test passes again.
    """
    from rayspec.policy.controls import EXTERNAL_CONTROLS

    found = _named_artefacts()
    unclassified = sorted(set(found) - set(EXTERNAL_CONTROLS))
    assert not unclassified, f"classify these in EXTERNAL_CONTROLS: {unclassified}"
    stale = sorted(set(EXTERNAL_CONTROLS) - set(found))
    assert not stale, f"no longer named by the source: {stale}"
    for name, entry in sorted(EXTERNAL_CONTROLS.items()):
        assert entry.why.strip(), f"{name}: give the one-line reason"


def test_the_lockfile_is_the_external_control_with_a_proof() -> None:
    from rayspec.limits.lockfile import LOCKFILE_NAME
    from rayspec.policy.controls import EXTERNAL_CONTROLS

    assert EXTERNAL_CONTROLS[LOCKFILE_NAME].control is True


# -- the carve-out: an agent with nothing to bypass keeps its escape hatch ------------------------


def test_an_agent_nothing_constrains_keeps_the_escape_hatch(tree: Tree) -> None:
    """No access limit, no tool list, no cap, no policy, no lockfile, no machine settings."""
    _, report = validated(
        tree, claude_wf("extra_args: {disallowedTools: ''}\nadd_dirs: [/]\nsettings: '{}'\n")
    )
    assert report.ok, report.errors


def test_the_carve_out_ends_the_moment_anything_constrains_the_agent(tree: Tree) -> None:
    """The same block, one field added — the difference between the two cases is the trigger."""
    options = "extra_args: {disallowedTools: ''}\n"
    _, open_case = validated(tree, claude_wf(options), name="open")
    _, closed = validated(tree, claude_wf(options, fields="access: workspace-write\n"), name="shut")
    assert open_case.ok, open_case.errors
    assert refusal_of(closed, "extra_args"), closed.errors


def test_the_machine_owners_provider_settings_are_a_control(tree: Tree) -> None:
    """``config.yaml`` belongs to the machine owner and a workflow is applied over it."""
    options = "extra_args: {disallowedTools: ''}\n"
    _, before = validated(tree, claude_wf(options), name="before")
    assert before.ok, before.errors

    tree.write("config.yaml", "providers:\n  claude: {setting_sources: [project]}\n")
    _, report = validated(tree, claude_wf(options), name="after")
    message = refusal_of(report, "extra_args")
    assert message, report.errors
    assert "config.yaml" in message


def test_a_refusal_counts_the_extra_controls_instead_of_reciting_them(tree: Tree) -> None:
    """A hardened agent is under a dozen controls at once; a message reciting all of them stops
    being read, and one that names none of the files is useless. So: the first few by name, the
    rest by count."""
    tree.policy("access:\n  max: read-only\ntools:\n  deny: [web]\n")
    _, report = validated(
        tree,
        claude_wf(
            "extra_args: {model: claude-opus-4-1}\n",
            fields=(
                "access: read-only\nnetwork: off\nmax_turns: 3\nbudget_usd: 1.0\n"
                "on_denial: fail\ntools: {deny: [shell]}\ncommands: {deny: ['^rm']}\n"
                "mcp: {docs: {command: d}}\n"
            ),
        ),
    )
    message = refusal_of(report, "extra_args")
    assert message, report.errors
    listed = re.search(r"while (.+?) are in force", message)
    assert listed, message
    named, _, counted = listed.group(1).partition(" and ")
    assert len(named.split(", ")) == 4  # the first few, by name
    assert re.fullmatch(r"\d+ more \(.+\)", counted), counted  # the rest, by count
    assert ".rayspec/policy.yaml:" in message

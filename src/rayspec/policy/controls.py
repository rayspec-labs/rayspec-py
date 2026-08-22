# SPDX-License-Identifier: Apache-2.0
"""What constrains one agent — the TRIGGER the ``provider_options`` allow-list hangs off.

Boundary: classification of controls, plus the two file existence checks the external ones need.
It reports what restricts a run; it never mutates a workflow, talks to a provider or decides what
to do about a violation — :mod:`rayspec.policy.enforce` does that.

Why this module exists as its own file. ``provider_options`` is read as an ALLOW-list "while a
control is in force", and the allow-list is proved total against the real SDK dataclass. The
trigger was not: it named ``network: off`` and the policy file, and every restriction outside
those two — the agent's own ``access:``, its ``tools.deny:``, its ``max_turns``/``budget_usd``,
its ``commands:``, its ``mcp:``, and the committed model lockfile — was a way to keep the escape
hatch wide open while looking constrained. An enumeration in the trigger is worth exactly as
much as an enumeration in the allow-list.

So a control is CLASSIFIED here rather than listed. Every field of the agent schema appears in
:data:`AGENT_CONTROLS` (security-shaped: its presence governs the agent) or in
:data:`AGENT_NON_CONTROLS` (with the one line saying why it restricts nothing); every artefact
rayspec reads from outside the workflow file appears in :data:`EXTERNAL_CONTROLS`; and
``tests/policy/test_control_trigger.py`` fails when any of those stops being total. A field
added later has to be classified — it cannot default to "not a control", which is how six live
bypasses arose.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rayspec.policy.layers import EffectivePolicy, PolicySource, short_path
from rayspec.policy.model import ACCESS_ORDER

if TYPE_CHECKING:  # type-only: importing the loader at runtime would close an import cycle
    from rayspec.loader.loader import ResolvedAgent

#: The KIND of restriction a control covers. A value guard on an allow-listed key matches on
#: these rather than on a control's spelling, so a new spelling of an existing kind — a second
#: field that withholds sandbox power, say — engages the guards that kind already has instead of
#: quietly slipping past them.
CONTROL_TAGS: frozenset[str] = frozenset(
    {
        "access",  # how much power the sandbox grants
        "commands",  # which shell commands may run
        "mcp",  # which MCP servers may be reached
        "model",  # which model answers
        "network",  # whether the agent can reach the network
        "provider",  # which vendor runs the step
        "settings",  # settings imposed from outside the workflow file
        "spend",  # turns and money
        "tools",  # which tools may run
        "trust",  # which workflows may run at all
        "workspace",  # what may change on disk
    }
)

#: Tool entries that mean "the provider's web access", neutral and native (mirrors the adapters'
#: web tool sets; duplicated so this package keeps its "no provider imports" boundary).
WEB_TOOL_ENTRIES: frozenset[str] = frozenset({"web", "WebFetch", "WebSearch", "web_search"})

#: The access level that withholds nothing — the one value of ``access:`` that is not a control.
UNRESTRICTED_ACCESS: str = ACCESS_ORDER[-1]

#: ``(spelled key, value, tags)`` — one restriction a field currently imposes.
Imposed = tuple[str, str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class Control:
    """One restriction in force: how it is spelled, what kind it is, where it came from."""

    key: str
    tags: frozenset[str]
    sources: tuple[PolicySource, ...]


@dataclass(frozen=True, slots=True)
class AgentControl:
    """A field of the agent schema that CONSTRAINS the agent setting it.

    ``why`` is the one line that earned the field its place — kept next to the entry so the next
    reader can check it rather than trust it. ``tags`` is every kind of restriction the field can
    cover; ``imposed`` reports the ones it covers for a given agent, and returns nothing when the
    field is set to a value that restricts nothing (``access: full``, an empty ``tools:``).
    """

    why: str
    tags: frozenset[str]
    imposed: Callable[[ResolvedAgent], tuple[Imposed, ...]]


def tool_entry_tags(entries: Iterable[str]) -> frozenset[str]:
    """Which kinds of restriction a list of tool entries covers.

    ``tools.deny: [web]`` is the field ``network: off`` is IMPLEMENTED by folding into, so it is
    a network control however it is spelled — treating the two differently is precisely what let
    an empty ``--disallowedTools`` through.
    """
    tags = {"tools"}
    for entry in entries:
        name = entry.rsplit(":", 1)[-1]
        if entry in WEB_TOOL_ENTRIES or name in WEB_TOOL_ENTRIES:
            tags.add("network")
        if entry == "mcp" or entry.startswith("mcp:"):
            tags.add("mcp")
    return frozenset(tags)


def _access(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.access == UNRESTRICTED_ACCESS:
        return ()
    return ((f"access: {agent.access}", agent.access, frozenset({"access"})),)


def _tools(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    out: list[Imposed] = []
    for name in ("deny", "allow"):
        entries = tuple(getattr(agent.tools, name))
        if entries:
            out.append((f"tools.{name}", ", ".join(entries), tool_entry_tags(entries)))
    return tuple(out)


def _network(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.network != "off":
        return ()
    return (("network: off", "off", frozenset({"network", "tools"})),)


def _commands(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    commands = agent.commands
    if commands is None:
        return ()
    out: list[Imposed] = []
    for name in ("deny", "allow"):
        entries = tuple(getattr(commands, name))
        if entries:
            out.append((f"commands.{name}", ", ".join(entries), frozenset({"commands", "tools"})))
    return tuple(out)


def _mcp(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if not agent.mcp:
        return ()
    servers = ", ".join(sorted(agent.mcp))
    return (("mcp", servers, frozenset({"mcp"})),)


def _max_turns(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.max_turns is None:
        return ()
    return ((f"max_turns: {agent.max_turns}", str(agent.max_turns), frozenset({"spend"})),)


def _budget_usd(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.budget_usd is None:
        return ()
    return ((f"budget_usd: {agent.budget_usd}", str(agent.budget_usd), frozenset({"spend"})),)


def _on_denial(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.on_denial != "fail":
        return ()
    return (("on_denial: fail", "fail", frozenset({"tools"})),)


#: Every SECURITY-SHAPED field of the agent schema: setting it constrains the agent, so it is a
#: control, so the escape hatch closes. These are workflow fields rather than policy keys — they
#: come with no policy file, which is the common case and the case where an unprotected control
#: does the most damage.
AGENT_CONTROLS: Mapping[str, AgentControl] = {
    "access": AgentControl(
        why=(
            "the sandbox level the run gets; anything below "
            f"{UNRESTRICTED_ACCESS!r} withholds power the provider would otherwise grant"
        ),
        tags=frozenset({"access"}),
        imposed=_access,
    ),
    "tools": AgentControl(
        why="which tools may run — the field a denied tool is actually kept away by",
        tags=frozenset({"tools", "network", "mcp"}),
        imposed=_tools,
    ),
    "network": AgentControl(
        why="whether the provider's web tools may run; enforced by folding 'web' into tools.deny",
        tags=frozenset({"network", "tools"}),
        imposed=_network,
    ),
    "commands": AgentControl(
        why="which shell commands may run, as patterns checked before the call",
        tags=frozenset({"commands", "tools"}),
        imposed=_commands,
    ),
    "mcp": AgentControl(
        why=(
            "the MCP servers the run may reach; declaring any makes the set strict, and it is "
            "the set mcp.allow_servers and the mcp_servers guard are checked against"
        ),
        tags=frozenset({"mcp"}),
        imposed=_mcp,
    ),
    "max_turns": AgentControl(
        why="a hard ceiling on turns — the reason max_turns is a computed option, not a raw one",
        tags=frozenset({"spend"}),
        imposed=_max_turns,
    ),
    "budget_usd": AgentControl(
        why="a hard ceiling on what one step may spend",
        tags=frozenset({"spend"}),
        imposed=_budget_usd,
    ),
    "on_denial": AgentControl(
        why="'fail' makes a refused tool call stop the step — the teeth of every tool denial",
        tags=frozenset({"tools"}),
        imposed=_on_denial,
    ),
}

#: Every other field of the agent schema — and of the resolved agent a provider receives — with
#: the one line saying why it restricts nothing. The reason is the point: a field that lands here
#: without one is a field nobody thought about, which is exactly how a bypass gets written.
AGENT_NON_CONTROLS: Mapping[str, str] = {
    "provider": (
        "which vendor runs the step: it picks an implementation rather than withholding "
        "anything; policy providers.allow is what restricts the choice, and that is a control"
    ),
    "model": (
        "which model answers — a choice, not a restriction. It becomes a control when something "
        "outside the workflow pins it, which is what the model lockfile does (EXTERNAL_CONTROLS)"
    ),
    "raw_model": "the tier or alias 'model' was written as, kept so a message can quote the file",
    "effort": "how hard the model thinks; it changes the answer, not what the agent may do",
    "instructions": "prose in the system prompt — persuasion, and nothing enforces persuasion",
    "instructions_file": "where that prose is read from",
    "instructions_mode": "whether the prose replaces the provider's preset or is appended to it",
    "thinking": "whether extended thinking is on; it grants no tool and withholds none",
    "provider_options": (
        "the escape hatch this check reads — the thing being judged, never a reason to judge it"
    ),
    "key": "the loader's opaque id for the agent",
    "name": "the agent's display name",
    "source": "which file the agent was defined in",
    "yaml_path": "the YAML path of the agent, for error messages",
    "locations": "file:line of every field, for error messages",
}


def agent_controls(agent: ResolvedAgent) -> tuple[Control, ...]:
    """Every control ``agent`` imposes on ITSELF, from every security-shaped field it sets."""
    out: list[Control] = []
    for name in sorted(AGENT_CONTROLS):
        rule = AGENT_CONTROLS[name]
        for key, value, tags in rule.imposed(agent):
            where = agent.location(name) or agent.field_path(name)
            out.append(
                Control(
                    key=key,
                    tags=tags,
                    sources=(PolicySource(layer="workflow", label=where, line=None, value=value),),
                )
            )
    return tuple(out)


#: Policy key (as :meth:`EffectivePolicy.control_sources` spells it) → the kinds it covers.
#: ``tools.deny`` is absent on purpose: its kinds depend on the entries, so they are read off the
#: entries. A key that is missing here gets EVERY tag rather than none — an unclassified control
#: must engage every guard, never slip past all of them.
POLICY_CONTROL_TAGS: Mapping[str, frozenset[str]] = {
    "models.deny": frozenset({"model"}),
    "access.max": frozenset({"access"}),
    "mcp.allow_servers": frozenset({"mcp"}),
    "providers.allow": frozenset({"provider"}),
    "trust.require": frozenset({"trust"}),
    "workspace.protected_paths": frozenset({"workspace"}),
    "workspace.max_changed_files": frozenset({"workspace"}),
    "workspace.max_changed_lines": frozenset({"workspace"}),
}


def policy_controls(effective: EffectivePolicy | None) -> tuple[Control, ...]:
    """Every control the policy layers impose, tagged by the kind of restriction it is."""
    if effective is None:
        return ()
    denied = effective.denied_tools()
    out: list[Control] = []
    for key, sources in sorted(effective.control_sources().items()):
        if key == "tools.deny":
            tags = tool_entry_tags(denied)
        else:
            tags = POLICY_CONTROL_TAGS.get(key, CONTROL_TAGS)
        out.append(Control(key=key, tags=tags, sources=sources))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ExternalControl:
    """One artefact rayspec reads from outside the workflow file, classified.

    ``control`` says whether it CONSTRAINS a run — in which case it turns the allow-list on, the
    same way a policy key does. ``why`` is the one line behind that answer, and it is required
    either way: "not a control" without a reason is an assumption, and every one of the six live
    bypasses was an assumption nobody had written down.
    """

    control: bool
    why: str


#: Every project or user file name rayspec's own source spells, classified. The test that keeps
#: this total reads the source tree rather than this table, so a file that lands in ``.rayspec/``
#: next has to be classified before the suite is green again.
EXTERNAL_CONTROLS: Mapping[str, ExternalControl] = {
    "rayspec.lock": ExternalControl(
        True,
        "the model lockfile: an externally imposed, committed pin on what every agent resolves "
        "to, enforced by --locked, which is on by default under CI",
    ),
    "config.yaml": ExternalControl(
        True,
        "the machine owner's adapter settings; a workflow's provider_options is applied OVER the "
        "providers: block on a shipped adapter (codex config), so a value the owner set there "
        "can be replaced from inside the workflow. Which keys an adapter protects cannot be "
        "known here without importing it, and the answer that does not depend on an adapter's "
        "internals is the fail-closed one",
    ),
    "policy.yaml": ExternalControl(
        False,
        "the policy layers themselves — reported key by key by EffectivePolicy.control_sources, "
        "so counting the file again would name the same restriction twice",
    ),
    "trusted.yaml": ExternalControl(
        False,
        "the trust list; it binds only where policy sets trust.require, which is a policy key",
    ),
    "spend.json": ExternalControl(
        False,
        "the spend ledger: what a run HAS cost. The ceiling it is measured against is a policy "
        "key, and a record of past spending restricts nothing on its own",
    ),
    "run.json": ExternalControl(False, "a run record rayspec writes; an output, never an input"),
    "events.jsonl": ExternalControl(False, "a run's event log; written after the fact"),
    "stream.jsonl": ExternalControl(False, "a run's raw provider stream; written after the fact"),
    "audit.jsonl": ExternalControl(False, "the audit log; it records what ran, it permits nothing"),
    "output.json": ExternalControl(False, "a step's structured output"),
    "context.json": ExternalControl(False, "the context handed to a shell/python step"),
    "inputs.json": ExternalControl(False, "the inputs a test case binds"),
    "checks.yaml": ExternalControl(
        False, "test cases for `rayspec test`; they assert about a run, they do not shape one"
    ),
    ".env": ExternalControl(
        False, "environment values for a run; it supplies values and withholds no capability"
    ),
    "auth.json": ExternalControl(
        False, "a provider CLI's own credentials, which `rayspec doctor` looks for"
    ),
    ".credentials.json": ExternalControl(
        False, "the same, under the other name Claude Code writes"
    ),
    ".schema.json": ExternalControl(
        False, "the suffix of the JSON Schemas rayspec generates for editors"
    ),
    "pyproject.toml": ExternalControl(
        False, "named by `rayspec init` to recognise a Python project it is scaffolding into"
    ),
    "agent.yaml": ExternalControl(False, "a scaffold template `rayspec new` copies"),
    "workflow.yaml": ExternalControl(False, "a scaffold template `rayspec new` copies"),
    "workflow_agent.yaml": ExternalControl(False, "a scaffold template `rayspec new` copies"),
    "example.yaml": ExternalControl(False, "a scaffold example `rayspec init` copies"),
}


@dataclass(frozen=True, slots=True)
class ExternalControls:
    """The external controls discovered for one project, ready to apply per agent.

    Built once per :func:`~rayspec.policy.apply.apply_policy` (it is the only place that knows
    the project root), and empty for a caller that has no root — a pure check must not go
    looking for files behind its caller's back.
    """

    shared: tuple[Control, ...] = ()
    per_provider: Mapping[str, tuple[Control, ...]] = field(default_factory=dict)

    def of(self, provider: str) -> tuple[Control, ...]:
        """The external controls that apply to an agent running on ``provider``."""
        return (*self.shared, *self.per_provider.get(provider, ()))


def discover_external_controls(project_root: Path, *, home: Path | None = None) -> ExternalControls:
    """Read the artefacts :data:`EXTERNAL_CONTROLS` marks as controls, for ``project_root``.

    Presence is the test, not contents: a lockfile that pins a different workflow still refuses
    the run under ``--locked``, and a lockfile too broken to parse is a control that is in force
    and unreadable rather than a control that is absent.
    """
    # lazy: rayspec.limits and rayspec.config both reach the loader, which imports this package
    from rayspec.config.paths import rayspec_home
    from rayspec.config.settings import config_layers
    from rayspec.limits.lockfile import LOCKFILE_NAME, lockfile_path

    root = Path(project_root)
    where = rayspec_home() if home is None else Path(home)
    shared: list[Control] = []
    lock = lockfile_path(root)
    if lock.is_file():
        shared.append(
            Control(
                key="lockfile",
                tags=frozenset({"model", "provider"}),
                sources=(
                    PolicySource(
                        layer="project",
                        label=short_path(lock, root, where),
                        line=None,
                        value=LOCKFILE_NAME,
                    ),
                ),
            )
        )
    per_provider: dict[str, list[Control]] = {}
    for path, data in config_layers(root, where):
        providers = data.get("providers")
        if not isinstance(providers, Mapping):
            continue
        for name, block in providers.items():
            if not block:
                continue
            per_provider.setdefault(str(name), []).append(
                Control(
                    key=f"providers.{name}",
                    tags=frozenset({"settings"}),
                    sources=(
                        PolicySource(
                            layer="config",
                            label=short_path(path, root, where),
                            line=None,
                            value=str(name),
                        ),
                    ),
                )
            )
    return ExternalControls(
        shared=tuple(shared),
        per_provider={name: tuple(items) for name, items in per_provider.items()},
    )


def merged_controls(
    controls: Sequence[Control],
) -> tuple[dict[str, tuple[PolicySource, ...]], dict[str, frozenset[str]]]:
    """``(key -> sources, key -> tags)`` for a list of controls, keys de-duplicated."""
    sources: dict[str, tuple[PolicySource, ...]] = {}
    tags: dict[str, frozenset[str]] = {}
    for control in controls:
        sources[control.key] = (*sources.get(control.key, ()), *control.sources)
        tags[control.key] = tags.get(control.key, frozenset()) | control.tags
    return sources, tags


__all__ = [
    "AGENT_CONTROLS",
    "AGENT_NON_CONTROLS",
    "CONTROL_TAGS",
    "EXTERNAL_CONTROLS",
    "POLICY_CONTROL_TAGS",
    "UNRESTRICTED_ACCESS",
    "WEB_TOOL_ENTRIES",
    "AgentControl",
    "Control",
    "ExternalControl",
    "ExternalControls",
    "agent_controls",
    "discover_external_controls",
    "merged_controls",
    "policy_controls",
    "tool_entry_tags",
]

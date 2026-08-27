# SPDX-License-Identifier: Apache-2.0
"""What constrains one run — the TRIGGER the ``provider_options`` allow-list hangs off.

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

So a control is CLASSIFIED here rather than listed — and over every schema a restriction can be
written in, not one of them. Classifying only the AGENT schema left the identical restriction one
level up invisible: ``defaults: {budget_usd: 0.01, max_tokens: 500}`` beside ``isolation:
worktree`` is four real caps, and the escape hatch stayed open beside them. A completeness test
aimed at one schema is total over that schema and silent about the rest.

The universe is therefore mechanical rather than chosen: every model reachable from the workflow
document and from the policy document, the resolved agent a provider receives, every project file
rayspec's own source names, and every option of every CLI command. Each field lands in exactly one
of three tables — it PRODUCES a restriction (:class:`Restriction`: tags, why, and the function
that reports what it imposes), it is READ by one that does (:class:`Carried`), or it restricts
nothing and says why in one line. ``tests/policy/test_control_universe.py`` reads the schemas and
fails when any of that stops being total, in either direction. A field added later has to be
classified — it cannot default to "not a control", which is how six live bypasses arose.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from rayspec.policy.layers import EffectivePolicy, PolicySource, short_path
from rayspec.policy.model import ACCESS_ORDER
from rayspec.schema import Defaults, Workflow
from rayspec.schema.steps import StepModel

if TYPE_CHECKING:  # type-only: importing the loader at runtime would close an import cycle
    from rayspec.loader.loader import ResolvedAgent, ResolvedWorkflow

#: The KIND of restriction a control covers. A value guard on an allow-listed key matches on
#: these rather than on a control's spelling, so a new spelling of an existing kind — a second
#: field that withholds sandbox power, say — engages the guards that kind already has instead of
#: quietly slipping past them.
CONTROL_TAGS: frozenset[str] = frozenset(
    {
        "access",  # how much power the sandbox grants
        "approvals",  # what may approve a gate, and what may never approve it automatically
        "commands",  # which shell commands may run
        "mcp",  # which MCP servers may be reached
        "model",  # which model answers
        "network",  # whether the agent can reach the network
        "provider",  # which vendor runs the step
        "secrets",  # values that must not be persisted or handed on
        "settings",  # settings imposed from outside the workflow file
        "spend",  # what a run may consume: turns, tokens, money and wall-clock
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

#: The ``isolation:`` value that withholds nothing. The DEFAULT is ``worktree``, which does
#: withhold something: the run works on a copy instead of the checkout a person is sitting in.
#: A restrictive default is still a restriction — ``access: workspace-write`` is treated the same
#: way — so the carve-out for "nothing to bypass" has to be asked for.
UNRESTRICTED_ISOLATION: str = "none"

S = TypeVar("S")


@dataclass(frozen=True, slots=True)
class ServerOpinion:
    """What ONE control says about which MCP servers the run may reach.

    A guard must never ask a single source whether a server is allowed — that is how an arbitrary
    stdio server walked past an agent's own ``mcp:`` set, its ``tools.deny: [mcp]``, its
    ``network: off`` and its ``access: read-only`` while a policy file was the only place being
    consulted. So each control states its own opinion here and :class:`ServerControls` folds them;
    the guard sees the fold and nothing else.

    ``admits`` is the set of names this control positively names (``None`` = it names none, which
    is not the same as naming an empty set: ``mcp.allow_servers: []`` admits nothing). ``denies``
    are the names it refuses outright and ``denies_all`` refuses every one of them.

    ``defines`` is the half a NAME cannot supply, and leaving it out inverted the invariant the
    whole document rests on. ``mcp.allow_servers: [github]`` contributes the name ``github`` and
    nothing else — not the command, not the endpoint, not the argv. A workflow that then writes
    its own ``github`` into ``provider_options`` supplied the definition itself, so admitting it
    on the name match made a restrictive-only key GRANT a capability: with no policy file the run
    was refused, and adding the allow-list handed the agent ``/bin/sh -c 'curl … | sh'``. Only a
    control that carries the definition where policy, ``rayspec plan`` and review all read it —
    today the agent's own ``mcp:`` block — puts a name here. Matching a permitted name is
    necessary; it is never sufficient.
    """

    admits: frozenset[str] | None = None
    defines: frozenset[str] = frozenset()
    denies: frozenset[str] = frozenset()
    denies_all: bool = False

    def __post_init__(self) -> None:
        # a control cannot define a server it does not itself permit: the fold would then admit a
        # definition no control stands behind, which is the defect this field exists to close
        if self.defines and (self.admits is None or not self.defines <= self.admits):
            raise ValueError("ServerOpinion.defines must be a subset of admits")


@dataclass(frozen=True, slots=True)
class Imposed:
    """One restriction a field currently imposes: how it is spelled, its value, its kinds.

    ``servers`` is the optional half — what this restriction says about the MCP servers the run
    may reach. It lives on the restriction rather than in the fold so a new control that bounds
    the server set says so where it is classified, instead of the fold growing a special case per
    spelling (which is the enumeration this package exists to avoid).
    """

    key: str
    value: str
    tags: frozenset[str]
    servers: ServerOpinion | None = None


@dataclass(frozen=True, slots=True)
class Control:
    """One restriction in force: how it is spelled, what kind it is, where it came from."""

    key: str
    tags: frozenset[str]
    sources: tuple[PolicySource, ...]
    servers: ServerOpinion | None = None


@dataclass(frozen=True, slots=True)
class Restriction(Generic[S]):
    """A field of some schema that CONSTRAINS the run setting it.

    ``why`` is the one line that earned the field its place — kept next to the entry so the next
    reader can check it rather than trust it. ``tags`` is every kind of restriction the field can
    cover; ``imposed`` reports the ones it covers for a given document, and returns nothing when
    the field is set to a value that restricts nothing (``access: full``, ``isolation: none``, an
    empty ``tools:``, a cap left unset).
    """

    why: str
    tags: frozenset[str]
    imposed: Callable[[S], tuple[Imposed, ...]]


@dataclass(frozen=True, slots=True)
class Carried:
    """A field of a nested schema that a control on the PARENT reads.

    ``tools.deny`` is a restriction, but it is not a restriction of its own: the agent's ``tools``
    control reads both lists and imposes them together. Naming the parent here rather than
    repeating the control keeps one restriction reported once — and ``by`` is checked against the
    parent's table, so a nested field cannot claim a carrier that does not exist.
    """

    by: str
    why: str


def tool_entry_servers(entries: Iterable[str], *, allow_list: bool) -> ServerOpinion:
    """What one ``tools:`` list says about the MCP servers the run may reach.

    ``deny: [mcp]`` refuses every server, ``deny: [mcp:github]`` refuses that one. A non-empty
    ``allow`` list means "nothing else", so it admits exactly the servers it names — and admits
    none at all when it names no MCP entry, which is the honest reading of "nothing else".

    It DEFINES nothing (:attr:`ServerOpinion.defines` stays empty). ``tools.allow: [mcp:github]``
    is a list of tool entries: it says which servers may be reached, never what ``github`` is —
    and an agent writes that list itself, so treating it as a definition let a workflow name the
    identifier and supply the command behind it in the same file.
    """
    servers = {entry[4:].split("/", 1)[0] for entry in entries if entry.startswith("mcp:")}
    servers.discard("")
    if allow_list:
        return ServerOpinion(admits=frozenset(servers) if "mcp" not in entries else None)
    return ServerOpinion(denies=frozenset(servers), denies_all="mcp" in entries)


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


# -- the agent schema ------------------------------------------------------------------------------


def _access(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.access == UNRESTRICTED_ACCESS:
        return ()
    return (Imposed(f"access: {agent.access}", agent.access, frozenset({"access"})),)


def _tools(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    out: list[Imposed] = []
    for name in ("deny", "allow"):
        entries = tuple(getattr(agent.tools, name))
        if entries:
            out.append(
                Imposed(
                    f"tools.{name}",
                    ", ".join(entries),
                    tool_entry_tags(entries),
                    tool_entry_servers(entries, allow_list=name == "allow"),
                )
            )
    return tuple(out)


def _network(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.network != "off":
        return ()
    return (Imposed("network: off", "off", frozenset({"network", "tools"})),)


def _commands(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    commands = agent.commands
    if commands is None:
        return ()
    out: list[Imposed] = []
    for name in ("deny", "allow"):
        entries = tuple(getattr(commands, name))
        if entries:
            out.append(
                Imposed(f"commands.{name}", ", ".join(entries), frozenset({"commands", "tools"}))
            )
    return tuple(out)


def _mcp(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    """The agent's own ``mcp:`` block — the one control that DEFINES servers, not just names them.

    It carries the command or endpoint behind each name, in the neutral field policy checks,
    ``rayspec plan`` prints and a reviewer reads. That is what makes it the only source a
    ``provider_options`` server may be matched against: both adapters merge that raw block UNDER
    this one, so a name declared here is this declaration either way.
    """
    if not agent.mcp:
        return ()
    servers = ", ".join(sorted(agent.mcp))
    return (
        Imposed(
            "mcp",
            servers,
            frozenset({"mcp"}),
            ServerOpinion(admits=frozenset(agent.mcp), defines=frozenset(agent.mcp)),
        ),
    )


def _max_turns(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.max_turns is None:
        return ()
    return (Imposed(f"max_turns: {agent.max_turns}", str(agent.max_turns), frozenset({"spend"})),)


def _budget_usd(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.budget_usd is None:
        return ()
    return (
        Imposed(f"budget_usd: {agent.budget_usd}", str(agent.budget_usd), frozenset({"spend"})),
    )


def _on_denial(agent: ResolvedAgent) -> tuple[Imposed, ...]:
    if agent.on_denial != "fail":
        return ()
    return (Imposed("on_denial: fail", "fail", frozenset({"tools"})),)


#: Every SECURITY-SHAPED field of the agent schema: setting it constrains the agent, so it is a
#: control, so the escape hatch closes. These are workflow fields rather than policy keys — they
#: come with no policy file, which is the common case and the case where an unprotected control
#: does the most damage.
AGENT_CONTROLS: Mapping[str, Restriction[ResolvedAgent]] = {
    "access": Restriction(
        why=(
            "the sandbox level the run gets; anything below "
            f"{UNRESTRICTED_ACCESS!r} withholds power the provider would otherwise grant"
        ),
        tags=frozenset({"access"}),
        imposed=_access,
    ),
    "tools": Restriction(
        why="which tools may run — the field a denied tool is actually kept away by",
        tags=frozenset({"tools", "network", "mcp"}),
        imposed=_tools,
    ),
    "network": Restriction(
        why="whether the provider's web tools may run; enforced by folding 'web' into tools.deny",
        tags=frozenset({"network", "tools"}),
        imposed=_network,
    ),
    "commands": Restriction(
        why="which shell commands may run, as patterns checked before the call",
        tags=frozenset({"commands", "tools"}),
        imposed=_commands,
    ),
    "mcp": Restriction(
        why=(
            "the servers the run may reach, as the workflow declares them — the set an "
            "mcp_servers entry in provider_options is checked against, because both adapters "
            "merge that block UNDER these"
        ),
        tags=frozenset({"mcp"}),
        imposed=_mcp,
    ),
    "max_turns": Restriction(
        why="a hard ceiling on turns — the reason max_turns is a computed option, not a raw one",
        tags=frozenset({"spend"}),
        imposed=_max_turns,
    ),
    "budget_usd": Restriction(
        why="a hard ceiling on what one step may spend",
        tags=frozenset({"spend"}),
        imposed=_budget_usd,
    ),
    "on_denial": Restriction(
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
    "extends": "the agent an override merges over: a lookup, and the merged result is classified",
    "key": "the loader's opaque id for the agent",
    "name": "the agent's display name",
    "source": "which file the agent was defined in",
    "yaml_path": "the YAML path of the agent, for error messages",
    "locations": "file:line of every field, for error messages",
}

#: ``tools:`` on an agent (``ToolsSpec``). Both lists are read by the agent's ``tools`` control.
AGENT_TOOLS_CARRIED: Mapping[str, Carried] = {
    "allow": Carried("tools", "a non-empty allow-list means 'nothing else' — a restriction"),
    "deny": Carried("tools", "the entries kept away from the agent"),
}

#: ``commands:`` on an agent (``CommandsSpec``), read by the agent's ``commands`` control.
AGENT_COMMANDS_CARRIED: Mapping[str, Carried] = {
    "allow": Carried("commands", "a non-empty allow-list means no other command may run"),
    "deny": Carried("commands", "the command patterns refused before the call"),
}

#: One declared MCP server (``McpServerDef``). The restriction is the SET of servers — which the
#: agent's ``mcp`` control imposes — never one server's own connection details.
AGENT_MCP_NON_CONTROLS: Mapping[str, str] = {
    "transport": "how one declared server is reached (stdio/http/sse)",
    "command": "the process a stdio server is started as",
    "args": "the arguments that process gets",
    "env": "environment for that process; it supplies values and withholds no capability",
    "url": "where an http/sse server lives",
    "headers": "headers sent to it",
}


def agent_controls(agent: ResolvedAgent) -> tuple[Control, ...]:
    """Every control ``agent`` imposes on ITSELF, from every security-shaped field it sets."""
    out: list[Control] = []
    for name in sorted(AGENT_CONTROLS):
        rule = AGENT_CONTROLS[name]
        for imposed in rule.imposed(agent):
            where = agent.location(name) or agent.field_path(name)
            out.append(
                Control(
                    key=imposed.key,
                    tags=imposed.tags,
                    sources=(
                        PolicySource(layer="workflow", label=where, line=None, value=imposed.value),
                    ),
                    servers=imposed.servers,
                )
            )
    return tuple(out)


# -- the workflow document -------------------------------------------------------------------------


def _cap(name: str, unit: str = "") -> Callable[[Defaults], tuple[Imposed, ...]]:
    """A run-level cap of ``Defaults``: a restriction exactly when it is set."""

    def imposed(defaults: Defaults) -> tuple[Imposed, ...]:
        value = getattr(defaults, name)
        if value is None:
            return ()
        text = f"{value}{unit}"
        return (Imposed(f"defaults.{name}: {text}", text, frozenset({"spend"})),)

    return imposed


#: The run-level caps of ``defaults:``. Same ceilings as the agent's own, one level up — and
#: exactly the ones a codex agent has to use, because ``budget_usd`` on the agent is not a
#: capability every provider has.
DEFAULTS_CONTROLS: Mapping[str, Restriction[Defaults]] = {
    "budget_usd": Restriction(
        why="a ceiling on what the whole run may spend; the run ends once it is passed",
        tags=frozenset({"spend"}),
        imposed=_cap("budget_usd"),
    ),
    "max_tokens": Restriction(
        why="the same for the run's total tokens — the cap that is always enforceable",
        tags=frozenset({"spend"}),
        imposed=_cap("max_tokens"),
    ),
    "timeout_total": Restriction(
        why="the same for the run's wall-clock, measured from the run's original start",
        tags=frozenset({"spend"}),
        imposed=_cap("timeout_total", unit="s"),
    ),
    "timeout": Restriction(
        why="how long any one step may run before it is cancelled",
        tags=frozenset({"spend"}),
        imposed=_cap("timeout", unit="s"),
    ),
}

#: The rest of ``defaults:``.
DEFAULTS_NON_CONTROLS: Mapping[str, str] = {
    "agent": "which agent a step gets when it names none — a choice, not a restriction",
    "max_parallel": (
        "how many leaves run at once: it paces the run, it withholds nothing from any of them, "
        "and the same work costs the same at any value"
    ),
    "on_unsupported": (
        "whether an option the provider cannot honour stops the run or warns — a report ABOUT "
        "other settings, each of which is classified on its own; erroring is the strict answer "
        "and never the only restriction in force"
    ),
    "on_step_failure": (
        "what happens to the other steps once one has failed: it shapes the shutdown, and "
        "withholds nothing from a step that is running or about to start"
    ),
}


def _isolation(workflow: Workflow) -> tuple[Imposed, ...]:
    if workflow.isolation == UNRESTRICTED_ISOLATION:
        return ()
    return (
        Imposed(
            f"isolation: {workflow.isolation}",
            workflow.isolation,
            frozenset({"workspace"}),
        ),
    )


def _defaults(workflow: Workflow) -> tuple[Imposed, ...]:
    return defaults_imposed(workflow.defaults)


def defaults_imposed(defaults: Defaults) -> tuple[Imposed, ...]:
    """Every cap a ``defaults:`` block sets — also used for an included document's own block."""
    out: list[Imposed] = []
    for name in sorted(DEFAULTS_CONTROLS):
        out.extend(DEFAULTS_CONTROLS[name].imposed(defaults))
    return tuple(out)


def _secret_inputs(workflow: Workflow) -> tuple[Imposed, ...]:
    return inputs_imposed(workflow.inputs)


def inputs_imposed(inputs: Mapping[str, Any]) -> tuple[Imposed, ...]:
    """``secret: true`` on any input — also used for an included document's own inputs."""
    names = sorted(name for name, spec in inputs.items() if getattr(spec, "secret", False))
    if not names:
        return ()
    return (Imposed("inputs.secret", ", ".join(names), frozenset({"secrets"})),)


#: Every field of the WORKFLOW document that constrains the run. ``defaults`` and ``inputs``
#: delegate to the tables that classify their own fields, so one restriction is reported once.
WORKFLOW_CONTROLS: Mapping[str, Restriction[Workflow]] = {
    "isolation": Restriction(
        why=(
            "where the run may write: 'worktree' (the DEFAULT) puts it on a copy on its own "
            "branch instead of the checkout a person is sitting in — the very thing an option "
            "that widens the agent's reach on disk would undo"
        ),
        tags=frozenset({"workspace"}),
        imposed=_isolation,
    ),
    "defaults": Restriction(
        why="the run-level caps (see DEFAULTS_CONTROLS): money, tokens and the two clocks",
        tags=frozenset({"spend"}),
        imposed=_defaults,
    ),
    "inputs": Restriction(
        why=(
            "a `secret: true` input is never persisted and reaches shell/python steps only as "
            "RAYSPEC_INPUT_<NAME> — a restriction on where a value may go"
        ),
        tags=frozenset({"secrets"}),
        imposed=_secret_inputs,
    ),
}

#: The rest of the workflow document.
WORKFLOW_NON_CONTROLS: Mapping[str, str] = {
    "rayspec": "the schema version the document is written in",
    "name": (
        "what the workflow is called; policy identifies a workflow by its resolved HASH "
        "(trust.require), never by this"
    ),
    "description": "prose for a human reading `rayspec workflows`",
    "agents": (
        "the agents defined here — each one is classified field by field (AGENT_CONTROLS), so "
        "counting the block again would name the same restriction twice"
    ),
    "steps": (
        "the work itself; a step's own fields are classified in STEP_CONTROLS and its agent in "
        "AGENT_CONTROLS"
    ),
    "outputs": "what the run reports when it finishes — a read of the result, not a limit on it",
}

#: One declared input (``InputSpec``). Only ``secret:`` restricts anything.
INPUT_CARRIED: Mapping[str, Carried] = {
    "secret": Carried(
        "inputs", "the value is never persisted and may be named only in a few places"
    )
}

#: The rest of one declared input.
INPUT_NON_CONTROLS: Mapping[str, str] = {
    "type": "the JSON type a passed value must have — it validates the CALLER, not the agent",
    "required": "whether the run refuses to start without it; the same",
    "default": "the value used when none is passed",
    "description": "prose for `rayspec show` and the prompt for a missing value",
    "enum": "the values that may be passed — again a check on the caller's value",
    "items": "the JSON schema of an array input's items",
    "properties": "the JSON schema of an object input's properties",
}


def workflow_controls(workflow: Workflow) -> tuple[Control, ...]:
    """Every control the workflow DOCUMENT imposes on every agent it runs."""
    out: list[Control] = []
    for name in sorted(WORKFLOW_CONTROLS):
        for imposed in WORKFLOW_CONTROLS[name].imposed(workflow):
            out.append(
                Control(
                    key=imposed.key,
                    tags=imposed.tags,
                    sources=(
                        PolicySource(layer="workflow", label=name, line=None, value=imposed.value),
                    ),
                    servers=imposed.servers,
                )
            )
    return tuple(out)


# -- the steps -------------------------------------------------------------------------------------


def _step_timeout(step: StepModel) -> tuple[Imposed, ...]:
    timeout = step.timeout
    if timeout is None:
        return ()
    return (Imposed(f"timeout: {timeout}s", f"{timeout}s", frozenset({"spend"})),)


#: Every field of a STEP that constrains the run. A step's restriction governs the agent of that
#: step and of every step nested inside it — a composite's timeout bounds its body.
STEP_CONTROLS: Mapping[str, Restriction[StepModel]] = {
    "timeout": Restriction(
        why="how long this step (and anything nested in it) may run before it is cancelled",
        tags=frozenset({"spend"}),
        imposed=_step_timeout,
    ),
}

#: Every other field of every step kind, with the one line saying why it restricts nothing.
STEP_NON_CONTROLS: Mapping[str, str] = {
    "id": "the step's name in the graph",
    "description": "prose for `rayspec plan`",
    "needs": "which steps must finish first — it orders the run, it withholds nothing",
    "when": "whether this step runs at all; skipping does less, it does not restrict what runs",
    "join": "which of the needed steps must have succeeded for this one to start",
    "always_run": "run it even after a failure — it widens when a step runs, never narrows it",
    "allow_failure": "let the run continue past a failure here; a widening, not a restriction",
    "artifacts": (
        "files the step promises to write, checked afterwards and copied into the run "
        "directory; a promise about output, not a limit on the step"
    ),
    "retry": (
        "how a FAILED attempt is repeated (RetryPolicy); every attempt is measured by the same "
        "ceilings as the first, so it moves no ceiling"
    ),
    "env": "environment for a shell/python step: it supplies values and withholds no capability",
    "output_schema": "the JSON shape the answer must have — it constrains the FORM of a reply",
    "prompt": "what the agent is asked",
    "prompt_file": "where that text is read from",
    "agent": "which agent runs it — the agent itself is classified field by field",
    "session": (
        "the ancestor step whose provider thread is continued; it shares state, it withholds "
        "none — and the usage carried over it is guarded on the option that sets it"
    ),
    "shell": "the command a shell step runs",
    "interpreter": "which shell runs it",
    "cwd": "where a shell/python step runs; naming a directory grants reach, it withholds none",
    "python": "the source a python step runs",
    "deps": "the packages that source needs",
    "loop": "the loop body and its extent (LoopSpec)",
    "each": "the collection an each step fans out over",
    "as_": "the name (`as:`) each item is bound to in the body",
    "steps": "a composite's body — every step in it is classified the same way",
    "max_parallel": "how many items of an each step run at once; it paces the fan-out",
    "on_failure": "what an each step does when one item fails",
    "approve": "a human gate on the run's progress (ApproveSpec)",
    "include": "the workflow file whose body is expanded here",
    "with_": "the inputs (`with:`) that included body is bound with",
    "stop": "end the run here with a status (StopSpec)",
}

#: ``retry:`` on a step (``RetryPolicy``).
STEP_RETRY_NON_CONTROLS: Mapping[str, str] = {
    "attempts": (
        "how many times a failed attempt is repeated: raising it spends more and lowering it "
        "spends less, and every attempt is measured by the ceilings that are already controls"
    ),
    "delay": "how long between attempts",
    "on_error": "which failures are retried at all",
}

#: ``loop:`` on a step (``LoopSpec``).
STEP_LOOP_NON_CONTROLS: Mapping[str, str] = {
    "steps": "the body, whose own steps are classified the same way",
    "max_iterations": (
        "how many times the body repeats — the loop's own extent, not a limit placed on it: a "
        "loop that did not say this would not terminate"
    ),
    "until": "the condition that ends it early",
    "on_exhausted": "whether running out of iterations fails the step or lets the run continue",
}

#: ``approve:`` on a step (``ApproveSpec``).
STEP_APPROVE_NON_CONTROLS: Mapping[str, str] = {
    "message": "what the human is asked",
    "on_reject": "what a rejection does to the run",
    "class_": (
        "the approval class (`class:`) the gate belongs to — it NAMES a class so an operator's "
        "own settings can say what may approve it; the workflow decides nothing by naming one"
    ),
    "auto_if": "a condition that approves the gate without asking — a widening, never a limit",
}

#: ``stop:`` on a step (``StopSpec``).
STEP_STOP_NON_CONTROLS: Mapping[str, str] = {
    "status": "the status the run ends with",
    "reason": "the sentence recorded with it",
}


def _agents_under(resolved: ResolvedWorkflow, path: str) -> tuple[str, ...]:
    """The agent keys of ``path`` and of every step nested inside it."""
    prefix = f"{path}/"
    return tuple(
        sorted(
            {
                key
                for step_path, key in resolved.step_agents.items()
                if step_path == path or step_path.startswith(prefix)
            }
        )
    )


def step_controls(resolved: ResolvedWorkflow) -> dict[str, tuple[Control, ...]]:
    """``agent key -> controls`` for every restriction spelled on a step.

    A step's restriction governs the agent of that step and of every step nested inside it: a
    composite's timeout bounds its body, and an agent that runs under it is subject to it. The
    same walk carries an INCLUDED document's own ``defaults:`` and ``inputs:`` to the agents of
    its body — an include is a workflow file, and its caps are as real as the root's.
    """
    out: dict[str, list[Control]] = {}

    def add(keys: Iterable[str], control: Control) -> None:
        for key in keys:
            out.setdefault(key, []).append(control)

    for path, step in resolved.all_steps():
        location = resolved.step_locations.get(path)
        for name in sorted(STEP_CONTROLS):
            for imposed in STEP_CONTROLS[name].imposed(step):
                found = location.location(name) if location is not None else None
                where = found or f"{path}.{name}"
                add(
                    _agents_under(resolved, path),
                    Control(
                        key=f"steps.{path}.{imposed.key}",
                        tags=imposed.tags,
                        sources=(
                            PolicySource(
                                layer="workflow", label=where, line=None, value=imposed.value
                            ),
                        ),
                        servers=imposed.servers,
                    ),
                )
    for path, body in resolved.includes.items():
        for imposed in (*defaults_imposed(body.defaults), *inputs_imposed(body.inputs)):
            add(
                _agents_under(resolved, path),
                Control(
                    key=f"{path}: {imposed.key}",
                    tags=imposed.tags,
                    sources=(
                        PolicySource(
                            layer="workflow",
                            label=body.workflow_name,
                            line=None,
                            value=imposed.value,
                        ),
                    ),
                    servers=imposed.servers,
                ),
            )
    return {key: tuple(controls) for key, controls in out.items()}


# -- the policy document ---------------------------------------------------------------------------

#: Policy key (as :meth:`EffectivePolicy.control_sources` spells it) → the kinds it covers.
#: ``tools.deny`` is absent on purpose: its kinds depend on the entries, so they are read off the
#: entries (:data:`POLICY_TAGS_FROM_VALUE`). A key that is missing from BOTH gets EVERY tag rather
#: than none — an unclassified control must engage every guard, never slip past all of them.
#:
#: ``approvals.classes`` is a control for the same reason every other key here is: it takes an
#: approval path away from a run that would otherwise have it. It is reported only for a class
#: that actually holds something (``allow_yes: false`` / ``require_tty: true``) — a class an
#: operator merely named forbids nothing, exactly as ``trust.require: false`` does not.
POLICY_CONTROL_TAGS: Mapping[str, frozenset[str]] = {
    "models.deny": frozenset({"model"}),
    "access.max": frozenset({"access"}),
    "mcp.allow_servers": frozenset({"mcp"}),
    "providers.allow": frozenset({"provider"}),
    "trust.require": frozenset({"trust"}),
    "approvals.classes": frozenset({"approvals"}),
    "workspace.protected_paths": frozenset({"workspace"}),
    "workspace.max_changed_files": frozenset({"workspace"}),
    "workspace.max_changed_lines": frozenset({"workspace"}),
    "budget.per_run": frozenset({"spend"}),
    "budget.per_day": frozenset({"spend"}),
    "budget.per_month": frozenset({"spend"}),
    "budget.max_consecutive_failures": frozenset({"spend"}),
    "max_consecutive_failures": frozenset({"spend"}),
    "max_concurrent_runs": frozenset({"spend"}),
}

#: Policy keys whose kinds are read off the VALUE rather than the key: ``tools.deny: [web]`` is a
#: network control and ``tools.deny: [mcp:x]`` an mcp one.
POLICY_TAGS_FROM_VALUE: frozenset[str] = frozenset({"tools.deny"})

#: Policy keys that restrict nothing. Empty, and that is the point of the document: every key of
#: ``policy.yaml`` is restrictive-only, so a key landing here would be a design change.
POLICY_NON_CONTROLS: Mapping[str, str] = {}


#: Policy keys that bound the SET of MCP servers, and how to read the bound off the document.
#: ``mcp.allow_servers`` names the servers a run may reach; ``tools.deny`` refuses entries.
#: Anything else has no opinion, which is not the same as admitting everything.
POLICY_SERVER_KEYS: frozenset[str] = frozenset({"mcp.allow_servers", "tools.deny"})


def _policy_servers(key: str, effective: EffectivePolicy) -> ServerOpinion | None:
    """What one policy key says about the MCP servers the run may reach.

    Neither key defines a server. ``mcp.allow_servers`` is a list of NAMES: it narrows the set a
    run may reach and says nothing about what any of them is, which is exactly why it cannot
    authorise a definition a workflow supplies elsewhere.
    """
    if key == "mcp.allow_servers":
        return ServerOpinion(admits=effective.allowed_mcp_servers() or frozenset())
    if key == "tools.deny":
        return tool_entry_servers(effective.denied_tools(), allow_list=False)
    return None


def policy_controls(effective: EffectivePolicy | None) -> tuple[Control, ...]:
    """Every control the policy layers impose, tagged by the kind of restriction it is."""
    if effective is None:
        return ()
    denied = effective.denied_tools()
    out: list[Control] = []
    for key, sources in sorted(effective.control_sources().items()):
        if key in POLICY_TAGS_FROM_VALUE:
            tags = tool_entry_tags(denied)
        else:
            tags = POLICY_CONTROL_TAGS.get(key, CONTROL_TAGS)
        out.append(
            Control(key=key, tags=tags, sources=sources, servers=_policy_servers(key, effective))
        )
    return tuple(out)


# -- outside the workflow --------------------------------------------------------------------------


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
    "cancel.json": ExternalControl(
        False,
        "the PRD-07 cancellation marker `rayspec cancel` writes beside run.json; it asks the run "
        "to stop at the next step boundary, which withholds nothing a running step may already do",
    ),
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


#: Every option of every CLI command, classified — the fourth place a restriction could be
#: expressed. Only one of them adds a restriction to a run: ``--worktree``, which the run command
#: writes onto the document BEFORE the policy check, so what the check reads is ``isolation``.
#: Every other flag chooses what is printed, where it goes, which run is addressed, or LOOSENS
#: something (``--yes``, ``--allow-unsupported``, ``--no-worktree``) — and a flag that loosens can
#: never be the reason an escape hatch should have been shut.
CLI_FLAGS: Mapping[str, ExternalControl] = {
    "--worktree": ExternalControl(
        True,
        "--worktree/--no-worktree overrides isolation:. The tightening half is applied to the "
        "document before the workflow is validated, so the check sees `isolation: worktree` — a "
        "workspace control — exactly as if the file had said so; --no-worktree only removes a "
        "restriction the check has already read",
    ),
    "--locked": ExternalControl(
        False,
        "enforces the model lockfile, which is classified as an external control on its "
        "presence — the check counts it whether or not this flag is passed",
    ),
    "--dry-run": ExternalControl(
        False, "stubs every provider: no provider_options block reaches a provider at all"
    ),
    "--stubs": ExternalControl(False, "the scripted answers a dry run uses instead of a provider"),
    "--stubs-from": ExternalControl(False, "the stored run whose recorded answers to replay"),
    "--stubs-init": ExternalControl(False, "write a stub scaffold and exit"),
    "--exec-shell": ExternalControl(False, "run shell/python steps in a dry run — a widening"),
    "--allow-unsupported": ExternalControl(
        False, "downgrades an unsupported option from an error to a warning — a widening"
    ),
    "--approve-class": ExternalControl(
        False, "pre-authorises one class of gate — a widening, and only of what the class permits"
    ),
    "--no-interactive": ExternalControl(
        False, "pause at a gate instead of prompting; it withholds nothing from an agent"
    ),
    "--fail-fast": ExternalControl(
        False, "cancel the running siblings once one step has failed — the shutdown's shape"
    ),
    "--force": ExternalControl(False, "proceed past a guard that would have stopped: a widening"),
    "--resume": ExternalControl(False, "which run to continue"),
    "--base": ExternalControl(False, "the branch a worktree is cut from"),
    "--repo": ExternalControl(False, "which project or checkout the run happens in"),
    "--root": ExternalControl(False, "which project root to read files from"),
    "--wait-slot": ExternalControl(False, "how long to wait for the workspace lock"),
    "--input": ExternalControl(False, "an input value the run is given"),
    "--inputs-file": ExternalControl(False, "the file those values are read from"),
    "--json": ExternalControl(False, "print JSONL instead of text"),
    "--output": ExternalControl(False, "which output format to print"),
    "--quiet": ExternalControl(False, "print less"),
    "--verbose": ExternalControl(False, "print more"),
    "--yes": ExternalControl(False, "auto-approve gates — a widening"),
    "--mark": ExternalControl(False, "the status a cancelled run is recorded with"),
    "--now": ExternalControl(False, "cancel by signalling the process instead of the flag"),
    "--detach": ExternalControl(
        False, "background the run behind a forked child; what it may do is unchanged"
    ),
    "--detached-child": ExternalControl(
        False, "internal: the run directory a --detach launcher pre-created for its child"
    ),
    "--step": ExternalControl(False, "which step to report on"),
    "--select": ExternalControl(False, "which test cases to run"),
    "--case": ExternalControl(False, "the same, by name"),
    "--junit": ExternalControl(False, "where to write a JUnit report"),
    "--shell": ExternalControl(False, "the expression `rayspec eval` renders"),
    "--render": ExternalControl(False, "render a step's body in `rayspec plan`"),
    "--risk": ExternalControl(False, "print the risk review of a plan"),
    "--full": ExternalControl(False, "print the whole explanation"),
    "--raw": ExternalControl(False, "print the provider's own stream"),
    "--stream": ExternalControl(False, "which stream to print"),
    "--follow": ExternalControl(False, "keep printing as the run goes"),
    "--exit-code": ExternalControl(False, "make `logs --follow` exit with the run's code"),
    "--check": ExternalControl(False, "report whether the lockfile is up to date and exit"),
    "--commands": ExternalControl(False, "print the audit log's command entries"),
    "--since": ExternalControl(False, "the window a cost report covers"),
    "--workflow": ExternalControl(False, "which workflow a cost report covers"),
    "--provider": ExternalControl(False, "which adapter `rayspec doctor` checks"),
    "--probe": ExternalControl(False, "let doctor actually call the adapter"),
    "--kind": ExternalControl(False, "which scaffold `rayspec init` writes"),
    "--from": ExternalControl(False, "the example `rayspec init` copies"),
    "--no-skill": ExternalControl(False, "skip writing the packaged skill"),
    "--no-init": ExternalControl(False, "`rayspec quickstart` writes no scaffold"),
    "--no-run": ExternalControl(
        False, "`rayspec quickstart` skips its own dry run, which stubs every provider anyway"
    ),
    "--out": ExternalControl(False, "where `rayspec schema` writes"),
    "--values": ExternalControl(False, "the completion values to print"),
    "--all": ExternalControl(False, "list the runs of every project, not just this one"),
    "--limit": ExternalControl(False, "how many runs to list"),
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


@dataclass(frozen=True, slots=True)
class ServerControls:
    """ "May this MCP server be reached" — folded over EVERY control in force.

    The fold is the whole point. ``mcp_servers`` in a ``provider_options`` block is merged into
    the same server set the agent's neutral ``mcp:`` field feeds, so the question it raises is a
    question about all of the controls at once. Asking one of them (the policy document) answered
    "nothing denies this" for a run whose agent declared its own servers, denied the ``mcp`` tool
    group, switched its network off and asked for read-only access.

    The rule is the same inversion the key allow-list uses, applied to server names: a server is
    permitted only when **some** control in force names it and **no** control refuses it. "Nobody
    named this server" is the "nobody knows" case, and under a control that is a refusal.

    Permission is one of two questions, and conflating them inverted the invariant. A name says
    which servers may be reached; a DEFINITION says what one of them is — the process rayspec
    starts, the endpoint it opens, the argv behind it. ``mcp.allow_servers`` and a ``tools.allow``
    entry contribute names only, and both are written where the run can already reach: the
    operator writes the first, the agent writes the second. So :meth:`refusing` answers the name
    question and :meth:`defining` answers the other, and a caller judging a definition the
    WORKFLOW supplies has to ask both — a matching name is necessary, never sufficient.
    """

    admitted: tuple[tuple[frozenset[str], tuple[PolicySource, ...]], ...] = ()
    defined: Mapping[str, tuple[PolicySource, ...]] = field(default_factory=dict)
    denied: Mapping[str, tuple[PolicySource, ...]] = field(default_factory=dict)
    denied_all: tuple[PolicySource, ...] = ()

    @property
    def named(self) -> bool:
        """Whether any control in force names MCP servers at all."""
        return bool(self.admitted)

    @property
    def admits(self) -> frozenset[str]:
        """The names every control that has an opinion admits — the intersection of them."""
        if not self.admitted:
            return frozenset()
        allowed = self.admitted[0][0]
        for names, _sources in self.admitted[1:]:
            allowed &= names
        return allowed

    def sources_for(self, servers: Iterable[str]) -> tuple[PolicySource, ...]:
        """The lines of every control that names servers but leaves ``servers`` out."""
        wanted = frozenset(servers)
        out: list[PolicySource] = []
        for names, sources in self.admitted:
            if not wanted <= names:
                out.extend(sources)
        return tuple(out)

    def refusing(self, server: str) -> tuple[PolicySource, ...] | None:
        """The controls refusing ``server``, or ``None`` when every one of them permits it.

        An empty tuple is a refusal too: it means no control in force names any server, so there
        is nothing to quote beyond the controls themselves — the caller names those.

        This is the NAME question only. A ``None`` here means every control permits a server so
        called; it does not mean any of them said what that server is (:meth:`defining`).
        """
        if self.denied_all:
            return self.denied_all
        refused = self.denied.get(server)
        # `is not None`, never truthiness: an empty tuple is a control that refuses this server
        # and has no line to quote, which this method's own contract calls a refusal. Reading it
        # as "not refused" is a refusal disappearing because its provenance happened to be empty
        if refused is not None:
            return refused
        if not self.named:
            return ()
        if server in self.admits:
            return None
        return self.sources_for((server,))

    def defining(self, server: str) -> tuple[PolicySource, ...] | None:
        """The controls that DEFINE ``server``, or ``None`` when none of them does.

        Only a control carrying the command or endpoint behind a name appears here — today the
        agent's own ``mcp:`` block, which both adapters merge a raw ``provider_options`` block
        UNDER, so its declaration wins on a name collision. An allow-list of names defines
        nothing, and answering "may this definition be used?" from one is how a restrictive-only
        policy key came to grant a capability.
        """
        sources = self.defined.get(server)
        return None if sources is None else sources  # () = defined, with no line to quote

    @property
    def definable(self) -> frozenset[str]:
        """The servers a workflow may name in a raw block: defined somewhere, refused nowhere."""
        return frozenset(name for name in self.defined if self.refusing(name) is None)


def merged_controls(
    controls: Sequence[Control],
) -> tuple[dict[str, tuple[PolicySource, ...]], dict[str, frozenset[str]], ServerControls]:
    """``(key -> sources, key -> tags, folded server view)`` for a list of controls."""
    sources: dict[str, tuple[PolicySource, ...]] = {}
    tags: dict[str, frozenset[str]] = {}
    admitted: list[tuple[frozenset[str], tuple[PolicySource, ...]]] = []
    defined: dict[str, tuple[PolicySource, ...]] = {}
    denied: dict[str, tuple[PolicySource, ...]] = {}
    denied_all: list[PolicySource] = []
    for control in controls:
        sources[control.key] = (*sources.get(control.key, ()), *control.sources)
        tags[control.key] = tags.get(control.key, frozenset()) | control.tags
        opinion = control.servers
        if opinion is None:
            continue
        if opinion.admits is not None:
            admitted.append((opinion.admits, control.sources))
        # a UNION, unlike admits: each definition stands on its own, and a control that defines
        # none must not erase one another control wrote down
        for name in sorted(opinion.defines):
            defined[name] = (*defined.get(name, ()), *control.sources)
        for name in sorted(opinion.denies):
            denied[name] = (*denied.get(name, ()), *control.sources)
        if opinion.denies_all:
            denied_all.extend(control.sources)
    servers = ServerControls(
        admitted=tuple(admitted),
        defined=defined,
        denied=denied,
        denied_all=tuple(denied_all),
    )
    return sources, tags, servers


__all__ = [
    "AGENT_COMMANDS_CARRIED",
    "AGENT_CONTROLS",
    "AGENT_MCP_NON_CONTROLS",
    "AGENT_NON_CONTROLS",
    "AGENT_TOOLS_CARRIED",
    "CLI_FLAGS",
    "CONTROL_TAGS",
    "DEFAULTS_CONTROLS",
    "DEFAULTS_NON_CONTROLS",
    "EXTERNAL_CONTROLS",
    "INPUT_CARRIED",
    "INPUT_NON_CONTROLS",
    "POLICY_CONTROL_TAGS",
    "POLICY_NON_CONTROLS",
    "POLICY_SERVER_KEYS",
    "POLICY_TAGS_FROM_VALUE",
    "STEP_APPROVE_NON_CONTROLS",
    "STEP_CONTROLS",
    "STEP_LOOP_NON_CONTROLS",
    "STEP_NON_CONTROLS",
    "STEP_RETRY_NON_CONTROLS",
    "STEP_STOP_NON_CONTROLS",
    "UNRESTRICTED_ACCESS",
    "UNRESTRICTED_ISOLATION",
    "WEB_TOOL_ENTRIES",
    "WORKFLOW_CONTROLS",
    "WORKFLOW_NON_CONTROLS",
    "Carried",
    "Control",
    "ExternalControl",
    "ExternalControls",
    "Imposed",
    "Restriction",
    "ServerControls",
    "ServerOpinion",
    "agent_controls",
    "defaults_imposed",
    "discover_external_controls",
    "inputs_imposed",
    "merged_controls",
    "policy_controls",
    "step_controls",
    "tool_entry_servers",
    "tool_entry_tags",
    "workflow_controls",
]

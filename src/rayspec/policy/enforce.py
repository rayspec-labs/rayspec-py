# SPDX-License-Identifier: Apache-2.0
"""Applying an :class:`~rayspec.policy.layers.EffectivePolicy` to a resolved workflow.

Boundary: pure checks. This module reports problems and describes the tool denials a caller
should fold into an agent; it never mutates the workflow, opens a file or talks to a provider.
:mod:`rayspec.loader.validate` calls it once and turns the result into validation errors, so a
policy violation is a load-time failure with a file and a line — not a surprise halfway through
a paid run.

Two rules shape every message here:

* name the layer. "denied by policy" without the file and line that denies it is useless to the
  person who has to fix it, so every problem quotes the policy location and a way out.
* never claim enforcement that will not happen. A denial the resolved provider cannot express is
  reported as an advisory *warning* instead of being folded in silently.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rayspec.policy.controls import (
    CONTROL_TAGS,
    Control,
    ExternalControls,
    ServerControls,
    agent_controls,
    merged_controls,
    policy_controls,
    step_controls,
    workflow_controls,
)
from rayspec.policy.layers import EffectivePolicy, PolicySource, sources_text
from rayspec.policy.trust import TrustStore
from rayspec.schema import provider_option_block

if TYPE_CHECKING:  # type-only: importing the loader at runtime would close an import cycle
    from rayspec.loader.loader import ResolvedAgent, ResolvedWorkflow
    from rayspec.providers.base import ProviderCapabilities

#: Capability name a provider declares (in ``ProviderCapabilities.extra``) when it hands rayspec
#: its tool calls before they run — the only way an agent ``commands:`` block can be enforced.
COMMAND_POLICY_CAPABILITY = "command_policy"

#: Neutral tool groups (mirrors ``providers.base.TOOL_GROUPS``; duplicated so this package keeps
#: its "no provider imports" boundary).
TOOL_GROUPS: frozenset[str] = frozenset({"read", "edit", "shell", "web", "agent", "mcp"})

#: ``provider_options.codex.approval_mode`` value that grants nothing: every sandbox escalation
#: request the agent makes is refused. Any other value answers them on the agent's behalf.
SAFE_APPROVAL_MODE = "deny_all"


@dataclass(frozen=True, slots=True)
class ControlsInForce:
    """The controls governing one agent: which ones, of what kind, and where each came from.

    ``sources`` is keyed the way each control is spelled — the policy file's own keys
    (``tools.deny``, ``access.max``), the agent's own fields (``access: read-only``,
    ``network: off``, ``max_turns: 2``) and the external ones (``lockfile``,
    ``providers.claude``) — and carries the lines that impose each one, so a refusal can always
    quote the file a person has to edit. ``tags`` says which KIND of restriction each control is
    (:data:`~rayspec.policy.controls.CONTROL_TAGS`); a value guard matches on those rather than
    on a spelling.

    **This is everything a guard may see.** There is deliberately no handle on the policy
    document, or on any other single source, because a guard that can reach one source will
    eventually decide from one source: ``mcp_servers`` asked the policy file whether a server was
    allowed and therefore admitted an arbitrary stdio server past the agent's own ``mcp:`` set,
    its ``tools.deny: [mcp]``, its ``network: off`` and its ``access: read-only`` — every one of
    which the trigger had already counted as a control. The trigger was total and the guard was
    not. So the union the trigger builds is the union the guards read, and ``servers`` is that
    union's answer to "may this MCP server be reached"
    (:class:`~rayspec.policy.controls.ServerControls`).
    """

    sources: Mapping[str, tuple[PolicySource, ...]] = field(default_factory=dict)
    tags: Mapping[str, frozenset[str]] = field(default_factory=dict)
    servers: ServerControls = field(default_factory=ServerControls)

    @classmethod
    def of(cls, controls: Iterable[Control]) -> ControlsInForce:
        """Fold every control governing one agent into one view of them."""
        sources, tags, servers = merged_controls(tuple(controls))
        return cls(sources=sources, tags=tags, servers=servers)

    @property
    def governed(self) -> bool:
        """True when the agent is subject to any control at all."""
        return bool(self.sources)

    @property
    def kinds(self) -> frozenset[str]:
        """Every kind of restriction in force, as tags. Unclassified controls cover them all."""
        return frozenset().union(*self.tags.values()) if self.tags else frozenset()

    def covering(self, tags: Iterable[str]) -> tuple[str, ...]:
        """The controls whose kind is one of ``tags`` — what a guard names in its refusal."""
        wanted = frozenset(tags)
        return tuple(sorted(key for key, kind in self.tags.items() if kind & wanted))

    def named(self, keys: Iterable[str] | None = None, *, limit: int = 4) -> str:
        """``access.max and network: off (.rayspec/policy.yaml:2, agents/a.yaml:4)``.

        A hardened agent can be under a dozen controls at once and a refusal that recites all of
        them stops being read. Past ``limit`` the rest are counted rather than listed — the
        message still names files a person can open, and the count says there are more.
        """
        chosen = sorted(set(self.sources) & set(keys)) if keys is not None else sorted(self.sources)
        shown = chosen[:limit]
        rest = len(chosen) - len(shown)
        sources = [source for key in shown for source in self.sources[key]]
        where = sources_text(sources)
        joined = ", ".join(shown) + f" and {rest} more" if rest else " and ".join(shown)
        return f"{joined} ({where})" if where else joined

    def named_covering(self, tags: Iterable[str]) -> str:
        """:meth:`named` for the controls of a KIND, falling back to all of them.

        A guard's message should name the control it is actually about, but a guard that runs
        under every control has to say something when none of them carries the kind it prefers to
        quote — and "" is how a refusal ends up naming no file at all.
        """
        chosen = self.covering(tags)
        return self.named(chosen) if chosen else self.named()

    @property
    def allowed_servers(self) -> str:
        """The MCP servers the controls in force do name, for the "use one of these" half."""
        return ", ".join(sorted(self.servers.admits)) or "(none)"


#: A value-level check on one allow-listed option: the parts of the requested value the controls
#: refuse, each as ``(key path suffix, message)`` — empty meaning the request is permitted.
OptionCheck = Callable[[object, ControlsInForce], tuple[tuple[str, str], ...]]


@dataclass(frozen=True, slots=True)
class Inert:
    """A NAMED statement that one allow-listed key needs no value guard, and why.

    An allow-listed key with no guard passes unread under every control, which is a second unsafe
    default hiding inside a safe design — ``usage_baseline`` sat there as "accounting only" while
    setting the number every spend ceiling is measured against. So "no guard" cannot be reached by
    omission: :attr:`AllowedOption.offenders` has no default, and an entry that wants none has to
    say :data:`INERT_BECAUSE` out loud. ``because`` has to be CHECKABLE — every entry here is
    paired with the test that holds the claim to the code
    (``tests/policy/test_provider_options.py::INERT_PROOFS``), and an unpaired one fails.
    """

    because: str


#: How :class:`Inert` is spelled at the call site: ``offenders=INERT_BECAUSE("…")``.
INERT_BECAUSE = Inert


@dataclass(frozen=True, slots=True)
class AllowedOption:
    """One ``provider_options`` key path rayspec can state the effect of.

    ``summary`` is that statement, in one line — the reasoning that earned the key its place on
    the allow-list, kept next to the entry so the next reader can check it rather than trust it.
    ``offenders`` is either a guard that inspects the requested VALUE and reports the parts a
    control refuses, or an :class:`Inert` saying in one line why no guard is needed; it has no
    default, so an entry cannot become unguarded by omission. ``guarded_by`` narrows when a guard
    runs to the KINDS of control it is about (:data:`~rayspec.policy.controls.CONTROL_TAGS`),
    empty meaning "under every control" — which is what every entry that ships says. Narrowing has
    to be EARNED: a guard that claims a kind and is silent under a control of another kind is the
    shape every one of these bypasses had, so ``tests/policy/test_guard_completeness.py`` runs each
    guard once per (kind, source that can carry that kind) and fails when it does not fire.

    A guard exists so the check can refuse the *value* a control refuses instead of the key:
    refusing ``mcp_servers`` outright would block the server the controls in force do name, and a
    control that blocks the permitted case teaches people to switch the control off.
    """

    summary: str
    offenders: OptionCheck | Inert
    guarded_by: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        unknown = self.guarded_by - CONTROL_TAGS
        if unknown:  # pragma: no cover - a typo here would silently disable the guard
            raise ValueError(f"unknown control tags: {sorted(unknown)}")
        if isinstance(self.offenders, Inert) and not self.offenders.because.strip():
            raise ValueError("INERT_BECAUSE needs the one line saying why no guard is needed")
        if isinstance(self.offenders, Inert) and self.guarded_by:
            raise ValueError("an inert key has no guard to narrow with guarded_by")


def _refused_mcp_servers(value: object, controls: ControlsInForce) -> tuple[tuple[str, str], ...]:
    """Each server the block would merge in, against EVERY control in force.

    An MCP server is a capability channel: a process rayspec starts (a stdio ``command``) or an
    endpoint it reaches, offering the agent whatever tools it likes. Both adapters merge this
    block UNDER the agent's own ``mcp:`` servers, so a name the agent already declares is
    overridden by the agent's own definition and a name it does not declare is a channel that
    exists only here — where ``rayspec plan``, a reviewer and the neutral checks do not look.

    So the same inversion the key allow-list uses is applied to the server names: while a control
    is in force, a server passes only if some control NAMES it (the agent's own ``mcp:`` block,
    or a policy ``mcp.allow_servers``) and none refuses it (a ``tools.deny`` naming ``mcp`` or
    ``mcp:<server>``, a non-empty ``tools.allow`` that does not, an ``mcp.allow_servers`` that
    leaves it out). "Nobody named this server" is the "nobody knows" case, and under a control
    that is a refusal — which is what the answer has to be when the trigger has already counted
    the agent's ``mcp:``, its tool lists, its network and its access level as controls.

    The answer comes from :class:`~rayspec.policy.controls.ServerControls`, folded over every
    source, because a guard that consults one source decides from one source.
    """
    if not isinstance(value, Mapping):
        return (
            (
                "",
                "must be a mapping of server name -> config; policy cannot tell which servers "
                "this would add",
            ),
        )
    out: list[tuple[str, str]] = []
    for name in sorted(str(key) for key in value):
        refusing = controls.servers.refusing(name)
        if refusing is None:
            continue  # a server the controls in force name: the permitted case
        where = sources_text(refusing) or controls.named()
        out.append(
            (
                name,
                f"MCP server {name!r} is not one the controls in force name ({where}), and both "
                "adapters merge this block under the agent's own mcp: servers — so it would add "
                "a server (and the process or endpoint behind it) that nothing else in the run "
                f"declares. Servers named right now: {controls.allowed_servers}. Declare it under "
                "the agent's own mcp: block, where policy, rayspec plan and review all read it",
            )
        )
    return tuple(out)


#: Environment variable names rayspec has WRITTEN DOWN the effect of — empty, and that is the
#: point. ``env`` was guarded by a two-prefix denylist (``ANTHROPIC_``, ``CLAUDE_``), which is the
#: enumeration this package exists to avoid: ``PATH``, ``NODE_OPTIONS``, ``NODE_EXTRA_CA_CERTS``,
#: ``HTTPS_PROXY`` and ``SSL_CERT_FILE`` reconfigure the CLI's runtime and its network far more
#: thoroughly than any vendor variable does, and the next one is always a name nobody listed. A
#: variable is read inside a process rayspec only starts, so which of the options it computed a
#: given name overrides is exactly what rayspec cannot say. Adding an entry here means writing
#: that answer down for one name; until someone does, the honest set is empty.
REASONED_ENV_NAMES: Mapping[str, str] = {}


def _refused_env(provider: str) -> OptionCheck:
    """Refuse every variable rayspec has not reasoned about — which is all of them, for now."""

    def offenders(value: object, controls: ControlsInForce) -> tuple[tuple[str, str], ...]:
        if not isinstance(value, Mapping):
            return (
                (
                    "",
                    "must be a mapping of NAME -> value; policy cannot tell which variables this "
                    "would set",
                ),
            )
        out: list[tuple[str, str]] = []
        for name in sorted(str(key) for key in value):
            if name in REASONED_ENV_NAMES:
                continue
            out.append(
                (
                    name,
                    f"{name} is set inside the {provider} CLI process rayspec starts, and rayspec "
                    "cannot say which of the options it computed a given variable overrides — the "
                    f"same 'nobody knows' the allow-list exists for, while {controls.named()} "
                    f"{'is' if len(controls.sources) == 1 else 'are'} in force. Set it in "
                    f"providers.{provider}.env in config.yaml (the machine owner's file, merged "
                    "OVER this block), in the env: of the mcp: server that needs it, or in the "
                    "env: of the shell/python step that needs it",
                )
            )
        return tuple(out)

    return offenders


#: Counter names of ``usage_baseline``; anything else in the block is a counter rayspec does not
#: know, which is refused for the same reason a positive one is.
USAGE_COUNTERS: frozenset[str] = frozenset(
    {"input", "cached_input", "cache_write", "output", "reasoning"}
)


def _refused_usage_baseline(
    value: object, controls: ControlsInForce
) -> tuple[tuple[str, str], ...]:
    """``usage_baseline`` sets the number every spend ceiling is measured against.

    The codex adapter reports a turn's usage as the thread's cumulative total MINUS this
    baseline, clamped at zero; the cost is derived from that same figure and the run's totals sum
    it. A baseline above what the thread will reach therefore reports zero tokens and zero cost
    for every turn on it — so under a spend ceiling only a baseline that subtracts nothing
    passes. Nothing is lost: the adapter carries the counters over a resumed thread itself.
    """
    if not isinstance(value, Mapping):
        return (
            (
                "",
                "must be a mapping of usage counters; policy cannot tell how much reported spend "
                "this would subtract",
            ),
        )
    named = controls.named_covering(("spend",))
    out: list[tuple[str, str]] = []
    for name, raw in sorted(value.items(), key=lambda item: str(item[0])):
        key = str(name)
        try:
            counted = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            counted = -1  # a counter policy cannot read is a counter it cannot clear
        if key in USAGE_COUNTERS and counted == 0:
            continue  # a baseline of zero subtracts nothing: the permitted case
        out.append(
            (
                key,
                f"{raw!r} is subtracted from the usage {named} is measured against, and from what "
                "the run REPORTS: a turn on a resumed thread reports its cumulative total minus "
                "this, clamped at zero, its cost is derived from that same figure, and spend.json, "
                "run.json and `rayspec costs` all read it — so a baseline the thread never reaches "
                "reports no spend at all. Remove it; the adapter carries a resumed thread's "
                "counters itself",
            )
        )
    return tuple(out)


def _refused_approval_mode(value: object, controls: ControlsInForce) -> tuple[tuple[str, str], ...]:
    """``auto_review`` grants the sandbox escalations the access control exists to withhold."""
    if str(value) == SAFE_APPROVAL_MODE:
        return ()
    named = controls.named_covering(("access", "network"))
    return (
        (
            "",
            f"{str(value)!r} answers the agent's sandbox escalation requests for "
            f"it, granting from inside the workflow the power {named} "
            f"{'is' if len(controls.sources) == 1 else 'are'} there to withhold; use "
            f"approval_mode: {SAFE_APPROVAL_MODE}, which is the default and refuses every one of "
            "them",
        ),
    )


#: ``provider_options`` key paths rayspec has REASONED ABOUT, per provider id and per key path
#: inside that provider's own block. While any control governs an agent (see
#: :func:`check_provider_options`) this is an ALLOW-list: a key that is not on it is refused at
#: load time.
#:
#: The default matters more than the entries. ``provider_options`` is a raw pass-through that the
#: adapters apply *over* the options rayspec computed, so the question every key raises is "could
#: this widen what the control narrowed?" — and for a key rayspec has never looked at, the honest
#: answer is "nobody knows". Enumerating the dangerous keys cannot work: ``extra_args`` alone
#: re-emits ANY Claude CLI flag, after the ones rayspec computed, where last wins; ``settings``
#: carries a whole permissions document; and the SDK grows fields between releases. Refusing the
#: unknown and listing the understood inverts that: a new SDK field is covered on the day it
#: ships, and the list only grows when someone can write down what a key does.
#:
#: A control-free agent is untouched — the escape hatch is still an escape hatch when nothing is
#: being escaped. What turns the allow-list on is a control OF ANY KIND, from any source and in
#: any schema: a ``policy.yaml`` key (:meth:`EffectivePolicy.control_sources`), a security-shaped
#: field the agent sets on itself (:data:`~rayspec.policy.controls.AGENT_CONTROLS`), a
#: restriction the workflow sets over every agent it runs
#: (:data:`~rayspec.policy.controls.WORKFLOW_CONTROLS` — ``isolation:``, the ``defaults:`` caps,
#: a ``secret:`` input), one spelled on the step that runs it
#: (:data:`~rayspec.policy.controls.STEP_CONTROLS`) or an external one
#: (:data:`~rayspec.policy.controls.EXTERNAL_CONTROLS` — the model lockfile, the machine owner's
#: ``providers:`` settings). That trigger is classified rather than listed, and proved total
#: against every schema a restriction can be written in, for the same reason this list is: an
#: enumeration in the trigger is worth exactly as much as an enumeration in the allow-list — and
#: a completeness test aimed at ONE schema is silent about the others.
ALLOWED_PROVIDER_OPTIONS: Mapping[str, Mapping[tuple[str, ...], AllowedOption]] = {
    "claude": {
        ("env",): AllowedOption(
            "extra environment variables for the CLI subprocess, merged UNDER both the variables "
            "rayspec computes and the machine owner's providers.claude.env, so a workflow can "
            "add one but never displace one of theirs (build_options builds env in that order). "
            "While a control is in force it carries nothing: a variable name rayspec has not "
            "reasoned about is refused, and REASONED_ENV_NAMES is empty",
            _refused_env("claude"),
        ),
        ("mcp_servers",): AllowedOption(
            "extra MCP servers, merged UNDER the agent's own mcp: block. While a control is in "
            "force only a server the controls themselves name passes — the agent's own mcp: set, "
            "or a policy mcp.allow_servers",
            _refused_mcp_servers,
        ),
        ("max_thinking_tokens",): AllowedOption(
            "how many tokens a turn may think for. It moves what a turn costs, never what a cost "
            "is measured against: the thinking tokens are reported as usage like any other and "
            "counted by the same ceilings",
            INERT_BECAUSE(
                "it changes no option build_options computes and no number a ceiling is compared "
                "against — a turn that thinks more is measured for thinking more"
            ),
        ),
        ("max_buffer_size",): AllowedOption(
            "how much CLI stdout the transport buffers before it gives up",
            INERT_BECAUSE(
                "a buffer size the transport applies to bytes it has already received; it "
                "changes no option build_options computes and reaches no vendor process"
            ),
        ),
        ("load_timeout_ms",): AllowedOption(
            "how long to wait for the CLI process to come up",
            INERT_BECAUSE(
                "how long the transport waits for a process to start before failing; the step's "
                "own deadline is enforced by the engine around the whole call, so a longer wait "
                "cannot outlast it"
            ),
        ),
        ("user",): AllowedOption(
            "an opaque end-user id forwarded to the API",
            INERT_BECAUSE("a label carried to the vendor; it selects nothing and grants nothing"),
        ),
    },
    "codex": {
        ("config", "mcp_servers"): AllowedOption(
            "extra MCP servers, merged UNDER the agent's own mcp: block. While a control is in "
            "force only a server the controls themselves name passes — the agent's own mcp: set, "
            "or a policy mcp.allow_servers",
            _refused_mcp_servers,
        ),
        ("config", "model_reasoning_summary"): AllowedOption(
            "how much of the model's reasoning is summarised into the stream: transcript "
            "verbosity, which grants no capability and withholds none",
            INERT_BECAUSE(
                "it decides how much of the reasoning is written into the transcript; the run "
                "does the same work either way"
            ),
        ),
        ("approval_mode",): AllowedOption(
            "how a sandbox escalation request is answered: deny_all (the default) refuses every "
            "one of them, auto_review grants them",
            _refused_approval_mode,
        ),
        ("ephemeral",): AllowedOption(
            "do not persist the thread — it withholds state, it grants nothing",
            INERT_BECAUSE(
                "it only asks the vendor not to keep the thread; rayspec's own run record, "
                "events and audit log are written by rayspec and are not affected"
            ),
        ),
        ("usage_baseline",): AllowedOption(
            "usage counters SUBTRACTED from a resumed thread's totals — the number every spend "
            "ceiling is then measured against, not a note in a ledger: a turn reports its "
            "cumulative total minus this, clamped at zero, and its cost is derived from that "
            "same figure",
            _refused_usage_baseline,
        ),
    },
}


@dataclass(frozen=True, slots=True)
class PolicyProblem:
    """One policy violation: where in the workflow, what is wrong, and where to look."""

    where: str
    message: str
    location: str | None = None


@dataclass(slots=True)
class PolicyReport:
    """What the policy pass found.

    ``tool_denials`` maps an agent key to the entries the caller should add to that agent's
    ``tools.deny`` — the part of the policy that is actually enforced rather than merely checked.
    ``policy_layers`` / ``policy_searched`` are the positive signal: which files were read and,
    when none were, which paths were looked at (see
    :func:`~rayspec.policy.layers.policy_note`).
    """

    errors: list[PolicyProblem] = field(default_factory=list)
    warnings: list[PolicyProblem] = field(default_factory=list)
    tool_denials: dict[str, tuple[str, ...]] = field(default_factory=dict)
    policy_layers: tuple[str, ...] = ()
    policy_searched: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def _location(agent: ResolvedAgent, *fields: str) -> str | None:
    """The best ``<file>:<line>`` for a field of ``agent``: the field itself, else its block."""
    for name in fields:
        found = agent.location(name)
        if found is not None:
            return found
    return next(iter(agent.locations.values()), None)


def _server_of(entry: str) -> str | None:
    """``mcp:github/create`` → ``github``; ``None`` for anything that is not an MCP entry."""
    if not entry.startswith("mcp:"):
        return None
    return entry[4:].split("/", 1)[0] or None


def _enforceable(entry: str, agent: ResolvedAgent, caps: ProviderCapabilities | None) -> bool:
    """Whether the resolved provider can actually be told to deny ``entry``.

    Unknown capabilities (no registry) count as enforceable: the neutral spelling is the
    contract, and an adapter that cannot honour it fails loudly rather than quietly.
    """
    if caps is None:
        return True
    if entry.startswith("mcp:"):
        return "mcp" in caps.tool_groups
    if entry in TOOL_GROUPS:
        return entry in caps.tool_groups
    prefix, _, _name = entry.partition(":")
    if prefix and prefix == agent.provider:
        return bool(caps.raw_tool_names)
    return False  # a raw name addressed at another provider is ignored by the adapters


def check_agent_controls(
    resolved: ResolvedWorkflow,
    *,
    capabilities_for: Callable[[str], ProviderCapabilities | None] | None = None,
) -> PolicyReport:
    """Check the neutral per-agent controls — ``network:`` and ``commands:``.

    These are workflow fields rather than policy-file keys, so they are checked whether or not a
    ``policy.yaml`` exists. ``network: off`` maps onto the one mechanism both shipped providers
    really have: their web tools are denied. That is a narrower promise than a firewall and the
    documentation says so — a shell command the agent runs can still open a socket unless the
    provider's own sandbox stops it.
    """
    report = PolicyReport()
    for key in sorted(resolved.agents):
        agent = resolved.agents[key]
        caps = None if capabilities_for is None else capabilities_for(agent.provider)
        _check_network(key, agent, report, caps)
        _check_commands(agent, report, caps)
    return report


def _check_network(
    key: str,
    agent: ResolvedAgent,
    report: PolicyReport,
    caps: ProviderCapabilities | None,
) -> None:
    if agent.network != "off":
        return
    if "web" in agent.tools.allow:
        report.errors.append(
            _problem(
                agent,
                "network",
                "network: off contradicts tools.allow: web — drop one of them",
                "tools",
            )
        )
        return
    if caps is not None and "web" not in caps.tool_groups:
        report.warnings.append(
            _problem(
                agent,
                "network",
                f"network: off cannot be enforced on provider {agent.provider!r} — it has no "
                "'web' tool group, so the setting is advisory there",
            )
        )
        return
    if "web" not in agent.tools.deny:
        report.tool_denials[key] = ("web",)


def _check_commands(
    agent: ResolvedAgent, report: PolicyReport, caps: ProviderCapabilities | None
) -> None:
    commands = agent.commands
    if commands is None or not (commands.allow or commands.deny):
        return
    if caps is not None and COMMAND_POLICY_CAPABILITY in caps.extra:
        return
    report.warnings.append(
        _problem(
            agent,
            "commands",
            f"commands: cannot be enforced on provider {agent.provider!r} — it does not hand "
            "rayspec its tool calls before they run, so the block is advisory there",
        )
    )


def check_policy(
    resolved: ResolvedWorkflow,
    effective: EffectivePolicy,
    *,
    capabilities_for: Callable[[str], ProviderCapabilities | None] | None = None,
    trusted: TrustStore | None = None,
) -> PolicyReport:
    """Check ``resolved`` — every agent, and the workflow itself — against ``effective``.

    Returns the errors and warnings to report and the tool denials to fold into the agents.
    Nothing is raised: an unsatisfiable policy is a report full of errors, each naming the layer
    that made it unsatisfiable. ``trusted`` is the project's trust list, needed only when a layer
    sets ``trust.require``.
    """
    report = PolicyReport()
    if effective.is_empty:
        return report
    _check_trust(resolved, effective, trusted, report)
    _check_workspace(effective, report)
    denied = effective.denied_tools()  # once per pass, not once per agent
    for key in sorted(resolved.agents):
        agent = resolved.agents[key]
        caps = None if capabilities_for is None else capabilities_for(agent.provider)
        _check_provider(agent, effective, report)
        _check_model(agent, effective, report)
        _check_access(agent, effective, report)
        _check_mcp(agent, effective, report)
        _check_tools(key, agent, denied, report, caps)
    return report


def agent_control_sources(agent: ResolvedAgent) -> dict[str, tuple[PolicySource, ...]]:
    """The controls one agent sets on *itself*, in the shape :meth:`control_sources` returns.

    Every security-shaped field of the agent schema, not a chosen few: ``access: read-only`` is
    as real a restriction as ``network: off``, and so is ``tools.deny``, a turn or money cap, a
    ``commands:`` block and a declared ``mcp:`` set. See
    :data:`~rayspec.policy.controls.AGENT_CONTROLS`, where each field carries the line that
    earned it its place, and its sibling ``AGENT_NON_CONTROLS`` for the fields that restrict
    nothing and why. These come with no policy file, which is the common case and the case where
    an unprotected control does the most damage.
    """
    sources, _tags, _servers = merged_controls(agent_controls(agent))
    return sources


def check_provider_options(
    resolved: ResolvedWorkflow,
    effective: EffectivePolicy | None = None,
    *,
    external: ExternalControls | None = None,
) -> PolicyReport:
    """Read every governed agent's ``provider_options`` block as an ALLOW-list.

    An agent is governed when ANY control applies to it, whatever its source and whatever schema
    it is spelled in: a ``policy.yaml`` key (:meth:`EffectivePolicy.control_sources`), a
    security-shaped field the agent sets on itself
    (:data:`~rayspec.policy.controls.AGENT_CONTROLS`), a restriction the WORKFLOW sets over every
    agent it runs (:data:`~rayspec.policy.controls.WORKFLOW_CONTROLS` — ``isolation:``, the
    ``defaults:`` caps, a ``secret:`` input), one spelled on the STEP that runs the agent
    (:data:`~rayspec.policy.controls.STEP_CONTROLS`) or an external one — the model lockfile, the
    machine owner's ``providers:`` settings — which the caller discovers and passes as
    ``external``, because this function performs no IO of its own. So this runs whether or not a
    policy file exists: a control a workflow sets on itself is still a control it must not be
    able to shed, and a control imposed from outside it even more so.

    For a governed agent every key of its own provider's block must appear in
    :data:`ALLOWED_PROVIDER_OPTIONS`, and a key that does not is refused at load time. An agent
    NO control applies to is left exactly as it was: the escape hatch is still an escape hatch
    when nothing is being escaped.

    The block is narrowed with :func:`~rayspec.schema.provider_option_block` — the same function
    the adapters narrow it with — so this check reads the block the adapter will act on rather
    than a shape a hand-written path walk assumed.
    """
    report = PolicyReport()
    from_policy = policy_controls(effective)
    from_workflow = workflow_controls(resolved.workflow)
    per_step = step_controls(resolved)
    for key in sorted(resolved.agents):
        agent = resolved.agents[key]
        outside = () if external is None else external.of(agent.provider)
        controls = ControlsInForce.of(
            (
                *from_policy,
                *from_workflow,
                *per_step.get(key, ()),
                *agent_controls(agent),
                *outside,
            )
        )
        _check_provider_options(agent, controls, report)
    return report


def _check_workspace(effective: EffectivePolicy, report: PolicyReport) -> None:
    """``workspace:`` is recorded but enforced by nothing in this build — say so, once.

    The change guard ships as a library (:mod:`rayspec.workspace.guard`) and the policy key that
    configures it is parsed and merged, but no executor runs it yet. A policy key that silently
    does nothing is worse than a missing one, so every run that sets one gets told.
    """
    sources = effective.workspace_sources()
    if not sources:
        return
    report.warnings.append(
        PolicyProblem(
            where="workspace",
            message=(
                "the change guard is not run by this build — protected_paths and "
                "max_changed_files/max_changed_lines are recorded but nothing enforces them "
                f"({sources_text(sources)})"
            ),
        )
    )


def _spelled(provider: str, path: Sequence[str]) -> str:
    """``provider_options.codex.config.mcp_servers`` — a key path as a person writes it."""
    return ".".join(("provider_options", provider, *path))


def _permitted_here(provider: str) -> str:
    """The keys of ``provider``'s allow-list, spelled inside its own block, comma-joined."""
    allowed = ALLOWED_PROVIDER_OPTIONS.get(provider, {})
    return ", ".join(sorted(".".join(path) for path in allowed))


def _way_out(provider: str) -> str:
    """What to do instead — never "drop the control", which is the thing being protected.

    A refusal that does not say what IS allowed leaves switching the control off as the only
    obvious way forward. The provider with NO allow-list at all (the stub, and any provider from
    a plugin) is the case that used to read worst: it printed "(nothing)" and then offered
    dropping the control, which is advice to remove the guardrail rather than the setting.
    """
    permitted = _permitted_here(provider)
    if permitted:
        return (
            f"under a control only the keys it has reasoned about pass ({permitted}). Set what "
            "this key changes through the agent's own fields, which policy and review both read, "
            f"or through providers.{provider} in config.yaml, which belongs to the machine owner"
        )
    return (
        f"rayspec has no allow-list for provider {provider!r} yet, so under a control its whole "
        "block is refused — the same fail-closed default applied to a provider rayspec knows "
        f"nothing about. Set what this key changes through providers.{provider} in config.yaml, "
        "which belongs to the machine owner rather than to the workflow this control governs"
    )


def _check_provider_options(
    agent: ResolvedAgent, controls: ControlsInForce, report: PolicyReport
) -> None:
    """Read one governed agent's own ``provider_options`` block against the allow-list."""
    if not controls.governed:
        return
    block = provider_option_block(agent.provider, agent.provider_options.get(agent.provider))
    if not block:
        return
    _walk_options(agent, block, (), controls, report)


def _walk_options(
    agent: ResolvedAgent,
    block: Mapping[str, Any],
    prefix: tuple[str, ...],
    controls: ControlsInForce,
    report: PolicyReport,
) -> None:
    """Walk ``block``, refusing every key path the allow-list does not carry.

    A key that is a *prefix* of an allow-listed path (``config`` on codex, which carries
    ``config.mcp_servers``) is a namespace: the walk descends into it and judges its keys one by
    one, so allowing one nested key never allows its siblings.
    """
    allowed = ALLOWED_PROVIDER_OPTIONS.get(agent.provider, {})
    for key, value in sorted(block.items(), key=lambda item: str(item[0])):
        path = (*prefix, str(key))
        rule = allowed.get(path)
        if rule is not None:
            _check_option_value(agent, path, value, rule, controls=controls, report=report)
            continue
        nested = any(len(p) > len(path) and p[: len(path)] == path for p in allowed)
        if nested and isinstance(value, Mapping):
            _walk_options(agent, value, path, controls, report)
            continue
        report.errors.append(
            _problem(
                agent,
                "provider_options",
                f"{_spelled(agent.provider, path)} is refused while {controls.named()} "
                f"{'is' if len(controls.sources) == 1 else 'are'} in force: the {agent.provider} "
                "adapter applies provider_options over the options rayspec computed, and rayspec "
                "cannot say whether this key widens what that control narrowed — "
                f"{_way_out(agent.provider)}",
            )
        )


def _check_option_value(
    agent: ResolvedAgent,
    path: tuple[str, ...],
    value: object,
    rule: AllowedOption,
    *,
    controls: ControlsInForce,
    report: PolicyReport,
) -> None:
    """An allow-listed key: permitted outright, or permitted for the values its guard allows."""
    if isinstance(rule.offenders, Inert):
        return
    if rule.guarded_by and not (rule.guarded_by & controls.kinds):
        return
    for suffix, message in rule.offenders(value, controls):
        spelled = _spelled(agent.provider, (*path, suffix) if suffix else path)
        report.errors.append(_problem(agent, "provider_options", f"{spelled}: {message}"))


def _check_trust(
    resolved: ResolvedWorkflow,
    effective: EffectivePolicy,
    trusted: TrustStore | None,
    report: PolicyReport,
) -> None:
    """``trust.require``: only a workflow whose resolved hash is listed may run."""
    sources = effective.trust_required()
    if not sources:
        return
    if trusted is None:
        report.errors.append(
            PolicyProblem(
                where="trust",
                message=(
                    f"policy requires a trusted workflow ({sources_text(sources)}) but the "
                    "trust list was not available to check it against"
                ),
            )
        )
        return
    problem = trusted.problem_for(resolved)
    if problem is None:
        return
    report.errors.append(
        PolicyProblem(
            where="trust",
            message=(
                f"{resolved.label} {problem}, and policy requires a trusted workflow "
                f"({sources_text(sources)}); review the workflow, then run: "
                f"rayspec trust add {resolved.workflow.name}"
            ),
        )
    )


def _problem(agent: ResolvedAgent, field_name: str, message: str, *extra: str) -> PolicyProblem:
    return PolicyProblem(
        where=agent.field_path(field_name),
        message=message,
        location=_location(agent, field_name, *extra),
    )


def _check_provider(agent: ResolvedAgent, effective: EffectivePolicy, report: PolicyReport) -> None:
    sources = effective.provider_denied(agent.provider)
    if not sources:
        return
    allowed = ", ".join(sorted(effective.allowed_providers() or ())) or "(nothing)"
    report.errors.append(
        _problem(
            agent,
            "provider",
            f"provider {agent.provider!r} is not allowed by policy: providers.allow = "
            f"{allowed} ({sources_text(sources)}); use an allowed provider or widen that layer",
        )
    )


def _check_model(agent: ResolvedAgent, effective: EffectivePolicy, report: PolicyReport) -> None:
    for candidate in (agent.model, agent.raw_model):
        if candidate is None:
            continue
        sources = effective.model_denied(candidate)
        if sources:
            report.errors.append(
                _problem(
                    agent,
                    "model",
                    f"model {candidate!r} is denied by policy: models.deny "
                    f"{_entries(sources)}; choose another model or drop that entry",
                )
            )
            return


def _check_access(agent: ResolvedAgent, effective: EffectivePolicy, report: PolicyReport) -> None:
    sources = effective.access_exceeded(agent.access)
    if not sources:
        return
    capped = effective.max_access()
    limit = "" if capped is None else capped[0]
    report.errors.append(
        _problem(
            agent,
            "access",
            f"access {agent.access!r} exceeds the policy maximum {limit!r} "
            f"({sources_text(sources)}); lower access: or raise access.max in that layer",
        )
    )


def _check_mcp(agent: ResolvedAgent, effective: EffectivePolicy, report: PolicyReport) -> None:
    servers = [(name, "mcp") for name in sorted(agent.mcp)]
    for list_name in ("allow", "deny"):
        for entry in getattr(agent.tools, list_name):
            server = _server_of(entry)
            if server is not None:
                servers.append((server, f"tools.{list_name}"))
    seen: set[str] = set()
    for server, field_name in servers:
        if server in seen:
            continue
        sources = effective.mcp_denied(server)
        if not sources:
            continue
        seen.add(server)
        allowed = ", ".join(sorted(effective.allowed_mcp_servers() or ())) or "(nothing)"
        report.errors.append(
            _problem(
                agent,
                field_name,
                f"MCP server {server!r} is not allowed by policy: mcp.allow_servers = "
                f"{allowed} ({sources_text(sources)}); use an allowed server or widen that layer",
                "mcp",
            )
        )


def _check_tools(
    key: str,
    agent: ResolvedAgent,
    denied: Mapping[str, tuple[PolicySource, ...]],
    report: PolicyReport,
    caps: ProviderCapabilities | None,
) -> None:
    if not denied:
        return
    for entry in agent.tools.allow:
        sources = denied.get(entry)
        if sources:
            report.errors.append(
                _problem(
                    agent,
                    "tools.allow",
                    f"tool {entry!r} is denied by policy: tools.deny "
                    f"({sources_text(sources)}); remove it from tools.allow or widen that layer",
                    "tools",
                )
            )
    add: list[str] = []
    for entry in sorted(denied):
        if entry in agent.tools.deny:
            continue
        if _enforceable(entry, agent, caps):
            add.append(entry)
        else:
            report.warnings.append(
                _problem(
                    agent,
                    "provider",
                    f"policy tools.deny: {entry!r} cannot be enforced on provider "
                    f"{agent.provider!r} — the restriction is advisory there "
                    f"({sources_text(denied[entry])})",
                )
            )
    if add:
        report.tool_denials[key] = tuple(add)


def _entries(sources: Sequence[PolicySource]) -> str:
    """``'*opus*' (.rayspec/policy.yaml:3)`` for one entry, comma-joined for several."""
    return ", ".join(f"{s.value!r} ({s.location})" for s in sources)


__all__ = [
    "ALLOWED_PROVIDER_OPTIONS",
    "COMMAND_POLICY_CAPABILITY",
    "INERT_BECAUSE",
    "REASONED_ENV_NAMES",
    "SAFE_APPROVAL_MODE",
    "TOOL_GROUPS",
    "USAGE_COUNTERS",
    "AllowedOption",
    "ControlsInForce",
    "Inert",
    "OptionCheck",
    "PolicyProblem",
    "PolicyReport",
    "agent_control_sources",
    "check_agent_controls",
    "check_policy",
    "check_provider_options",
]

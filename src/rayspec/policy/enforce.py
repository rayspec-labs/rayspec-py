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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rayspec.policy.layers import EffectivePolicy, PolicySource, sources_text
from rayspec.policy.trust import TrustStore

if TYPE_CHECKING:  # type-only: importing the loader at runtime would close an import cycle
    from rayspec.loader.loader import ResolvedAgent, ResolvedWorkflow
    from rayspec.providers.base import ProviderCapabilities

#: Capability name a provider declares (in ``ProviderCapabilities.extra``) when it hands rayspec
#: its tool calls before they run — the only way an agent ``commands:`` block can be enforced.
COMMAND_POLICY_CAPABILITY = "command_policy"

#: Neutral tool groups (mirrors ``providers.base.TOOL_GROUPS``; duplicated so this package keeps
#: its "no provider imports" boundary).
TOOL_GROUPS: frozenset[str] = frozenset({"read", "edit", "shell", "web", "agent", "mcp"})

#: ``provider_options`` key paths that an adapter applies straight over a field a control
#: computes, keyed by the control and then by provider id (a path is a key path inside that
#: provider's own option block). ``provider_options`` is a raw pass-through: the adapter sets
#: the SDK field from it *after* rayspec computed that field, so an agent naming one of these
#: keys would undo the control from inside the workflow the control governs. Refusing it at load
#: time is what keeps a control from being one the party it constrains can silently remove.
#:
#: This is the single table the check is derived from, and it covers BOTH kinds of control: the
#: ``policy.yaml`` keys :meth:`EffectivePolicy.control_sources` reports, and the workflow's own
#: security fields (:func:`agent_control_sources`) — those are unprotected by any policy file, so
#: leaving them out was how ``network: off`` came to be defeatable with no policy at all. A new
#: control belongs here on the day it is added; a key that restricts something and is missing
#: from this table is an escape hatch waiting to be found.
POLICY_CONTROLLED_OPTIONS: Mapping[str, Mapping[str, tuple[tuple[str, ...], ...]]] = {
    "tools.deny": {
        "claude": (("tools",), ("allowed_tools",), ("disallowed_tools",), ("permission_mode",)),
        "codex": (("config", "tools"), ("config", "web_search")),
    },
    "access.max": {
        "claude": (("permission_mode",),),
        "codex": (("config", "sandbox_mode"),),
    },
    "models.deny": {
        "claude": (("model",),),
        "codex": (("model",), ("config", "model")),
    },
    # both adapters MERGE provider_options servers into the computed set instead of replacing
    # them, so an allow-list has to be checked against what is merged in, not only against
    # `agent.mcp`. `strict_mcp_config: false` is the same hole by another route: it lets the
    # Claude CLI pick up MCP servers from files rayspec never saw.
    "mcp.allow_servers": {
        "claude": (("mcp_servers",), ("strict_mcp_config",)),
        "codex": (("config", "mcp_servers"),),
    },
    # a workflow field, not a policy key: `network: off` is folded into tools.deny and is
    # therefore undone by exactly the options that undo a denied tool.
    "network: off": {
        "claude": (("tools",), ("allowed_tools",), ("disallowed_tools",)),
        "codex": (("config", "tools"), ("config", "web_search")),
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

    ``network: off`` is a security claim a reviewer reads off the agent file, and it is enforced
    by folding ``web`` into ``tools.deny`` — the same field ``provider_options`` can overwrite.
    It comes with no policy file, which is the common case, so the escape-hatch check has to see
    it from here rather than from a layer.
    """
    if agent.network != "off":
        return {}
    where = _location(agent, "network") or agent.field_path("network")
    return {"network: off": (PolicySource(layer="workflow", label=where, line=None, value="off"),)}


def check_provider_options(
    resolved: ResolvedWorkflow, effective: EffectivePolicy | None = None
) -> PolicyReport:
    """Refuse every ``provider_options`` key that would undo a control in force.

    Runs whether or not a policy file exists, because :func:`agent_control_sources` contributes
    controls the workflow sets on itself. Everything it knows comes from
    :data:`POLICY_CONTROLLED_OPTIONS`, so protecting a new control is one table entry rather than
    a new special case.
    """
    report = PolicyReport()
    from_policy = {} if effective is None else effective.control_sources()
    for key in sorted(resolved.agents):
        agent = resolved.agents[key]
        _check_provider_options(agent, {**from_policy, **agent_control_sources(agent)}, report)
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


def _names(options: Mapping[str, object], path: Sequence[str]) -> bool:
    """Whether ``options`` sets the key path ``path`` (``("config", "tools")``)."""
    current: object = options
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return False
        current = current[key]
    return True


def _check_provider_options(
    agent: ResolvedAgent,
    controls: Mapping[str, tuple[PolicySource, ...]],
    report: PolicyReport,
) -> None:
    """Refuse an agent whose ``provider_options`` would overwrite a field policy controls."""
    options = agent.provider_options.get(agent.provider)
    if not options or not controls:
        return
    hits: dict[tuple[str, ...], list[str]] = {}
    for control in sorted(controls):
        for path in POLICY_CONTROLLED_OPTIONS.get(control, {}).get(agent.provider, ()):
            if _names(options, path):
                hits.setdefault(path, []).append(control)
    for path, defeated in sorted(hits.items()):
        sources = tuple(source for control in defeated for source in controls[control])
        spelled = ".".join(("provider_options", agent.provider, *path))
        report.errors.append(
            _problem(
                agent,
                "provider_options",
                f"{spelled} would undo {' and '.join(defeated)} — the {agent.provider} adapter "
                f"applies provider_options over the value rayspec computed "
                f"({sources_text(sources)}); remove that key, or drop the restriction it undoes",
            )
        )


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
    "COMMAND_POLICY_CAPABILITY",
    "POLICY_CONTROLLED_OPTIONS",
    "TOOL_GROUPS",
    "PolicyProblem",
    "PolicyReport",
    "agent_control_sources",
    "check_agent_controls",
    "check_policy",
    "check_provider_options",
]

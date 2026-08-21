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

from collections.abc import Callable, Sequence
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
    """

    errors: list[PolicyProblem] = field(default_factory=list)
    warnings: list[PolicyProblem] = field(default_factory=list)
    tool_denials: dict[str, tuple[str, ...]] = field(default_factory=dict)

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
    for key in sorted(resolved.agents):
        agent = resolved.agents[key]
        caps = None if capabilities_for is None else capabilities_for(agent.provider)
        _check_provider(agent, effective, report)
        _check_model(agent, effective, report)
        _check_access(agent, effective, report)
        _check_mcp(agent, effective, report)
        _check_tools(key, agent, effective, report, caps)
    return report


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
    effective: EffectivePolicy,
    report: PolicyReport,
    caps: ProviderCapabilities | None,
) -> None:
    denied = effective.denied_tools()
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
    "TOOL_GROUPS",
    "PolicyProblem",
    "PolicyReport",
    "check_agent_controls",
    "check_policy",
]

# SPDX-License-Identifier: Apache-2.0
"""``apply_policy`` — the one function that turns policy into something a run is subject to.

Boundary: this is the seam between :mod:`rayspec.policy` and everything that loads a workflow.
It discovers the layers, runs the checks in :mod:`rayspec.policy.enforce`, and folds the tool
denials into the resolved agents. It performs no IO beyond reading the policy files and the trust
list, and it never talks to the engine or a provider.

Why it exists as a function of its own: enforcement used to be a side effect of validation, so a
caller that loaded a workflow and ran it without validating first got no policy at all. Anything
that is about to *run* a resolved workflow calls this, whether it validates or not.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from rayspec.policy.enforce import (
    PolicyProblem,
    PolicyReport,
    check_agent_controls,
    check_policy,
    check_provider_options,
)
from rayspec.policy.layers import EffectivePolicy, load_policy
from rayspec.policy.trust import TrustStore
from rayspec.schema import ToolsSpec

if TYPE_CHECKING:  # type-only: importing the loader at runtime would close an import cycle
    from rayspec.loader.loader import ResolvedWorkflow
    from rayspec.providers.base import ProviderCapabilities


def policy_root(resolved: ResolvedWorkflow) -> Path:
    """The project root ``resolved``'s policy layers are discovered against.

    A workflow loaded through :func:`~rayspec.loader.load_workflow` carries the root it was loaded
    from; one built by hand does not, and the directory holding the document (or its parent, when
    that is ``.rayspec/``) is the best guess left.
    """
    if resolved.project_root is not None:
        return resolved.project_root
    base = resolved.base_dir
    return base.parent if base.name == ".rayspec" else base


def apply_policy(
    resolved: ResolvedWorkflow,
    *,
    capabilities_for: Callable[[str], ProviderCapabilities | None] | None = None,
    policy: EffectivePolicy | None = None,
) -> PolicyReport:
    """Check ``resolved`` against the policy in force and fold the denials into its agents.

    ``policy`` is the set of layers to apply; the default (``None``) discovers them from the
    workflow's own roots — ``$RAYSPEC_POLICY``, ``<project>/.rayspec/policy.yaml`` and
    ``<home>/policy.yaml``. Pass an empty :class:`~rayspec.policy.EffectivePolicy` to apply no
    policy at all.

    The per-agent controls (``network:``, ``commands:``) are part of the workflow rather than of
    a policy file, so they are always applied — and so is the ``provider_options`` check, because
    a workflow field is a control the workflow must not be able to remove either. Returns the
    problems to report; nothing is raised, and the caller decides whether an error refuses the
    run. Calling it twice is harmless: a denial already present on an agent is not added again.
    """
    root = policy_root(resolved)
    report = _fold(resolved, check_agent_controls(resolved, capabilities_for=capabilities_for))
    effective = policy if policy is not None else load_policy(root, home=resolved.home)
    report = _merge(report, check_provider_options(resolved, effective))
    if not effective.is_empty:
        trusted = TrustStore.load(root) if effective.trust_required() else None
        outcome = _fold(
            resolved,
            check_policy(resolved, effective, capabilities_for=capabilities_for, trusted=trusted),
        )
        report = _merge(report, outcome)
    report.policy_layers = effective.labels
    report.policy_searched = effective.searched
    return report


def problem_line(problem: PolicyProblem) -> str:
    """``<where>: <message> (at <file>:<line>)`` — a problem as the CLI prints validation errors."""
    text = f"{problem.where}: {problem.message}"
    return f"{text} (at {problem.location})" if problem.location else text


def _fold(resolved: ResolvedWorkflow, report: PolicyReport) -> PolicyReport:
    """Add ``report.tool_denials`` to the agents' ``tools.deny`` (the enforcement half)."""
    for key, entries in sorted(report.tool_denials.items()):
        agent = resolved.agents[key]
        deny = [*agent.tools.deny, *(e for e in entries if e not in agent.tools.deny)]
        tools = ToolsSpec(allow=list(agent.tools.allow), deny=deny)
        resolved.agents[key] = dataclasses.replace(agent, tools=tools)
    return report


def _merge(first: PolicyReport, second: PolicyReport) -> PolicyReport:
    """One report out of two, keeping both sets of denials per agent."""
    denials = dict(first.tool_denials)
    for key, entries in second.tool_denials.items():
        merged = [*denials.get(key, ()), *(e for e in entries if e not in denials.get(key, ()))]
        denials[key] = tuple(merged)
    return PolicyReport(
        errors=[*first.errors, *second.errors],
        warnings=[*first.warnings, *second.warnings],
        tool_denials=denials,
        policy_layers=first.policy_layers or second.policy_layers,
        policy_searched=first.policy_searched or second.policy_searched,
    )


__all__ = ["apply_policy", "policy_root", "problem_line"]

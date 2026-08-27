# SPDX-License-Identifier: Apache-2.0
"""Resolve an agent's input-backed numbers (E1): ``budget_usd`` / ``max_turns`` set to
``{{ inputs.<name> }}`` become concrete numbers from the run's inputs.

The reference is discovered at load time (``ResolvedAgent.input_refs``, the number left ``None``);
this fills the number once the run's inputs are known — after ``_prepare_record`` in the engine,
so a resume uses the inputs the record already fixed. It is a reference, not an expression: the
value is looked up and coerced, never evaluated. Idempotent: an agent with no ``input_refs`` is
returned unchanged, and running the pass twice on the same inputs is a no-op.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from rayspec.errors import LoaderError
from rayspec.loader.loader import ResolvedAgent, ResolvedWorkflow


def _coerce_turns(agent: ResolvedAgent, name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoaderError(
            f"{agent.field_path('max_turns')} = {{{{ inputs.{name} }}}} resolved to "
            f"{value!r}, which is not a positive integer",
            location=agent.location("max_turns"),
        )
    if value < 1:
        raise LoaderError(
            f"{agent.field_path('max_turns')} = {{{{ inputs.{name} }}}} resolved to {value}, "
            f"which is not >= 1",
            location=agent.location("max_turns"),
        )
    return value


def _coerce_budget(agent: ResolvedAgent, name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LoaderError(
            f"{agent.field_path('budget_usd')} = {{{{ inputs.{name} }}}} resolved to "
            f"{value!r}, which is not a positive number",
            location=agent.location("budget_usd"),
        )
    amount = float(value)
    if amount <= 0:
        raise LoaderError(
            f"{agent.field_path('budget_usd')} = {{{{ inputs.{name} }}}} resolved to {amount}, "
            f"which is not > 0",
            location=agent.location("budget_usd"),
        )
    return amount


def _resolved_agent(agent: ResolvedAgent, inputs: Mapping[str, Any]) -> ResolvedAgent:
    if not agent.input_refs:
        return agent
    changes: dict[str, Any] = {}
    for field_name, input_name in agent.input_refs.items():
        if input_name not in inputs:
            raise LoaderError(
                f"{agent.field_path(field_name)} = {{{{ inputs.{input_name} }}}} but the run has "
                f"no input {input_name!r}",
                location=agent.location(field_name),
            )
        value = inputs[input_name]
        if field_name == "max_turns":
            changes["max_turns"] = _coerce_turns(agent, input_name, value)
        elif field_name == "budget_usd":
            changes["budget_usd"] = _coerce_budget(agent, input_name, value)
    return dataclasses.replace(agent, **changes)


def resolve_agent_numbers(rw: ResolvedWorkflow, inputs: Mapping[str, Any]) -> ResolvedWorkflow:
    """A copy of ``rw`` with every agent's ``{{ inputs.<name> }}`` number filled from ``inputs``.

    Raises :class:`LoaderError` (naming the agent field and the input) when a referenced input is
    absent, or resolves to a value that is not a positive number / integer. Agents without an
    ``input_refs`` entry are untouched, so the pass is safe to run on any workflow.
    """
    if not any(agent.input_refs for agent in rw.agents.values()):
        return rw
    agents = {key: _resolved_agent(agent, inputs) for key, agent in rw.agents.items()}
    return dataclasses.replace(rw, agents=agents)


__all__ = ["resolve_agent_numbers"]

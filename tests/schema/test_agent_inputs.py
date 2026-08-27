# SPDX-License-Identifier: Apache-2.0
"""E1 (PRD-09 F6/F13): an agent's `budget_usd`/`max_turns` accept EXACTLY `{{ inputs.<name> }}`
besides a literal — the one numeric field that takes an input reference, so a workflow can raise a
budget by passing `--input`, not by ejecting and editing YAML. It is a reference, not an
expression: no arithmetic, no partial template, no step reference."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rayspec.schema.agent import AgentDef


def test_literals_are_accepted_and_bounded():
    assert AgentDef(budget_usd=1.5).budget_usd == 1.5
    assert AgentDef(budget_usd="$2.50").budget_usd == 2.5
    assert AgentDef(max_turns=3).max_turns == 3


@pytest.mark.parametrize("ref", ["{{ inputs.budget }}", "{{inputs.b}}", "  {{ inputs.max_t }}  "])
def test_an_exact_input_reference_is_kept_verbatim(ref: str):
    """The reference string is preserved for the loader to resolve per run — not coerced now."""
    assert AgentDef(budget_usd=ref).budget_usd == ref
    assert AgentDef(max_turns=ref).max_turns == ref


@pytest.mark.parametrize("bad", [0, -1, "0", "abc", "$0"])
def test_non_positive_or_nonsense_budget_is_refused(bad):
    with pytest.raises(ValidationError):
        AgentDef(budget_usd=bad)


@pytest.mark.parametrize("bad", [0, 1.5, "abc", "3"])
def test_a_non_integer_or_non_positive_max_turns_is_refused(bad):
    with pytest.raises(ValidationError):
        AgentDef(max_turns=bad)


@pytest.mark.parametrize(
    "bad",
    [
        "{{ inputs.x + 1 }}",  # arithmetic — an expression, not a reference
        "{{ steps.a.output }}",  # a step reference, not an input
        "{{ inputs.x }} and more",  # not the whole value
        "prefix {{ inputs.x }}",
        "{{ inputs }}",  # the whole inputs object, no name
        "{{ inputs.x.y }}",  # a nested path
    ],
)
def test_a_reference_that_is_not_exactly_one_input_is_refused(bad):
    with pytest.raises(ValidationError):
        AgentDef(budget_usd=bad)
    with pytest.raises(ValidationError):
        AgentDef(max_turns=bad)


def test_the_override_model_inherits_the_templated_fields():
    from rayspec.schema.agent import AgentOverride

    ov = AgentOverride(extends="base", budget_usd="{{ inputs.b }}", max_turns=5)
    assert ov.budget_usd == "{{ inputs.b }}" and ov.max_turns == 5

"""Codex frontier models (gpt-5.6-*) expose `max` and `ultra` reasoning efforts."""

from __future__ import annotations

import pytest

from rayspec.providers.capabilities import CODEX_CAPABILITIES
from rayspec.providers.codex import _effort
from rayspec.schema import parse_agent_def


@pytest.mark.parametrize("level", ["max", "ultra"])
def test_codex_accepts_max_and_ultra(level):
    assert level in CODEX_CAPABILITIES.effort_levels
    assert "max" not in CODEX_CAPABILITIES.effort_aliases  # no longer downgraded to xhigh
    eff = _effort(level)
    assert eff is not None and eff.value == level


def test_schema_accepts_ultra():
    agent = parse_agent_def({"provider": "codex", "model": "gpt-5.6-sol", "effort": "ultra"})
    assert agent.effort == "ultra"

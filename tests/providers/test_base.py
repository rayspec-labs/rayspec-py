"""Contract tests for the neutral provider layer (rayspec.providers.base)."""

from __future__ import annotations

import dataclasses

import pytest

from rayspec.providers.base import (
    AccessLevel,
    AgentError,
    AgentRequest,
    AgentResult,
    ProviderAuthError,
    ProviderCapabilities,
    ProviderError,
    ProviderNotInstalledError,
    ToolPolicy,
    Usage,
)

_BASE_CAPS = ProviderCapabilities(
    structured_output="enforced",
    session_resume=True,
    session_fork=False,
    instructions_modes=frozenset({"append"}),
    access_levels=frozenset(AccessLevel),
    tool_groups=frozenset({"read"}),
    raw_tool_names=False,
    max_turns=False,
    budget_usd=False,
    cost_reporting=False,
    effort_levels=frozenset({"low", "high"}),
)


def _caps(**overrides) -> ProviderCapabilities:
    return dataclasses.replace(_BASE_CAPS, **overrides)


def test_usage_adds_fieldwise_and_reports_total():
    a = Usage(input=100, cached_input=40, cache_write=10, output=20, reasoning=5)
    b = Usage(input=1, cached_input=1, cache_write=1, output=1, reasoning=1)
    s = a + b
    assert s == Usage(input=101, cached_input=41, cache_write=11, output=21, reasoning=6)
    assert s.total == 122
    assert Usage().total == 0


def test_usage_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Usage().input = 3  # type: ignore[misc]


def test_capabilities_are_frozen_and_hashable():
    caps = _caps()
    assert hash(caps)
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.max_turns = True  # type: ignore[misc]
    assert caps.effort_aliases == {}
    assert caps.thinking is False and caps.mcp_servers is False and caps.images is False


def test_access_level_values_are_the_yaml_spellings():
    assert AccessLevel("read-only") is AccessLevel.READ_ONLY
    assert AccessLevel("workspace-write") is AccessLevel.WORKSPACE_WRITE
    assert AccessLevel("full") is AccessLevel.FULL
    assert str(AccessLevel.FULL) == "full"


def test_agent_request_defaults():
    req = AgentRequest(step_path="assess", prompt="hi", cwd="/tmp")
    assert req.access is AccessLevel.WORKSPACE_WRITE
    assert req.instructions_mode == "append"
    assert req.tools == ToolPolicy()
    assert req.tools.allow == () and req.tools.deny == ()
    assert req.env == {} and req.provider_options == {}
    assert req.step_attempt == 1 and req.fork_session is False


def test_agent_result_defaults_and_error_payload():
    res = AgentResult(status="success", text="ok")
    assert res.usage == Usage() and res.cost_usd is None and res.cost_source == "none"
    err = AgentError(kind="api", message="boom", transient=True, code=529)
    failed = AgentResult(status="error", text="", error=err)
    assert failed.error is not None and failed.error.transient is True


def test_provider_error_hierarchy_carries_transient_and_hint():
    e = ProviderNotInstalledError("no cli", hint="pip install claude-agent-sdk")
    assert isinstance(e, ProviderError)
    assert e.transient is False and e.hint == "pip install claude-agent-sdk"
    assert ProviderAuthError("nope").transient is False
    assert ProviderError("again", transient=True).transient is True
    assert "no cli" in str(e)

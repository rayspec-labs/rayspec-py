"""Loud denials: the neutral shape, both adapters' mapping, the record and ``on_denial: fail``."""

from __future__ import annotations

from typing import Any

import pytest

from rayspec.providers.base import (
    AgentRequest,
    AgentResult,
    Denial,
    EmitFn,
    ProviderCapabilities,
    ProviderHealth,
)
from rayspec.providers.capabilities import STUB_CAPABILITIES
from rayspec.schema import RunStatus, SchemaError, StepStatus, parse_workflow
from rayspec.schema.agent import AgentDef

from .conftest import Project

pytestmark = pytest.mark.anyio


class DenyingProvider:
    """A provider whose one turn reports denied tool calls (no SDK, no network)."""

    id = "claude"
    capabilities: ProviderCapabilities = STUB_CAPABILITIES

    def __init__(self, *denials: Denial) -> None:
        self.denials = denials

    async def open(self, **kw: Any) -> None:
        return None

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        return AgentResult(status="success", text="did what I could", denials=self.denials)

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
        return ProviderHealth(ok=True)

    async def aclose(self) -> None:
        return None


WORKFLOW = """
rayspec: 1
name: t
agents:
  worker:
    provider: claude
    on_denial: {mode}
steps:
  - {{id: a, prompt: "do it", agent: worker}}
  - {{id: b, needs: [a], shell: "echo {{{{ steps.a.denials | length }}}}"}}
"""


# -- schema ------------------------------------------------------------------------------------


def test_on_denial_defaults_to_warn_and_only_takes_warn_or_fail() -> None:
    assert AgentDef().on_denial == "warn"
    assert AgentDef(on_denial="fail").on_denial == "fail"
    doc = {
        "rayspec": 1,
        "name": "t",
        "agents": {"w": {"provider": "claude", "on_denial": "explode"}},
        "steps": [],
    }
    with pytest.raises(SchemaError) as exc:
        parse_workflow(doc)
    assert "on_denial" in str(exc.value)


def test_the_resolved_agent_carries_on_denial(project: Project) -> None:
    project.workflow("t", WORKFLOW.format(mode="fail"))
    resolved = project.load("t")
    assert resolved.agent_for("a").on_denial == "fail"


# -- adapters ----------------------------------------------------------------------------------


def test_claude_permission_denials_map_onto_the_neutral_shape() -> None:
    from rayspec.providers.claude import denial_of

    denial = denial_of(
        {"tool_name": "Bash", "tool_use_id": "tu_9", "tool_input": {"command": "rm -rf /"}}
    )
    assert denial.tool == "Bash" and denial.call_id == "tu_9"
    assert "permission" in denial.reason.lower()
    assert denial_of("something odd").tool == "unknown"


def test_a_denial_reason_is_a_note_not_a_data_dump() -> None:
    from rayspec.providers.claude import DENIAL_REASON_MAX, denial_of

    denial = denial_of({"tool_name": "Bash", "message": "x" * 5000})
    assert len(denial.reason) == DENIAL_REASON_MAX and denial.reason.endswith("\u2026")


def test_codex_sandbox_errors_map_onto_the_same_shape() -> None:
    from enum import Enum

    from rayspec.providers.codex import sandbox_denial

    class Code(Enum):
        sandbox = "sandboxError"
        bad = "badRequest"

    def error(code: Code, message: str) -> Any:
        info = type("Info", (), {"root": code})()
        return type("Error", (), {"codex_error_info": info, "message": message})()

    denial = sandbox_denial(error(Code.sandbox, "write blocked: /etc/hosts"))
    assert denial is not None
    assert denial.tool == "shell" and "write blocked" in denial.reason
    assert sandbox_denial(None) is None
    assert sandbox_denial(error(Code.bad, "nope")) is None


# -- engine ------------------------------------------------------------------------------------


async def test_denials_reach_the_record_the_event_and_a_template(project: Project) -> None:
    project.workflow("t", WORKFLOW.format(mode="warn"))
    provider = DenyingProvider(
        Denial(tool="Bash", reason="permission denied", call_id="tu_1"),
        Denial(tool="Write", reason="permission denied"),
    )
    result = await project.run("t", providers={"claude": provider})
    assert result.status is RunStatus.SUCCEEDED, result.reason
    record = project.record(result.run_id).steps["a"]
    assert [d.tool for d in record.denials] == ["Bash", "Write"]
    assert record.denials[0].call_id == "tu_1"
    step_b = project.record(result.run_id).steps["b"]
    assert step_b.status is StepStatus.SUCCEEDED
    # the template sees them: `steps.a.denials | length`
    assert project.store.read_output(result.run_id, step_b.output_ref or "").strip() == "2"


async def test_on_denial_fail_fails_the_step_naming_the_tools(project: Project) -> None:
    project.workflow("t", WORKFLOW.format(mode="fail"))
    provider = DenyingProvider(
        Denial(tool="Bash", reason="permission denied"),
        Denial(tool="Write", reason="permission denied"),
    )
    result = await project.run("t", providers={"claude": provider})
    assert result.status is RunStatus.FAILED
    record = project.record(result.run_id).steps["a"]
    assert record.status is StepStatus.FAILED
    assert record.error is not None and record.error.type == "denied"
    assert "Bash" in record.error.message and "Write" in record.error.message
    assert len(record.denials) == 2  # the record still says what was denied


async def test_no_denials_is_not_a_failure_even_with_on_denial_fail(project: Project) -> None:
    project.workflow("t", WORKFLOW.format(mode="fail"))
    result = await project.run("t", providers={"claude": DenyingProvider()})
    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert project.record(result.run_id).steps["a"].denials == []


# -- capability discipline ----------------------------------------------------------------------


DENIAL_WF = """
rayspec: 1
name: t
agents:
  worker:
    provider: {provider}
    on_denial: fail
steps:
  - {{id: a, prompt: "do it", agent: worker}}
"""


def test_on_denial_fail_is_refused_on_a_provider_that_cannot_report_denials(
    project: Project,
) -> None:
    """A policy silently ignored on a provider that cannot honour it is not a policy."""
    from rayspec.loader import validate_workflow
    from rayspec.providers.capabilities import CLAUDE_CAPABILITIES, CODEX_CAPABILITIES

    caps = {"claude": CLAUDE_CAPABILITIES, "codex": CODEX_CAPABILITIES}
    project.workflow("t", DENIAL_WF.format(provider="codex"))
    report = validate_workflow(
        project.load("t"), capabilities_for=caps.get, provider_ids=("claude", "codex")
    )
    assert not report.ok
    assert any("on_denial" in e for e in report.errors), report.errors
    assert any("claude" in e for e in report.errors)  # the adapter that can

    warned = validate_workflow(project.load("t"), capabilities_for=caps.get, on_unsupported="warn")
    assert warned.ok and any("on_denial" in w for w in warned.warnings)

    project.workflow("u", DENIAL_WF.format(provider="claude").replace("name: t", "name: u"))
    assert validate_workflow(project.load("u"), capabilities_for=caps.get).ok


def test_on_denial_warn_is_fine_everywhere(project: Project) -> None:
    from rayspec.loader import validate_workflow
    from rayspec.providers.capabilities import CODEX_CAPABILITIES

    project.workflow("t", DENIAL_WF.format(provider="codex").replace("fail", "warn"))
    report = validate_workflow(
        project.load("t"), capabilities_for={"codex": CODEX_CAPABILITIES}.get
    )
    assert report.ok, report.errors


def test_a_recovered_error_does_not_become_a_denial() -> None:
    """`state.last_error` collects every error the stream reported, retried ones included — a
    turn that COMPLETED was not refused anything."""
    from enum import Enum

    from rayspec.providers.codex import turn_denials

    class Code(Enum):
        sandbox = "sandboxError"

    info = type("Info", (), {"root": Code.sandbox})()
    error = type("Error", (), {"codex_error_info": info, "message": "write blocked"})()
    state = type("State", (), {"last_error": error})()
    completed = type("Turn", (), {"error": None})()
    assert turn_denials(completed, state) == ()
    failed = type("Turn", (), {"error": error})()
    assert [d.tool for d in turn_denials(failed, state)] == ["shell"]

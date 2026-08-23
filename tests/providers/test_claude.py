"""ClaudeProvider: option building, stream mapping, status/usage extraction, errors, healthcheck.

Every test drives the adapter through a fake ``query`` (monkeypatched on the adapter module) that
yields REAL ``claude_agent_sdk`` dataclasses and records the ``ClaudeAgentOptions`` it received.
No network, no CLI.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any

import anyio
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import rayspec.providers.claude as claude_mod
from rayspec import __version__
from rayspec.providers.base import (
    AccessLevel,
    AgentEvent,
    AgentRequest,
    McpServerSpec,
    Provider,
    ProviderError,
    ProviderNotInstalledError,
    ToolPolicy,
)
from rayspec.providers.capabilities import CLAUDE_CAPABILITIES
from rayspec.providers.claude import (
    ADAPTER_OWNED_OPTIONS,
    MERGED_OPTIONS,
    ClaudeProvider,
    build_options,
)
from rayspec.providers.registry import create_provider

pytestmark = pytest.mark.anyio


# -- helpers ------------------------------------------------------------------------------------


class Collector:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def of(self, kind: str) -> list[AgentEvent]:
        return [e for e in self.events if e.kind == kind]


def _req(tmp_path: Path, **kw: Any) -> AgentRequest:
    kw.setdefault("step_path", "review")
    kw.setdefault("prompt", "Review the code")
    kw.setdefault("cwd", str(tmp_path))
    return AgentRequest(**kw)


def _init(session_id: str = "sess-1", model: str = "claude-sonnet-4-5") -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "model": model,
            "tools": ["Read", "Glob", "Grep"],
            "cwd": "/tmp",
            "permission_mode": "dontAsk",
        },
    )


def _result(
    *,
    subtype: str = "success",
    is_error: bool = False,
    result: str | None = "done",
    structured_output: Any = None,
    session_id: str = "sess-1",
    num_turns: int = 1,
    total_cost_usd: float | None = 0.0123,
    usage: dict[str, Any] | None = None,
    model_usage: dict[str, Any] | None = None,
    terminal_reason: str | None = "completed",
    api_error_status: int | None = None,
    errors: list[str] | None = None,
    permission_denials: list[Any] | None = None,
) -> ResultMessage:
    if usage is None:
        usage = {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40,
        }
    return ResultMessage(
        subtype=subtype,
        duration_ms=1234,
        duration_api_ms=1000,
        is_error=is_error,
        num_turns=num_turns,
        session_id=session_id,
        total_cost_usd=total_cost_usd,
        usage=usage,
        result=result,
        structured_output=structured_output,
        model_usage=model_usage,
        permission_denials=permission_denials,
        errors=errors,
        api_error_status=api_error_status,
        terminal_reason=terminal_reason,
    )


def _result_error(result: ResultMessage) -> ResultError:
    data = {
        "type": "result",
        "subtype": result.subtype,
        "is_error": result.is_error,
        "result": result.result,
        "errors": result.errors,
        "api_error_status": result.api_error_status,
        "terminal_reason": result.terminal_reason,
        "session_id": result.session_id,
    }
    return ResultError("Claude Code returned an error result", data=data, exit_code=1)


def _text_delta(text: str, *, parent: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="sess-1",
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        parent_tool_use_id=parent,
    )


def _thinking_delta(text: str) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="sess-1",
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    )


def _message_start(message_id: str, model: str = "claude-sonnet-4-5") -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="sess-1",
        event={"type": "message_start", "message": {"id": message_id, "model": model}},
    )


class FakeQuery:
    """Fake ``claude_agent_sdk.query``: yields scripted messages, records options.

    ``script`` is a list of messages; an Exception instance in the list is raised at that point.
    """

    def __init__(self, script: Iterable[Any] = (), *, sleep_forever: bool = False) -> None:
        self.script = list(script)
        self.sleep_forever = sleep_forever
        self.calls: list[tuple[Any, ClaudeAgentOptions]] = []
        self.finalised = False
        self.exit_exc: str | None = None

    @property
    def options(self) -> ClaudeAgentOptions:
        assert self.calls, "query() was not called"
        return self.calls[-1][1]

    def __call__(
        self, *, prompt: Any, options: ClaudeAgentOptions | None = None, transport: Any = None
    ) -> AsyncIterator[Any]:
        assert options is not None
        self.calls.append((prompt, options))
        return self._gen()

    async def _gen(self) -> AsyncIterator[Any]:
        try:
            for item in self.script:
                if isinstance(item, BaseException):
                    raise item
                yield item
            if self.sleep_forever:
                await anyio.sleep_forever()
        finally:
            self.finalised = True
            exc = sys.exc_info()[1]
            self.exit_exc = type(exc).__name__ if exc is not None else None


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeQuery) -> FakeQuery:
    monkeypatch.setattr(claude_mod, "query", fake)
    return fake


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    req: AgentRequest,
    script: Iterable[Any],
    *,
    settings: dict[str, Any] | None = None,
) -> tuple[Any, Collector, FakeQuery]:
    fake = _install(monkeypatch, FakeQuery(script))
    provider = ClaudeProvider(settings or {})
    collector = Collector()
    result = await provider.run(req, collector)
    return result, collector, fake


HAPPY = [
    _init(),
    _message_start("msg_1"),
    _text_delta("Hel"),
    _text_delta("lo"),
    AssistantMessage(
        content=[TextBlock(text="Hello")], model="claude-sonnet-4-5", message_id="msg_1"
    ),
    _result(result="Hello"),
]


# -- construction / protocol ------------------------------------------------------------------


def test_provider_protocol_and_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    provider = create_provider("claude", {})
    assert isinstance(provider, ClaudeProvider)
    assert isinstance(provider, Provider)
    assert provider.id == "claude"
    assert provider.capabilities is CLAUDE_CAPABILITIES
    # the SDK reads this from the PARENT environment at connect() time
    assert os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] == "1"


def test_cli_bundled_version_matches_sdk_module():
    from claude_agent_sdk._cli_version import __cli_version__

    assert __cli_version__ == claude_mod.CLI_BUNDLED_VERSION
    assert claude_mod.CLI_BUNDLED_VERSION is not None


def test_skip_version_check_is_setdefault_not_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "")
    ClaudeProvider({})
    assert os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] == ""


async def test_open_and_aclose_are_cheap(tmp_path: Path):
    provider = ClaudeProvider({})
    await provider.open(run_id="r1", workdir=str(tmp_path), env={"RUN_VAR": "x"}, max_parallel=2)
    assert provider.run_id == "r1"
    await provider.aclose()


# -- option building --------------------------------------------------------------------------


def test_options_defaults(tmp_path: Path):
    provider = ClaudeProvider({})
    opts, tr = build_options(provider, _req(tmp_path), _stderr_sink())
    assert isinstance(opts, ClaudeAgentOptions)
    assert opts.cwd == str(tmp_path)
    assert opts.include_partial_messages is True
    assert opts.setting_sources == ["project"]
    assert opts.strict_mcp_config is False
    assert opts.mcp_servers == {}
    assert opts.env["CLAUDE_AGENT_SDK_CLIENT_APP"] == f"rayspec/{__version__}"
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code"}
    # workspace-write is the default access level
    assert opts.permission_mode == "acceptEdits"
    assert opts.allowed_tools == ["Bash"]
    assert opts.tools is None
    assert opts.disallowed_tools == []
    assert opts.output_format is None
    assert opts.resume is None and opts.fork_session is False
    assert opts.thinking is None and opts.effort is None and opts.model is None
    assert opts.max_turns is None and opts.max_budget_usd is None
    assert opts.stderr is not None
    assert tr.ok


def test_options_instructions_append_and_replace(tmp_path: Path):
    provider = ClaudeProvider({})
    opts, _ = build_options(provider, _req(tmp_path, instructions="Be terse."), _stderr_sink())
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code", "append": "Be terse."}
    opts, _ = build_options(
        provider,
        _req(tmp_path, instructions="You are vanilla.", instructions_mode="replace"),
        _stderr_sink(),
    )
    assert opts.system_prompt == "You are vanilla."


def test_options_read_only(tmp_path: Path):
    provider = ClaudeProvider({})
    opts, _ = build_options(provider, _req(tmp_path, access=AccessLevel.READ_ONLY), _stderr_sink())
    assert opts.permission_mode == "dontAsk"
    assert opts.tools == ["Read", "Glob", "Grep"]
    assert opts.allowed_tools == ["Read", "Glob", "Grep"]


def test_options_read_only_with_web_allowed(tmp_path: Path):
    provider = ClaudeProvider({})
    req = _req(tmp_path, access=AccessLevel.READ_ONLY, tools=ToolPolicy(allow=("read", "web")))
    opts, _ = build_options(provider, req, _stderr_sink())
    assert opts.tools == ["Read", "Glob", "Grep", "WebFetch", "WebSearch"]
    assert opts.allowed_tools == ["Read", "Glob", "Grep", "WebFetch", "WebSearch"]


def test_options_workspace_write_with_web_and_deny(tmp_path: Path):
    provider = ClaudeProvider({})
    req = _req(tmp_path, tools=ToolPolicy(allow=("web",), deny=("agent", "claude:NotebookEdit")))
    opts, _ = build_options(provider, req, _stderr_sink())
    assert opts.permission_mode == "acceptEdits"
    assert opts.allowed_tools == ["Bash", "WebFetch", "WebSearch"]
    assert opts.disallowed_tools == ["Agent", "NotebookEdit"]
    assert opts.tools is None


def test_options_full_access(tmp_path: Path):
    provider = ClaudeProvider({})
    req = _req(tmp_path, access=AccessLevel.FULL, tools=ToolPolicy(deny=("web",)))
    opts, _ = build_options(provider, req, _stderr_sink())
    assert opts.permission_mode == "bypassPermissions"
    assert opts.disallowed_tools == ["WebFetch", "WebSearch"]


def test_options_mcp_servers_and_mcp_group(tmp_path: Path):
    provider = ClaudeProvider({})
    servers = (
        McpServerSpec(name="fs", command="npx", args=("-y", "fs-server"), env={"A": "1"}),
        McpServerSpec(name="api", transport="http", url="https://x/mcp", headers={"H": "v"}),
        McpServerSpec(name="ev", transport="sse", url="https://x/sse"),
    )
    req = _req(
        tmp_path,
        access=AccessLevel.READ_ONLY,
        mcp_servers=servers,
        tools=ToolPolicy(allow=("mcp", "mcp:fs/read_file"), deny=("mcp:api",)),
    )
    opts, _ = build_options(provider, req, _stderr_sink())
    assert opts.strict_mcp_config is True
    assert opts.mcp_servers == {
        "fs": {"type": "stdio", "command": "npx", "args": ["-y", "fs-server"], "env": {"A": "1"}},
        "api": {"type": "http", "url": "https://x/mcp", "headers": {"H": "v"}},
        "ev": {"type": "sse", "url": "https://x/sse"},
    }
    # bare `mcp` expands to mcp__<server> over req.mcp_servers (no wildcard in Claude Code)
    assert opts.allowed_tools == [
        "Read",
        "Glob",
        "Grep",
        "mcp__fs__read_file",
        "mcp__fs",
        "mcp__api",
        "mcp__ev",
    ]
    assert opts.disallowed_tools == ["mcp__api"]


def test_options_model_effort_turns_budget_thinking_schema_resume(tmp_path: Path):
    provider = ClaudeProvider({})
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    req = _req(
        tmp_path,
        model="claude-opus-4-5",
        effort="high",
        max_turns=7,
        budget_usd=1.5,
        thinking=True,
        output_schema=schema,
        resume_session="sess-old",
        fork_session=True,
        env={"MY_VAR": "1"},
    )
    opts, _ = build_options(provider, req, _stderr_sink())
    assert opts.model == "claude-opus-4-5"
    assert opts.effort == "high"
    assert opts.max_turns == 7
    assert opts.max_budget_usd == 1.5
    assert opts.thinking == {"type": "adaptive"}
    assert opts.output_format == {"type": "json_schema", "schema": schema}
    assert opts.resume == "sess-old" and opts.fork_session is True
    assert opts.env["MY_VAR"] == "1"
    assert opts.env["CLAUDE_AGENT_SDK_CLIENT_APP"] == f"rayspec/{__version__}"
    opts2, _ = build_options(
        provider, _req(tmp_path, thinking=False, effort="minimal"), _stderr_sink()
    )
    assert opts2.thinking == {"type": "disabled"}
    assert opts2.effort == "low"  # alias from the capability table


def test_options_settings_keys(tmp_path: Path):
    provider = ClaudeProvider(
        {
            "setting_sources": ["user", "project"],
            "cli_path": "/opt/claude/bin/claude",
            "env": {"FROM_SETTINGS": "s", "MY_VAR": "settings"},
        }
    )
    opts, _ = build_options(provider, _req(tmp_path, env={"MY_VAR": "req"}), _stderr_sink())
    assert opts.setting_sources == ["user", "project"]
    assert opts.cli_path == "/opt/claude/bin/claude"
    assert opts.env["FROM_SETTINGS"] == "s"
    assert opts.env["MY_VAR"] == "req"  # request env wins over settings env
    provider2 = ClaudeProvider({"setting_sources": None})
    opts2, _ = build_options(provider2, _req(tmp_path), _stderr_sink())
    assert opts2.setting_sources is None


async def test_options_run_env_merges_under_request_env(tmp_path: Path, monkeypatch):
    fake = _install(monkeypatch, FakeQuery(HAPPY))
    provider = ClaudeProvider({})
    await provider.open(
        run_id="r", workdir=str(tmp_path), env={"RUN": "1", "MY": "run"}, max_parallel=1
    )
    await provider.run(_req(tmp_path, env={"MY": "req"}), Collector())
    assert fake.options.env["RUN"] == "1"
    assert fake.options.env["MY"] == "req"


def test_options_provider_options_passthrough_and_unknown(tmp_path: Path):
    provider = ClaudeProvider({})
    req = _req(
        tmp_path,
        provider_options={"sandbox": {"enabled": True}, "add_dirs": ["/x"], "bogus_key": 1},
    )
    opts, tr = build_options(provider, req, _stderr_sink())
    assert opts.sandbox == {"enabled": True}
    assert opts.add_dirs == ["/x"]
    assert any("bogus_key" in w for w in tr.warnings)


def test_options_provider_options_env_and_mcp_servers_are_merged(tmp_path: Path):
    provider = ClaudeProvider({"env": {"FROM_SETTINGS": "s", "BOTH": "settings"}})
    req = _req(
        tmp_path,
        env={"REQ": "r", "BOTH": "req"},
        mcp_servers=(McpServerSpec(name="fs", command="npx"),),
        provider_options={
            "env": {"X": "1", "BOTH": "provider_options", "REQ": "provider_options"},
            "mcp_servers": {"extra": {"type": "http", "url": "https://x/mcp"}},
        },
    )
    opts, tr = build_options(provider, req, _stderr_sink(), run_env={"RUN": "1"})
    # CLIENT_APP < settings.env < provider_options.env < open(env) < req.env
    assert opts.env["CLAUDE_AGENT_SDK_CLIENT_APP"] == f"rayspec/{__version__}"
    assert opts.env["FROM_SETTINGS"] == "s" and opts.env["X"] == "1" and opts.env["RUN"] == "1"
    assert opts.env["BOTH"] == "req" and opts.env["REQ"] == "r"
    assert isinstance(opts.mcp_servers, dict) and set(opts.mcp_servers) == {"fs", "extra"}
    assert opts.strict_mcp_config is True
    assert not tr.warnings


@pytest.mark.parametrize("key", sorted(ADAPTER_OWNED_OPTIONS))
def test_options_provider_options_adapter_owned_keys_are_ignored_with_warning(
    tmp_path: Path, key: str
):
    provider = ClaudeProvider({})
    req = _req(tmp_path, resume_session="sess-old", provider_options={key: "override"})
    opts, tr = build_options(provider, req, _stderr_sink())
    assert getattr(opts, key) != "override"
    assert any(f"provider_options.{key}" in w and "ignored" in w for w in tr.warnings)
    assert opts.cwd == str(tmp_path) and opts.resume == "sess-old"


def test_every_computed_option_is_owned_or_merged():
    """No field the adapter computes may be silently replaceable from ``provider_options``.

    Read straight off the ``ClaudeAgentOptions(...)`` call in :func:`build_options`, so a field
    added there later has to be classified as owned or merged before this passes again — the
    forgetting is what turns a control into an escape hatch.
    """
    tree = ast.parse(inspect.getsource(build_options))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ClaudeAgentOptions"
    ]
    assert len(calls) == 1
    computed = {kw.arg for kw in calls[0].keywords if kw.arg}
    assert computed == ADAPTER_OWNED_OPTIONS | MERGED_OPTIONS


def test_options_provider_options_cannot_widen_the_computed_tool_policy(tmp_path: Path):
    """The reported repro: ``disallowed_tools: []`` must not empty a computed denial."""
    provider = ClaudeProvider({})
    req = _req(
        tmp_path,
        access=AccessLevel.READ_ONLY,
        tools=ToolPolicy(deny=("web",)),
        model="claude-sonnet-4-5",
        provider_options={
            "disallowed_tools": [],
            "allowed_tools": ["Bash", "WebSearch"],
            "tools": None,
            "permission_mode": "bypassPermissions",
            "model": "claude-opus-4-1",
        },
    )
    opts, tr = build_options(provider, req, _stderr_sink())
    assert opts.disallowed_tools == ["WebFetch", "WebSearch"]
    assert "Bash" not in opts.allowed_tools
    assert opts.tools == ["Read", "Glob", "Grep"]
    assert opts.permission_mode == "dontAsk"
    assert opts.model == "claude-sonnet-4-5"
    for key in ("disallowed_tools", "allowed_tools", "tools", "permission_mode", "model"):
        assert any(f"provider_options.{key}" in w and "ignored" in w for w in tr.warnings)


def test_options_read_only_warns_about_unrepresentable_allowed_tools(tmp_path: Path):
    provider = ClaudeProvider({})
    req = _req(
        tmp_path,
        access=AccessLevel.READ_ONLY,
        tools=ToolPolicy(allow=("shell", "agent", "web", "claude:Edit")),
    )
    opts, tr = build_options(provider, req, _stderr_sink())
    assert opts.tools == ["Read", "Glob", "Grep", "WebFetch", "WebSearch"]
    assert "Bash" not in opts.allowed_tools and "Agent" not in opts.allowed_tools
    warning = next(w for w in tr.warnings if "read-only" in w)
    assert "Bash" in warning and "Agent" in warning and "Edit" in warning
    assert "WebFetch" not in warning


def test_options_workspace_write_keeps_explicitly_allowed_tools(tmp_path: Path):
    provider = ClaudeProvider({})
    req = _req(tmp_path, tools=ToolPolicy(allow=("agent", "edit")))
    opts, tr = build_options(provider, req, _stderr_sink())
    assert opts.permission_mode == "acceptEdits"
    assert opts.allowed_tools == ["Bash", "Agent", "Edit", "Write", "NotebookEdit"]
    assert opts.tools is None
    assert not tr.warnings


@pytest.mark.parametrize(
    ("settings", "needle"),
    [
        ({"env": "NOT=a-mapping"}, "providers.claude.env"),
        ({"setting_sources": "project"}, "providers.claude.setting_sources"),
        ({"setting_sources": ["project", "global"]}, "providers.claude.setting_sources"),
        ({"cli_path": ["x"]}, "providers.claude.cli_path"),
    ],
)
def test_settings_are_validated(settings: dict[str, Any], needle: str):
    with pytest.raises(ProviderError) as ei:
        ClaudeProvider(settings)
    assert ei.value.kind == "provider" and ei.value.transient is False
    assert needle in (ei.value.hint or "") or needle in str(ei.value)


def test_options_cwd_must_exist(tmp_path: Path):
    provider = ClaudeProvider({})
    with pytest.raises(ProviderError) as ei:
        build_options(provider, _req(tmp_path, cwd=str(tmp_path / "missing")), _stderr_sink())
    assert ei.value.transient is False
    assert "missing" in str(ei.value)


def test_options_tool_translation_error_raises(tmp_path: Path):
    provider = ClaudeProvider({})
    with pytest.raises(ProviderError) as ei:
        build_options(
            provider, _req(tmp_path, tools=ToolPolicy(allow=("nonsense",))), _stderr_sink()
        )
    assert ei.value.transient is False


def _stderr_sink() -> Callable[[str], None]:
    return lambda _line: None


# -- stream mapping ---------------------------------------------------------------------------


async def test_happy_path_events_and_result(tmp_path: Path, monkeypatch):
    req = _req(tmp_path)
    result, col, fake = await _run(monkeypatch, req, HAPPY)
    assert fake.calls[0][0] == "Review the code"
    assert result.status == "success"
    assert result.text == "Hello"
    assert result.session_ref == "sess-1"
    assert result.num_turns == 1
    assert result.cost_usd == pytest.approx(0.0123) and result.cost_source == "provider"
    assert result.usage.input == 10 + 30 + 40
    assert result.usage.cached_input == 30
    assert result.usage.cache_write == 40
    assert result.usage.output == 20
    assert result.model == "claude-sonnet-4-5"  # from init / assistant (no model_usage)
    assert result.error is None
    assert result.duration_ms >= 0
    assert result.raw["subtype"] == "success"
    assert col.kinds() == ["session", "text_delta", "text_delta"]
    session = col.of("session")[0]
    assert session.data["session_id"] == "sess-1"
    assert session.data["model"] == "claude-sonnet-4-5"
    assert session.data["tools"] == ["Read", "Glob", "Grep"]
    assert "".join(e.text for e in col.of("text_delta")) == "Hello"
    assert all(e.ts > 0 for e in col.events)


async def test_text_block_emitted_when_no_deltas_streamed(tmp_path: Path, monkeypatch):
    script = [
        _init(),
        AssistantMessage(
            content=[TextBlock(text="No stream")], model="claude-sonnet-4-5", message_id="m1"
        ),
        _result(result="No stream"),
    ]
    result, col, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert col.kinds() == ["session", "text"]
    assert col.of("text")[0].text == "No stream"
    assert result.text == "No stream"


async def test_result_text_falls_back_to_assistant_text(tmp_path: Path, monkeypatch):
    script = [
        _init(),
        AssistantMessage(content=[TextBlock(text="Fallback")], model="claude-sonnet-4-5"),
        _result(result=None),
    ]
    result, _, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert result.text == "Fallback"


async def test_tool_call_and_result_mapping(tmp_path: Path, monkeypatch):
    script = [
        _init(),
        AssistantMessage(
            content=[
                TextBlock(text="Let me look."),
                ToolUseBlock(id="tu_1", name="Bash", input={"command": "ls"}),
            ],
            model="claude-sonnet-4-5",
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="tu_1", content="a.py\nb.py", is_error=False)]
        ),
        AssistantMessage(
            content=[ToolUseBlock(id="tu_2", name="Read", input={"file_path": "a.py"})],
            model="claude-sonnet-4-5",
            parent_tool_use_id="tu_agent",
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu_2",
                    content=[
                        {"type": "text", "text": "line 1"},
                        {"type": "text", "text": "line 2"},
                    ],
                    is_error=True,
                )
            ],
            parent_tool_use_id="tu_agent",
        ),
        _result(result="ok"),
    ]
    _, col, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert col.kinds() == [
        "session",
        "text",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
    ]
    call1, call2 = col.of("tool_call")
    assert call1.name == "Bash" and call1.call_id == "tu_1" and call1.data == {"command": "ls"}
    assert call1.nested is False
    assert call2.name == "Read" and call2.nested is True
    res1, res2 = col.of("tool_result")
    assert res1.call_id == "tu_1" and res1.text == "a.py\nb.py" and res1.name == "Bash"
    assert res1.data["is_error"] is False and res1.nested is False
    assert res2.call_id == "tu_2" and res2.text == "line 1\nline 2" and res2.name == "Read"
    assert res2.data["is_error"] is True and res2.nested is True


async def test_thinking_streamed_vs_block(tmp_path: Path, monkeypatch):
    script = [
        _init(),
        _message_start("m1"),
        _thinking_delta("hmm "),
        _thinking_delta("ok"),
        AssistantMessage(
            content=[ThinkingBlock(thinking="hmm ok", signature="sig"), TextBlock(text="Answer")],
            model="claude-sonnet-4-5",
            message_id="m1",
        ),
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="second thought", signature="sig"),
                TextBlock(text="More"),
            ],
            model="claude-sonnet-4-5",
            message_id="m2",
        ),
        _result(result="More"),
    ]
    _, col, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert col.kinds() == [
        "session",
        "reasoning",
        "reasoning",
        "text",  # m1: thinking was streamed, text was not
        "reasoning",
        "text",  # m2: nothing streamed
    ]
    assert [e.text for e in col.of("reasoning")] == ["hmm ", "ok", "second thought"]


async def test_nested_text_deltas_and_rate_limit_warning(tmp_path: Path, monkeypatch):
    script = [
        _init(),
        RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning", rate_limit_type="five_hour", utilization=0.9, raw={"x": 1}
            ),
            uuid="u",
            session_id="sess-1",
        ),
        _text_delta("sub", parent="tu_agent"),
        _result(result="done"),
    ]
    _, col, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert col.kinds() == ["session", "warning", "text_delta"]
    warn = col.of("warning")[0]
    assert "allowed_warning" in warn.text and "five_hour" in warn.text
    assert col.of("text_delta")[0].nested is True


async def test_unknown_system_message_is_raw(tmp_path: Path, monkeypatch):
    script = [
        _init(),
        SystemMessage(
            subtype="compact_boundary", data={"type": "system", "subtype": "compact_boundary"}
        ),
        _result(result="done"),
    ]
    _, col, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert col.kinds() == ["session", "raw"]
    assert col.of("raw")[0].name == "compact_boundary"


async def test_structured_output_and_model_usage(tmp_path: Path, monkeypatch):
    model_usage = {
        "claude-haiku-4-5": {"inputTokens": 5, "outputTokens": 3, "costUSD": 0.001},
        "claude-opus-4-5": {"inputTokens": 50, "outputTokens": 300, "costUSD": 0.5},
    }
    script = [
        _init(),
        _result(result='{"ok": true}', structured_output={"ok": True}, model_usage=model_usage),
    ]
    result, _, _ = await _run(monkeypatch, _req(tmp_path, output_schema={"type": "object"}), script)
    assert result.status == "success"
    assert result.structured == {"ok": True}
    assert result.model == "claude-opus-4-5"


async def test_permission_denials_become_warnings(tmp_path: Path, monkeypatch):
    denials = [{"tool_name": "Bash", "tool_use_id": "tu_9", "tool_input": {"command": "rm -rf /"}}]
    script = [_init(), _result(result="done", permission_denials=denials)]
    result, col, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert result.status == "success"
    warn = col.of("warning")
    assert len(warn) == 1 and warn[0].name == "Bash" and warn[0].call_id == "tu_9"
    assert "permission denied" in warn[0].text
    assert result.raw["permission_denials"] == denials


# -- status / error classification ------------------------------------------------------------


async def test_error_max_turns_yielded_then_raised(tmp_path: Path, monkeypatch):
    err = _result(
        subtype="error_max_turns",
        is_error=True,
        result=None,
        num_turns=3,
        terminal_reason="max_turns",
    )
    script = [_init(), err, _result_error(err)]
    result, _, fake = await _run(monkeypatch, _req(tmp_path, max_turns=3), script)
    assert result.status == "max_turns"
    assert result.num_turns == 3
    assert result.error is not None and result.error.transient is False
    assert result.error.code == "error_max_turns"
    assert fake.finalised


async def test_error_max_budget(tmp_path: Path, monkeypatch):
    err = _result(subtype="error_max_budget_usd", is_error=True, result=None, total_cost_usd=2.0)
    script = [_init(), err, _result_error(err)]
    result, _, _ = await _run(monkeypatch, _req(tmp_path, budget_usd=1.0), script)
    assert result.status == "budget"
    assert result.cost_usd == 2.0
    assert result.error is not None and result.error.kind == "budget"


async def test_aborted_is_interrupted(tmp_path: Path, monkeypatch):
    script = [_init(), _result(result="partial", terminal_reason="aborted_streaming")]
    result, _, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert result.status == "interrupted"


async def test_api_error_529_is_transient(tmp_path: Path, monkeypatch):
    synthetic = AssistantMessage(
        content=[TextBlock(text="API Error: 529 overloaded")],
        model="<synthetic>",
        error="server_error",
    )
    err = _result(
        subtype="success",
        is_error=True,
        result="API Error: 529 overloaded",
        terminal_reason="api_error",
        api_error_status=529,
    )
    script = [_init(), synthetic, err, _result_error(err)]
    result, col, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert result.status == "error"
    assert result.error is not None
    assert result.error.kind == "api"
    assert result.error.transient is True
    assert result.error.code == 529
    assert "529" in result.error.message
    # the synthetic message is not output text, but it is surfaced as an error event
    assert "text" not in col.kinds()
    assert col.of("error")[0].data["error"] == "server_error"
    assert result.text == ""


async def test_synthetic_rate_limit_without_status_is_transient(tmp_path: Path, monkeypatch):
    synthetic = AssistantMessage(
        content=[TextBlock(text="rate limited")], model="<synthetic>", error="rate_limit"
    )
    err = _result(
        subtype="success", is_error=True, result="rate limited", terminal_reason="api_error"
    )
    script = [_init(), synthetic, err, _result_error(err)]
    result, _, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert result.status == "error" and result.error is not None
    assert result.error.transient is True and result.error.code == "rate_limit"


@pytest.mark.parametrize(
    ("kind", "status", "expected_kind"),
    [
        ("authentication_failed", 401, "auth"),
        ("billing_error", 402, "budget"),
        ("invalid_request", 400, "api"),
    ],
)
async def test_fatal_synthetic_errors(tmp_path: Path, monkeypatch, kind, status, expected_kind):
    synthetic = AssistantMessage(
        content=[TextBlock(text=f"API Error: {status}")], model="<synthetic>", error=kind
    )
    err = _result(
        subtype="success",
        is_error=True,
        result=f"API Error: {status}",
        terminal_reason="api_error",
        api_error_status=status,
    )
    script = [_init(), synthetic, err, _result_error(err)]
    result, _, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert result.status == "error" and result.error is not None
    assert result.error.transient is False
    assert result.error.kind == expected_kind


async def test_fatal_synthetic_wins_over_transient_status(tmp_path: Path, monkeypatch):
    synthetic = AssistantMessage(
        content=[TextBlock(text="auth")], model="<synthetic>", error="authentication_failed"
    )
    err = _result(
        subtype="success",
        is_error=True,
        result="auth",
        terminal_reason="api_error",
        api_error_status=529,
    )
    script = [_init(), synthetic, err, _result_error(err)]
    result, _, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert (
        result.error is not None and result.error.transient is False and result.error.kind == "auth"
    )


async def test_error_during_execution(tmp_path: Path, monkeypatch):
    err = _result(
        subtype="error_during_execution",
        is_error=True,
        result=None,
        errors=["boom happened"],
        terminal_reason=None,
    )
    script = [_init(), err, _result_error(err)]
    result, _, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert result.status == "error" and result.error is not None
    assert result.error.transient is False and "boom happened" in result.error.message


async def test_result_error_without_prior_result_message(tmp_path: Path, monkeypatch):
    err = _result(
        subtype="error_during_execution", is_error=True, result=None, errors=["startup failed"]
    )
    script = [_init(), _result_error(err)]
    result, _, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert result.status == "error" and result.error is not None
    assert "startup failed" in result.error.message


async def test_stream_ends_without_result_is_transport_error(tmp_path: Path, monkeypatch):
    script = [_init(), AssistantMessage(content=[TextBlock(text="hi")], model="claude-sonnet-4-5")]
    with pytest.raises(ProviderError) as ei:
        await _run(monkeypatch, _req(tmp_path), script)
    assert ei.value.transient is True and ei.value.kind == "transport"


async def test_cli_not_found_maps_to_not_installed(tmp_path: Path, monkeypatch):
    script = [
        CLINotFoundError(
            "Claude Code not found. Install with:\n  npm install -g @anthropic-ai/claude-code"
        )
    ]
    with pytest.raises(ProviderNotInstalledError) as ei:
        await _run(monkeypatch, _req(tmp_path), script)
    assert ei.value.hint and "claude" in ei.value.hint.lower()
    assert ei.value.transient is False


async def test_cli_connection_error_is_fatal_transport(tmp_path: Path, monkeypatch):
    script = [CLIConnectionError("Working directory does not exist: /nope")]
    with pytest.raises(ProviderError) as ei:
        await _run(monkeypatch, _req(tmp_path), script)
    assert not isinstance(ei.value, ProviderNotInstalledError)
    assert ei.value.transient is False and ei.value.kind == "transport"


async def test_process_error_is_transient_with_stderr_tail(tmp_path: Path, monkeypatch):
    fake = FakeQuery([_init(), ProcessError("Command failed", exit_code=1)])
    _install(monkeypatch, fake)

    async def run_with_stderr() -> None:
        provider = ClaudeProvider({})
        # feed stderr through the callback the adapter registered, then let the fake fail
        orig = fake._gen

        async def gen_with_stderr() -> AsyncIterator[Any]:
            opts = fake.options
            assert opts.stderr is not None
            for i in range(50):
                opts.stderr(f"stderr line {i}")
            async for m in orig():
                yield m

        fake._gen = gen_with_stderr  # type: ignore[method-assign]
        await provider.run(_req(tmp_path), Collector())

    with pytest.raises(ProviderError) as ei:
        await run_with_stderr()
    assert ei.value.transient is True and ei.value.kind == "transport"
    assert ei.value.hint is not None
    assert "stderr line 49" in ei.value.hint
    assert "stderr line 9" not in ei.value.hint  # only the last 40 lines are kept


@pytest.mark.parametrize(
    "exc",
    [Exception("Control request timeout: initialize"), RuntimeError("hook failed during resume")],
    ids=["bare-exception", "runtime-error"],
)
async def test_non_sdk_exception_is_wrapped_as_transient_transport_error(
    tmp_path: Path, monkeypatch, exc: Exception
):
    fake = FakeQuery([_init(), exc])
    _install(monkeypatch, fake)
    provider = ClaudeProvider({})
    with pytest.raises(ProviderError) as ei:
        await provider.run(_req(tmp_path), Collector())
    assert ei.value.kind == "transport" and ei.value.transient is True
    assert str(exc) in str(ei.value)
    assert ei.value.__cause__ is exc
    assert fake.finalised


async def test_timeout_after_result_message_folds_the_result(tmp_path: Path, monkeypatch):
    # the SDK's shielded transport close can take seconds after the result frame; a deadline that
    # lands in that window must not turn a finished step into a "timeout" without usage/cost
    fake = FakeQuery(
        [_init(), _result(result="done", structured_output={"ok": True})], sleep_forever=True
    )
    _install(monkeypatch, fake)
    provider = ClaudeProvider({})
    with anyio.fail_after(5):
        result = await provider.run(_req(tmp_path, timeout_s=0.1), Collector())
    assert result.status == "success"
    assert result.text == "done" and result.structured == {"ok": True}
    assert result.usage.output == 20 and result.cost_usd == 0.0123
    assert result.raw["teardown_timed_out"] is True
    assert fake.finalised


async def test_timeout_partial_text_prefers_streamed_deltas(tmp_path: Path, monkeypatch):
    script = [
        _init(),
        _message_start("m1"),
        _text_delta("first"),
        AssistantMessage(content=[TextBlock(text="first")], model="claude-sonnet-4-5"),
        _message_start("m2"),
        _text_delta("second "),
        _text_delta("half"),
    ]
    fake = FakeQuery(script, sleep_forever=True)
    _install(monkeypatch, fake)
    with anyio.fail_after(5):
        result = await ClaudeProvider({}).run(_req(tmp_path, timeout_s=0.1), Collector())
    assert result.status == "timeout"
    assert result.text == "first\nsecond half"


async def test_external_cancellation_propagates_and_closes_generator(tmp_path: Path, monkeypatch):
    fake = FakeQuery([_init(), _text_delta("x")], sleep_forever=True)
    _install(monkeypatch, fake)
    provider = ClaudeProvider({})
    outcome: dict[str, Any] = {}

    async def runner() -> None:
        outcome["result"] = await provider.run(_req(tmp_path, timeout_s=None), Collector())

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(runner)
            await anyio.sleep(0.05)
            tg.cancel_scope.cancel()
    assert "result" not in outcome  # no AgentResult synthesised on engine cancellation
    assert fake.finalised
    assert fake.exit_exc is not None and "ancel" in fake.exit_exc


async def test_timeout_returns_timeout_status_and_closes_generator(tmp_path: Path, monkeypatch):
    fake = FakeQuery([_init(), _message_start("m1"), _text_delta("partial ")], sleep_forever=True)
    _install(monkeypatch, fake)
    provider = ClaudeProvider({})
    col = Collector()
    with anyio.fail_after(5):
        result = await provider.run(_req(tmp_path, timeout_s=0.1), col)
    assert result.status == "timeout"
    assert result.error is not None and result.error.kind == "timeout"
    assert result.error.transient is False
    assert result.session_ref == "sess-1"
    assert result.text == "partial "
    assert fake.finalised  # the generator was finalised (cancelled + aclosed)
    assert "text_delta" in col.kinds()


async def test_no_timeout_when_timeout_s_is_none(tmp_path: Path, monkeypatch):
    result, _, _ = await _run(monkeypatch, _req(tmp_path, timeout_s=None), HAPPY)
    assert result.status == "success"


async def test_usage_missing_fields_default_to_zero(tmp_path: Path, monkeypatch):
    script = [_init(), _result(result="x", usage={"input_tokens": 3}, total_cost_usd=None)]
    result, _, _ = await _run(monkeypatch, _req(tmp_path), script)
    assert result.usage.input == 3 and result.usage.output == 0
    assert result.cost_usd is None and result.cost_source == "none"


# -- healthcheck ------------------------------------------------------------------------------


async def test_healthcheck_without_cli(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(claude_mod, "_bundled_cli_path", lambda: None)
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(claude_mod, "_KNOWN_CLI_LOCATIONS", (tmp_path / "nope",))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    health = await ClaudeProvider({}).healthcheck()
    assert health.ok is False
    assert health.sdk_version == claude_mod.SDK_VERSION
    assert health.cli_path is None and health.cli_version is None
    assert health.auth == "unknown"
    assert any("not found" in d for d in health.details)


async def test_healthcheck_with_fake_cli(monkeypatch, tmp_path: Path):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho '2.1.237 (Claude Code)'\n")
    cli.chmod(0o755)
    monkeypatch.setattr(claude_mod, "_bundled_cli_path", lambda: None)
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(claude_mod, "_KNOWN_CLI_LOCATIONS", (cli,))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    health = await ClaudeProvider({}).healthcheck()
    assert health.ok is True
    assert health.cli_path == str(cli)
    assert health.cli_version == "2.1.237"
    assert health.auth == "ok"
    assert any(d == f"bundled CLI {claude_mod.CLI_BUNDLED_VERSION}" for d in health.details)


async def test_healthcheck_not_ok_when_cli_does_not_report_a_version(monkeypatch, tmp_path: Path):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
    cli.chmod(0o755)
    monkeypatch.setattr(claude_mod, "_bundled_cli_path", lambda: str(cli))
    health = await ClaudeProvider({}).healthcheck()
    assert health.ok is False
    assert health.cli_path == str(cli) and health.cli_version is None
    assert any("did not report a version" in d for d in health.details)


def test_find_cli_uses_windows_names(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(claude_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(claude_mod, "_bundled_dir", lambda: tmp_path / "_bundled")
    monkeypatch.setattr(claude_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(claude_mod.Path, "home", classmethod(lambda cls: tmp_path))
    assert claude_mod.find_cli() is None
    assert claude_mod._known_cli_locations() == (tmp_path / ".local/bin/claude.exe",)
    (tmp_path / "_bundled").mkdir()
    (tmp_path / "_bundled/claude").write_text("posix name must not match on windows")
    assert claude_mod.find_cli() is None
    (tmp_path / "_bundled/claude.exe").write_text("x")
    assert claude_mod.find_cli() == str(tmp_path / "_bundled/claude.exe")


async def test_healthcheck_settings_cli_path_wins(monkeypatch, tmp_path: Path):
    cli = tmp_path / "mycli"
    cli.write_text("#!/bin/sh\necho '9.9.9'\n")
    cli.chmod(0o755)
    monkeypatch.setattr(claude_mod, "_bundled_cli_path", lambda: str(tmp_path / "bundled"))
    health = await ClaudeProvider({"cli_path": str(cli)}).healthcheck()
    assert health.cli_path == str(cli) and health.cli_version == "9.9.9"


async def test_healthcheck_probe_uses_one_turn_query(monkeypatch, tmp_path: Path):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho '2.1.237'\n")
    cli.chmod(0o755)
    monkeypatch.setattr(claude_mod, "_bundled_cli_path", lambda: str(cli))
    fake = _install(monkeypatch, FakeQuery([_init(), _result(result="OK")]))
    health = await ClaudeProvider({}).healthcheck(probe=True)
    assert health.ok is True
    assert any("probe" in d and "ok" in d.lower() for d in health.details)
    assert fake.options.tools == []
    assert fake.options.max_turns == 1
    assert fake.options.permission_mode == "dontAsk"
    assert fake.calls[0][0] == "Reply with exactly OK"


async def test_healthcheck_probe_failure_reported(monkeypatch, tmp_path: Path):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho '2.1.237'\n")
    cli.chmod(0o755)
    monkeypatch.setattr(claude_mod, "_bundled_cli_path", lambda: str(cli))
    _install(monkeypatch, FakeQuery([CLIConnectionError("cannot connect")]))
    health = await ClaudeProvider({}).healthcheck(probe=True)
    assert health.ok is False
    assert any("probe" in d and "cannot connect" in d for d in health.details)


# -- live smoke -------------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RAYSPEC_LIVE") != "1", reason="set RAYSPEC_LIVE=1 to run")
async def test_live_one_turn_query(tmp_path: Path):
    provider = ClaudeProvider({})
    col = Collector()
    req = _req(
        tmp_path,
        prompt="Reply with exactly OK",
        access=AccessLevel.READ_ONLY,
        max_turns=1,
        timeout_s=120,
    )
    result = await provider.run(req, col)
    assert result.status == "success", result
    assert "OK" in result.text
    assert result.session_ref
    assert result.usage.output > 0
    assert "session" in col.kinds()


# -- usage events from completed assistant messages (partial usage of cut-off attempts) ----


async def test_assistant_message_usage_streams_usage_events_with_turn_total(
    tmp_path: Path, monkeypatch
):
    """Every completed assistant message with ``usage`` becomes a ``usage`` event whose
    ``turn_total`` accumulates per message id (Claude Code repeats the message usage on every
    content-block message of the same API message — counted once), so an interrupted attempt
    can record what the provider billed so far."""
    u1 = {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3}
    u2 = {"input_tokens": 20, "cache_creation_input_tokens": 4, "output_tokens": 7}
    script = [
        _init(),
        AssistantMessage(
            content=[TextBlock(text="a")], model="claude-sonnet-4-5", message_id="m1", usage=u1
        ),
        AssistantMessage(  # same API message, second content block: same usage, not re-added
            content=[TextBlock(text="b")], model="claude-sonnet-4-5", message_id="m1", usage=u1
        ),
        AssistantMessage(
            content=[TextBlock(text="c")], model="claude-sonnet-4-5", message_id="m2", usage=u2
        ),
        _result(result="done"),
    ]
    _, col, _ = await _run(monkeypatch, _req(tmp_path), script)
    usage_events = col.of("usage")
    assert [e.data["turn_total"]["input"] for e in usage_events] == [15, 15 + 24]
    assert usage_events[-1].data["turn_total"]["output"] == 10
    assert usage_events[-1].data["turn_total"]["cached_input"] == 5
    assert usage_events[-1].data["turn_total"]["cache_write"] == 4
    assert usage_events[0].data["usage"]["input"] == 15
    # messages without usage (the happy-path fixtures) emit no usage event at all
    _, plain, _ = await _run(monkeypatch, _req(tmp_path), HAPPY)
    assert plain.of("usage") == []

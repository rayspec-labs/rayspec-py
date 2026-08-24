"""CodexProvider: SDK kwargs, notification → AgentEvent mapping, usage deltas, status, cancellation.

No network and no real ``codex`` process: ``rayspec.providers.codex.AsyncCodex`` is replaced by a
fake that records every ``thread_start``/``thread_resume``/``thread_fork``/``turn`` call and
replays notifications built from the **real** generated pydantic models (``model_validate`` on
camelCase wire dicts). The fake's stream blocks on a real ``queue.Queue`` inside
``asyncio.to_thread`` exactly like the SDK, so the shielded-consumer tests exercise the real
thread-leak hazard.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import anyio
import pytest
from openai_codex import ApprovalMode, Sandbox, ServerBusyError, TransportClosedError
from openai_codex.generated.notification_registry import NOTIFICATION_MODELS
from openai_codex.generated.v2_all import GetAccountResponse, ReasoningEffort
from openai_codex.models import Notification

import rayspec.providers.codex as codex_mod
from rayspec.providers.base import (
    AccessLevel,
    AgentEvent,
    AgentRequest,
    McpServerSpec,
    Provider,
    ProviderError,
    ProviderNotInstalledError,
    ToolPolicy,
    Usage,
)
from rayspec.providers.capabilities import CODEX_CAPABILITIES
from rayspec.providers.codex import CodexProvider, classify_turn_error
from rayspec.providers.registry import create_provider

pytestmark = pytest.mark.anyio

# -- wire helpers (real generated models) ------------------------------------------------------


def note(method: str, params: Mapping[str, Any]) -> Notification:
    """Build a ``Notification`` the way the SDK reader thread does (model_validate on wire JSON)."""
    model = NOTIFICATION_MODELS[method]
    payload = cast(Any, model.model_validate(dict(params)))
    return Notification(method=method, payload=payload)


def turn_started(thread_id: str, turn_id: str) -> Notification:
    return note(
        "turn/started",
        {"threadId": thread_id, "turn": {"id": turn_id, "items": [], "status": "inProgress"}},
    )


def turn_completed(
    thread_id: str,
    turn_id: str,
    status: str = "completed",
    error: Mapping[str, Any] | None = None,
) -> Notification:
    turn: dict[str, Any] = {"id": turn_id, "items": [], "status": status, "durationMs": 42}
    if error is not None:
        turn["error"] = dict(error)
    return note("turn/completed", {"threadId": thread_id, "turn": turn})


def agent_delta(thread_id: str, turn_id: str, item_id: str, delta: str) -> Notification:
    return note(
        "item/agentMessage/delta",
        {"threadId": thread_id, "turnId": turn_id, "itemId": item_id, "delta": delta},
    )


def item_started(thread_id: str, turn_id: str, item: Mapping[str, Any]) -> Notification:
    return note(
        "item/started",
        {"threadId": thread_id, "turnId": turn_id, "startedAtMs": 1, "item": dict(item)},
    )


def item_completed(thread_id: str, turn_id: str, item: Mapping[str, Any]) -> Notification:
    return note(
        "item/completed",
        {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 2, "item": dict(item)},
    )


def agent_message(item_id: str, text: str, phase: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "agentMessage", "id": item_id, "text": text}
    if phase is not None:
        item["phase"] = phase
    return item


def breakdown(
    input_tokens: int, output: int, cached: int = 0, reasoning: int = 0, cache_write: int = 0
) -> dict[str, int]:
    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached,
        "cacheWriteInputTokens": cache_write,
        "outputTokens": output,
        "reasoningOutputTokens": reasoning,
        "totalTokens": input_tokens + output,
    }


def token_usage(
    thread_id: str, turn_id: str, *, last: Mapping[str, int], total: Mapping[str, int]
) -> Notification:
    return note(
        "thread/tokenUsage/updated",
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "tokenUsage": {"last": dict(last), "total": dict(total)},
        },
    )


def error_note(
    thread_id: str, turn_id: str, message: str, *, will_retry: bool, info: Any = None
) -> Notification:
    err: dict[str, Any] = {"message": message}
    if info is not None:
        err["codexErrorInfo"] = info
    return note(
        "error",
        {"threadId": thread_id, "turnId": turn_id, "error": err, "willRetry": will_retry},
    )


def simple_turn(thread_id: str, turn_id: str, text: str = "hello") -> list[Notification]:
    return [
        turn_started(thread_id, turn_id),
        agent_delta(thread_id, turn_id, "m1", text),
        item_completed(thread_id, turn_id, agent_message("m1", text, "final_answer")),
        turn_completed(thread_id, turn_id),
    ]


# -- fake SDK ----------------------------------------------------------------------------------


class RecordingStream:
    """Async-generator wrapper that counts explicit ``aclose()`` calls (``contextlib.aclosing``)."""

    def __init__(self, turn: FakeTurn, gen: AsyncIterator[Notification]) -> None:
        self._turn = turn
        self._gen = gen

    def __aiter__(self) -> RecordingStream:
        return self

    async def __anext__(self) -> Notification:
        return await self._gen.__anext__()

    async def aclose(self) -> None:
        self._turn.aclose_calls += 1
        await cast(Any, self._gen).aclose()


@dataclass
class FakeTurn:
    """Stands in for ``AsyncTurnHandle``: a real queue drained through ``asyncio.to_thread``."""

    client: FakeClient
    thread_id: str
    id: str
    on_interrupt: str = "complete"  # complete | complete_success | raise_transport | hang
    queue: queue.Queue[Notification | BaseException] = field(default_factory=queue.Queue)
    interrupt_calls: int = 0
    aclose_calls: int = 0
    registered: bool = False
    closed_stream: bool = False
    waiters: int = 0  # worker threads currently blocked in ``queue.get``

    def push(self, *items: Notification | BaseException) -> None:
        for item in items:
            self.queue.put(item)

    def _blocking_get(self) -> Notification | BaseException:
        self.waiters += 1
        try:
            return self.queue.get()
        finally:
            self.waiters -= 1

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        if self.on_interrupt == "complete":
            self.push(turn_completed(self.thread_id, self.id, "interrupted"))
        elif self.on_interrupt == "complete_success":
            # the turn finished just before the interrupt landed: a real final answer arrives
            self.push(
                item_completed(
                    self.thread_id, self.id, agent_message("m9", "DONE", "final_answer")
                ),
                turn_completed(self.thread_id, self.id, "completed"),
            )
        elif self.on_interrupt == "raise_transport":
            # transport died between turn/start and the interrupt request: the stream's
            # worker thread is woken by MessageRouter.fail_all() with the same error
            self.push(TransportClosedError("Codex process closed stdout"))
            raise TransportClosedError("Codex process is not running")
        # "hang": nothing — only client.close() wakes the stream

    def stream(self) -> RecordingStream:
        return RecordingStream(self, self._stream())

    async def _stream(self) -> AsyncIterator[Notification]:
        self.registered = True
        try:
            while True:
                item = await asyncio.to_thread(self._blocking_get)  # mirrors the SDK
                if isinstance(item, BaseException):
                    raise item
                yield item
                if item.method == "turn/completed":
                    break
        finally:
            self.registered = False
            self.closed_stream = True


@dataclass
class FakeThread:
    client: FakeClient
    id: str
    turn_calls: list[dict[str, Any]] = field(default_factory=list)

    async def turn(self, input: Any, **kwargs: Any) -> FakeTurn:
        self.turn_calls.append({"input": input, **kwargs})
        return self.client.world.start_turn(self.client, self.id)


@dataclass
class FakeClient:
    """Stands in for ``AsyncCodex``; created through ``world.make_client(config)``."""

    world: FakeWorld
    config: Any
    closed: bool = False
    close_calls: int = 0
    thread_start_calls: list[dict[str, Any]] = field(default_factory=list)
    thread_resume_calls: list[dict[str, Any]] = field(default_factory=list)
    thread_fork_calls: list[dict[str, Any]] = field(default_factory=list)
    threads: list[FakeThread] = field(default_factory=list)
    turns: list[FakeTurn] = field(default_factory=list)
    account_calls: int = 0
    start_waiters: int = 0  # worker threads blocked inside a hung ``thread_start``

    def _blocking_start(self, gate: queue.Queue[Any]) -> Any:
        self.start_waiters += 1
        try:
            return gate.get()
        finally:
            self.start_waiters -= 1

    async def thread_start(self, **kwargs: Any) -> FakeThread:
        self.world.maybe_raise("thread_start")
        if self.world.start_gate is not None:  # hung app-server: block a worker like the SDK
            item = await asyncio.to_thread(self._blocking_start, self.world.start_gate)
            if isinstance(item, BaseException):
                raise item
        self.thread_start_calls.append(kwargs)
        thread = FakeThread(self, f"thr-{self.world.next_thread()}")
        self.threads.append(thread)
        return thread

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeThread:
        self.thread_resume_calls.append({"thread_id": thread_id, **kwargs})
        thread = FakeThread(self, thread_id)
        self.threads.append(thread)
        return thread

    async def thread_fork(self, thread_id: str, **kwargs: Any) -> FakeThread:
        self.thread_fork_calls.append({"thread_id": thread_id, **kwargs})
        thread = FakeThread(self, f"{thread_id}-fork")
        self.threads.append(thread)
        return thread

    async def account(self, *, refresh_token: bool = False) -> GetAccountResponse:
        self.account_calls += 1
        return self.world.account_response

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        for turn in self.turns:
            if turn.registered:  # MessageRouter.fail_all(): wake every blocked stream
                turn.push(TransportClosedError("Codex process closed stdout"))
        if self.world.start_gate is not None:  # ... and every blocked response waiter
            for _ in range(self.start_waiters):
                self.world.start_gate.put(TransportClosedError("Codex process closed stdout"))


@dataclass
class FakeWorld:
    """Per-test registry of fake clients/turns plus the queued turn scripts."""

    clients: list[FakeClient] = field(default_factory=list)
    scripts: deque[Callable[[FakeTurn], None]] = field(default_factory=deque)
    turns: list[FakeTurn] = field(default_factory=list)
    thread_counter: int = 0
    turn_counter: int = 0
    raise_on: dict[str, BaseException] = field(default_factory=dict)
    start_gate: queue.Queue[Any] | None = None  # when set, thread_start blocks on it
    account_response: GetAccountResponse = field(
        default_factory=lambda: GetAccountResponse.model_validate(
            {"requiresOpenaiAuth": False, "account": {"type": "apiKey"}}
        )
    )

    def make_client(self, config: Any = None) -> FakeClient:
        client = FakeClient(self, config)
        self.clients.append(client)
        return client

    def next_thread(self) -> int:
        self.thread_counter += 1
        return self.thread_counter

    def maybe_raise(self, where: str) -> None:
        exc = self.raise_on.pop(where, None)
        if exc is not None:
            raise exc

    def start_turn(self, client: FakeClient, thread_id: str) -> FakeTurn:
        self.turn_counter += 1
        turn = FakeTurn(client, thread_id, f"turn-{self.turn_counter}")
        client.turns.append(turn)
        self.turns.append(turn)
        if self.scripts:
            self.scripts.popleft()(turn)
        else:
            turn.push(*simple_turn(thread_id, turn.id))
        return turn

    # -- scripting helpers ----------------------------------------------------------------

    def script(self, build: Callable[[str, str], list[Notification]]) -> None:
        """Queue a turn whose notifications are ``build(thread_id, turn_id)``."""
        self.scripts.append(lambda t: t.push(*build(t.thread_id, t.id)))

    def script_hang(
        self,
        on_interrupt: str = "complete",
        prefix: Callable[[str, str], list[Notification]] | None = None,
    ) -> None:
        """Queue a turn that emits ``prefix`` then blocks until interrupted/closed."""

        def setup(turn: FakeTurn) -> None:
            turn.on_interrupt = on_interrupt
            if prefix is not None:
                turn.push(*prefix(turn.thread_id, turn.id))

        self.scripts.append(setup)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> FakeWorld:
    w = FakeWorld()
    monkeypatch.setattr(codex_mod, "AsyncCodex", w.make_client)
    return w


class Collector:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def of(self, kind: str) -> list[AgentEvent]:
        return [e for e in self.events if e.kind == kind]


def _req(prompt: str = "Say hi", **kw: Any) -> AgentRequest:
    kw.setdefault("cwd", "/tmp/work")
    return AgentRequest(step_path="review", prompt=prompt, **kw)


async def _wait_for_turns(world: FakeWorld, count: int) -> None:
    """Block until ``count`` turns have been started.

    The tests below act *while a turn is running*; that state has no awaitable to wait on, so it
    is polled. Polling the state instead of sleeping a fixed amount removes the race entirely —
    the ``fail_after`` is only here to turn a hang into a readable failure.
    """
    with anyio.fail_after(5):
        while len(world.turns) < count:
            await anyio.sleep(0)


class _FrozenPerfCounter:
    """Stands in for the adapter module's ``time``: ``perf_counter()`` walks the given readings.

    Only ``rayspec.providers.codex``'s view of ``time`` is replaced — the event loop keeps the
    real clock — so the turn's measured duration is exactly ``readings[1] - readings[0]``.
    """

    def __init__(self, *readings: float) -> None:
        self._readings = iter(readings)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    def perf_counter(self) -> float:
        return next(self._readings)


async def _open(settings: Mapping[str, Any] | None = None, **env: str) -> CodexProvider:
    provider = CodexProvider(settings or {})
    await provider.open(run_id="r1", workdir="/tmp/work", env=env, max_parallel=4)
    return provider


# -- lifecycle / protocol ------------------------------------------------------------------------


async def test_codex_provider_protocol_and_registry(world: FakeWorld):
    provider = CodexProvider({})
    assert isinstance(provider, Provider)
    assert provider.id == "codex"
    assert provider.capabilities is CODEX_CAPABILITIES
    via_registry = create_provider("codex", {"approval_mode": "auto_review"})
    assert isinstance(via_registry, CodexProvider)
    await provider.open(run_id="r1", workdir="/tmp/work", env={}, max_parallel=4)
    assert world.clients == []  # clients are created lazily on first use
    await provider.aclose()


async def test_open_creates_client_pool_lazily_and_aclose_closes_every_client(world: FakeWorld):
    provider = await _open(RUN_VAR="1")
    await provider.run(_req(), Collector())
    await provider.run(_req(env={"STEP_VAR": "x"}), Collector())
    await provider.run(_req(env={"STEP_VAR": "x"}), Collector())  # same signature → same client
    assert len(world.clients) == 2
    cfg0, cfg1 = world.clients[0].config, world.clients[1].config
    assert cfg0.cwd == "/tmp/work" and cfg0.client_name == "rayspec" and cfg0.client_version
    assert cfg0.env == {"RUN_VAR": "1"}
    assert cfg1.env == {"RUN_VAR": "1", "STEP_VAR": "x"}
    await provider.aclose()
    assert all(c.closed for c in world.clients)


async def test_settings_codex_bin_is_passed_to_codex_config(world: FakeWorld):
    provider = await _open({"codex_bin": "/opt/codex"})
    await provider.run(_req(), Collector())
    assert world.clients[0].config.codex_bin == "/opt/codex"
    await provider.aclose()


# -- thread_start / turn kwargs ----------------------------------------------------------------


async def test_thread_start_kwargs_defaults(world: FakeWorld):
    provider = await _open()
    result = await provider.run(
        _req(model="gpt-5.4", effort="high", instructions="Be terse."), Collector()
    )
    call = world.clients[0].thread_start_calls[0]
    assert call["cwd"] == "/tmp/work"
    assert call["model"] == "gpt-5.4"
    assert call["sandbox"] is Sandbox.workspace_write
    assert call["approval_mode"] is ApprovalMode.deny_all
    assert call["developer_instructions"] == "Be terse."
    assert call.get("base_instructions") is None
    assert call.get("ephemeral") in (None, False)
    assert call["config"] == {}
    turn = world.clients[0].threads[0].turn_calls[0]
    assert turn["input"] == "Say hi"
    assert turn["effort"] is ReasoningEffort.high
    assert turn["model"] == "gpt-5.4"
    assert turn.get("output_schema") is None
    assert "sandbox" not in turn or turn["sandbox"] is None
    assert result.session_ref == "thr-1"
    assert result.model == "gpt-5.4"
    await provider.aclose()


@pytest.mark.parametrize(
    ("access", "sandbox"),
    [
        (AccessLevel.READ_ONLY, Sandbox.read_only),
        (AccessLevel.WORKSPACE_WRITE, Sandbox.workspace_write),
        (AccessLevel.FULL, Sandbox.full_access),
    ],
)
async def test_access_maps_to_sandbox(world: FakeWorld, access: AccessLevel, sandbox: Sandbox):
    provider = await _open()
    await provider.run(_req(access=access), Collector())
    assert world.clients[0].thread_start_calls[0]["sandbox"] is sandbox
    await provider.aclose()


async def test_replace_instructions_and_effort_alias_and_provider_options(world: FakeWorld):
    provider = await _open({"config": {"model_reasoning_summary": "detailed"}})
    await provider.run(
        _req(
            instructions="You are vanilla.",
            instructions_mode="replace",
            effort="max",
            provider_options={"codex": {"approval_mode": "auto_review", "config": {"foo": "bar"}}},
        ),
        Collector(),
    )
    call = world.clients[0].thread_start_calls[0]
    assert call["base_instructions"] == "You are vanilla."
    assert call.get("developer_instructions") is None
    assert call["approval_mode"] is ApprovalMode.auto_review
    assert call["config"] == {"model_reasoning_summary": "detailed", "foo": "bar"}
    turn = world.clients[0].threads[0].turn_calls[0]
    assert turn["effort"].value == "max"  # max is passed through (gpt-5.6 family), no alias
    await provider.aclose()


async def test_provider_options_already_narrowed_to_codex_are_accepted(world: FakeWorld):
    provider = await _open()
    await provider.run(_req(provider_options={"approval_mode": "auto_review"}), Collector())
    assert world.clients[0].thread_start_calls[0]["approval_mode"] is ApprovalMode.auto_review
    await provider.aclose()


def test_the_block_the_adapter_reads_is_the_block_policy_checks() -> None:
    """One narrowing for both, so a nesting variant cannot reach a thread unexamined.

    ``provider_options.codex.codex.config`` is a shape this adapter honours. While the load-time
    check walked a hand-written path instead, that shape was an unguarded pass-through — the
    policy said "no such server" and the thread got the server.
    """
    from rayspec.schema import provider_option_block

    shapes: list[dict[str, Any]] = [
        {"codex": {"config": {"mcp_servers": {"evil": {"command": "/bin/sh"}}}}},
        {"config": {"mcp_servers": {"evil": {"command": "/bin/sh"}}}},
        {"approval_mode": "auto_review"},
        {"codex": {}},
        {},
    ]
    for options in shapes:
        assert CodexProvider._options(_req(provider_options=options)) == provider_option_block(
            "codex", options
        )


async def test_invalid_approval_mode_is_a_provider_error(world: FakeWorld):
    provider = await _open({"approval_mode": "yolo"})
    with pytest.raises(ProviderError) as info:
        await provider.run(_req(), Collector())
    assert "approval_mode" in str(info.value)
    await provider.aclose()


async def test_mcp_servers_and_web_deny_land_in_config(world: FakeWorld):
    provider = await _open()
    await provider.run(
        _req(
            tools=ToolPolicy(deny=("web",)),
            mcp_servers=(
                McpServerSpec(name="gh", command="gh-mcp", args=("--x",), env={"T": "1"}),
                McpServerSpec(
                    name="docs", transport="http", url="https://d.example", headers={"A": "b"}
                ),
            ),
        ),
        Collector(),
    )
    config = world.clients[0].thread_start_calls[0]["config"]
    assert config["web_search"] == "disabled"
    assert config["mcp_servers"] == {
        "gh": {"command": "gh-mcp", "args": ["--x"], "env": {"T": "1"}},
        "docs": {"url": "https://d.example", "http_headers": {"A": "b"}},
    }
    await provider.aclose()


async def test_provider_options_cannot_widen_the_computed_config(world: FakeWorld):
    """``provider_options.codex.config`` may not name a key the adapter computes itself.

    ``config`` is a raw pass-through applied over the adapter's own keys, so without this the
    workflow re-enables web search, raises the sandbox or swaps the model from inside itself.
    """
    provider = await _open()
    collector = Collector()
    await provider.run(
        _req(
            tools=ToolPolicy(deny=("web",)),
            model="gpt-5.6",
            access=AccessLevel.READ_ONLY,
            provider_options={
                "codex": {
                    "config": {
                        "tools": {"web_search": True, "view_image": True},
                        "web_search": "enabled",
                        "model": "gpt-5.6-pro",
                        "sandbox_mode": "danger-full-access",
                        "model_reasoning_effort": "high",
                    }
                }
            },
        ),
        collector,
    )
    call = world.clients[0].thread_start_calls[0]
    config = call["config"]
    assert config["web_search"] == "disabled"
    assert config["tools"] == {"view_image": True}
    assert "model" not in config and "sandbox_mode" not in config
    assert config["model_reasoning_effort"] == "high"  # not a key the adapter computes
    assert call["model"] == "gpt-5.6" and call["sandbox"] == "read-only"
    warned = "\n".join(e.text or "" for e in collector.of("warning"))
    for key in (
        "config.tools.web_search",
        "config.web_search",
        "config.model",
        "config.sandbox_mode",
    ):
        assert f"provider_options.codex.{key}" in warned
    await provider.aclose()


async def test_provider_options_mcp_servers_still_merge(world: FakeWorld):
    """The merged keys stay merged: only the computed ones are refused."""
    provider = await _open()
    await provider.run(
        _req(
            mcp_servers=(McpServerSpec(name="gh", command="gh-mcp"),),
            provider_options={"codex": {"config": {"mcp_servers": {"docs": {"command": "d"}}}}},
        ),
        Collector(),
    )
    config = world.clients[0].thread_start_calls[0]["config"]
    assert set(config["mcp_servers"]) == {"gh", "docs"}
    await provider.aclose()


async def test_unsupported_tool_group_raises_provider_error(world: FakeWorld):
    provider = await _open()
    with pytest.raises(ProviderError) as info:
        await provider.run(_req(tools=ToolPolicy(allow=("shell",))), Collector())
    assert "tool group" in str(info.value)
    await provider.aclose()


async def test_output_schema_is_normalised_to_strict_and_structured_is_parsed(world: FakeWorld):
    world.script(
        lambda t, u: [
            turn_started(t, u),
            item_completed(t, u, agent_message("m1", '{"ok": true, "n": 2}', "final_answer")),
            turn_completed(t, u),
        ]
    )
    provider = await _open()
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "n": {"type": "integer"}}}
    result = await provider.run(_req(output_schema=schema), Collector())
    passed = world.clients[0].threads[0].turn_calls[0]["output_schema"]
    assert passed["additionalProperties"] is False and passed["required"] == ["ok", "n"]
    assert result.structured == {"ok": True, "n": 2}
    assert result.text == '{"ok": true, "n": 2}'
    await provider.aclose()


async def test_invalid_json_with_schema_gives_structured_none(world: FakeWorld):
    world.script(
        lambda t, u: [
            item_completed(t, u, agent_message("m1", "not json", "final_answer")),
            turn_completed(t, u),
        ]
    )
    provider = await _open()
    result = await provider.run(_req(output_schema={"type": "object"}), Collector())
    assert result.status == "success" and result.structured is None and result.text == "not json"
    await provider.aclose()


async def test_open_schema_warning_is_emitted_as_warning_event(world: FakeWorld):
    provider = await _open()
    collector = Collector()
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": True,
    }
    await provider.run(_req(output_schema=schema), collector)
    assert any("additionalProperties" in e.text for e in collector.of("warning"))
    await provider.aclose()


# -- resume / fork -----------------------------------------------------------------------------


async def test_resume_uses_thread_resume_with_same_kwargs(world: FakeWorld):
    provider = await _open()
    result = await provider.run(
        _req(resume_session="thr-old", access=AccessLevel.READ_ONLY, model="gpt-5.4"), Collector()
    )
    client = world.clients[0]
    assert client.thread_start_calls == []
    call = client.thread_resume_calls[0]
    assert call["thread_id"] == "thr-old"
    assert call["sandbox"] is Sandbox.read_only and call["model"] == "gpt-5.4"
    assert call["approval_mode"] is ApprovalMode.deny_all and call["cwd"] == "/tmp/work"
    assert result.session_ref == "thr-old"
    await provider.aclose()


async def test_fork_uses_thread_fork(world: FakeWorld):
    provider = await _open()
    result = await provider.run(_req(resume_session="thr-old", fork_session=True), Collector())
    client = world.clients[0]
    assert client.thread_resume_calls == []
    assert client.thread_fork_calls[0]["thread_id"] == "thr-old"
    assert result.session_ref == "thr-old-fork"
    await provider.aclose()


# -- event mapping -----------------------------------------------------------------------------


async def test_event_mapping_full_turn(world: FakeWorld):
    def build(t: str, u: str) -> list[Notification]:
        return [
            turn_started(t, u),
            note(
                "item/reasoning/summaryTextDelta",
                {
                    "threadId": t,
                    "turnId": u,
                    "itemId": "r1",
                    "summaryIndex": 0,
                    "delta": "thinking",
                },
            ),
            item_completed(t, u, {"type": "reasoning", "id": "r1", "summary": ["thinking"]}),
            item_started(
                t,
                u,
                {
                    "type": "commandExecution",
                    "id": "c1",
                    "command": "ls -la",
                    "commandActions": [],
                    "cwd": "/tmp/work",
                    "status": "inProgress",
                },
            ),
            note(
                "item/commandExecution/outputDelta",
                {"threadId": t, "turnId": u, "itemId": "c1", "delta": "a.py\n"},
            ),
            item_completed(
                t,
                u,
                {
                    "type": "commandExecution",
                    "id": "c1",
                    "command": "ls -la",
                    "commandActions": [],
                    "cwd": "/tmp/work",
                    "status": "completed",
                    "exitCode": 0,
                    "durationMs": 12,
                    "aggregatedOutput": "a.py\n",
                },
            ),
            item_completed(
                t,
                u,
                {
                    "type": "fileChange",
                    "id": "f1",
                    "status": "completed",
                    "changes": [
                        {"path": "a.py", "kind": {"type": "add"}, "diff": "+x"},
                        {
                            "path": "b.py",
                            "kind": {"type": "update", "move_path": None},
                            "diff": "-y",
                        },
                    ],
                },
            ),
            item_started(
                t,
                u,
                {
                    "type": "mcpToolCall",
                    "id": "mc1",
                    "server": "gh",
                    "tool": "search",
                    "arguments": {"q": "x"},
                    "status": "inProgress",
                },
            ),
            item_completed(
                t,
                u,
                {
                    "type": "mcpToolCall",
                    "id": "mc1",
                    "server": "gh",
                    "tool": "search",
                    "arguments": {"q": "x"},
                    "status": "completed",
                    "durationMs": 5,
                    "result": {"content": [{"type": "text", "text": "found"}]},
                },
            ),
            item_started(t, u, {"type": "webSearch", "id": "w1", "query": "anyio docs"}),
            item_completed(
                t,
                u,
                {
                    "type": "webSearch",
                    "id": "w1",
                    "query": "anyio docs",
                    "action": {"type": "search", "query": "anyio docs"},
                },
            ),
            note(
                "turn/plan/updated",
                {
                    "threadId": t,
                    "turnId": u,
                    "explanation": "plan",
                    "plan": [
                        {"step": "read", "status": "completed"},
                        {"step": "fix", "status": "pending"},
                    ],
                },
            ),
            agent_delta(t, u, "m1", "I looked"),
            item_completed(t, u, agent_message("m1", "I looked around", "commentary")),
            agent_delta(t, u, "m2", "Done"),
            item_completed(t, u, agent_message("m2", "Done.", "final_answer")),
            token_usage(t, u, last=breakdown(100, 20), total=breakdown(100, 20)),
            error_note(t, u, "rate limited", will_retry=True, info="serverOverloaded"),
            note("account/rateLimits/updated", {"rateLimits": {}}),
            turn_completed(t, u),
        ]

    world.script(build)
    provider = await _open()
    collector = Collector()
    result = await provider.run(_req(model="gpt-5.4"), collector)
    kinds = collector.kinds()
    assert kinds[0] == "session"
    session = collector.of("session")[0]
    assert session.text == "thr-1" and session.data["turn_id"] == "turn-1"

    reasoning = collector.of("reasoning")
    assert [e.text for e in reasoning] == ["thinking"]  # no duplicate from item/completed

    start = collector.of("command_start")[0]
    assert start.text == "ls -la" and start.call_id == "c1" and start.data["cwd"] == "/tmp/work"
    out = collector.of("command_output")[0]
    assert out.text == "a.py\n" and out.call_id == "c1"
    end = collector.of("command_end")[0]
    assert end.call_id == "c1"
    assert end.data["exit_code"] == 0 and end.data["status"] == "completed"
    assert end.data["duration_ms"] == 12

    changes = collector.of("file_change")
    assert [(e.name, e.data["kind"], e.text) for e in changes] == [
        ("a.py", "add", "+x"),
        ("b.py", "update", "-y"),
    ]
    assert all(e.data["status"] == "completed" for e in changes)

    calls = collector.of("tool_call")
    results = collector.of("tool_result")
    assert [(e.name, e.call_id) for e in calls] == [("gh/search", "mc1"), ("web_search", "w1")]
    assert calls[0].data["arguments"] == {"q": "x"} and calls[1].text == "anyio docs"
    assert [(e.name, e.call_id) for e in results] == [("gh/search", "mc1"), ("web_search", "w1")]
    assert "found" in results[0].text and results[0].data["status"] == "completed"

    plan = collector.of("plan")[0]
    assert "fix" in plan.text and plan.data["plan"][1]["status"] == "pending"

    assert [e.text for e in collector.of("text_delta")] == ["I looked", "Done"]
    texts = collector.of("text")
    assert [(e.text, e.data["phase"]) for e in texts] == [
        ("I looked around", "commentary"),
        ("Done.", "final_answer"),
    ]

    usage_events = collector.of("usage")
    assert len(usage_events) == 1 and usage_events[0].data["usage"]["input"] == 100

    warnings = collector.of("warning")
    assert any(e.text == "rate limited" and e.data["will_retry"] is True for e in warnings)
    assert collector.of("error") == []
    raw = collector.of("raw")
    assert raw and raw[0].name == "account/rateLimits/updated"

    assert result.status == "success"
    assert result.text == "Done."  # final_answer wins; commentary is not output
    assert result.usage == Usage(input=100, output=20)
    assert result.session_ref == "thr-1"
    assert result.raw["turn_id"] == "turn-1" and result.raw["turn_status"] == "completed"
    await provider.aclose()


async def test_duration_ms_is_the_adapter_s_own_measurement(world: FakeWorld, monkeypatch):
    """``duration_ms`` is rayspec's wall time around the turn, not the SDK's reported figure.

    Pinned exactly against an injected clock: ``>= 0`` holds for every implementation, including
    one that stopped measuring, so only a fixed number can catch a regression. (The SDK's own
    per-command figure is asserted separately, in the event-mapping test.)
    """
    monkeypatch.setattr(codex_mod, "time", _FrozenPerfCounter(100.0, 100.25))
    provider = await _open()
    result = await provider.run(_req(), Collector())
    assert result.duration_ms == 250
    await provider.aclose()


async def test_text_fallbacks_phaseless_then_partial_deltas(world: FakeWorld):
    world.script(
        lambda t, u: [
            item_completed(t, u, agent_message("m0", "first")),
            item_completed(t, u, agent_message("m1", "chatter", "commentary")),
            item_completed(t, u, agent_message("m2", "last phaseless")),
            turn_completed(t, u),
        ]
    )
    world.script(
        lambda t, u: [
            agent_delta(t, u, "m1", "par"),
            agent_delta(t, u, "m1", "tial"),
            turn_completed(t, u, "interrupted"),
        ]
    )
    world.script(
        lambda t, u: [
            item_completed(t, u, agent_message("m1", "only commentary", "commentary")),
            turn_completed(t, u),
        ]
    )
    provider = await _open()
    r1 = await provider.run(_req(), Collector())
    assert r1.text == "last phaseless"
    r2 = await provider.run(_req(), Collector())
    assert r2.status == "interrupted" and r2.text == "partial"
    r3 = await provider.run(_req(), Collector())
    assert r3.text == ""
    await provider.aclose()


# -- usage: total delta per thread ---------------------------------------------------------------


async def test_usage_is_total_delta_across_two_turns_on_one_thread(world: FakeWorld):
    # turn 1: two updates on a fresh thread — usage = final total, never the sum of `last`
    world.script(
        lambda t, u: [
            token_usage(
                t, u, last=breakdown(100, 10, cached=20), total=breakdown(100, 10, cached=20)
            ),
            token_usage(
                t, u, last=breakdown(150, 30, cached=90), total=breakdown(250, 40, cached=110)
            ),
            item_completed(t, u, agent_message("m1", "one", "final_answer")),
            turn_completed(t, u),
        ]
    )
    # turn 2 (resumed on the same thread id): totals continue from 250/40
    world.script(
        lambda t, u: [
            token_usage(
                t, u, last=breakdown(300, 5, cached=250), total=breakdown(550, 45, cached=360)
            ),
            item_completed(t, u, agent_message("m2", "two", "final_answer")),
            turn_completed(t, u),
        ]
    )
    provider = await _open()
    c1 = Collector()
    r1 = await provider.run(_req(model="gpt-5.4"), c1)
    assert r1.usage == Usage(input=250, cached_input=110, output=40)
    deltas = [e.data["usage"] for e in c1.of("usage")]
    assert deltas[0]["input"] == 100 and deltas[1]["input"] == 150 and deltas[1]["output"] == 30

    c2 = Collector()
    r2 = await provider.run(_req(model="gpt-5.4", resume_session=r1.session_ref), c2)
    assert r2.usage == Usage(input=300, cached_input=250, output=5)
    assert c2.of("usage")[0].data["total"]["input"] == 550
    await provider.aclose()


async def test_usage_on_resumed_thread_unknown_to_this_provider_infers_the_baseline(
    world: FakeWorld,
):
    # first update for a thread we never saw: the history before this turn is total - last
    world.script(
        lambda t, u: [
            token_usage(t, u, last=breakdown(30, 3), total=breakdown(1030, 103)),
            token_usage(t, u, last=breakdown(40, 4), total=breakdown(1070, 107)),
            turn_completed(t, u),
        ]
    )
    provider = await _open()
    result = await provider.run(_req(resume_session="thr-history"), Collector())
    assert result.usage == Usage(input=70, output=7)
    await provider.aclose()


async def test_cost_from_pricing_table_else_none(world: FakeWorld):
    world.script(
        lambda t, u: [
            token_usage(
                t,
                u,
                last=breakdown(1_000_000, 100_000, cached=500_000),
                total=breakdown(1_000_000, 100_000, cached=500_000),
            ),
            turn_completed(t, u),
        ]
    )
    provider = await _open(
        {"pricing": {"gpt-5*": {"input": 2.0, "cached_input": 0.5, "output": 8.0}}}
    )
    result = await provider.run(_req(model="gpt-5.4"), Collector())
    assert result.cost_source == "table"
    assert result.cost_usd == pytest.approx(0.5 * 2.0 + 0.5 * 0.5 + 0.1 * 8.0)
    unpriced = await provider.run(_req(model="o9-mini"), Collector())
    assert unpriced.cost_usd is None and unpriced.cost_source == "none"
    await provider.aclose()


# -- status mapping ----------------------------------------------------------------------------


async def test_interrupted_turn_without_our_deadline_is_interrupted(world: FakeWorld):
    world.script(lambda t, u: [turn_completed(t, u, "interrupted")])
    provider = await _open()
    result = await provider.run(_req(), Collector())
    assert result.status == "interrupted"
    await provider.aclose()


@pytest.mark.parametrize(
    ("info", "kind", "transient", "code"),
    [
        ("serverOverloaded", "api", True, "serverOverloaded"),
        ("internalServerError", "api", True, "internalServerError"),
        (
            {"httpConnectionFailed": {"httpStatusCode": 502}},
            "transport",
            True,
            "httpConnectionFailed",
        ),
        ({"responseStreamDisconnected": {}}, "transport", True, "responseStreamDisconnected"),
        (
            {"responseStreamConnectionFailed": {}},
            "transport",
            True,
            "responseStreamConnectionFailed",
        ),
        ({"responseTooManyFailedAttempts": {}}, "transport", True, "responseTooManyFailedAttempts"),
        ("unauthorized", "auth", False, "unauthorized"),
        ("usageLimitExceeded", "budget", False, "usageLimitExceeded"),
        ("badRequest", "api", False, "badRequest"),
        ("contextWindowExceeded", "model", False, "contextWindowExceeded"),
        ("cyberPolicy", "api", False, "cyberPolicy"),
        ("sandboxError", "sandbox", False, "sandboxError"),
        (None, "unknown", False, None),
    ],
)
async def test_failed_turn_is_classified_by_codex_error_info(
    world: FakeWorld, info: Any, kind: str, transient: bool, code: str | None
):
    err: dict[str, Any] = {"message": "it broke"}
    if info is not None:
        err["codexErrorInfo"] = info
    world.script(lambda t, u: [turn_completed(t, u, "failed", error=err)])
    provider = await _open()
    collector = Collector()
    result = await provider.run(_req(), collector)
    assert result.status == "error"
    assert result.error is not None
    assert result.error.kind == kind and result.error.transient is transient
    assert result.error.code == code and result.error.message == "it broke"
    await provider.aclose()


def test_classify_turn_error_helper_handles_none():
    err = classify_turn_error(None)
    assert err.kind == "unknown" and err.transient is False


async def test_error_notification_without_retry_is_error_event_and_feeds_failed_turn(
    world: FakeWorld,
):
    world.script(
        lambda t, u: [
            error_note(t, u, "auth failed", will_retry=False, info="unauthorized"),
            turn_completed(t, u, "failed"),  # no turn.error → fall back to the last error note
        ]
    )
    provider = await _open()
    collector = Collector()
    result = await provider.run(_req(), collector)
    assert [e.text for e in collector.of("error")] == ["auth failed"]
    assert result.status == "error" and result.error is not None
    assert result.error.kind == "auth" and result.error.message == "auth failed"
    await provider.aclose()


# -- SDK exceptions → ProviderError --------------------------------------------------------------


async def test_server_busy_on_thread_start_is_transient_provider_error(world: FakeWorld):
    world.raise_on["thread_start"] = ServerBusyError(-32000, "busy", "server_overloaded")
    provider = await _open()
    with pytest.raises(ProviderError) as info:
        await provider.run(_req(), Collector())
    assert info.value.transient is True and info.value.kind == "api"
    await provider.aclose()


async def test_missing_runtime_is_provider_not_installed(world: FakeWorld):
    world.raise_on["thread_start"] = FileNotFoundError("Unable to locate the pinned Codex runtime")
    provider = await _open()
    with pytest.raises(ProviderNotInstalledError) as info:
        await provider.run(_req(), Collector())
    assert info.value.hint and "codex_bin" in info.value.hint
    await provider.aclose()


async def test_transport_closed_on_thread_start_poisons_client_and_recreates(world: FakeWorld):
    world.raise_on["thread_start"] = TransportClosedError("Codex process closed stdout")
    provider = await _open()
    with pytest.raises(ProviderError) as info:
        await provider.run(_req(), Collector())
    assert info.value.transient is True and info.value.kind == "transport"
    assert world.clients[0].closed
    result = await provider.run(_req(), Collector())  # retry → a fresh client
    assert result.status == "success" and len(world.clients) == 2
    await provider.aclose()


async def test_transport_closed_mid_stream_is_transient_provider_error(world: FakeWorld):
    world.script_hang(prefix=lambda t, u: [agent_delta(t, u, "m1", "par")])
    provider = await _open()

    async def kill_later() -> None:
        await _wait_for_turns(world, 1)  # the stream exists: the failure really lands mid-stream
        world.turns[0].push(TransportClosedError("Codex process closed stdout"))

    errors: list[ProviderError] = []
    async with anyio.create_task_group() as tg:
        tg.start_soon(kill_later)
        with pytest.raises(ProviderError) as info:
            await provider.run(_req(), Collector())
        errors.append(info.value)
    assert errors[0].transient is True and errors[0].kind == "transport"
    assert world.clients[0].closed is True  # poisoned client is closed
    await provider.run(_req(), Collector())
    assert len(world.clients) == 2
    await provider.aclose()


# -- shielded consumer: timeout / cancel ---------------------------------------------------------


async def _settle(baseline: int, *, timeout: float = 5.0) -> None:
    """Wait until the thread count is back at ``baseline`` (blocked workers have returned)."""
    with anyio.move_on_after(timeout):
        while threading.active_count() > baseline:
            await anyio.sleep(0.02)


async def test_timeout_interrupts_drains_and_returns_timeout_without_leaking_threads(
    world: FakeWorld,
):
    prefix = lambda t, u: [turn_started(t, u), agent_delta(t, u, "m1", "par")]  # noqa: E731
    world.script_hang(prefix=prefix)
    world.script_hang(prefix=prefix)
    provider = await _open({"drain_s": 2.0})
    await provider.run(_req(timeout_s=0.1), Collector())  # warm the worker pool (same shape)
    baseline = threading.active_count()
    collector = Collector()
    result = await provider.run(_req(timeout_s=0.2), collector)
    turn = world.turns[1]
    assert turn.interrupt_calls == 1
    assert result.status == "timeout"
    assert result.error is not None and result.error.kind == "timeout"
    assert result.text == "par"
    assert result.raw["turn_status"] == "interrupted"  # the drained turn/completed
    assert turn.closed_stream is True  # the generator finished normally (queue unregistered)
    assert turn.waiters == 0  # no worker thread is left blocked in queue.get
    assert world.clients[0].closed is False  # no need to kill the client
    await _settle(baseline)
    assert threading.active_count() <= baseline
    await provider.aclose()


async def test_external_cancellation_interrupts_and_reraises(world: FakeWorld):
    world.script_hang(prefix=lambda t, u: [turn_started(t, u)])
    world.script_hang(prefix=lambda t, u: [turn_started(t, u)])
    provider = await _open({"drain_s": 2.0})
    await provider.run(_req(timeout_s=0.1), Collector())  # warm the worker pool
    baseline = threading.active_count()
    results: list[Any] = []

    async def runner() -> None:
        results.append(await provider.run(_req(), Collector()))

    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tg:
            tg.start_soon(runner)
            await _wait_for_turns(world, 2)  # the second turn is running: cancel mid-turn
            scope.cancel()
    assert scope.cancelled_caught
    assert results == []  # cancellation propagated, no result was produced
    turn = world.turns[1]
    assert turn.interrupt_calls == 1 and turn.closed_stream is True and turn.waiters == 0
    await _settle(baseline)
    assert threading.active_count() <= baseline
    await provider.aclose()


async def test_hung_stream_after_interrupt_closes_and_recreates_client(world: FakeWorld):
    world.script_hang(on_interrupt="hang", prefix=lambda t, u: [turn_started(t, u)])
    provider = await _open({"drain_s": 0.1})
    result = await provider.run(_req(timeout_s=0.1), Collector())
    assert result.status == "timeout"
    turn = world.turns[0]
    assert turn.interrupt_calls == 1
    assert world.clients[0].closed is True and world.clients[0].close_calls == 1
    assert turn.closed_stream is True and turn.waiters == 0
    # the next request gets a fresh client
    again = await provider.run(_req(), Collector())
    assert again.status == "success" and len(world.clients) == 2
    await provider.aclose()


async def test_transport_closed_during_interrupt_closes_and_recreates_client(world: FakeWorld):
    world.script_hang(on_interrupt="raise_transport", prefix=lambda t, u: [turn_started(t, u)])
    provider = await _open({"drain_s": 1.0})
    result = await provider.run(_req(timeout_s=0.1), Collector())
    assert result.status == "timeout"  # our deadline fired; the transport death is secondary
    assert world.clients[0].closed is True
    again = await provider.run(_req(), Collector())
    assert again.status == "success" and len(world.clients) == 2
    await provider.aclose()


async def test_capacity_limiter_leaves_spare_workers(world: FakeWorld):
    provider = await _open()
    assert provider.limiter is not None
    assert provider.limiter.total_tokens >= 1
    assert provider.limiter.total_tokens <= provider.executor_workers - 4
    await provider.aclose()


# -- healthcheck -------------------------------------------------------------------------------


async def test_healthcheck_reports_sdk_and_auth_via_env(world: FakeWorld, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider = CodexProvider({})
    health = await provider.healthcheck()
    assert health.sdk_version
    assert health.auth == "ok"
    assert world.clients == []  # env auth → no app-server spawned
    await provider.aclose()


async def test_healthcheck_uses_account_when_no_env_key(world: FakeWorld, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = CodexProvider({})
    health = await provider.healthcheck()
    assert health.auth == "ok" and world.clients[0].account_calls == 1
    world.account_response = GetAccountResponse.model_validate({"requiresOpenaiAuth": True})
    provider2 = CodexProvider({})
    health2 = await provider2.healthcheck()
    assert health2.auth == "missing"
    await provider.aclose()
    await provider2.aclose()


async def test_healthcheck_probe_runs_an_ephemeral_read_only_turn(world: FakeWorld, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    world.script(
        lambda t, u: [
            item_completed(t, u, agent_message("m1", "OK", "final_answer")),
            turn_completed(t, u),
        ]
    )
    provider = CodexProvider({})
    health = await provider.healthcheck(probe=True)
    call = world.clients[0].thread_start_calls[0]
    assert call["ephemeral"] is True
    assert call["sandbox"] is Sandbox.read_only and call["approval_mode"] is ApprovalMode.deny_all
    assert health.ok and any("probe" in d for d in health.details)
    await provider.aclose()


async def test_healthcheck_probe_failure_is_reported_not_raised(world: FakeWorld, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    world.raise_on["thread_start"] = ServerBusyError(-32000, "busy", "server_overloaded")
    provider = CodexProvider({})
    health = await provider.healthcheck(probe=True)
    assert health.ok is False and any("busy" in d for d in health.details)
    await provider.aclose()


async def test_run_before_open_works_with_defaults(world: FakeWorld):
    provider = CodexProvider({})
    result = await provider.run(_req(), Collector())
    assert result.status == "success"
    await provider.aclose()


async def test_emit_failure_propagates_untouched_and_unregisters_the_stream(world: FakeWorld):
    provider = await _open()

    async def boom(_event: AgentEvent) -> None:
        raise RuntimeError("sink exploded")

    with pytest.raises(RuntimeError, match="sink exploded"):
        await provider.run(_req(), boom)
    assert world.turns[0].closed_stream is True and world.turns[0].waiters == 0
    assert world.clients[0].closed is False  # not an SDK failure: the client stays healthy
    await provider.aclose()


async def test_emit_failure_interrupts_the_server_side_turn_and_closes_the_stream(
    world: FakeWorld,
):
    # a sink failure must not leave an orphan turn editing the worktree
    world.script_hang(prefix=lambda t, u: [turn_started(t, u), agent_delta(t, u, "m1", "x")])
    provider = await _open({"drain_s": 2.0})

    async def boom(event: AgentEvent) -> None:
        if event.kind == "text_delta":
            raise RuntimeError("sink exploded")

    with pytest.raises(RuntimeError, match="sink exploded"):
        await provider.run(_req(), boom)
    turn = world.turns[0]
    assert turn.interrupt_calls == 1  # the turn was interrupted server-side
    assert turn.closed_stream is True and turn.waiters == 0
    assert turn.aclose_calls == 1  # contextlib.aclosing: deterministic unregister, not GC timing
    assert world.clients[0].closed is False
    await provider.aclose()


async def test_value_error_from_emit_is_reraised_untouched(world: FakeWorld):
    # only pre-stream ValueErrors are request-parameter errors
    provider = await _open()

    async def reject(_event: AgentEvent) -> None:
        raise ValueError("store rejected record")

    with pytest.raises(ValueError, match="store rejected record"):
        await provider.run(_req(), reject)
    await provider.aclose()


async def test_timeout_covers_a_hung_thread_start(world: FakeWorld):
    # req.timeout_s must bound thread_start/turn, not only the stream wait
    world.start_gate = queue.Queue()
    provider = await _open({"drain_s": 0.2})
    with anyio.fail_after(3):
        result = await provider.run(_req(timeout_s=0.2), Collector())
    assert result.status == "timeout"
    assert result.error is not None and result.error.kind == "timeout"
    assert result.session_ref is None and result.raw["thread_id"] is None
    client = world.clients[0]
    assert client.closed is True  # the hung start was woken by closing the client
    assert client.start_waiters == 0  # no worker thread left blocked
    world.start_gate = None
    again = await provider.run(_req(), Collector())  # fresh client
    assert again.status == "success" and len(world.clients) == 2
    await provider.aclose()


async def test_thread_start_finishing_during_the_drain_interrupts_the_started_turn(
    world: FakeWorld,
):
    world.start_gate = queue.Queue()
    world.script_hang(prefix=lambda t, u: [turn_started(t, u)])
    # The gate is released 0.3 s in and the drain must still be open then. The drain closes at
    # deadline + drain_s, so drain_s buys the slack directly; 5 s makes it ~17x the release,
    # and it costs nothing, because the drain returns the moment the start lands.
    provider = await _open({"drain_s": 5.0})

    async def release_later() -> None:
        await anyio.sleep(0.3)
        assert world.start_gate is not None
        world.start_gate.put(None)

    results: list[Any] = []
    async with anyio.create_task_group() as tg:
        tg.start_soon(release_later)
        with anyio.fail_after(30):  # a hang net, never a deadline the drain can reach
            results.append(await provider.run(_req(timeout_s=0.1), Collector()))
    result = results[0]
    assert result.status == "timeout"
    assert world.turns, "the gate was released after the drain had already closed the client"
    turn = world.turns[0]
    assert turn.interrupt_calls == 1 and turn.closed_stream is True and turn.waiters == 0
    assert world.clients[0].closed is False
    await provider.aclose()


async def test_turn_completing_successfully_during_the_drain_is_a_success(world: FakeWorld):
    # a valid final answer must not be reported as a timeout
    world.script_hang(on_interrupt="complete_success", prefix=lambda t, u: [turn_started(t, u)])
    provider = await _open({"drain_s": 2.0})
    collector = Collector()
    result = await provider.run(_req(timeout_s=0.1), collector)
    assert result.status == "success" and result.error is None
    assert result.text == "DONE"
    assert result.raw["turn_status"] == "completed"
    assert result.raw["deadline_exceeded"] is True and result.raw["timed_out"] is True
    assert any("exceeded" in e.text for e in collector.of("warning"))
    await provider.aclose()


async def test_usage_baseline_option_makes_resumed_usage_exact_and_raw_reports_total(
    world: FakeWorld,
):
    # a carry-over usage update at turn start would be over-counted by the inference;
    # the engine can pass the stored total back via provider_options.codex.usage_baseline
    world.script(
        lambda t, u: [
            token_usage(t, u, last=breakdown(1000, 100), total=breakdown(1000, 100)),  # carry-over
            token_usage(t, u, last=breakdown(50, 5), total=breakdown(1050, 105)),
            turn_completed(t, u),
        ]
    )
    provider = await _open()
    result = await provider.run(
        _req(
            resume_session="thr-history",
            provider_options={"codex": {"usage_baseline": {"input": 1000, "output": 100}}},
        ),
        Collector(),
    )
    assert result.usage == Usage(input=50, output=5)
    assert result.raw["usage_total"] == {
        "input": 1050,
        "cached_input": 0,
        "cache_write": 0,
        "output": 105,
        "reasoning": 0,
    }
    await provider.aclose()


async def test_a_usage_baseline_above_the_server_totals_reports_no_usage_at_all(
    world: FakeWorld,
):
    """The baseline is the number a turn's usage is measured against, not a note in a ledger.

    ``usage_delta`` is field-wise ``total - baseline`` clamped at zero, so a baseline above
    anything the thread will reach reports zero tokens for every turn on it. ``CodexProvider``
    derives the turn's cost from that same figure and the engine sums it, so a resumed step
    reports zero spend however much it actually costs — which is what ``policy`` refuses under a
    spend ceiling (``tests/policy/test_provider_options.py``).
    """
    world.script(
        lambda t, u: [
            token_usage(t, u, last=breakdown(400_000, 120_000), total=breakdown(400_000, 120_000)),
            turn_completed(t, u),
        ]
    )
    provider = await _open()
    honest = await provider.run(_req(resume_session="thr-a"), Collector())
    assert honest.usage.input + honest.usage.output == 520_000

    await provider.aclose()
    provider = await _open()
    silenced = await provider.run(
        _req(
            resume_session="thr-a",
            provider_options={
                "codex": {"usage_baseline": {"input": 999_999_999, "output": 999_999_999}}
            },
        ),
        Collector(),
    )
    assert silenced.usage.input + silenced.usage.output == 0
    await provider.aclose()


async def test_usage_baseline_must_be_a_mapping_and_aclose_clears_totals(world: FakeWorld):
    provider = await _open()
    with pytest.raises(ProviderError, match="usage_baseline"):
        await provider.run(
            _req(resume_session="thr-x", provider_options={"codex": {"usage_baseline": 3}}),
            Collector(),
        )
    world.script(
        lambda t, u: [
            token_usage(t, u, last=breakdown(1, 1), total=breakdown(1, 1)),
            turn_completed(t, u),
        ]
    )
    await provider.run(_req(), Collector())
    assert provider._last_totals
    await provider.aclose()
    assert provider._last_totals == {}


@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        (McpServerSpec(name="docs", transport="http"), "needs a url"),
        (McpServerSpec(name="docs", transport="sse", url="https://d.example"), "sse"),
    ],
)
async def test_malformed_mcp_spec_is_a_provider_error(
    world: FakeWorld, spec: McpServerSpec, needle: str
):
    provider = await _open()
    with pytest.raises(ProviderError) as info:
        await provider.run(_req(mcp_servers=(spec,)), Collector())
    assert "docs" in str(info.value) and needle in str(info.value)
    await provider.aclose()


async def test_config_mcp_servers_must_be_a_mapping(world: FakeWorld):
    provider = await _open({"config": {"mcp_servers": ["gh"]}})
    with pytest.raises(ProviderError, match="mcp_servers"):
        await provider.run(
            _req(mcp_servers=(McpServerSpec(name="gh", command="gh-mcp"),)), Collector()
        )
    await provider.aclose()


async def test_unknown_effort_is_a_provider_error(world: FakeWorld):
    provider = await _open()
    with pytest.raises(ProviderError, match="effort"):
        await provider.run(_req(effort="ludicrous"), Collector())
    await provider.aclose()


async def test_open_replaces_a_small_default_executor_and_shuts_the_old_one_down(
    world: FakeWorld,
):
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    old = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(old)
    provider = await _open()
    assert provider.executor_workers >= 32
    assert old._shutdown is True
    await provider.aclose()

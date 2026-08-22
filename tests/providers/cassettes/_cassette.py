# SPDX-License-Identifier: Apache-2.0
"""The cassette file format and the two replays that drive it through the provider adapters.

Boundary: loading, validating and replaying the committed transcripts of this package (see the
package docstring). Nothing here is shipped and nothing here reimplements a provider: a cassette
is handed to the real ``ClaudeProvider`` / ``CodexProvider`` through the same seam the unit tests
use (a fake ``query`` / a fake ``AsyncCodex``), so what is asserted is the adapter's own fold.

The fake Codex client is deliberately minimal — the SDK's threading and cancellation hazards are
covered by ``tests/providers/test_codex.py``; a cassette is about message shapes. Both doubles do
bind every call they receive against the real SDK signature (:func:`bound`), so the seam is pinned
in both directions: what the SDK sends back, and what the adapter sends it.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rayspec.providers.base import AgentEvent, AgentRequest, AgentResult

CASSETTE_DIR = Path(__file__).resolve().parent

#: The provenance values a cassette may declare (see the package docstring).
CAPTURES = ("recorded", "authored")


def bound(target: Any, *args: Any, **kwargs: Any) -> inspect.BoundArguments:
    """The arguments of one recorded call, bound against the SDK's own signature.

    A cassette pins the messages an SDK sends *back*; this pins what the adapter sends *it*. A
    double that accepts anything would answer a call the SDK no longer accepts — a renamed or
    removed parameter would then surface in production instead of here. ``TypeError`` is what the
    real object would raise, so it is what the double raises.
    """
    return inspect.signature(target).bind(*args, **kwargs)


@dataclass(frozen=True)
class Cassette:
    """One committed transcript: what the SDK delivered and what the adapter must make of it."""

    path: Path
    data: Mapping[str, Any]

    @property
    def id(self) -> str:
        """``claude/auth_failure`` — the pytest id and the name in a failure message."""
        return f"{self.path.parent.name}/{self.path.stem}"

    @property
    def provider(self) -> str:
        return str(self.data["provider"])

    @property
    def request(self) -> Mapping[str, Any]:
        """The ``AgentRequest`` fields the turn was made with (prompt, model, schema…)."""
        return dict(self.data.get("request") or {})

    @property
    def transcript(self) -> list[Any]:
        """The SDK messages, in the wire shapes the SDK's own parser accepts."""
        return list(self.data["transcript"])

    @property
    def expect(self) -> Mapping[str, Any]:
        """``{"events": [...], "result": {...}}`` — the fold this cassette pins."""
        return dict(self.data["expect"])

    def __str__(self) -> str:
        return self.id


def load_cassettes(provider: str) -> list[Cassette]:
    """Every cassette of one provider, in file-name order (empty is a test failure, never a pass)."""
    found = [
        Cassette(path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted((CASSETTE_DIR / provider).glob("*.json"))
    ]
    assert found, f"no cassettes under {CASSETTE_DIR / provider}"
    return found


def agent_request(cassette: Cassette, *, cwd: str) -> AgentRequest:
    """The :class:`AgentRequest` a cassette is replayed with (``cwd`` is the test's tmp dir)."""
    fields = dict(cassette.request)
    fields.setdefault("step_path", "review")
    fields.setdefault("prompt", "Say hi")
    fields["cwd"] = cwd
    return AgentRequest(**fields)


class Collector:
    """Emit sink that keeps every event the adapter streamed."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)


def event_rows(events: list[AgentEvent]) -> list[dict[str, Any]]:
    """Events as comparable JSON rows — ``ts`` is dropped (it is the clock, not the fold)."""
    rows: list[dict[str, Any]] = []
    for event in events:
        row: dict[str, Any] = {"kind": event.kind}
        if event.text:
            row["text"] = event.text
        if event.name is not None:
            row["name"] = event.name
        if event.call_id is not None:
            row["call_id"] = event.call_id
        if event.data:
            row["data"] = json.loads(json.dumps(dict(event.data), default=str))
        if event.nested:
            row["nested"] = True
        rows.append(row)
    return rows


def result_row(result: AgentResult) -> dict[str, Any]:
    """The result as a comparable JSON row (durations and raw payloads are not pinned)."""
    row: dict[str, Any] = {
        "status": result.status,
        "text": result.text,
        "session_ref": result.session_ref,
        "model": result.model,
        "cost_usd": result.cost_usd,
        "cost_source": result.cost_source,
        "num_turns": result.num_turns,
        "usage": {
            "input": result.usage.input,
            "cached_input": result.usage.cached_input,
            "cache_write": result.usage.cache_write,
            "output": result.usage.output,
            "reasoning": result.usage.reasoning,
        },
        "structured": result.structured,
        "denials": [
            {"tool": d.tool, "reason": d.reason, "call_id": d.call_id} for d in result.denials
        ],
    }
    row["error"] = (
        None
        if result.error is None
        else {
            "kind": result.error.kind,
            "message": result.error.message,
            "transient": result.error.transient,
            "code": result.error.code,
        }
    )
    return row


# -- claude ---------------------------------------------------------------------------------------


def claude_messages(cassette: Cassette) -> list[Any]:
    """The transcript through ``claude_agent_sdk``'s own CLI parser (raises on an unknown shape)."""
    from claude_agent_sdk._internal.message_parser import parse_message

    messages = [parse_message(dict(raw)) for raw in cassette.transcript]
    assert None not in messages, f"{cassette.id}: the SDK parser dropped a message"
    return messages


class _FakeQuery:
    """Stands in for ``claude_agent_sdk.query``: yields the cassette's parsed messages."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.options: Any = None

    def __call__(self, **kwargs: Any) -> Any:
        from claude_agent_sdk import query

        self.options = bound(query, **kwargs).arguments.get("options")
        return self._gen()

    async def _gen(self) -> AsyncIterator[Any]:
        for message in self.messages:
            yield message


async def replay_claude(cassette: Cassette, *, cwd: str, monkeypatch: Any) -> tuple[Any, Collector]:
    """Drive one Claude cassette through :class:`ClaudeProvider`; returns ``(result, collector)``."""
    import rayspec.providers.claude as claude_mod
    from rayspec.providers.claude import ClaudeProvider

    monkeypatch.setattr(claude_mod, "query", _FakeQuery(claude_messages(cassette)))
    provider = ClaudeProvider({})
    collector = Collector()
    result = await provider.run(agent_request(cassette, cwd=cwd), collector)
    return result, collector


# -- codex ----------------------------------------------------------------------------------------


def codex_notifications(cassette: Cassette) -> list[Any]:
    """The transcript as ``Notification`` objects, coerced exactly as the SDK client coerces them.

    The SDK falls back to ``UnknownNotification`` for a payload its models no longer accept — in
    production that turns a tool call or a usage update into an opaque ``raw`` event without
    anybody noticing, so a cassette insists on the typed payload.
    """
    from openai_codex.generated.notification_registry import NOTIFICATION_MODELS
    from openai_codex.models import Notification, UnknownNotification

    notifications: list[Any] = []
    for raw in cassette.transcript:
        method, params = str(raw["method"]), dict(raw["params"])
        model = NOTIFICATION_MODELS.get(method)
        assert model is not None, f"{cassette.id}: the SDK no longer knows {method!r}"
        payload = cast(Any, model.model_validate(params))
        assert not isinstance(payload, UnknownNotification), (
            f"{cassette.id}: the SDK no longer accepts the payload of {method!r}"
        )
        notifications.append(Notification(method=method, payload=payload))
    return notifications


class _FakeTurn:
    """Stands in for ``AsyncTurnHandle``: streams the cassette's notifications, then stops."""

    def __init__(self, notifications: list[Any], turn_id: str) -> None:
        self.notifications = notifications
        self.id = turn_id
        self.interrupts = 0

    async def interrupt(self) -> None:
        self.interrupts += 1

    def stream(self) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        for notification in self.notifications:
            yield notification


class _FakeThread:
    """Stands in for ``AsyncThread``: every call is bound against the SDK's signature first."""

    def __init__(self, client: _FakeCodex, thread_id: str) -> None:
        self.client = client
        self.id = thread_id

    async def turn(self, *args: Any, **kwargs: Any) -> _FakeTurn:
        from openai_codex import AsyncThread

        call = bound(AsyncThread.turn, self, *args, **kwargs)
        self.client.turn_inputs.append(call.arguments["input"])
        return _FakeTurn(self.client.notifications, self.client.turn_id)


class _FakeCodex:
    """Stands in for ``AsyncCodex``: one thread, one turn, the cassette's notifications."""

    def __init__(self, notifications: list[Any], thread_id: str, turn_id: str) -> None:
        self.notifications = notifications
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.turn_inputs: list[Any] = []
        self.closed = False

    async def thread_start(self, *args: Any, **kwargs: Any) -> _FakeThread:
        from openai_codex import AsyncCodex

        bound(AsyncCodex.thread_start, self, *args, **kwargs)
        return _FakeThread(self, self.thread_id)

    async def thread_resume(self, *args: Any, **kwargs: Any) -> _FakeThread:
        from openai_codex import AsyncCodex

        call = bound(AsyncCodex.thread_resume, self, *args, **kwargs)
        return _FakeThread(self, str(call.arguments["thread_id"]))

    async def close(self) -> None:
        self.closed = True


def cassette_ids(cassette: Cassette) -> tuple[str, str]:
    """The ``(thread id, turn id)`` the transcript belongs to — every notification carries them."""
    thread_id = turn_id = ""
    for raw in cassette.transcript:
        params = dict(raw["params"])
        thread_id = thread_id or str(params.get("threadId") or "")
        turn = params.get("turn")
        if isinstance(turn, dict):
            turn_id = turn_id or str(turn.get("id") or "")
        turn_id = turn_id or str(params.get("turnId") or "")
    assert thread_id and turn_id, f"{cassette.id}: no thread/turn id in the transcript"
    return thread_id, turn_id


async def replay_codex(cassette: Cassette, *, cwd: str, monkeypatch: Any) -> tuple[Any, Collector]:
    """Drive one Codex cassette through :class:`CodexProvider`; returns ``(result, collector)``."""
    import rayspec.providers.codex as codex_mod
    from rayspec.providers.codex import CodexProvider

    notifications = codex_notifications(cassette)
    thread_id, turn_id = cassette_ids(cassette)
    monkeypatch.setattr(
        codex_mod, "AsyncCodex", lambda config=None: _FakeCodex(notifications, thread_id, turn_id)
    )
    provider = CodexProvider({})
    await provider.open(run_id="cassette", workdir=cwd, env={}, max_parallel=1)
    collector = Collector()
    try:
        result = await provider.run(agent_request(cassette, cwd=cwd), collector)
    finally:
        await provider.aclose()
    return result, collector


def all_cassettes() -> Iterator[Cassette]:
    """Every cassette of every provider (the hygiene scan walks these)."""
    for directory in sorted(p for p in CASSETTE_DIR.iterdir() if p.is_dir()):
        yield from (
            Cassette(path, json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(directory.glob("*.json"))
        )

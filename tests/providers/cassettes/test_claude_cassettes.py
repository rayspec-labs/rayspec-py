# SPDX-License-Identifier: Apache-2.0
"""Claude cassettes: the committed transcripts still fold into the events rayspec promises.

Boundary: replay only (see ``_cassette.py``). These tests fail when ``claude-agent-sdk`` changes
the shape of a CLI message, when its parser stops accepting one, or when the adapter's fold
changes — which is the point: all three are invisible to the unit tests, because those build the
SDK objects themselves instead of parsing what the CLI writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._cassette import (
    Cassette,
    _FakeQuery,
    event_rows,
    load_cassettes,
    replay_claude,
    result_row,
)

pytestmark = pytest.mark.anyio

CASSETTES = load_cassettes("claude")


def cassette(name: str) -> Cassette:
    """The one cassette with this file name (a rename must break the test, not skip it)."""
    return next(c for c in CASSETTES if c.path.stem == name)


@pytest.mark.parametrize("tape", CASSETTES, ids=str)
async def test_cassette_replays_into_the_recorded_events_and_result(
    tape: Cassette, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, collector = await replay_claude(tape, cwd=str(tmp_path), monkeypatch=monkeypatch)
    assert event_rows(collector.events) == list(tape.expect["events"]), tape.id
    assert result_row(result) == dict(tape.expect["result"]), tape.id


async def test_an_authentication_failure_is_fatal_not_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 must never look retryable: the engine would spend the whole retry budget on it."""
    result, collector = await replay_claude(
        cassette("auth_failure"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    assert result.status == "error"
    assert result.error is not None
    assert (result.error.kind, result.error.transient, result.error.code) == ("auth", False, 401)
    assert "401" in result.error.message
    assert [e.kind for e in collector.events if e.kind == "error"] == ["error"]


async def test_a_tool_call_and_its_result_are_paired_by_call_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool result carries the NAME of the call it answers — the CLI only sends the id."""
    result, collector = await replay_claude(
        cassette("tool_use_turn"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    calls = [e for e in collector.events if e.kind == "tool_call"]
    results = [e for e in collector.events if e.kind == "tool_result"]
    assert [(e.name, e.call_id) for e in calls] == [("Bash", "toolu_cassette_1")]
    assert [(e.name, e.call_id) for e in results] == [("Bash", "toolu_cassette_1")]
    assert result.structured == {"verdict": "approve", "summary": "All three tests pass."}


async def test_streamed_text_is_not_emitted_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text that arrived as deltas must not be repeated when its assistant message lands."""
    _, collector = await replay_claude(
        cassette("tool_use_turn"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    deltas = "".join(e.text for e in collector.events if e.kind == "text_delta")
    texts = [e.text for e in collector.events if e.kind == "text"]
    assert deltas == "Running the tests."
    assert texts == ["All three tests pass."], "the streamed message must not be repeated"


async def test_a_refused_tool_call_reaches_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denial is what the step needs to know; the refused input is deliberately not kept."""
    result, collector = await replay_claude(
        cassette("tool_use_turn"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    assert [(d.tool, d.call_id) for d in result.denials] == [("Write", "toolu_cassette_2")]
    denials = [e for e in collector.events if e.text.startswith("permission denied")]
    assert [(e.kind, e.name) for e in denials] == [("warning", "Write")]


async def test_usage_counts_cache_reads_and_writes_as_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Usage.input`` includes cached and cache-write tokens — pricing depends on it.

    Both halves are asserted where each is visible: the turn's first message is the one that
    writes to the cache, the final one only reads from it. Pinning the result alone would leave
    the cache-WRITE half of the claim untested.
    """
    result, collector = await replay_claude(
        cassette("tool_use_turn"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    written = next(e for e in collector.events if e.kind == "usage").data["usage"]
    assert written["input"] == 11 + 9_500 + 480
    assert (written["cached_input"], written["cache_write"]) == (9_500, 480)
    assert result.usage.input == 14 + 9_980
    assert (result.usage.cached_input, result.usage.cache_write) == (9_980, 0)
    assert (result.cost_usd, result.cost_source) == (0.0416, "provider")


async def test_the_double_refuses_a_call_the_sdk_itself_would_refuse() -> None:
    """The request side of the seam is pinned too: the call is bound against the real signature."""
    fake = _FakeQuery([])
    with pytest.raises(TypeError):
        fake(prompt="hi", no_such_option=1)
    assert [message async for message in fake(prompt="hi", options=None)] == []

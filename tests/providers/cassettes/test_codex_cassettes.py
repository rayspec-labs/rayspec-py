# SPDX-License-Identifier: Apache-2.0
"""Codex cassettes: the committed app-server transcripts still fold into the promised events.

Boundary: replay only (see ``_cassette.py``). The sharpest of these is not an assertion at all:
``codex_notifications`` refuses a payload the SDK's generated models no longer accept. In
production that payload would silently become an ``UnknownNotification`` and a tool call or a
usage update would turn into an opaque ``raw`` event — a run that looks fine and reports nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._cassette import (
    Cassette,
    _FakeCodex,
    event_rows,
    load_cassettes,
    replay_codex,
    result_row,
)

pytestmark = pytest.mark.anyio

CASSETTES = load_cassettes("codex")


def cassette(name: str) -> Cassette:
    """The one cassette with this file name (a rename must break the test, not skip it)."""
    return next(c for c in CASSETTES if c.path.stem == name)


@pytest.mark.parametrize("tape", CASSETTES, ids=str)
async def test_cassette_replays_into_the_recorded_events_and_result(
    tape: Cassette, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, collector = await replay_codex(tape, cwd=str(tmp_path), monkeypatch=monkeypatch)
    assert event_rows(collector.events) == list(tape.expect["events"]), tape.id
    assert result_row(result) == dict(tape.expect["result"]), tape.id


async def test_a_retrying_error_is_a_warning_and_the_final_one_fails_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``willRetry`` decides: the app-server is still trying, so the step is not failing yet."""
    result, collector = await replay_codex(
        cassette("turn_failure"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    warnings = [e for e in collector.events if e.kind == "warning"]
    assert warnings and all(e.data["will_retry"] for e in warnings)
    assert [e.data["will_retry"] for e in collector.events if e.kind == "error"] == [False]
    assert result.status == "error"
    assert result.error is not None and "401" in result.error.message


async def test_a_command_execution_becomes_a_start_an_output_and_an_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command the agent ran is start → output → end, all three tied to the same item id."""
    _, collector = await replay_codex(
        cassette("tool_use_turn"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    command = [
        e for e in collector.events if e.kind in {"command_start", "command_output", "command_end"}
    ]
    assert [e.kind for e in command] == ["command_start", "command_output", "command_end"]
    assert {e.call_id for e in command} == {"item-2"}
    assert command[1].text == "3 passed in 0.10s\n"
    assert (command[2].data["exit_code"], command[2].data["status"]) == (0, "completed")


async def test_an_item_type_rayspec_does_not_model_is_kept_as_a_raw_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user message the app-server echoes has no neutral shape — it is kept, never dropped."""
    _, collector = await replay_codex(
        cassette("tool_use_turn"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    raw = [e for e in collector.events if e.kind == "raw"]
    assert [e.name for e in raw] == ["userMessage", "userMessage"]
    assert [e.data["completed"] for e in raw] == [False, True]


async def test_the_final_answer_wins_over_the_streamed_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``final_answer`` is the step output; the deltas are what the console showed on the way."""
    result, collector = await replay_codex(
        cassette("tool_use_turn"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    assert "".join(e.text for e in collector.events if e.kind == "text_delta") == result.text
    assert result.text == "All three tests pass."
    assert result.status == "success"


async def test_the_turn_reports_the_delta_of_the_thread_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex reports a cumulative thread total; a turn bills the difference, never the total."""
    result, _ = await replay_codex(
        cassette("tool_use_turn"), cwd=str(tmp_path), monkeypatch=monkeypatch
    )
    assert result.usage.input == 1_200
    assert (result.usage.output, result.usage.reasoning) == (260, 128)
    assert result.session_ref == "thr-cassette-2"


async def test_the_double_refuses_a_call_the_sdk_itself_would_refuse() -> None:
    """A cassette pins what the SDK sends back; the double pins what the adapter sends it.

    A parameter the SDK renamed or removed is invisible to a stand-in that accepts anything, so
    every recorded call is bound against the real signature before it is answered.
    """
    fake = _FakeCodex([], "thr-x", "turn-x")
    with pytest.raises(TypeError):
        await fake.thread_start(no_such_option=1)
    with pytest.raises(TypeError):
        await fake.thread_resume("thr-x", no_such_option=1)
    thread = await fake.thread_start(cwd="/workspace")
    with pytest.raises(TypeError):
        await thread.turn("hi", no_such_option=1)
    handle = await thread.turn("hi", model="gpt-5.4", effort="medium", output_schema=None)
    assert handle.id == "turn-x"

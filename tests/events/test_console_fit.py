"""ConsoleSink: height budget, children cap, robustness (markup, ANSI, malformed data, Live)."""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest
from rich.console import Console

from rayspec.events import EventType, RunEvent, StreamRecord
from rayspec.events.sinks import ConsoleSink, QuietConsoleSink
from rayspec.events.sinks import console as console_mod
from rayspec.events.sinks.console import (
    DEFAULT_MAX_CHILDREN,
    RunView,
    StepView,
    render_view,
)

pytestmark = pytest.mark.anyio

RUN = "20260820-101500-ab2c"


def ev(type_: EventType, step_path: str | None = None, **data) -> RunEvent:
    return RunEvent(type=type_, run_id=RUN, step_path=step_path, data=data)


def rec(kind: str, text: str = "", **kw: Any) -> StreamRecord:
    return StreamRecord(kind=kind, text=text, **kw)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def plain_console(height: int | None = None) -> Console:
    return Console(
        record=True,
        width=120,
        height=height,
        force_terminal=False,
        color_system=None,
        soft_wrap=True,
    )


def tty_console(height: int | None = None) -> Console:
    return Console(
        file=io.StringIO(),
        record=True,
        width=120,
        height=height,
        force_terminal=True,
        color_system=None,
        soft_wrap=True,
    )


def make_sink(console: Console, **kw: Any) -> tuple[ConsoleSink, FakeClock]:
    clock = FakeClock()
    sink = ConsoleSink(console, clock=clock, live=True, display=False, **kw)
    return sink, clock


def render_text(console: Console, renderable: Any) -> str:
    console.print(renderable)
    return console.export_text()


async def start_each(sink: ConsoleSink, total: int, running: int, *, tails: int = 0) -> None:
    """An ``each`` with ``total`` items; the last ``running`` items are still running."""
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "fan", kind="each", attempt=1))
    for i in range(total):
        await sink.emit(ev(EventType.EACH_ITEM, "fan", index=i, total=total))
        await sink.emit(ev(EventType.STEP_STARTED, f"fan[{i}]/work", kind="prompt", attempt=1))
        if i < total - running:
            await sink.emit(
                ev(EventType.STEP_FINISHED, f"fan[{i}]/work", status="succeeded", duration_ms=1)
            )
        else:
            for n in range(tails):
                await sink.emit_stream(f"fan[{i}]/work", rec("text_delta", f"item {i} line {n}\n"))


# -- children cap / collapse -------------------------------------------------------------------


async def test_children_cap_shows_running_plus_last_n_and_more_line():
    console = plain_console()
    sink, _ = make_sink(console)
    await start_each(sink, 200, 8)
    out = render_text(console, sink.render())
    lines = out.splitlines()
    assert len(lines) < 40
    hidden = 200 - 8 - DEFAULT_MAX_CHILDREN
    assert f"… +{hidden} more" in out
    for i in range(192, 200):
        assert f"→ item {i}/200" in out
    assert "item 0/200" not in out and f"item {191 - DEFAULT_MAX_CHILDREN}/200" not in out
    assert f"✓ item {192 - DEFAULT_MAX_CHILDREN}/200" in out  # last N finished stay visible


async def test_children_cap_keeps_failed_and_tolerated_items_visible():
    console = plain_console()
    sink, _ = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "fan", kind="each", attempt=1))
    for i in range(40):
        await sink.emit(ev(EventType.EACH_ITEM, "fan", index=i, total=40))
        await sink.emit(ev(EventType.STEP_STARTED, f"fan[{i}]/work", kind="shell", attempt=1))
        status = "failed" if i == 3 else "succeeded"
        await sink.emit(
            ev(
                EventType.STEP_FINISHED,
                f"fan[{i}]/work",
                status=status,
                duration_ms=1,
                tolerated=i == 3,
                error={"type": "ShellError", "message": "exit 1"} if i == 3 else None,
            )
        )
    out = render_text(console, sink.render())
    assert "✓ item 3/40" in out and "(tolerated)" in out
    assert "✗ work (shell)" in out
    assert "… +3 more" in out and "… +28 more" in out  # items 0-2 and 4-31 hidden


async def test_finished_clean_composites_collapse_to_one_line():
    console = plain_console()
    sink, clock = make_sink(console)
    await start_each(sink, 200, 0)
    clock.advance(1)
    await sink.emit(ev(EventType.STEP_FINISHED, "fan", status="succeeded", duration_ms=1000))
    await sink.emit(ev(EventType.STEP_STARTED, "review", kind="include", attempt=1))
    await sink.emit(ev(EventType.STEP_STARTED, "review/lint", kind="shell", attempt=1))
    await sink.emit(ev(EventType.STEP_FINISHED, "review/lint", status="succeeded", duration_ms=1))
    await sink.emit(ev(EventType.STEP_FINISHED, "review", status="succeeded", duration_ms=1))
    out = render_text(console, sink.render())
    assert out == (
        "▶ wf · 20260820-101500-ab2c · running 1.0s\n"
        "├── ✓ fan (each) 1.0s\n"
        "└── ✓ review (include) 1ms\n"
    )


async def test_finished_composite_with_a_problem_inside_stays_expanded():
    console = plain_console()
    sink, _ = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "review", kind="include", attempt=1))
    await sink.emit(ev(EventType.STEP_STARTED, "review/lint", kind="shell", attempt=1))
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "review/lint",
            status="failed",
            duration_ms=1,
            tolerated=True,
            error={"type": "ShellError", "message": "exit 1"},
        )
    )
    await sink.emit(ev(EventType.STEP_FINISHED, "review", status="succeeded", duration_ms=1))
    out = render_text(console, sink.render())
    assert "✗ lint (shell) 1ms (tolerated) — ShellError: exit 1" in out


# -- height budget ---------------------------------------------------------------------------------


async def test_height_budget_shrinks_tails_and_keeps_footer():
    console = plain_console()
    sink, _ = make_sink(console)
    await start_each(sink, 4, 4, tails=6)
    await sink.emit(ev(EventType.STEP_STARTED, "gate", kind="approve", attempt=1))
    await sink.emit(ev(EventType.RUN_PAUSED, "gate", token="gate#1", step="gate", message="ok?"))
    full = render_text(plain_console(), sink.render())
    assert len(full.splitlines()) > 24
    out = render_text(console, sink.render(height=24))
    lines = out.splitlines()
    assert len(lines) <= 24
    assert lines[-1].startswith("‖ paused at gate — ok?")
    # every running step keeps the most recent tail lines
    for i in range(4):
        assert f"item {i} line 5" in out
        assert f"item {i} line 0" not in out


async def test_height_budget_crops_from_the_top_as_a_last_resort():
    console = plain_console()
    sink, _ = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    for i in range(10):
        await sink.emit(ev(EventType.STEP_STARTED, f"s{i}", kind="shell", attempt=1))
    await sink.emit(ev(EventType.RUN_PAUSED, "s9", token="t", step="s9", message="ok?"))
    await sink.emit(ev(EventType.WARNING, message="careful"))
    out = render_text(console, sink.render(height=8))
    lines = out.splitlines()
    assert len(lines) <= 8
    assert lines[0].startswith("▶ wf")
    assert "…" in lines[1]
    assert "‖ s9 (shell)" in out and "s0 (shell)" not in out
    assert lines[-2] == "! warning: careful"
    assert lines[-1].startswith("‖ paused at s9 — ok?")


async def test_render_view_without_height_is_unbounded():
    view = RunView()
    view.apply(ev(EventType.RUN_STARTED, workflow="wf"))
    for i in range(30):
        view.apply(ev(EventType.STEP_STARTED, f"s{i}", kind="shell", attempt=1))
    out = render_text(plain_console(), render_view(view))
    assert len(out.splitlines()) == 31


async def test_live_frame_in_a_24_row_tty_shows_pause_and_tails():
    console = tty_console(height=24)
    sink = ConsoleSink(console, clock=FakeClock())
    await start_each(sink, 4, 4, tails=6)
    await sink.emit(ev(EventType.STEP_STARTED, "gate", kind="approve", attempt=1))
    await sink.emit(ev(EventType.RUN_PAUSED, "gate", token="gate#1", step="gate", message="ok?"))
    assert sink.is_live and sink._live is not None
    sink._live.refresh()
    frame = console.file.getvalue()  # type: ignore[attr-defined]
    assert "paused at gate" in frame
    assert "item 3 line 5" in frame
    assert "\n...\n" not in frame  # Rich's overflow ellipsis never needed
    await sink.aclose()


# -- summary robustness ------------------------------------------------------------------------


async def test_summary_shows_markup_like_text_verbatim():
    console = plain_console()
    sink, _ = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.WORKSPACE_CREATED, workdir="/wt/[red]x", branch="b[/]"))
    await sink.emit(
        ev(
            EventType.RUN_FINISHED,
            status="succeeded",
            reason="done [i]really[/i]",
            outputs={"note": "[/]", "x": "[bold]y[/bold]", "u": {"k": "日本"}},
        )
    )
    out = render_text(console, sink.render_summary())
    assert "[/]" in out and "[bold]y[/bold]" in out
    assert "/wt/[red]x (branch b[/])" in out
    assert "done [i]really[/i]" in out
    assert '{"k": "日本"}' in out


# -- never raises ------------------------------------------------------------------------------


def _malformed_events() -> list[RunEvent]:
    return [
        ev(EventType.RUN_STARTED, workflow="wf"),
        ev(EventType.STEP_STARTED, "a", kind="prompt", attempt="x"),
        ev(EventType.STEP_RETRY, "a", attempt="y", delay_s="soon", error=42),
        ev(EventType.LOOP_ITERATION, "l", n="two", max="many"),
        ev(EventType.EACH_ITEM, "e", index="0", total="lots"),
        ev(
            EventType.STEP_FINISHED,
            "a",
            status="succeeded",
            duration_ms="fast",
            usage="lots",
            cost_usd="cheap",
        ),
        ev(EventType.STEP_FINISHED, "b", status=None, duration_ms=None, error={"message": "m"}),
        ev(
            EventType.RUN_FINISHED,
            status="succeeded",
            reason=42,
            usage=3,
            cost_usd="x",
            outputs=[1, 2],
        ),
    ]


async def test_malformed_event_data_never_raises_from_tree_sink(caplog):
    console = plain_console()
    sink, _ = make_sink(console)
    with caplog.at_level(logging.WARNING, logger="rayspec.events"):
        for event in _malformed_events():
            await sink.emit(event)
    console.print(sink.render())
    console.print(sink.render_summary())
    out = console.export_text()
    assert "✓ a (prompt)" in out
    assert "item 0" in out
    assert "│ 42" in out  # non-string reason shown in the summary panel


async def test_malformed_event_data_never_raises_from_quiet_sink():
    console = plain_console()
    sink = QuietConsoleSink(console, show_started=True)
    for event in _malformed_events():
        await sink.emit(event)
    out = console.export_text()
    assert "run 20260820-101500-ab2c succeeded" in out


async def test_apply_errors_are_logged_once_and_do_not_raise(caplog, monkeypatch):
    console = plain_console()
    sink, _ = make_sink(console)

    def boom(self: RunView, event: RunEvent) -> None:
        raise RuntimeError("model exploded")

    monkeypatch.setattr(RunView, "apply", boom)
    with caplog.at_level(logging.WARNING, logger="rayspec.events"):
        await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
        await sink.emit(ev(EventType.STEP_STARTED, "a", kind="shell", attempt=1))
    assert sum("model exploded" in r.getMessage() for r in caplog.records) == 1


# -- ANSI / control characters in tails ------------------------------------------------------


async def test_ansi_escapes_are_stripped_from_tails():
    sink, _ = make_sink(plain_console())
    await sink.emit(ev(EventType.STEP_STARTED, "s", kind="shell", attempt=1))
    await sink.emit_stream("s", rec("stdout", "\x1b[31mFAILED\x1b[0m tests/x.py\n"))
    await sink.emit_stream("s", rec("text_delta", "\x1b[1mbold\x1b"))
    await sink.emit_stream("s", rec("text_delta", "[0m rest\n"))
    await sink.emit_stream("s", rec("warning", "\x1b]0;title\x07multi\nline"))
    step = sink.view.get("s")
    assert step is not None
    assert step.tail_lines() == ["FAILED tests/x.py", "bold rest", "! multi line"]
    out = render_text(plain_console(), sink.render())
    assert "\x1b" not in out


async def test_multiline_error_and_pause_messages_render_on_one_line():
    console = plain_console()
    sink, _ = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="shell", attempt=1))
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "a",
            status="failed",
            duration_ms=1,
            error={"type": "ShellError", "message": "line one\nline two"},
        )
    )
    await sink.emit(ev(EventType.STEP_STARTED, "g", kind="approve", attempt=1))
    await sink.emit(ev(EventType.RUN_PAUSED, "g", token="t", step="g", message="why?\nbecause"))
    await sink.emit(ev(EventType.WARNING, message="w1\nw2"))
    out = render_text(console, sink.render())
    assert len(out.splitlines()) == 6
    assert "ShellError: line one line two" in out


# -- Live failure / resume -----------------------------------------------------------------------


async def test_live_start_failure_degrades_to_quiet_lines(monkeypatch, caplog):
    class BrokenLive:
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise RuntimeError("no live for you")

    monkeypatch.setattr(console_mod, "Live", BrokenLive)
    console = tty_console()
    sink = ConsoleSink(console)
    with caplog.at_level(logging.WARNING, logger="rayspec.events"):
        await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
        await sink.emit(ev(EventType.STEP_STARTED, "a", kind="shell", attempt=1))
        await sink.emit(
            ev(EventType.STEP_FINISHED, "a", status="failed", duration_ms=5, error="boom")
        )
        await sink.emit(ev(EventType.RUN_FINISHED, status="failed", reason="a failed"))
    assert sink.is_live is False and sink.tree_enabled is False
    out = console.export_text()
    assert "▶ run 20260820-101500-ab2c started (wf)" in out
    assert "✗ a failed 5ms — boom" in out
    assert "■ run 20260820-101500-ab2c failed — a failed" in out
    assert sum("cannot start the live display" in r.getMessage() for r in caplog.records) == 1
    await sink.aclose()


async def test_resume_starts_live_when_events_arrived_while_paused():
    sink = ConsoleSink(tty_console())
    await sink.pause()
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="shell", attempt=1))
    assert sink.is_live is False
    await sink.resume()
    assert sink.is_live is True
    await sink.aclose()
    # still nothing when no event arrived at all
    sink2 = ConsoleSink(tty_console())
    await sink2.pause()
    await sink2.resume()
    assert sink2.is_live is False
    await sink2.aclose()


# -- nits ------------------------------------------------------------------------------------------


def test_views_compare_by_identity():
    a = StepView(path="a", name="a")
    b = StepView(path="a", name="a")
    child = StepView(path="a/c", name="c", parent=a)
    a.children.append(child)
    b.children.append(StepView(path="a/c", name="c", parent=b))
    assert a != b
    assert a == a
    assert child in a.children and child not in b.children
    assert RunView() != RunView()


async def test_tail_lines_zero_means_no_tail():
    sink, _ = make_sink(plain_console(), tail_lines=0)
    assert sink.tail_lines == 0
    await sink.emit(ev(EventType.STEP_STARTED, "s", kind="shell", attempt=1))
    await sink.emit_stream("s", rec("stdout", "x\n"))
    await sink.emit_stream("s", rec("text_delta", "partial"))
    step = sink.view.get("s")
    assert step is not None and step.tail_lines() == []

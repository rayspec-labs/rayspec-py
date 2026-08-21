"""Console sinks: escape neutralisation, cost markers on the run line/footer and
provider warnings surfaced on the console."""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console

from rayspec.events import EventType, RunEvent, StreamRecord
from rayspec.events.sinks import ConsoleSink, QuietConsoleSink

pytestmark = pytest.mark.anyio

RUN = "20260820-101500-ab2c"
ESC = "\x1b"
NASTY = f"before{ESC}]0;PWNED{ESC}\\{ESC}[31mRED{ESC}[0m [bold red]MARKUP[/] {ESC}[2J after"
CLEAN = "beforeRED [bold red]MARKUP[/]  after"


def ev(type_: EventType, step_path: str | None = None, **data: Any) -> RunEvent:
    return RunEvent(type=type_, run_id=RUN, step_path=step_path, data=data)


def plain_console() -> Console:
    return Console(record=True, width=160, force_terminal=False, color_system=None, soft_wrap=True)


def tty_console() -> Console:
    return Console(
        file=io.StringIO(),
        record=True,
        width=160,
        force_terminal=True,
        color_system=None,
        soft_wrap=True,
    )


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def tree_sink(console: Console, **kw: Any) -> ConsoleSink:
    return ConsoleSink(console, clock=Clock(), live=True, display=False, **kw)


# -- escapes -------------------------------------------------------------------------------


async def test_quiet_lines_neutralise_escapes_everywhere():
    console = plain_console()
    sink = QuietConsoleSink(console, show_started=True)
    await sink.emit(ev(EventType.RUN_STARTED, workflow=NASTY))
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind=NASTY))
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "a",
            status="failed",
            error={"type": "exit", "message": NASTY},
        )
    )
    await sink.emit(ev(EventType.STEP_FINISHED, "b", status="skipped", skip_reason=NASTY))
    await sink.emit(ev(EventType.STEP_RETRY, "c", attempt=2, error=NASTY))
    await sink.emit(ev(EventType.WARNING, "c", message=NASTY))
    await sink.emit(ev(EventType.RUN_PAUSED, "gate", step="gate", message=NASTY))
    await sink.emit(ev(EventType.RUN_DECISION, approved=True, comment=NASTY))
    await sink.emit(ev(EventType.WORKSPACE_CREATED, workdir=NASTY, branch=NASTY))
    await sink.emit(ev(EventType.RUN_FINISHED, status="failed", reason=NASTY))
    out = console.export_text()
    assert ESC not in out and "\x07" not in out
    assert out.count(CLEAN) == 11, out


async def test_tree_and_summary_neutralise_escapes():
    console = tty_console()
    sink = tree_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow=NASTY))
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="shell"))
    await sink.emit_stream("a", StreamRecord(kind="stdout", text=NASTY + "\n"))
    await sink.emit_stream("a", StreamRecord(kind="tool_call", name=NASTY, data={"path": NASTY}))
    await sink.emit(ev(EventType.WARNING, "a", message=NASTY))
    await sink.emit(ev(EventType.RUN_PAUSED, "a", step="a", message=NASTY))
    console.print(sink.render())
    mid = console.export_text(clear=True)
    assert ESC not in mid and CLEAN in mid
    await sink.emit(ev(EventType.RUN_DECISION, approved=True, comment=NASTY))
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "a",
            status="failed",
            error={"type": "exit", "message": NASTY},
            skip_reason=None,
        )
    )
    await sink.emit(
        ev(
            EventType.RUN_FINISHED,
            status="failed",
            reason=NASTY,
            outputs={NASTY: NASTY, "json": {"k": NASTY}},
        )
    )
    console.print(sink.render())
    console.print(sink.render_summary())
    out = console.export_text()
    assert ESC not in out and "\x07" not in out
    assert CLEAN in out


# -- cost markers -----------------------------------------------------------------------


def _finished_step(path: str, *, cost: float | None, source: str | None, tokens: int) -> RunEvent:
    data: dict[str, Any] = {
        "status": "succeeded",
        "duration_ms": 1000,
        "usage": {"input": tokens, "output": 0},
        "cost_usd": cost,
    }
    if source is not None:
        data["cost_source"] = source
    return ev(EventType.STEP_FINISHED, path, **data)


async def test_quiet_run_line_uses_the_table_marker():
    console = plain_console()
    sink = QuietConsoleSink(console)
    await sink.emit(_finished_step("review", cost=0.04, source="table", tokens=32_900))
    # the engine reports cost_source on run.finished …
    await sink.emit(
        ev(
            EventType.RUN_FINISHED,
            status="succeeded",
            usage={"input": 32_900, "output": 0},
            cost_usd=0.04,
            cost_source="table",
        )
    )
    out = console.export_text(clear=True)
    assert "■ run" in out and "~$0.04" in out
    # … and an older engine without it still gets the marker from the step lines seen
    sink = QuietConsoleSink(console)
    await sink.emit(_finished_step("review", cost=0.04, source="table", tokens=32_900))
    await sink.emit(
        ev(EventType.RUN_FINISHED, status="succeeded", usage={"input": 32_900}, cost_usd=0.04)
    )
    assert "~$0.04" in console.export_text()


async def test_quiet_partial_cost_is_marked_as_lower_bound():
    console = plain_console()
    sink = QuietConsoleSink(console)
    await sink.emit(_finished_step("implement", cost=None, source=None, tokens=50_200))
    await sink.emit(_finished_step("review", cost=0.02, source="provider", tokens=19_900))
    await sink.emit(
        ev(
            EventType.RUN_FINISHED,
            status="succeeded",
            usage={"input": 70_100},
            cost_usd=0.02,
            cost_source="partial",
        )
    )
    out = console.export_text(clear=True)
    assert "≥$0.02" in out
    sink = QuietConsoleSink(console)
    await sink.emit(_finished_step("implement", cost=None, source=None, tokens=50_200))
    await sink.emit(_finished_step("review", cost=0.02, source="provider", tokens=19_900))
    await sink.emit(
        ev(EventType.RUN_FINISHED, status="succeeded", usage={"input": 70_100}, cost_usd=0.02)
    )
    assert "≥$0.02" in console.export_text()


async def test_tree_run_line_and_summary_use_the_same_marker():
    console = tty_console()
    sink = tree_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="review_codex"))
    await sink.emit(ev(EventType.STEP_STARTED, "review", kind="prompt"))
    await sink.emit(_finished_step("review", cost=0.04, source="table", tokens=32_900))
    console.print(sink.render())
    running = console.export_text(clear=True)
    assert "~$0.04" in running  # the live run line while running
    await sink.emit(
        ev(EventType.RUN_FINISHED, status="succeeded", usage={"input": 32_900}, cost_usd=0.04)
    )
    console.print(sink.render())
    console.print(sink.render_summary())
    out = console.export_text()
    assert out.count("~$0.04") >= 3, out  # step line, run line, totals
    assert "$0.04" not in out.replace("~$0.04", "")


async def test_tree_partial_cost_marker():
    console = tty_console()
    sink = tree_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="fix_loop"))
    await sink.emit(_finished_step("implement", cost=None, source=None, tokens=50_200))
    await sink.emit(_finished_step("review", cost=0.02, source="provider", tokens=19_900))
    await sink.emit(
        ev(EventType.RUN_FINISHED, status="succeeded", usage={"input": 70_100}, cost_usd=0.02)
    )
    console.print(sink.render())
    console.print(sink.render_summary())
    out = console.export_text()
    assert out.count("≥$0.02") >= 2, out  # run line + totals; the review step line keeps $0.02


# -- provider warnings ---------------------------------------------------------------------


async def test_quiet_sink_prints_stream_warnings():
    console = plain_console()
    sink = QuietConsoleSink(console)
    await sink.emit(ev(EventType.STEP_STARTED, "review", kind="prompt"))
    await sink.emit_stream(
        "review",
        StreamRecord(kind="warning", text=f"rate limit allowed_warning utilization 98%{ESC}[2J"),
    )
    await sink.emit_stream("review", StreamRecord(kind="text_delta", text="hello"))
    await sink.emit_stream("review", StreamRecord(kind="error", text="boom"))
    out = console.export_text()
    assert "⚠ review: rate limit allowed_warning utilization 98%" in out
    assert ESC not in out
    assert "hello" not in out and "boom" not in out  # only warnings break the quiet


async def test_tree_sink_keeps_stream_warnings_in_the_footer():
    console = tty_console()
    sink = tree_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="example"))
    await sink.emit(ev(EventType.STEP_STARTED, "review", kind="prompt"))
    await sink.emit_stream(
        "review", StreamRecord(kind="warning", text="rate limit allowed_warning utilization 98%")
    )
    await sink.emit(_finished_step("review", cost=0.03, source="provider", tokens=45_700))
    console.print(sink.render())
    out = console.export_text()
    assert "review: rate limit allowed_warning utilization 98%" in out
    assert sink.view.warnings == ["review: rate limit allowed_warning utilization 98%"]

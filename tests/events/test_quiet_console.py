"""QuietConsoleSink: one plain line per step/run event (the non-TTY default renderer)."""

from __future__ import annotations

import logging

import pytest
from rich.console import Console
from rich.text import Text

from rayspec.events import EventSink, EventType, QuietConsoleSink, RunEvent, StreamRecord
from rayspec.events.sinks.console import fmt_cost, fmt_duration, fmt_tokens
from rayspec.providers.base import Usage

pytestmark = pytest.mark.anyio

RUN = "20260820-101500-ab2c"


def ev(type_: EventType, step: str | None = None, **data) -> RunEvent:
    return RunEvent(type=type_, run_id=RUN, step_path=step, data=data)


def make_console() -> Console:
    return Console(record=True, width=120, force_terminal=False, color_system=None, soft_wrap=True)


def test_format_helpers():
    assert fmt_duration(850) == "850ms"
    assert fmt_duration(1234) == "1.2s"
    assert fmt_duration(59_950) == "60.0s"
    assert fmt_duration(125_000) == "2m05s"
    assert fmt_duration(3_725_000) == "1h02m"
    assert fmt_tokens(999) == "999 tok"
    assert fmt_tokens(1234) == "1.2k tok"
    assert fmt_tokens(1_260_000) == "1.3M tok"
    assert fmt_cost(0.01234) == "$0.01"
    assert fmt_cost(12.5) == "$12.50"
    assert fmt_cost(0) == "$0.00"
    assert fmt_cost(0.12, approx=True) == "~$0.12"


async def test_quiet_console_snapshot():
    console = make_console()
    sink = QuietConsoleSink(console)
    assert isinstance(sink, EventSink)
    events = [
        ev(EventType.RUN_STARTED, workflow="fix_issue"),
        ev(EventType.WORKSPACE_CREATED, workdir="/wt/fix-ab2c", branch="rayspec/fix_issue-ab2c"),
        ev(EventType.STEP_STARTED, "assess", kind="prompt", attempt=1),
        ev(
            EventType.STEP_FINISHED,
            "assess",
            status="succeeded",
            duration_ms=8500,
            usage={"input": 1200, "output": 300},
            cost_usd=0.0123,
            cost_source="table",
        ),
        ev(EventType.LOOP_ITERATION, "build", n=1, max=3),
        ev(EventType.STEP_STARTED, "build[1]/implement", kind="shell", attempt=2),
        ev(
            EventType.STEP_RETRY,
            "build[1]/implement",
            attempt=2,
            delay_s=3.0,
            error={"type": "RateLimit", "message": "429 slow down", "transient": True},
        ),
        ev(
            EventType.STEP_FINISHED,
            "build[1]/implement",
            status="failed",
            duration_ms=400,
            error={"type": "ShellError", "message": "exit 1", "transient": False},
            tolerated=True,
        ),
        ev(EventType.STEP_FINISHED, "lint", status="skipped", skip_reason="when: false"),
        ev(EventType.WARNING, message="item hash changed; re-running"),
        ev(EventType.RUN_PAUSED, token="gate#1", step="gate", message="ship it?"),
        ev(EventType.RUN_DECISION, approved=True, comment="lgtm", by="cli"),
        ev(EventType.RUN_RESUMED),
        ev(
            EventType.RUN_FINISHED,
            status="succeeded",
            reason=None,
            usage=Usage(input=5000, output=1000),
            cost_usd=0.5,
        ),
    ]
    for e in events:
        await sink.emit(e)
    await sink.emit_stream("assess", StreamRecord(kind="text_delta", text="should not print"))
    await sink.aclose()
    text = console.export_text()
    assert text == (
        f"▶ run {RUN} started (fix_issue)\n"
        "● workspace /wt/fix-ab2c (rayspec/fix_issue-ab2c)\n"
        "✓ assess succeeded 8.5s · 1.5k tok · ~$0.01\n"
        "↻ build[1]/implement retry in 3s (attempt 2): RateLimit: 429 slow down\n"
        "✗ build[1]/implement failed (tolerated) 400ms — ShellError: exit 1\n"
        "○ lint skipped — when: false\n"
        "! warning: item hash changed; re-running\n"
        f"‖ run {RUN} paused at gate — ship it?\n"
        "● decision: approved — lgtm\n"
        f"▶ run {RUN} resumed\n"
        f"■ run {RUN} succeeded · 6.0k tok · ~$0.50\n"  # a table step was seen: same marker
    )


async def test_quiet_console_show_started_opt_in():
    console = make_console()
    sink = QuietConsoleSink(console, show_started=True)
    await sink.emit(ev(EventType.STEP_STARTED, "assess", kind="prompt", attempt=1))
    await sink.emit(ev(EventType.STEP_STARTED, "build[1]/implement", kind="shell", attempt=2))
    await sink.emit(ev(EventType.STEP_FINISHED, "assess", status="succeeded", duration_ms=10))
    assert console.export_text() == (
        "→ assess (prompt)\n→ build[1]/implement (shell) attempt 2\n✓ assess succeeded 10ms\n"
    )


async def test_quiet_console_failed_run_and_minimal_data():
    console = make_console()
    sink = QuietConsoleSink(console)
    await sink.emit(ev(EventType.RUN_STARTED))
    await sink.emit(ev(EventType.STEP_FINISHED, "x", status="failed", error="boom"))
    await sink.emit(ev(EventType.STEP_FINISHED, "y", status="cancelled"))
    await sink.emit(ev(EventType.RUN_DECISION, approved=False))
    await sink.emit(ev(EventType.RUN_FINISHED, status="failed", reason="step x failed"))
    await sink.emit(ev(EventType.EACH_ITEM, "fan", index=0, total=2))
    await sink.aclose()
    assert console.export_text() == (
        f"▶ run {RUN} started\n"
        "✗ x failed — boom\n"
        "· y cancelled\n"
        "● decision: rejected\n"
        f"■ run {RUN} failed — step x failed\n"
    )


async def test_quiet_console_never_raises(caplog: pytest.LogCaptureFixture):
    class BadConsole:
        def print(self, *a, **kw):
            raise OSError("no tty")

    sink = QuietConsoleSink(BadConsole())  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="rayspec.events"):
        await sink.emit(ev(EventType.RUN_STARTED))
        await sink.emit(ev(EventType.RUN_FINISHED, status="failed"))
    assert sum(r.levelno >= logging.WARNING for r in caplog.records) == 1


async def test_quiet_console_is_subclassable():
    console = make_console()
    seen: list[str] = []

    class Live(QuietConsoleSink):
        async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
            seen.append(record.text)

        def format_event(self, event: RunEvent):
            if event.type is EventType.STEP_STARTED:
                return None  # the live tree shows running steps itself
            return super().format_event(event)

    sink = Live(console)
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="shell", attempt=1))
    await sink.emit(ev(EventType.STEP_FINISHED, "a", status="succeeded", duration_ms=10))
    await sink.emit_stream("a", StreamRecord(kind="stdout", text="hi"))
    assert console.export_text() == "✓ a succeeded 10ms\n"
    assert seen == ["hi"]


async def test_quiet_console_format_hooks_are_overridable():
    """``format_<event>()`` are real override points: dispatch must go through the instance."""
    console = make_console()

    class Mine(QuietConsoleSink):
        def format_step_finished(self, event: RunEvent):
            return Text("OVERRIDDEN")

        def format_run_finished(self, event: RunEvent):
            return None  # suppressed

    sink = Mine(console)
    await sink.emit(ev(EventType.STEP_FINISHED, "a", status="succeeded"))
    await sink.emit(ev(EventType.RUN_FINISHED, status="succeeded"))
    assert console.export_text() == "OVERRIDDEN\n"

"""ConsoleSink: the Rich Live tree (model + deterministic renderer + Live lifecycle)."""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest
from rich.console import Console

from rayspec.events import EventSink, EventType, RunEvent, StreamRecord
from rayspec.events.sinks import ConsoleSink, QuietConsoleSink
from rayspec.events.sinks.console import RunView, StepView

pytestmark = pytest.mark.anyio

RUN = "20260820-101500-ab2c"


def ev(type_: EventType, step_path: str | None = None, **data) -> RunEvent:
    return RunEvent(type=type_, run_id=RUN, step_path=step_path, data=data)


def rec(kind: str, text: str = "", **kw: Any) -> StreamRecord:
    return StreamRecord(kind=kind, text=text, **kw)


class FakeClock:
    """Deterministic monotonic clock for elapsed-time rendering."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def plain_console() -> Console:
    """A non-TTY recording console (``is_terminal`` is False)."""
    return Console(record=True, width=120, force_terminal=False, color_system=None, soft_wrap=True)


def tty_console(file: io.StringIO | None = None) -> Console:
    """A recording console that claims to be a terminal (Live is allowed)."""
    return Console(
        file=file or io.StringIO(),
        record=True,
        width=120,
        force_terminal=True,
        color_system=None,
        soft_wrap=True,
    )


def snapshot(console: Console, sink: ConsoleSink) -> str:
    console.print(sink.render())
    return console.export_text()


def make_sink(console: Console, **kw: Any) -> tuple[ConsoleSink, FakeClock]:
    clock = FakeClock()
    sink = ConsoleSink(console, clock=clock, live=True, display=False, **kw)
    return sink, clock


# -- model -------------------------------------------------------------------------------------


async def test_sink_is_an_event_sink_and_quiet_subclass():
    sink, _ = make_sink(plain_console())
    assert isinstance(sink, EventSink)
    assert isinstance(sink, QuietConsoleSink)
    assert isinstance(sink.view, RunView)


async def test_model_tracks_steps_and_nesting():
    sink, clock = make_sink(plain_console())
    await sink.emit(ev(EventType.RUN_STARTED, workflow="fix_issue"))
    await sink.emit(ev(EventType.STEP_STARTED, "assess", kind="prompt", attempt=1))
    clock.advance(2.5)
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "assess",
            status="succeeded",
            duration_ms=2500,
            usage={"input": 1000, "output": 200},
            cost_usd=0.05,
        )
    )
    await sink.emit(ev(EventType.STEP_STARTED, "build", kind="loop", attempt=1))
    await sink.emit(ev(EventType.LOOP_ITERATION, "build", n=1, max=3))
    await sink.emit(ev(EventType.STEP_STARTED, "build[1]/implement", kind="prompt", attempt=1))
    view = sink.view
    assert view.workflow == "fix_issue"
    assert view.run_id == RUN
    assert [s.path for s in view.roots] == ["assess", "build"]
    assess = view.get("assess")
    assert isinstance(assess, StepView)
    assert assess.status == "succeeded"
    assert assess.duration_ms == 2500
    build = view.get("build")
    assert build is not None and [c.path for c in build.children] == ["build[1]"]
    it = view.get("build[1]")
    assert it is not None and it.iteration == (1, 3)
    assert [c.path for c in it.children] == ["build[1]/implement"]
    impl = view.get("build[1]/implement")
    assert impl is not None and impl.status == "running" and impl.kind == "prompt"


async def test_model_each_items_and_deep_paths_autocreate_ancestors():
    sink, _ = make_sink(plain_console())
    # no step.started for the composite: the body path creates the missing ancestors
    await sink.emit(ev(EventType.EACH_ITEM, "build[2]/fix_all", index=0, total=2))
    await sink.emit(
        ev(EventType.STEP_STARTED, "build[2]/fix_all[0]/patch", kind="prompt", attempt=1)
    )
    view = sink.view
    assert [s.path for s in view.roots] == ["build"]
    item = view.get("build[2]/fix_all[0]")
    assert item is not None and item.item == (0, 2)
    assert view.get("build[2]/fix_all[0]/patch") is not None


async def test_tail_collects_text_deltas_tools_and_shell_lines():
    sink, _ = make_sink(plain_console(), tail_lines=6)
    await sink.emit(ev(EventType.STEP_STARTED, "assess", kind="prompt", attempt=1))
    await sink.emit_stream("assess", rec("text_delta", "Looking at "))
    await sink.emit_stream("assess", rec("text_delta", "the issue\nSecond line"))
    await sink.emit_stream("assess", rec("tool_call", name="Read", data={"file_path": "a.py"}))
    await sink.emit_stream("assess", rec("tool_result", "contents...", name="Read"))
    # the completed text block repeats the streamed deltas: no duplicate lines
    await sink.emit_stream("assess", rec("text", "Looking at the issue\nSecond line"))
    step = sink.view.get("assess")
    assert step is not None
    assert step.tail_lines() == [
        "Looking at the issue",
        "Second line",
        "⚙ Read a.py",
        "  ↳ contents...",
    ]
    # a text block without preceding deltas is shown
    await sink.emit_stream("assess", rec("text", "Done."))
    assert step.tail_lines()[-1] == "Done."
    # shell steps: stdout lines (trailing newline stripped)
    await sink.emit(ev(EventType.STEP_STARTED, "test", kind="shell", attempt=1))
    await sink.emit_stream("test", rec("stdout", "collected 3 items\n"))
    await sink.emit_stream("test", rec("stderr", "warning: slow\n"))
    await sink.emit_stream("test", rec("exit", data={"exit_code": 0}))
    shell = sink.view.get("test")
    assert shell is not None
    assert shell.tail_lines() == ["collected 3 items", "warning: slow"]


async def test_stream_for_unknown_step_is_ignored_and_never_raises():
    sink, _ = make_sink(plain_console())
    await sink.emit_stream("ghost", rec("stdout", "x\n"))  # no step.started yet
    assert sink.view.get("ghost") is None


# -- renderer snapshots ------------------------------------------------------------------------


async def test_snapshot_single_step_progress_then_finished():
    console = plain_console()
    sink, clock = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="fix_issue"))
    await sink.emit(ev(EventType.STEP_STARTED, "assess", kind="prompt", attempt=1))
    await sink.emit_stream("assess", rec("text_delta", "Reading the issue…\n"))
    await sink.emit_stream("assess", rec("tool_call", name="Bash", data={"command": "pytest -q"}))
    clock.advance(12.3)
    running = snapshot(console, sink)
    assert running == (
        "▶ fix_issue · 20260820-101500-ab2c · running 12.3s\n"
        "└── → assess (prompt) 12.3s\n"
        "    ├── Reading the issue…\n"
        "    └── ⚙ Bash pytest -q\n"
    )
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "assess",
            status="succeeded",
            duration_ms=12300,
            usage={"input": 1200, "output": 300},
            cost_usd=0.0123,
            cost_source="table",
        )
    )
    await sink.emit(
        ev(
            EventType.RUN_FINISHED,
            status="succeeded",
            usage={"input": 1200, "output": 300},
            cost_usd=0.0123,
            cost_source="table",
            outputs={"summary": "ok"},
        )
    )
    done = snapshot(console, sink)
    assert done == (
        "■ fix_issue · 20260820-101500-ab2c · succeeded 12.3s · 1.5k tok · ~$0.01\n"
        "└── ✓ assess (prompt) 12.3s · 1.5k tok · ~$0.01\n"
    )


async def test_snapshot_nested_loop_and_each():
    console = plain_console()
    sink, clock = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "build", kind="loop", attempt=1))
    await sink.emit(ev(EventType.LOOP_ITERATION, "build", n=1, max=3))
    await sink.emit(ev(EventType.STEP_STARTED, "build[1]/implement", kind="prompt", attempt=1))
    clock.advance(4)
    await sink.emit(
        ev(EventType.STEP_FINISHED, "build[1]/implement", status="succeeded", duration_ms=4000)
    )
    await sink.emit(ev(EventType.STEP_STARTED, "build[1]/fix_all", kind="each", attempt=1))
    await sink.emit(ev(EventType.EACH_ITEM, "build[1]/fix_all", index=0, total=2))
    await sink.emit(ev(EventType.EACH_ITEM, "build[1]/fix_all", index=1, total=2))
    await sink.emit(
        ev(EventType.STEP_STARTED, "build[1]/fix_all[0]/patch", kind="shell", attempt=1)
    )
    await sink.emit(
        ev(EventType.STEP_STARTED, "build[1]/fix_all[1]/patch", kind="shell", attempt=1)
    )
    await sink.emit_stream("build[1]/fix_all[1]/patch", rec("stdout", "patching b\n"))
    clock.advance(1)
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "build[1]/fix_all[0]/patch",
            status="failed",
            duration_ms=1000,
            error={"type": "ShellError", "message": "exit 1"},
            tolerated=True,
        )
    )
    running = snapshot(console, sink)
    assert running == (
        "▶ wf · 20260820-101500-ab2c · running 5.0s\n"
        "└── → build (loop) 5.0s\n"
        "    └── → iteration 1/3 5.0s\n"
        "        ├── ✓ implement (prompt) 4.0s\n"
        "        └── → fix_all (each) 1.0s\n"
        "            ├── ✓ item 0/2 1.0s (tolerated)\n"
        "            │   └── ✗ patch (shell) 1.0s (tolerated) — ShellError: exit 1\n"
        "            └── → item 1/2 1.0s\n"
        "                └── → patch (shell) 1.0s\n"
        "                    └── patching b\n"
    )
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "build[1]/fix_all[1]/patch",
            status="succeeded",
            duration_ms=1000,
        )
    )
    await sink.emit(
        ev(EventType.STEP_FINISHED, "build[1]/fix_all", status="succeeded", duration_ms=1000)
    )
    await sink.emit(ev(EventType.LOOP_ITERATION, "build", n=2, max=3))
    await sink.emit(ev(EventType.STEP_STARTED, "build[2]/implement", kind="prompt", attempt=1))
    clock.advance(2)
    second = snapshot(console, sink)
    # a finished iteration stays expanded down to its problem (the tolerated patch); clean
    # finished items collapse to one line
    assert second == (
        "▶ wf · 20260820-101500-ab2c · running 7.0s\n"
        "└── → build (loop) 7.0s\n"
        "    ├── ✓ iteration 1/3 5.0s\n"
        "    │   ├── ✓ implement (prompt) 4.0s\n"
        "    │   └── ✓ fix_all (each) 1.0s\n"
        "    │       ├── ✓ item 0/2 1.0s (tolerated)\n"
        "    │       │   └── ✗ patch (shell) 1.0s (tolerated) — ShellError: exit 1\n"
        "    │       └── ✓ item 1/2 1.0s\n"
        "    └── → iteration 2/3 2.0s\n"
        "        └── → implement (prompt) 2.0s\n"
    )
    await sink.emit(
        ev(EventType.STEP_FINISHED, "build[2]/implement", status="succeeded", duration_ms=2000)
    )
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "build",
            status="succeeded",
            duration_ms=7000,
            iterations=2,
            converged=True,
        )
    )
    await sink.emit(ev(EventType.RUN_FINISHED, status="succeeded", usage=None, cost_usd=None))
    final = snapshot(console, sink)
    # a finished loop with a tolerated failure inside stays expanded (clean ones collapse)
    assert final == (
        "■ wf · 20260820-101500-ab2c · succeeded 7.0s\n"
        "└── ✓ build (loop) 7.0s\n"
        "    ├── ✓ iteration 1/3 5.0s\n"
        "    │   ├── ✓ implement (prompt) 4.0s\n"
        "    │   └── ✓ fix_all (each) 1.0s\n"
        "    │       ├── ✓ item 0/2 1.0s (tolerated)\n"
        "    │       │   └── ✗ patch (shell) 1.0s (tolerated) — ShellError: exit 1\n"
        "    │       └── ✓ item 1/2 1.0s\n"
        "    └── ✓ iteration 2/3 2.0s\n"
    )


async def test_snapshot_tail_truncation_and_verbose_depth():
    console = plain_console()
    sink, _ = make_sink(console)  # default: 6 lines
    await sink.emit(ev(EventType.STEP_STARTED, "s", kind="shell", attempt=1))
    for n in range(10):
        await sink.emit_stream("s", rec("stdout", f"line {n}\n"))
    long_line = "x" * 300
    await sink.emit_stream("s", rec("stdout", long_line + "\n"))
    out = snapshot(console, sink)
    lines = out.splitlines()
    tail = [ln for ln in lines if "line " in ln or "xxx" in ln]
    assert len(tail) == 6
    assert "line 5" in tail[0] and "line 9" in tail[4]
    assert tail[5].endswith("…")  # truncated to the console width, never wrapped
    assert len(tail[5]) <= 120

    verbose = ConsoleSink(plain_console(), verbose=True, live=True, display=False)
    assert verbose.tail_lines == 20
    await verbose.emit(ev(EventType.STEP_STARTED, "s", kind="shell", attempt=1))
    for n in range(30):
        await verbose.emit_stream("s", rec("stdout", f"line {n}\n"))
    step = verbose.view.get("s")
    assert step is not None and len(step.tail_lines()) == 20
    assert step.tail_lines()[0] == "line 10"


async def test_snapshot_warning_pause_decision_and_retry():
    console = plain_console()
    sink, clock = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.WARNING, message="item hash changed; re-running"))
    await sink.emit(ev(EventType.STEP_STARTED, "fetch", kind="shell", attempt=1))
    await sink.emit(
        ev(
            EventType.STEP_RETRY,
            "fetch",
            attempt=2,
            delay_s=3.0,
            error={"type": "RateLimit", "message": "429 slow down", "transient": True},
        )
    )
    await sink.emit(ev(EventType.STEP_STARTED, "fetch", kind="shell", attempt=2))
    clock.advance(1)
    await sink.emit(ev(EventType.STEP_FINISHED, "fetch", status="succeeded", duration_ms=1000))
    await sink.emit(ev(EventType.STEP_STARTED, "gate", kind="approve", attempt=1))
    await sink.emit(
        ev(EventType.RUN_PAUSED, "gate", token="gate#1", step="gate", message="ship it?")
    )
    out = snapshot(console, sink)
    assert out == (
        "▶ wf · 20260820-101500-ab2c · running 1.0s\n"
        "├── ✓ fetch (shell) 1.0s attempt 2\n"
        "└── ‖ gate (approve) 0ms\n"
        "    └── ‖ approval required: ship it?\n"
        "! warning: item hash changed; re-running\n"
        "‖ paused at gate — ship it? (rayspec approve 20260820-101500-ab2c / rayspec reject "
        "20260820-101500-ab2c)\n"
    )
    await sink.emit(ev(EventType.RUN_DECISION, "gate", approved=True, comment="lgtm", by="cli"))
    await sink.emit(ev(EventType.STEP_FINISHED, "gate", status="succeeded", duration_ms=0))
    out = snapshot(console, sink)
    assert "● decision: approved — lgtm" in out
    assert "paused at" not in out


async def test_snapshot_final_summary_panel():
    console = plain_console()
    sink, clock = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(
        ev(EventType.WORKSPACE_CREATED, workdir="/wt/wf-ab2c", branch="rayspec/wf-ab2c")
    )
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="prompt", attempt=1))
    clock.advance(3)
    await sink.emit(
        ev(
            EventType.STEP_FINISHED,
            "a",
            status="succeeded",
            duration_ms=3000,
            usage={"input": 10, "output": 5},
            cost_usd=0.5,
        )
    )
    await sink.emit(
        ev(
            EventType.RUN_FINISHED,
            status="failed",
            reason="step b failed",
            usage={"input": 10, "output": 5},
            cost_usd=0.5,
            outputs={"pr": "https://x/1", "count": 3},
        )
    )
    console.print(sink.render_summary())
    out = console.export_text()
    assert out == (
        "╭─ run 20260820-101500-ab2c failed ───────────────────────────────────────────────────"
        "─────────────────────────────────╮\n"
        "│ step b failed                                                                       "
        "                                 │\n"
        "│                                                                                     "
        "                                 │\n"
        "│ outputs                                                                             "
        "                                 │\n"
        "│ pr     https://x/1                                                                  "
        "                                 │\n"
        "│ count  3                                                                            "
        "                                 │\n"
        "│                                                                                     "
        "                                 │\n"
        "│ workspace  /wt/wf-ab2c (branch rayspec/wf-ab2c)                                     "
        "                                 │\n"
        "│ totals     1 step · 3.0s · 15 tok · $0.50                                           "
        "                                 │\n"
        "╰─────────────────────────────────────────────────────────────────────────────────────"
        "─────────────────────────────────╯\n"
    )


# -- degradation -------------------------------------------------------------------------------


async def test_quiet_mode_prints_quiet_lines_and_ignores_streams():
    console = tty_console()
    sink = ConsoleSink(console, quiet=True)
    assert sink.live_enabled is False
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="prompt", attempt=1))
    await sink.emit_stream("a", rec("text_delta", "hello"))
    await sink.emit(ev(EventType.STEP_FINISHED, "a", status="succeeded", duration_ms=10))
    await sink.emit(ev(EventType.RUN_FINISHED, status="succeeded"))
    await sink.aclose()
    out = console.export_text()
    assert out == "✓ a succeeded 10ms\n■ run 20260820-101500-ab2c succeeded\n"


async def test_non_tty_falls_back_to_quiet_lines():
    console = plain_console()
    sink = ConsoleSink(console)  # live=None → auto: console.is_terminal is False
    assert sink.live_enabled is False
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="prompt", attempt=1))
    await sink.emit_stream("a", rec("text_delta", "hello"))
    await sink.emit(ev(EventType.STEP_FINISHED, "a", status="succeeded", duration_ms=10))
    await sink.aclose()
    out = console.export_text()
    assert out == "▶ run 20260820-101500-ab2c started (wf)\n✓ a succeeded 10ms\n"
    assert sink.is_live is False


async def test_verbose_non_tty_shows_starts():
    console = plain_console()
    sink = ConsoleSink(console, verbose=True)
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="prompt", attempt=1))
    assert console.export_text() == "→ a (prompt)\n"


# -- Live lifecycle ----------------------------------------------------------------------------


async def test_live_starts_on_first_event_and_stops_on_aclose():
    console = tty_console()
    sink = ConsoleSink(console, refresh_per_second=8)
    assert sink.live_enabled is True
    assert sink.is_live is False
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    assert sink.is_live is True
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="prompt", attempt=1))
    await sink.emit(ev(EventType.STEP_FINISHED, "a", status="succeeded", duration_ms=10))
    await sink.aclose()
    assert sink.is_live is False
    out = console.export_text()
    assert "✓ a (prompt) 10ms" in out
    # closing twice is harmless
    await sink.aclose()


async def test_run_finished_stops_live_and_prints_summary_once():
    console = tty_console()
    sink = ConsoleSink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "a", kind="prompt", attempt=1))
    await sink.emit(ev(EventType.STEP_FINISHED, "a", status="succeeded", duration_ms=10))
    await sink.emit(ev(EventType.RUN_FINISHED, status="succeeded", outputs={"x": "1"}))
    assert sink.is_live is False
    await sink.aclose()
    out = console.export_text()
    assert out.count("run 20260820-101500-ab2c succeeded") == 1
    assert "outputs" in out and "x  1" in out


async def test_pause_and_resume_around_a_prompt():
    console = tty_console()
    sink = ConsoleSink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "gate", kind="approve", attempt=1))
    assert sink.is_live is True
    async with sink.suspended():
        assert sink.is_live is False
        console.print("Approve? (a/r)")
        # nested suspension is a no-op (still not live)
        async with sink.suspended():
            assert sink.is_live is False
        assert sink.is_live is False
    assert sink.is_live is True
    await sink.emit(ev(EventType.STEP_FINISHED, "gate", status="succeeded", duration_ms=0))
    await sink.aclose()
    out = console.export_text()
    # the frozen tree is printed before the prompt, and the display continues afterwards
    assert out.index("→ gate (approve)") < out.index("Approve? (a/r)")
    assert out.index("Approve? (a/r)") < out.index("✓ gate (approve) 0ms")
    # explicit pause()/resume() work too, and are idempotent
    sink2 = ConsoleSink(tty_console())
    await sink2.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink2.pause()
    await sink2.pause()
    assert sink2.is_live is False
    await sink2.resume()
    await sink2.resume()
    assert sink2.is_live is True
    await sink2.aclose()


async def test_pause_before_any_event_does_not_start_live():
    sink = ConsoleSink(tty_console())
    async with sink.suspended():
        assert sink.is_live is False
    assert sink.is_live is False
    await sink.aclose()


async def test_concurrent_emits_are_serialised():
    import anyio

    sink = ConsoleSink(tty_console())
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))

    async def worker(i: int) -> None:
        path = f"s{i}"
        await sink.emit(ev(EventType.STEP_STARTED, path, kind="shell", attempt=1))
        for n in range(20):
            await sink.emit_stream(path, rec("stdout", f"{path} line {n}\n"))
        await sink.emit(ev(EventType.STEP_FINISHED, path, status="succeeded", duration_ms=1))

    async with anyio.create_task_group() as tg:
        for i in range(8):
            tg.start_soon(worker, i)
    assert len(sink.view.roots) == 8
    assert all(s.status == "succeeded" for s in sink.view.roots)
    await sink.aclose()


async def test_console_errors_are_logged_not_raised(caplog):
    class BrokenConsole(Console):
        def print(self, *a: Any, **k: Any) -> None:  # type: ignore[override]
            raise OSError("boom")

    console = BrokenConsole(file=io.StringIO(), force_terminal=True, width=80)
    sink = ConsoleSink(console)
    with caplog.at_level(logging.WARNING, logger="rayspec.events"):
        await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
        await sink.emit(ev(EventType.RUN_FINISHED, status="succeeded"))
        await sink.aclose()
    assert any("console sink" in r.message for r in caplog.records)


# -- more tail / footer details --------------------------------------------------------------------


async def test_tail_command_file_change_retry_and_verbose_reasoning():
    sink, _ = make_sink(plain_console())
    await sink.emit(ev(EventType.STEP_STARTED, "p", kind="prompt", attempt=1))
    await sink.emit_stream("p", rec("command_start", data={"command": "make test"}))
    await sink.emit_stream("p", rec("command_output", "ok\n"))
    await sink.emit_stream("p", rec("command_end", data={"exit_code": 2}))
    await sink.emit_stream("p", rec("file_change", name="src/a.py", data={"kind": "edit"}))
    await sink.emit_stream("p", rec("reasoning", "thinking hard"))  # hidden unless verbose
    await sink.emit_stream("p", rec("warning", "rate limited"))
    await sink.emit_stream("p", rec("usage", data={"usage": {}}))  # nothing to show
    await sink.emit(
        ev(EventType.STEP_RETRY, "p", attempt=2, delay_s=3.0, error={"type": "E", "message": "m"})
    )
    step = sink.view.get("p")
    assert step is not None
    assert step.tail_lines() == [
        "$ make test",
        "ok",
        "  ↳ exit 2",
        "✎ src/a.py",
        "! rate limited",
        "↻ retry in 3s: E: m",
    ]
    # the tail is reset by the next attempt's step.started
    await sink.emit(ev(EventType.STEP_STARTED, "p", kind="prompt", attempt=2))
    assert step.tail_lines() == [] and step.attempt == 2

    verbose = ConsoleSink(plain_console(), verbose=True, live=True, display=False)
    await verbose.emit(ev(EventType.STEP_STARTED, "p", kind="prompt", attempt=1))
    await verbose.emit_stream("p", rec("reasoning", "thinking hard\nmore"))
    await verbose.emit_stream("p", rec("tool_call", name="mcp/grep", data={"pattern": "x", "n": 1}))
    await verbose.emit_stream("p", rec("tool_call", name="weird", data={"n": 1}))
    node = verbose.view.get("p")
    assert node is not None
    assert node.tail_lines() == ["· thinking: thinking hard", "⚙ mcp/grep x", '⚙ weird {"n": 1}']


async def test_footer_caps_warnings_and_prefixes_step_path():
    console = plain_console()
    sink, _ = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    for n in range(7):
        await sink.emit(ev(EventType.WARNING, "s", message=f"w{n}"))
    out = snapshot(console, sink)
    assert "! … 2 more warnings" in out
    assert "! warning: s: w6" in out and "w1" not in out


async def test_include_children_visible_while_running_collapse_when_finished():
    console = plain_console()
    sink, clock = make_sink(console)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.STEP_STARTED, "review", kind="include", attempt=1))
    await sink.emit(ev(EventType.STEP_STARTED, "review/lint", kind="shell", attempt=1))
    clock.advance(1)
    await sink.emit(
        ev(EventType.STEP_FINISHED, "review/lint", status="succeeded", duration_ms=1000)
    )
    assert snapshot(console, sink) == (
        "▶ wf · 20260820-101500-ab2c · running 1.0s\n"
        "└── → review (include) 1.0s\n"
        "    └── ✓ lint (shell) 1.0s\n"
    )
    await sink.emit(ev(EventType.STEP_FINISHED, "review", status="succeeded", duration_ms=1000))
    # a clean finished composite collapses to its one line
    assert snapshot(console, sink) == (
        "▶ wf · 20260820-101500-ab2c · running 1.0s\n└── ✓ review (include) 1.0s\n"
    )


async def test_events_after_run_finished_degrade_to_quiet_lines():
    console = tty_console()
    sink = ConsoleSink(console, summary=False)
    await sink.emit(ev(EventType.RUN_STARTED, workflow="wf"))
    await sink.emit(ev(EventType.RUN_FINISHED, status="succeeded"))
    assert sink.is_live is False
    await sink.emit(ev(EventType.WARNING, message="late"))
    await sink.aclose()
    out = console.export_text()
    assert out.endswith("! warning: late\n")
    assert "outputs" not in out

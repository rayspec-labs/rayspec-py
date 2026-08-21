"""`rayspec logs`: readable Claude streams, one-line step-path errors, escape
neutralisation + `--raw`."""

from __future__ import annotations

from datetime import timedelta

from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.events.model import EventType, RunEvent, StreamRecord

from .conftest import SUCCEEDED_ID, T0, Seeded

ESC = "\x1b"
NASTY = f"before{ESC}]0;PWNED\x07{ESC}[31mRED{ESC}[0m [bold red]MARKUP[/] {ESC}[2J after"
CLEAN = "beforeRED [bold red]MARKUP[/]  after"


def _claude_stream(seeded: Seeded, step: str = "think") -> None:
    t = T0 + timedelta(seconds=30)
    records = [
        StreamRecord(kind="warning", text="rate limit allowed_warning (seven_day) utilization 98%"),
        StreamRecord(kind="session", text="a4e81d5b"),
        StreamRecord(kind="raw", name="status", text=""),
        StreamRecord(kind="raw", name="thinking_tokens", text=""),
        StreamRecord(kind="reasoning", text="The user wants me"),
        StreamRecord(kind="raw", name="thinking_tokens", text=""),
        StreamRecord(kind="reasoning", text=" to review a file.\n\nLet me first read"),
        StreamRecord(kind="raw", name="thinking_tokens", text=""),
        StreamRecord(kind="reasoning", text=" it"),
        StreamRecord(kind="tool_call", name="Read", call_id="c1", data={"input": {"p": "x"}}),
        StreamRecord(kind="tool_result", call_id="c1", text="1 def add(a, b): …"),
        StreamRecord(kind="raw", name="status", text=""),
        StreamRecord(kind="reasoning", text="file is simple"),
        StreamRecord(kind="text", text='{"verdict": "ok"}'),
    ]
    for i, rec in enumerate(records):
        seeded.store.append_stream(
            SUCCEEDED_ID, step, rec.model_copy(update={"ts": t + timedelta(seconds=i)})
        )


def test_logs_step_joins_thinking_and_hides_raw_records(cli: CliRunner, seeded: Seeded) -> None:
    _claude_stream(seeded)
    result = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "think", "--root", str(seeded.project)]
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "raw status" not in out and "raw thinking_tokens" not in out
    assert "thinking: The user wants me to review a file." in out
    assert "thinking: Let me first read it" in out  # flushed at the block end, whole
    assert "thinking: file is simple" in out
    assert out.index("thinking: Let me first read it") < out.index("⚙ Read")
    assert "warning: rate limit allowed_warning (seven_day) utilization 98%" in out
    assert '{"verdict": "ok"}' in out
    verbose = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "think", "--verbose", "--root", str(seeded.project)]
    )
    assert verbose.exit_code == 0, verbose.output
    assert "raw status" in verbose.output and "raw thinking_tokens" in verbose.output
    streamed = cli.invoke(app, ["logs", SUCCEEDED_ID, "--stream", "--root", str(seeded.project)])
    assert "raw thinking_tokens" not in streamed.output
    assert "[think] thinking: The user wants me to review a file." in streamed.output


def test_logs_step_path_errors_are_printed_once(cli: CliRunner, seeded: Seeded) -> None:
    traversal = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "../../run.json", "--root", str(seeded.project)]
    )
    assert traversal.exit_code == 2
    assert traversal.output.count("invalid step path") == 1, traversal.output
    assert "error: invalid step path '../../run.json': bad segment '..'" in traversal.output
    absolute = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "/etc/hosts", "--root", str(seeded.project)]
    )
    assert absolute.exit_code == 2
    assert absolute.output.count("invalid step path") == 1
    assert "absolute paths are not step paths" in absolute.output
    assert "bad segment ''" not in absolute.output


def test_logs_neutralises_escapes_unless_raw(cli: CliRunner, seeded: Seeded) -> None:
    for rec in (
        StreamRecord(kind="stdout", text=NASTY + "\n"),
        StreamRecord(kind="text_delta", text=NASTY),
        StreamRecord(kind="text", text=NASTY),
        StreamRecord(kind="reasoning", text=NASTY),
        StreamRecord(kind="tool_call", name=NASTY, data={"input": {"cmd": NASTY}}),
        StreamRecord(kind="tool_result", text=NASTY),
        StreamRecord(kind="warning", text=NASTY),
        StreamRecord(kind="command_start", data={"command": NASTY}),
        StreamRecord(kind="stderr", text=NASTY),
        StreamRecord(kind="file_change", name=NASTY, text=NASTY),
    ):
        seeded.store.append_stream(SUCCEEDED_ID, "esc", rec)
    seeded.store.append_event(
        SUCCEEDED_ID,
        RunEvent(
            type=EventType.WARNING, run_id=SUCCEEDED_ID, step_path="esc", data={"message": NASTY}
        ),
    )
    step = cli.invoke(app, ["logs", SUCCEEDED_ID, "--step", "esc", "--root", str(seeded.project)])
    assert step.exit_code == 0, step.output
    assert ESC not in step.output and "\x07" not in step.output
    assert step.output.count(CLEAN) >= 8, step.output
    events = cli.invoke(app, ["logs", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert ESC not in events.output and CLEAN in events.output
    raw = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "esc", "--raw", "--root", str(seeded.project)]
    )
    assert raw.exit_code == 0
    assert ESC in raw.output  # debugging escape hatch: the stored bytes, unescaped
    as_json = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "esc", "--json", "--root", str(seeded.project)]
    )
    assert ESC not in as_json.output and "\\u001b" in as_json.output  # JSON-escaped, as before

"""The run summary never forwards terminal escapes from step outputs."""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from rayspec.cli.commands.run import print_summary
from rayspec.engine.runner import RunResult, Workspace
from rayspec.schema import RunStatus
from rayspec.textsafe import safe_markup, safe_text


def test_safe_text_strips_escapes_and_controls_keeps_newlines() -> None:
    hostile = "ok\x1b[31mred\x1b[0m\x1b]0;title\x07 \x07bell\ttab\nline2\r\nline3\x9b!"
    assert safe_text(hostile) == "okred bell\ttab\nline2\nline3!"
    assert safe_text(hostile, keep_newlines=False) == "okred bell tab line2 line3!"
    assert safe_markup("[bold]x\x1b[2J") == "\\[bold]x"


def test_run_summary_outputs_are_sanitised() -> None:
    result = RunResult(
        run_id="20260820-101010-abcd",
        status=RunStatus.SUCCEEDED,
        exit_code=0,
        run_dir=Path("/tmp/run"),
        workspace=Workspace.in_place(Path("/tmp/proj")),
        outputs={"verdict": "ok\x1b[2J\x1b]0;pwned\x07 [bold]still literal[/bold]"},
    )
    console = Console(file=io.StringIO(), width=200, force_terminal=True, color_system="truecolor")
    print_summary(console, result, json_mode=False)
    assert isinstance(console.file, io.StringIO)
    out = console.file.getvalue()
    assert "\x1b[2J" not in out and "\x07" not in out and "pwned" not in out
    assert "[bold]still literal[/bold]" in out  # run data is never Rich markup

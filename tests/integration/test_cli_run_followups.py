"""Follow-ups on ``rayspec run``: the ``--json`` stdout contract, ConsoleSink wiring and
markup-safe output.

These drive the real CLI modules (``rayspec.cli.commands.run`` / ``_loader_common``) with the stub
provider; nothing is mocked except the terminal (a ``rich.console.Console`` with
``force_terminal``).
"""

from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from rayspec.cli import _runs_common as runs_common
from rayspec.cli.app import app
from rayspec.cli.commands import _loader_common as common
from rayspec.cli.commands import run as run_cmd
from rayspec.engine.approval import ApprovalAnswer, ApprovalRequest
from rayspec.events.sinks import ConsoleSink, JsonStdoutSink, MultiSink

WORKFLOW = """
rayspec: 1
name: demo
steps:
  - id: think
    agent: { provider: stub }
    prompt: "[stub] think"
outputs:
  said: "{{ steps.think.output }}"
"""


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "demo.yaml").write_text(textwrap.dedent(WORKFLOW))
    home = tmp_path / "home"
    home.mkdir()
    return root, home


def _invoke(args: list[str], home: Path):
    return CliRunner().invoke(app, args, env={"RAYSPEC_HOME": str(home), "NO_COLOR": "1"})


# -- --json summary object on stdout -------------------------------------------------------


def test_json_summary_object_is_the_last_stdout_line(tmp_path: Path) -> None:
    root, home = _project(tmp_path)
    res = _invoke(["run", "demo", "--root", str(root), "--dry-run", "--json"], home)
    assert res.exit_code == 0, res.output
    stdout_lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    summary = stdout_lines[-1]
    assert "type" not in summary, "the last stdout line must be the summary object, not an event"
    assert summary["exit_code"] == 0 and summary["status"] == "succeeded"
    assert set(summary) == run_cmd.SUMMARY_KEYS
    assert stdout_lines[-2]["type"] == "run.finished"
    assert all("type" in line for line in stdout_lines[:-1]), "events precede the summary"
    # nothing machine-readable leaks to stderr
    assert not any(line.lstrip().startswith("{") for line in res.stderr.splitlines())


# -- markup safety --------------------------------------------------------------------------------


def test_outputs_table_renders_stub_markers_literally(tmp_path: Path) -> None:
    root, home = _project(tmp_path)
    res = _invoke(["run", "demo", "--root", str(root), "--dry-run"], home)
    assert res.exit_code == 0, res.output
    assert "[stub] think" in res.stdout, res.stdout


def test_fail_is_markup_safe(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as info:
        common.fail("bad [stub] thing [bold]x[/bold]", hint="try [dim] again")
    assert info.value.exit_code == 2
    err = capsys.readouterr().err
    assert "error: bad [stub] thing [bold]x[/bold]" in err
    assert "hint: try [dim] again" in err


def test_fail_accepts_a_custom_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as info:
        common.fail("nope", code=4)
    assert info.value.exit_code == 4
    assert "error: nope" in capsys.readouterr().err


# -- --stubs must fail loudly, never with a traceback --------------------------------------------


def test_missing_stubs_file_is_a_usage_error(tmp_path: Path) -> None:
    """A non-existent/unreadable ``--stubs`` path exits 2 with an actionable message (no traceback)."""
    root, home = _project(tmp_path)
    missing = tmp_path / "nope.yaml"
    res = _invoke(["run", "demo", "--root", str(root), "--dry-run", "--stubs", str(missing)], home)
    assert res.exit_code == 2, res.output
    assert "Traceback" not in res.output and "FileNotFoundError" not in res.output, res.output
    assert str(missing) in res.output and "stubs" in res.output, res.output
    assert "--stubs-init" in res.output, "the hint names the scaffold option"
    assert res.stdout.strip() == "" or "{" not in res.stdout, "nothing run, no events"


def test_stubs_directory_is_a_usage_error(tmp_path: Path) -> None:
    root, home = _project(tmp_path)
    res = _invoke(["run", "demo", "--root", str(root), "--dry-run", "--stubs", str(tmp_path)], home)
    assert res.exit_code == 2, res.output
    assert "Traceback" not in res.output, res.output


def test_summary_keys_match_print_summary(tmp_path: Path) -> None:
    """``SUMMARY_KEYS`` is the single source of truth for the ``--json`` summary object."""
    root, home = _project(tmp_path)
    res = _invoke(["run", "demo", "--root", str(root), "--dry-run", "--json"], home)
    assert res.exit_code == 0, res.output
    summary = json.loads(res.stdout.splitlines()[-1])
    assert set(summary) == run_cmd.SUMMARY_KEYS == set(summary)
    assert isinstance(run_cmd.SUMMARY_KEYS, frozenset)


# -- --json does not imply --no-interactive ------------------------------------------------------

GATE_WORKFLOW = """
rayspec: 1
name: gated
steps:
  - id: gate
    approve: "ship?"
  - id: ship
    needs: [gate]
    shell: echo shipped
outputs:
  shipped: "{{ steps.ship.output }}"
"""


def _gated_project(tmp_path: Path) -> tuple[Path, Path]:
    root, home = _project(tmp_path)
    (root / ".rayspec" / "workflows" / "gated.yaml").write_text(textwrap.dedent(GATE_WORKFLOW))
    return root, home


def test_json_on_a_tty_still_prompts_at_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented in cli.md: ``--json`` changes stdout, not interactivity — a terminal still asks
    (on stderr); ``--no-interactive``/``--yes`` are the unattended switches."""
    root, home = _gated_project(tmp_path)
    asked: list[str] = []

    class FakePrompt:
        def __init__(self, console: object | None = None) -> None:
            pass

        async def __call__(self, request: ApprovalRequest) -> ApprovalAnswer | None:
            asked.append(request.step_path)
            return ApprovalAnswer(approved=True, comment="from tty")

    monkeypatch.setattr(runs_common, "stdin_is_tty", lambda: True)
    monkeypatch.setattr(run_cmd, "ConsoleApprovalPrompt", FakePrompt)
    res = _invoke(["run", "gated", "--root", str(root), "--json"], home)
    assert res.exit_code == 0, res.output
    assert asked == ["gate"]
    lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    decision = next(line for line in lines if line.get("type") == "run.decision")
    assert decision["data"] == {"approved": True, "comment": "from tty", "by": "tty"}
    assert set(lines[-1]) == run_cmd.SUMMARY_KEYS and lines[-1]["status"] == "succeeded"


def test_json_with_no_interactive_pauses_on_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, home = _gated_project(tmp_path)
    monkeypatch.setattr(runs_common, "stdin_is_tty", lambda: True)
    res = _invoke(["run", "gated", "--root", str(root), "--json", "--no-interactive"], home)
    assert res.exit_code == 3, res.output
    summary = json.loads(res.stdout.splitlines()[-1])
    assert summary["status"] == "paused" and summary["pause"]["step"] == "gate"


# -- ConsoleSink wiring ---------------------------------------------------------------------------


def _console(*, tty: bool) -> Console:
    return Console(file=io.StringIO(), force_terminal=tty, width=100, height=40)


def _only(sinks: MultiSink):
    assert isinstance(sinks, MultiSink)
    assert len(sinks.sinks) == 1, sinks.sinks
    return sinks.sinks[0]


def test_sinks_build_the_live_tree_on_a_terminal() -> None:
    sink = _only(run_cmd._sinks(False, _console(tty=True), verbose=False, quiet=False))
    assert isinstance(sink, ConsoleSink)
    assert sink.tree_enabled and sink.live_enabled
    assert sink.summary is False, "the CLI prints its own summary (run dir, approve hint)"


def test_sinks_degrade_to_quiet_lines_off_a_terminal() -> None:
    sink = _only(run_cmd._sinks(False, _console(tty=False), verbose=True, quiet=False))
    assert isinstance(sink, ConsoleSink)
    assert not sink.tree_enabled
    assert sink.show_started is True  # --verbose still shows step starts in line mode


def test_sinks_quiet_never_builds_a_tree() -> None:
    sink = _only(run_cmd._sinks(False, _console(tty=True), verbose=False, quiet=True))
    assert not isinstance(sink, ConsoleSink) or not sink.tree_enabled


def test_sinks_json_puts_nothing_on_stdout_but_events() -> None:
    sink = _only(run_cmd._sinks(True, _console(tty=True), verbose=False, quiet=False))
    assert isinstance(sink, JsonStdoutSink)


@pytest.mark.anyio
async def test_approval_prompt_runs_inside_sink_suspended() -> None:
    sinks = run_cmd._sinks(False, _console(tty=True), verbose=False, quiet=False)
    console_sink = _only(sinks)
    assert isinstance(console_sink, ConsoleSink)
    events: list[str] = []
    real_pause, real_resume = console_sink.pause, console_sink.resume

    async def pause() -> None:
        events.append("pause")
        await real_pause()

    async def resume() -> None:
        events.append("resume")
        await real_resume()

    console_sink.pause = pause  # type: ignore[method-assign]
    console_sink.resume = resume  # type: ignore[method-assign]

    async def inner(request: ApprovalRequest) -> ApprovalAnswer | None:
        events.append(f"prompt:{request.step_path}")
        return ApprovalAnswer(approved=True, comment="ok")

    prompt = run_cmd.approval_prompt_for(sinks, interactive=True, prompt=inner)
    assert prompt is not None
    request = ApprovalRequest(
        run_id="r", step_path="gate", message="go?", attempt=1, workdir="/tmp", needs=[], totals={}
    )
    answer = await prompt(request)
    assert answer == ApprovalAnswer(approved=True, comment="ok")
    assert events == ["pause", "prompt:gate", "resume"]


def test_approval_prompt_is_none_when_not_interactive() -> None:
    sinks = run_cmd._sinks(False, _console(tty=True), verbose=False, quiet=False)
    assert run_cmd.approval_prompt_for(sinks, interactive=False) is None


def test_approval_prompt_defaults_to_the_console_prompt() -> None:
    from rayspec.engine.approval import ConsoleApprovalPrompt

    sinks = run_cmd._sinks(False, _console(tty=False), verbose=False, quiet=False)
    prompt = run_cmd.approval_prompt_for(sinks, interactive=True)
    assert isinstance(prompt, run_cmd.SuspendingApprovalPrompt)
    assert isinstance(prompt.inner, ConsoleApprovalPrompt)

# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R1: ``launch_detached`` — the handshake protocol, unit-tested with an injected spawn.

No real subprocess: a fake ``spawn`` stands in for the child and (optionally) writes the
handshake the way a real child would just before acquiring its slot. This pins the launcher's
contract — pre-create the dir, wait for handshake-or-exit with no fixed deadline, print the id,
reflect a launch failure as exit 2 with the log tail — without a wall-clock race.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer
from rich.console import Console

from rayspec.cli._detach import (
    DETACH_LAUNCH_LOG,
    launch_detached,
    write_handshake,
)


class _Fail(Exception):
    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def _fail(message: str, *, hint: str | None = None, code: int = 2) -> None:
    raise _Fail(message, hint)


class FakeChild:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def _run_dir(tmp_path: Path) -> tuple[Path, Path, str]:
    runs_root = tmp_path / "runs"
    run_id = "20260827-120000-abcd"
    return runs_root, runs_root / run_id, run_id


def _spawn_that_handshakes(run_dir: Path, run_id: str, *, pid: int = 4321, queued: bool = False):
    captured: dict[str, Any] = {}

    def spawn(argv: list[str], **kwargs: Any) -> FakeChild:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        write_handshake(run_dir, run_id=run_id, pid=pid, queued=queued)
        return FakeChild(returncode=None)

    return spawn, captured


def test_happy_path_prints_the_run_id_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    runs_root, run_dir, run_id = _run_dir(tmp_path)
    spawn, _captured = _spawn_that_handshakes(run_dir, run_id)
    with pytest.raises(typer.Exit) as exc:
        launch_detached(
            run_id=run_id,
            run_dir=run_dir,
            runs_root=runs_root,
            child_argv=[
                "run",
                "wf.yaml",
                "--quiet",
                "--no-interactive",
                "--detached-child",
                str(run_dir),
            ],
            json_output=False,
            gate_note=None,
            err=Console(file=io.StringIO()),
            fail=_fail,
            spawn=spawn,
            sleep=lambda _s: None,
        )
    assert exc.value.exit_code == 0
    assert capsys.readouterr().out.strip() == run_id
    assert (run_dir / DETACH_LAUNCH_LOG).exists()


def test_json_output_is_a_single_object_with_the_launch_facts(tmp_path: Path, capsys) -> None:
    runs_root, run_dir, run_id = _run_dir(tmp_path)
    spawn, _ = _spawn_that_handshakes(run_dir, run_id, pid=9999)
    with pytest.raises(typer.Exit) as exc:
        launch_detached(
            run_id=run_id,
            run_dir=run_dir,
            runs_root=runs_root,
            child_argv=["run", "wf.yaml"],
            json_output=True,
            gate_note=None,
            err=Console(file=io.StringIO()),
            fail=_fail,
            spawn=spawn,
            sleep=lambda _s: None,
        )
    assert exc.value.exit_code == 0
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj == {
        "run_id": run_id,
        "pid": 9999,
        "run_dir": str(run_dir),
        "launch_log": str(run_dir / DETACH_LAUNCH_LOG),
        "started": True,
    }


def test_child_exit_before_handshake_is_a_launch_failure(tmp_path: Path) -> None:
    runs_root, run_dir, run_id = _run_dir(tmp_path)

    def spawn(argv: list[str], **kwargs: Any) -> FakeChild:
        # writes a boot error to the log the launcher opened on stdout, then "exits" non-zero
        kwargs["stdout"].write("error: could not load workflow 'wf.yaml'\n")
        kwargs["stdout"].flush()
        return FakeChild(returncode=2)

    with pytest.raises(_Fail) as exc:
        launch_detached(
            run_id=run_id,
            run_dir=run_dir,
            runs_root=runs_root,
            child_argv=["run", "wf.yaml"],
            json_output=False,
            gate_note=None,
            err=Console(file=io.StringIO()),
            fail=_fail,
            spawn=spawn,
            sleep=lambda _s: None,
        )
    assert "exited before it started" in exc.value.message
    assert "could not load workflow" in (exc.value.hint or "")
    assert not run_dir.exists(), "a launch that never became a run leaves nothing behind"


def test_a_child_that_wrote_run_json_is_not_cleaned_up(tmp_path: Path) -> None:
    """If the child got far enough to create run.json but died before handshaking, that is a
    real (short-lived) run — the launcher reports the failure but does NOT delete the record."""
    runs_root, run_dir, run_id = _run_dir(tmp_path)

    def spawn(argv: list[str], **kwargs: Any) -> FakeChild:
        (run_dir / "run.json").write_text("{}", encoding="utf-8")
        return FakeChild(returncode=1)

    with pytest.raises(_Fail):
        launch_detached(
            run_id=run_id,
            run_dir=run_dir,
            runs_root=runs_root,
            child_argv=["run", "wf.yaml"],
            json_output=False,
            gate_note=None,
            err=Console(file=io.StringIO()),
            fail=_fail,
            spawn=spawn,
            sleep=lambda _s: None,
        )
    assert (run_dir / "run.json").exists(), "a real run must survive for inspection"


def test_spawn_gets_the_child_command_and_a_hardened_environment(tmp_path: Path) -> None:
    runs_root, run_dir, run_id = _run_dir(tmp_path)
    spawn, captured = _spawn_that_handshakes(run_dir, run_id)
    with pytest.raises(typer.Exit):
        launch_detached(
            run_id=run_id,
            run_dir=run_dir,
            runs_root=runs_root,
            child_argv=["run", "wf.yaml", "--detached-child", str(run_dir)],
            json_output=False,
            gate_note=None,
            err=Console(file=io.StringIO()),
            fail=_fail,
            spawn=spawn,
            sleep=lambda _s: None,
            environ={"PATH": "/usr/bin"},
        )
    argv = captured["argv"]
    assert argv[1:3] == ["-m", "rayspec.cli.app"]
    assert argv[3:5] == ["run", "wf.yaml"]
    kwargs = captured["kwargs"]
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True


def test_a_queued_handshake_still_reports_success(tmp_path: Path, capsys) -> None:
    """A run that will wait for a slot (--wait-slot) hands back queued=True and no run.json yet;
    the launcher must still print the id and exit 0 rather than wait for a run.json."""
    runs_root, run_dir, run_id = _run_dir(tmp_path)
    spawn, _ = _spawn_that_handshakes(run_dir, run_id, queued=True)
    with pytest.raises(typer.Exit) as exc:
        launch_detached(
            run_id=run_id,
            run_dir=run_dir,
            runs_root=runs_root,
            child_argv=["run", "wf.yaml"],
            json_output=False,
            gate_note=None,
            err=Console(file=io.StringIO()),
            fail=_fail,
            spawn=spawn,
            sleep=lambda _s: None,
        )
    assert exc.value.exit_code == 0
    assert capsys.readouterr().out.strip() == run_id
    assert not (run_dir / "run.json").exists()


def test_ctrl_c_while_waiting_hands_back_the_id_and_exits_130(tmp_path: Path, capsys) -> None:
    runs_root, run_dir, run_id = _run_dir(tmp_path)

    def spawn(argv: list[str], **kwargs: Any) -> FakeChild:
        return FakeChild(returncode=None)  # alive, never handshakes

    def sleep(_s: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(typer.Exit) as exc:
        launch_detached(
            run_id=run_id,
            run_dir=run_dir,
            runs_root=runs_root,
            child_argv=["run", "wf.yaml"],
            json_output=False,
            gate_note=None,
            err=Console(file=io.StringIO()),
            fail=_fail,
            spawn=spawn,
            sleep=sleep,
        )
    assert exc.value.exit_code == 130
    assert capsys.readouterr().out.strip() == run_id


def test_gate_note_is_printed_on_success(tmp_path: Path) -> None:
    runs_root, run_dir, run_id = _run_dir(tmp_path)
    spawn, _ = _spawn_that_handshakes(run_dir, run_id)
    buf = io.StringIO()
    with pytest.raises(typer.Exit):
        launch_detached(
            run_id=run_id,
            run_dir=run_dir,
            runs_root=runs_root,
            child_argv=["run", "wf.yaml"],
            json_output=False,
            gate_note="note: this workflow has an approval gate and will pause",
            err=Console(file=buf),
            fail=_fail,
            spawn=spawn,
            sleep=lambda _s: None,
        )
    assert "approval gate" in buf.getvalue()


def test_waits_through_a_slow_start_then_succeeds(tmp_path: Path, capsys) -> None:
    """No fixed deadline while the child is alive: the handshake can arrive after many polls
    (a slow --repo clone), and the launcher keeps waiting rather than flipping to 'did not
    start' the way the old 8 s poll did."""
    runs_root, run_dir, run_id = _run_dir(tmp_path)
    state = {"polls": 0}

    def spawn(argv: list[str], **kwargs: Any) -> FakeChild:
        return FakeChild(returncode=None)

    def sleep(_s: float) -> None:
        state["polls"] += 1
        if state["polls"] == 25:  # the child finally handshakes, well past any short deadline
            write_handshake(run_dir, run_id=run_id, pid=1, queued=False)

    with pytest.raises(typer.Exit) as exc:
        launch_detached(
            run_id=run_id,
            run_dir=run_dir,
            runs_root=runs_root,
            child_argv=["run", "wf.yaml"],
            json_output=False,
            gate_note=None,
            err=Console(file=io.StringIO()),
            fail=_fail,
            spawn=spawn,
            sleep=sleep,
        )
    assert exc.value.exit_code == 0
    assert state["polls"] >= 25
    assert capsys.readouterr().out.strip() == run_id

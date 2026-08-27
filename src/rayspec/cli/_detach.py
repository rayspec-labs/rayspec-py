# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R1: the ``rayspec run --detach`` launcher and its child.

The launcher pre-creates the run's directory, spawns a ``setsid`` child that runs the same
workflow with ``--detach`` swapped for the hidden ``--detached-child <run dir>`` plus
``--quiet --no-interactive``, then waits for the child to hand back a handshake file (or exit),
prints the run id, and exits 0 — the *launch* succeeding, not the workflow's outcome. The child
writes ``detach-handshake.json`` just before it acquires its host slot, so a launcher does not
stall behind ``--wait-slot`` or a slow ``--repo`` clone: it waits only while the child is making
progress (alive), with no fixed deadline (the 8 s poll it replaces flipped legitimate slow
starts to "did not start"). ``child_run_argv`` rebuilds the child's command line from the PARSED
parameters (not ``sys.argv``), so a future ``run`` option cannot be silently dropped — a test
pins that. The child's stdout/stderr are redirected into ``<run dir>/detach-launch.log`` so an
early-boot crash (a bad interpreter, an import error, a validation failure) is captured there;
because the child runs ``--quiet`` that log stays small in practice.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from rich.console import Console

DETACH_LAUNCH_LOG = "detach-launch.log"
DETACH_HANDSHAKE = "detach-handshake.json"
#: hidden option: the launcher hands the child the run directory it pre-created and named.
DETACHED_CHILD_OPT = "--detached-child"
#: how long the tail of the launch log shown on a launch failure may be.
DETACH_LOG_TAIL_BYTES = 4096
DETACH_POLL_INTERVAL_S = 0.02


def child_run_argv(
    *,
    workflow: str,
    inputs: Sequence[str] | None,
    inputs_file: str | None,
    root: str | None,
    dry_run: bool,
    stubs: str | None,
    stubs_from: str | None,
    exec_shell: bool,
    yes: bool,
    approve_class: Sequence[str] | None,
    allow_unsupported: bool,
    fail_fast: bool,
    worktree: bool | None,
    base: str | None,
    locked: bool | None,
    wait_slot: str | None,
    repo: str | None,
    run_dir: Path,
) -> list[str]:
    """The child's ``rayspec run …`` argv, rebuilt from the run command's parsed parameters.

    ``--detach`` / ``--json`` / ``--output`` / ``--verbose`` are not passed (the launcher owns
    detaching and its own output); ``--resume`` / ``--stubs-init`` / ``--force`` never reach here
    (refused with ``--detach``). The child always runs ``--quiet --no-interactive`` and carries
    the hidden ``--detached-child <run dir>``. Every other ``run`` option is threaded verbatim.
    """
    argv: list[str] = ["run", workflow]
    for value in inputs or ():
        argv += ["--input", value]
    if inputs_file is not None:
        argv += ["--inputs-file", inputs_file]
    if root is not None:
        argv += ["--root", root]
    if dry_run:
        argv.append("--dry-run")
    if exec_shell:
        argv.append("--exec-shell")
    if stubs is not None:
        argv += ["--stubs", stubs]
    if stubs_from is not None:
        argv += ["--stubs-from", stubs_from]
    if yes:
        argv.append("--yes")
    for name in approve_class or ():
        argv += ["--approve-class", name]
    if allow_unsupported:
        argv.append("--allow-unsupported")
    if fail_fast:
        argv.append("--fail-fast")
    if worktree is True:
        argv.append("--worktree")
    elif worktree is False:
        argv.append("--no-worktree")
    if base is not None:
        argv += ["--base", base]
    if locked is True:
        argv.append("--locked")
    elif locked is False:
        argv.append("--no-locked")
    if wait_slot is not None:
        argv += ["--wait-slot", wait_slot]
    if repo is not None:
        argv += ["--repo", repo]
    # the child never prompts and never chatters into the launch log
    argv += ["--quiet", "--no-interactive", DETACHED_CHILD_OPT, str(run_dir)]
    return argv


def write_handshake(run_dir: Path, *, run_id: str, pid: int, queued: bool) -> None:
    """The child's proof-of-life to the launcher, written just before it acquires its host slot
    (so ``--wait-slot`` does not stall the launcher). ``queued`` says the run is waiting for a
    slot and its ``run.json`` may not exist yet."""
    from rayspec.store.file import open_private

    path = run_dir / DETACH_HANDSHAKE
    payload = {"run_id": run_id, "pid": pid, "run_dir": str(run_dir), "queued": queued}
    with open_private(path, "w") as fh:
        fh.write(json.dumps(payload) + "\n")


def read_handshake(run_dir: Path) -> dict[str, Any] | None:
    """The child's handshake, or ``None`` when it has not written one yet / it is unreadable."""
    try:
        raw = json.loads((run_dir / DETACH_HANDSHAKE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _log_tail(log_path: Path, *, limit: int = DETACH_LOG_TAIL_BYTES) -> str:
    """The last ``limit`` bytes of the launch log, decoded leniently — what the child printed
    before it died, so a launch failure names the actual cause rather than a bare exit code."""
    try:
        data = log_path.read_bytes()
    except OSError:
        return ""
    tail = data[-limit:]
    return tail.decode("utf-8", "replace").strip()


def _cleanup_failed(run_dir: Path) -> None:
    """Remove a pre-created run directory whose child never created ``run.json`` — a launch that
    never became a run leaves nothing behind. A directory that DID get a ``run.json`` is a real
    (if short-lived) run and is left for inspection."""
    if (run_dir / "run.json").exists():
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(run_dir)


def _await_handshake(
    child: Any,
    run_dir: Path,
    *,
    sleep: Callable[[float], None],
) -> dict[str, Any] | None:
    """Block until the child writes its handshake (success) or exits without one (failure).

    No fixed deadline while the child is alive: the child writes the handshake right before slot
    acquisition, so everything before it — load, validate, prepare a worktree, clone a ``--repo``
    source — is legitimate work we WANT to wait through. A child that exits first is a launch
    failure; the last read closes the race where it writes the handshake then exits immediately.
    """
    while True:
        handshake = read_handshake(run_dir)
        if handshake is not None:
            return handshake
        if child.poll() is not None:
            return read_handshake(run_dir)
        sleep(DETACH_POLL_INTERVAL_S)


def launch_detached(
    *,
    run_id: str,
    run_dir: Path,
    runs_root: Path,
    child_argv: Sequence[str],
    json_output: bool,
    gate_note: str | None,
    err: Console,
    fail: Callable[..., None],
    spawn: Callable[..., Any] = subprocess.Popen,
    sleep: Callable[[float], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Fork a background run: pre-create the run dir, spawn a detached child, wait for its
    handshake, print the run id, exit 0. ``spawn``/``sleep``/``environ`` are injectable for tests.

    The bare run id (text) or a ``{"run_id","pid","run_dir","launch_log","started"}`` object
    (``--json``) goes to stdout via the builtin ``print`` so exactly one clean line lands there;
    a gate note (the run will pause with no ``--yes``/``--approve-class``) goes to ``err``.
    """
    import time

    from rayspec.store.file import open_private, secure_mkdir

    if sleep is None:
        sleep = time.sleep
    secure_mkdir(runs_root)
    # the child accepts a pre-created dir (FileRunStore.create claims run.json with O_EXCL)
    secure_mkdir(run_dir)
    log_path = run_dir / DETACH_LAUNCH_LOG
    handshake_path = run_dir / DETACH_HANDSHAKE
    with contextlib.suppress(FileNotFoundError):
        handshake_path.unlink()  # no stale handshake can pre-satisfy the wait
    env = {
        **(dict(environ) if environ is not None else os.environ),
        "GIT_TERMINAL_PROMPT": "0",  # a private URL must fail fast, never block on a prompt
        "PYTHONUNBUFFERED": "1",  # the launch log captures the child's boot output promptly
    }
    with open_private(log_path, "a") as log_fh:
        child = spawn(
            [sys.executable, "-m", "rayspec.cli.app", *child_argv],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # setsid: no controlling terminal (a gate pauses, R6)
            close_fds=True,
            env=env,
        )
    try:
        handshake = _await_handshake(child, run_dir, sleep=sleep)
    except KeyboardInterrupt:
        # the child keeps running in its own session; hand back the id so it can be followed
        print(run_id)
        raise typer.Exit(code=130) from None
    if handshake is None:
        returncode = getattr(child, "returncode", None)
        tail = _log_tail(log_path)
        _cleanup_failed(run_dir)
        code = f" (exit {returncode})" if returncode is not None else ""
        fail(
            f"the detached run {run_id} exited before it started{code}",
            hint=(f"what it printed:\n{tail}" if tail else f"see {log_path}"),
        )
        return
    if gate_note:
        err.print(gate_note)
    if json_output:
        from rayspec.cli.commands._loader_common import json_line

        # the house renderer (one compact object per line), so `... --json | jq` reads cleanly
        print(
            json_line(
                {
                    "run_id": run_id,
                    "pid": handshake.get("pid"),
                    "run_dir": str(run_dir),
                    "launch_log": str(log_path),
                    "started": True,
                }
            )
        )
    else:
        print(run_id)
    raise typer.Exit(code=0)


__all__ = [
    "DETACHED_CHILD_OPT",
    "DETACH_HANDSHAKE",
    "DETACH_LAUNCH_LOG",
    "child_run_argv",
    "launch_detached",
    "read_handshake",
    "write_handshake",
]

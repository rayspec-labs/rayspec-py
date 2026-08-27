# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R1: the ``rayspec run --detach`` launcher and its child.

The launcher validates the run, pre-creates its directory, spawns a ``setsid`` child that runs
the same workflow with ``--detach`` swapped for the hidden ``--detached-child <run dir>`` and
``--quiet --no-interactive`` added, waits for the child to hand back a handshake (or exit), then
prints the run id and exits 0 — the *launch* succeeding, not the workflow's outcome. The child
writes ``detach-handshake.json`` just before it acquires its host slot, so a launcher does not
stall behind ``--wait-slot``; its console is capped in place so an unwatched run cannot fill the
launch log. ``child_run_argv`` rebuilds the child's command line from the PARSED parameters (not
``sys.argv``), so a future ``run`` option cannot be silently dropped — a test pins that.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DETACH_LAUNCH_LOG = "detach-launch.log"
DETACH_HANDSHAKE = "detach-handshake.json"
#: hidden option: the launcher hands the child the run directory it pre-created and named.
DETACHED_CHILD_OPT = "--detached-child"
#: once the child is alive, wait this long for its ``run.json`` before reporting a launch failure.
DETACH_START_GRACE_S = 5.0
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

    ``--detach`` / ``--json`` / ``--output`` / ``--stubs-init`` / ``--resume`` / ``--force`` are
    not passed (the launcher owns detaching and its own output; the refused-with-detach options
    never reach here). The child always runs ``--quiet --no-interactive`` and carries the hidden
    ``--detached-child <run dir>``. Every other ``run`` option is threaded through verbatim.
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


__all__ = [
    "DETACHED_CHILD_OPT",
    "DETACH_HANDSHAKE",
    "DETACH_LAUNCH_LOG",
    "DETACH_START_GRACE_S",
    "child_run_argv",
    "read_handshake",
    "write_handshake",
]

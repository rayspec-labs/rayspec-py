"""Host-level run slots: the limit holds, a dead holder's slot is free again, waiting works."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from rayspec.limits import RunSlot, SlotBusyError, acquire_slots, slot_dir
from rayspec.limits.policy import limits_for
from rayspec.limits.slots import read_holder, slot_path


def test_slot_files_live_under_the_home(tmp_path: Path) -> None:
    assert slot_dir(tmp_path, "claude") == tmp_path / "limits" / "slots" / "claude"
    assert slot_dir(tmp_path, "we/ird") == tmp_path / "limits" / "slots" / "we_ird"


def test_a_limit_of_two_admits_two_and_refuses_the_third(tmp_path: Path) -> None:
    first = RunSlot(tmp_path, "claude", 2, run_id="r1").acquire()
    second = RunSlot(tmp_path, "claude", 2, run_id="r2").acquire()
    assert {first.index, second.index} == {1, 2}
    with pytest.raises(SlotBusyError) as exc:
        RunSlot(tmp_path, "claude", 2, run_id="r3").acquire()
    assert "claude" in str(exc.value) and "r1" in str(exc.value)
    assert exc.value.hint is not None and "--wait-slot" in exc.value.hint
    first.release()
    third = RunSlot(tmp_path, "claude", 2, run_id="r3").acquire()
    assert third.index == 1
    second.release()
    third.release()


def test_the_holder_file_names_the_run_and_pid(tmp_path: Path) -> None:
    with RunSlot(tmp_path, "codex", 1, run_id="r1") as slot:
        assert slot.held and slot.path is not None
        holder = read_holder(slot_path(tmp_path, "codex", 1), "codex", 1)
        assert holder.run_id == "r1" and holder.pid == os.getpid()
        assert slot_path(tmp_path, "codex", 1).stat().st_mode & 0o777 == 0o600
    assert read_holder(slot_path(tmp_path, "codex", 1), "codex", 1).run_id is None


def test_a_slot_held_by_a_dead_process_is_reclaimed(tmp_path: Path) -> None:
    """No stale-holder detection: the kernel drops the flock when the holder dies."""
    script = tmp_path / "hold.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys, time
            from rayspec.limits import RunSlot
            slot = RunSlot(sys.argv[1], "claude", 1, run_id="zombie").acquire()
            print("held", flush=True)
            time.sleep(300)
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(SlotBusyError) as exc:
            RunSlot(tmp_path, "claude", 1, run_id="mine").acquire()
        assert "zombie" in str(exc.value)
        proc.send_signal(signal.SIGKILL)  # not a clean exit: no release, no cleanup
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on an assertion failure above
            proc.kill()
    # the holder JSON still names the dead run — and the slot is free anyway
    assert read_holder(slot_path(tmp_path, "claude", 1), "claude", 1).run_id == "zombie"
    reclaimed = RunSlot(tmp_path, "claude", 1, run_id="mine").acquire(wait_s=10)
    assert reclaimed.index == 1
    reclaimed.release()


def test_waiting_gives_up_after_the_timeout(tmp_path: Path) -> None:
    held = RunSlot(tmp_path, "claude", 1, run_id="r1").acquire()
    started = time.monotonic()
    with pytest.raises(SlotBusyError) as exc:
        RunSlot(tmp_path, "claude", 1, run_id="r2").acquire(wait_s=0.3, poll_s=0.02)
    assert time.monotonic() - started >= 0.3
    assert "after waiting" in str(exc.value)
    held.release()


def test_waiting_succeeds_once_the_holder_releases(tmp_path: Path) -> None:
    script = tmp_path / "hold.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys, time
            from rayspec.limits import RunSlot
            slot = RunSlot(sys.argv[1], "claude", 1, run_id="other").acquire()
            print("held", flush=True)
            time.sleep(0.5)
            slot.release()
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    slot = RunSlot(tmp_path, "claude", 1, run_id="mine").acquire(wait_s=30, poll_s=0.05)
    assert slot.index == 1
    slot.release()
    assert proc.wait(timeout=30) == 0


def test_acquire_slots_takes_one_per_capped_provider(tmp_path: Path) -> None:
    with acquire_slots(
        tmp_path, ["claude", "codex", "stub"], {"claude": 1, "codex": 2}, run_id="r1"
    ) as held:
        assert [s.provider for s in held] == ["claude", "codex"]
        with pytest.raises(SlotBusyError):
            RunSlot(tmp_path, "claude", 1, run_id="r2").acquire()
    assert RunSlot(tmp_path, "claude", 1, run_id="r2").acquire().index == 1


def test_acquire_slots_releases_everything_when_a_later_one_is_busy(tmp_path: Path) -> None:
    blocker = RunSlot(tmp_path, "codex", 1, run_id="blocker").acquire()
    with pytest.raises(SlotBusyError):
        with acquire_slots(tmp_path, ["claude", "codex"], {"claude": 1, "codex": 1}, run_id="r1"):
            pass  # pragma: no cover - never entered
    assert RunSlot(tmp_path, "claude", 1, run_id="r2").acquire().index == 1  # not left held
    blocker.release()


def test_limits_for_applies_a_wildcard_to_every_provider() -> None:
    assert limits_for({"*": 2}, ["claude", "codex"]) == {"claude": 2, "codex": 2}
    assert limits_for({"*": 2, "codex": 1}, ["claude", "codex"]) == {"claude": 2, "codex": 1}
    assert limits_for({}, ["claude"]) == {}


def test_a_limit_below_one_is_a_programming_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RunSlot(tmp_path, "claude", 0, run_id="r1")

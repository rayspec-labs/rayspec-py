# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R5: the cooperative cancel flag as a file — written like the store writes, cleared by
the run that consumed it, and never a traceback when the run directory is gone."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from rayspec.engine import cancel as cancel_mod
from rayspec.engine.cancel import clear_cancel_flag, read_cancel_flag, write_cancel_flag


def test_write_uses_the_store_tmp_convention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cancel.json.<pid>.<n>.tmp`` → ``os.replace`` — the same shape as ``run.json``, so two
    writers never share a tmp name and a crash never leaves a half-written flag."""
    seen: list[str] = []
    real_replace = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(cancel_mod.os, "replace", spy)
    write_cancel_flag(tmp_path, reason="operator asked")
    assert seen and re.fullmatch(rf"cancel\.json\.{os.getpid()}\.\d+\.tmp", seen[0]), seen
    assert (tmp_path / "cancel.json").exists()
    assert not list(tmp_path.glob("cancel.json.*.tmp"))


def test_flag_file_is_private(tmp_path: Path) -> None:
    write_cancel_flag(tmp_path, reason="x")
    assert stat.S_IMODE((tmp_path / "cancel.json").stat().st_mode) == 0o600


def test_write_raises_when_the_run_dir_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_cancel_flag(tmp_path / "gone", reason="x")


def test_clear_removes_the_flag_and_is_idempotent(tmp_path: Path) -> None:
    write_cancel_flag(tmp_path, reason="x", actor="me")
    assert read_cancel_flag(tmp_path) is not None
    assert clear_cancel_flag(tmp_path) is True
    assert read_cancel_flag(tmp_path) is None
    assert clear_cancel_flag(tmp_path) is False
    assert clear_cancel_flag(tmp_path / "gone") is False


def test_read_tolerates_garbage(tmp_path: Path) -> None:
    (tmp_path / "cancel.json").write_text("{not json", encoding="utf-8")
    assert read_cancel_flag(tmp_path) is None
    (tmp_path / "cancel.json").write_text('{"reason": 7}', encoding="utf-8")
    assert read_cancel_flag(tmp_path) is None

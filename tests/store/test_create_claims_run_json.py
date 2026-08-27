# SPDX-License-Identifier: Apache-2.0
"""``FileRunStore.create()`` claims the run by its ``run.json``, not by its directory: a
launcher may pre-create the directory (to put a launch log in it) before the run exists, and two
creates of one id are still exclusive."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.store.file import RUN_JSON, FileRunStore, RunExistsError, secure_mkdir
from rayspec.store.model import RunRecord


def _record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="w",
        workflow_path="w.yaml",
        workflow_hash="a" * 64,
        project_slug="local/x",
        project_root="/x",
    )


def test_create_accepts_a_pre_created_directory_with_other_files(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "store")
    run = _record("20260827-120000-pre1")
    run_dir = store.run_dir(run.run_id)
    secure_mkdir(run_dir)
    (run_dir / "detach-launch.log").write_text("starting\n", encoding="utf-8")
    store.create(run)
    assert (run_dir / RUN_JSON).exists()
    assert (run_dir / "detach-launch.log").read_text(encoding="utf-8") == "starting\n"
    assert store.load(run.run_id).run_id == run.run_id


def test_create_refuses_when_run_json_exists(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "store")
    run = _record("20260827-120000-dup1")
    store.create(run)
    with pytest.raises(RunExistsError):
        store.create(run)


def test_create_refuses_a_directory_that_already_holds_a_run_json_of_its_own(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "store")
    run = _record("20260827-120000-dup2")
    run_dir = store.run_dir(run.run_id)
    secure_mkdir(run_dir)
    (run_dir / RUN_JSON).write_text("{}", encoding="utf-8")  # planted, not written by the store
    with pytest.raises(RunExistsError):
        store.create(run)

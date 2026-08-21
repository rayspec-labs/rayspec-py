# SPDX-License-Identifier: Apache-2.0
"""``FileRunStore.write_prompt`` — the rendered prompt beside the output.

New store writes go through the store, never through a bare ``open()``: this is the writer
``rayspec explain --full`` reads back, so it must mirror ``write_output`` (private file, durable
replace, run-dir-relative ref).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from rayspec.store.file import FileRunStore


@pytest.fixture
def store(tmp_path: Path) -> FileRunStore:
    return FileRunStore(tmp_path / "store")


def test_write_prompt_returns_a_run_dir_relative_ref_readable_through_read_output(
    store: FileRunStore,
) -> None:
    ref = store.write_prompt("20260820-100000-aaaa", "assess", "Review the diff\n")
    assert ref == "steps/assess/prompt.txt"
    assert store.read_output("20260820-100000-aaaa", ref) == "Review the diff\n"


def test_write_prompt_file_is_private(store: FileRunStore) -> None:
    run_id = "20260820-100000-aaaa"
    ref = store.write_prompt(run_id, "assess", "secretish prompt")
    path = store.run_dir(run_id) / ref
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_prompt_handles_nested_paths_and_rewrites(store: FileRunStore) -> None:
    run_id = "20260820-100000-aaaa"
    ref = store.write_prompt(run_id, "build[2]/implement", "attempt one")
    assert ref == "steps/build[2]/implement/prompt.txt"
    again = store.write_prompt(run_id, "build[2]/implement", "attempt two")
    assert again == ref
    assert store.read_output(run_id, ref) == "attempt two"


def test_write_prompt_rejects_an_empty_step_path(store: FileRunStore) -> None:
    with pytest.raises(ValueError):
        store.write_prompt("20260820-100000-aaaa", "", "x")

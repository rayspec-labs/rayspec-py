# SPDX-License-Identifier: Apache-2.0
"""Docs-as-tests: every fenced YAML block of the docs is either checked or explained.

Boundary: the pytest half of the marker convention that lives in ``scripts/check_examples.py``
(``find_doc_blocks`` / ``doc_block_problems`` / ``check_doc_block``) — the same entry point CI
runs as ``check_examples.py --docs``. A block whose fence says ``rayspec:validate`` is loaded and
validated like ``rayspec validate`` does; ``rayspec:run`` additionally drives it through
``rayspec run --dry-run``; a block with neither marker must carry a one-line
``<!-- rayspec:skip … -->`` reason, so "nobody checks this snippet" is always a decision somebody
wrote down rather than an oversight.

No network and no provider: a dry run replaces every agent with the scripted stub.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_examples.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_examples", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


check_examples = _load_script()

BLOCKS: list[Any] = check_examples.find_doc_blocks(REPO_ROOT)
CHECKED: list[Any] = [b for b in BLOCKS if b.marker is not None]


def test_the_docs_have_yaml_blocks_to_check() -> None:
    """A parametrised suite that silently collects nothing is not a suite."""
    assert len(BLOCKS) >= 30, f"only {len(BLOCKS)} fenced yaml blocks found — parser broken?"
    assert len(CHECKED) >= 10, f"only {len(CHECKED)} marked blocks: {[b.id for b in CHECKED]}"


def test_every_yaml_block_is_marked_or_explained() -> None:
    """Totality: no fenced yaml block may be silently unchecked."""
    problems = check_examples.doc_block_problems(BLOCKS)
    assert not problems, "\n".join(problems)


def test_no_marker_is_stranded() -> None:
    """A marker that sits above no fenced yaml block is a check nobody runs."""
    assert not check_examples.stray_doc_markers(REPO_ROOT)


def test_the_workflow_snippets_readers_copy_are_run_checked() -> None:
    """The blocks a reader is most likely to paste are the ones that must actually run."""
    run_checked = {
        (b.source, check_examples.doc_workflow_name(b)) for b in BLOCKS if b.marker == "run"
    }
    assert ("README.md", "fix_issue") in run_checked, sorted(run_checked)
    assert ("docs/examples.md", "review") in run_checked, sorted(run_checked)


@pytest.mark.parametrize("block", CHECKED, ids=lambda b: b.id)
def test_marked_block_still_works(block: Any, tmp_path: Path) -> None:
    problems = check_examples.check_doc_block(block, home=tmp_path)
    assert not problems, f"{block.id}\n" + "\n".join(problems)


def _marked(marker: str) -> Any:
    return next(b for b in CHECKED if b.marker == marker)


def test_a_broken_snippet_fails_validation(tmp_path: Path) -> None:
    """The gate notices a documented snippet that stopped loading."""
    block = _marked("validate")
    broken = dataclasses.replace(block, text=block.text + "\n  - id: 4nvalid\n    shell: 'x'\n")
    assert check_examples.check_doc_block(broken, home=tmp_path)


def test_a_snippet_that_validates_but_cannot_run_fails(tmp_path: Path) -> None:
    """``rayspec:run`` catches what ``rayspec:validate`` cannot: the block runs, not just parses."""
    block = _marked("run")
    broken = dataclasses.replace(
        block, text=block.text.replace("steps.fetch.output", "steps.fetch.output.verdict")
    )
    assert (
        check_examples.check_doc_block(
            dataclasses.replace(broken, marker="validate"), home=tmp_path
        )
        == []
    ), "the corrupted block must still VALIDATE — otherwise this proves nothing"
    assert check_examples.check_doc_block(broken, home=tmp_path)

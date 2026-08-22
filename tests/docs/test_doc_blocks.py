# SPDX-License-Identifier: Apache-2.0
"""Docs-as-tests: every fenced YAML block of the docs is either checked or explained.

Boundary: the pytest half of the marker convention that lives in ``scripts/check_examples.py``
(``find_doc_blocks`` / ``doc_block_problems`` / ``check_doc_block``) — the same entry point CI
runs as ``check_examples.py --docs``. A block carrying ``rayspec:validate`` on the line above it
is loaded and validated like ``rayspec validate`` does; ``rayspec:run`` additionally drives it through
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
    """Load ``check_examples.py`` under a key of this module's own.

    ``tests/examples`` loads the same file, and a dataclass resolves its string annotations
    through ``sys.modules[__name__]`` — so two loaders sharing one key would leave whichever ran
    second owning it, and the other copy's ``DocBlock``/``Case`` annotations resolving against a
    different module object depending on collection order.
    """
    spec = importlib.util.spec_from_file_location("check_examples_docs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


check_examples = _load_script()

BLOCKS: list[Any] = check_examples.find_doc_blocks(REPO_ROOT)
CHECKED: list[Any] = [b for b in BLOCKS if b.marker is not None]


def test_the_loaded_script_owns_its_module_key() -> None:
    """Two suites load this script; each must keep its own ``sys.modules`` entry.

    ``DocBlock`` resolves its string annotations through ``sys.modules[DocBlock.__module__]``, so
    a key shared with ``tests/examples`` would make the answer depend on collection order.
    """
    assert sys.modules[check_examples.__name__] is check_examples


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


def _page(tmp_path: Path, body: str) -> Path:
    """A throwaway repo root whose only documentation page is ``README.md``."""
    (tmp_path / "README.md").write_text(body, encoding="utf-8")
    return tmp_path


def test_a_tilde_fence_is_a_fenced_yaml_block_too(tmp_path: Path) -> None:
    """``~~~yaml`` is the same block to every markdown renderer, so it is to the gate."""
    root = _page(tmp_path, "~~~yaml\nsteps: []\n~~~\n")
    blocks = check_examples.find_doc_blocks(root)
    assert [b.line for b in blocks] == [1], blocks
    assert check_examples.doc_block_problems(blocks)


def test_the_fence_language_is_matched_case_insensitively(tmp_path: Path) -> None:
    """`````YAML`` renders as YAML; a gate that only knows lower case is off by a keystroke."""
    root = _page(tmp_path, "```YAML\nsteps: []\n```\n")
    blocks = check_examples.find_doc_blocks(root)
    assert [b.line for b in blocks] == [1], blocks
    assert check_examples.doc_block_problems(blocks)


def test_a_block_inside_a_longer_fence_is_not_a_block_of_its_own(tmp_path: Path) -> None:
    """A four-backtick wrapper quotes its contents — the inner fence is prose, not a snippet."""
    root = _page(
        tmp_path,
        "````markdown\n<!-- rayspec:run -->\n```yaml\nsteps: []\n```\n````\n",
    )
    assert check_examples.find_doc_blocks(root) == []
    assert check_examples.stray_doc_markers(root) == []


def test_a_marker_separated_from_its_fence_is_stranded(tmp_path: Path) -> None:
    """The rule is "the line above"; a blank line in between means the marker binds to nothing."""
    root = _page(tmp_path, "<!-- rayspec:run -->\n\n```yaml\nsteps: []\n```\n")
    assert check_examples.stray_doc_markers(root) == [
        "README.md:1: rayspec marker is not above a fenced block"
    ]
    assert check_examples.doc_block_problems(check_examples.find_doc_blocks(root))

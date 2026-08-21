# SPDX-License-Identifier: Apache-2.0
"""No unresolved merge-conflict marker may reach a commit.

This exists because three of them did. A merge left ``<<<<<<< HEAD`` / ``=======`` /
``>>>>>>> origin/main`` in ``docs/cli.md``; ``scripts/gen_skill.py`` faithfully copied the hunk
into both packaged copies of the CLI reference; and the whole gate stayed green, because nothing
looked for them. They shipped inside the wheel, so every project scaffolded from it got a skill
file with a conflict hunk in the middle of the ``rayspec plan`` section.

Every other guard in this directory checks that a documented claim is *true*. This one only checks
that the file is a file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Anchored at the start of a line, which is the only place git writes them.
MARKERS = ("<<<<<<< ", ">>>>>>> ")
#: ``=======`` needs an exact match: a markdown setext underline is also a run of ``=``.
SEPARATOR = "======="

#: Text suffixes worth scanning; a binary file cannot carry a marker that would survive anyway.
SUFFIXES = frozenset({".md", ".py", ".yaml", ".yml", ".json", ".toml", ".txt", ".cfg", ".sh"})


def tracked_files() -> list[Path]:
    """Every file git tracks — exactly the set that can reach a commit."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


def test_no_unresolved_conflict_markers() -> None:
    offenders: list[str] = []
    for path in tracked_files():
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - not a text file after all
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.startswith(MARKERS) or line.rstrip() == SEPARATOR:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line[:40]}")
    assert not offenders, "unresolved merge-conflict markers:\n" + "\n".join(offenders)

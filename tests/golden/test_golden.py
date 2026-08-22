"""The golden run corpus: the ``--json`` stream, the summary object and ``run.json`` of every case.

Accidental changes to those three shapes are invisible to unit tests and break the things that
consume them — scripts reading the JSONL stream, alternative sinks, ``resume`` reading old
records. Every runnable case of every example (and of the repo's own workflows) is replayed as
``rayspec run --dry-run --json --stubs …``, masked (see ``_capture.py``) and diffed against the
committed corpus under ``tests/golden/<suite>/<case>/``.

Regenerate after an intentional change and read the diff as the record of what changed::

    RAYSPEC_UPDATE_GOLDEN=1 uv run pytest tests/golden -q

The captured stream is grouped by step first (``_capture.canonical_order``): the order within a
step is the engine's and is compared, the order between steps that ran at the same time is the
scheduler's and is not.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

import pytest

from rayspec.testing.spec import Case, CaseFileError, Suite, discover_suites

from ._capture import canonical_order, capture

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = Path(__file__).resolve().parent
#: The three files captured per case (``_capture.capture`` returns exactly these keys).
FILES = ("events.jsonl", "summary.json", "run.json")
UPDATE = os.environ.get("RAYSPEC_UPDATE_GOLDEN") == "1"
UPDATE_HINT = "regenerate with: RAYSPEC_UPDATE_GOLDEN=1 uv run pytest tests/golden -q"

#: How many cases must have a committed corpus — a deleted corpus is a red test, a *new* case
#: without one (several branches add cases in parallel) is a skip, not a tripwire for everyone.
MINIMUM_COVERED = 28


def discover(root: Path) -> tuple[list[Suite], str | None]:
    """``(suites, error)`` — a malformed case file anywhere in the repo must not break collection.

    Discovery runs at import time to parametrise this module. Raising here would turn one broken
    file into a *collection* error for the whole test session; the error is carried instead and
    asserted by :func:`test_case_discovery_succeeds`.
    """
    try:
        return discover_suites(root), None
    except CaseFileError as exc:
        return [], str(exc)


SUITES, DISCOVERY_ERROR = discover(REPO_ROOT)
#: Only cases that actually run: a ``validate: error`` case never reaches the engine.
CASES = [
    (suite, case)
    for suite in SUITES
    for case in suite.checks
    if case.run and case.validate_ == "ok"
]
IDS = [f"{suite.name}:{case.id}" for suite, case in CASES]


def golden_dir(suite: Suite, case: Case) -> Path:
    """Where the corpus of one case lives (a suite name may contain ``/``)."""
    return GOLDEN_DIR / suite.name.replace("/", "-") / case.id


def _diff(expected: str, actual: str, label: str) -> str:
    lines = list(
        difflib.unified_diff(
            expected.splitlines(), actual.splitlines(), "golden", "captured", lineterm=""
        )
    )
    shown = "\n".join(lines[:40])
    if len(lines) > 40:
        shown += f"\n… ({len(lines) - 40} more diff lines)"
    return f"{label} drifted from the golden corpus:\n{shown}\n{UPDATE_HINT}"


def test_case_discovery_succeeds() -> None:
    """A malformed case file is one failing test here, not a collection error."""
    assert DISCOVERY_ERROR is None, DISCOVERY_ERROR


def test_the_corpus_is_not_empty() -> None:
    """A deleted corpus directory is caught here; a brand-new case without one only skips."""
    covered = [
        ids
        for ids, (suite, case) in zip(IDS, CASES, strict=True)
        if golden_dir(suite, case).is_dir()
    ]
    assert len(covered) >= MINIMUM_COVERED, sorted(set(IDS) - set(covered))


@pytest.mark.parametrize(("suite", "case"), CASES, ids=IDS)
def test_golden(suite: Suite, case: Case, home: Path, tmp_path: Path) -> None:
    captured = capture(suite, case, home=home, tmp_path=tmp_path)
    assert set(captured) == set(FILES)
    dest = golden_dir(suite, case)
    if UPDATE:
        dest.mkdir(parents=True, exist_ok=True)
        for name, text in captured.items():
            (dest / name).write_text(text, encoding="utf-8")
        return
    if not dest.is_dir():
        pytest.skip(f"no golden corpus for {suite.name}:{case.id} yet — {UPDATE_HINT}")
    for name in FILES:
        path = dest / name
        assert path.is_file(), f"{path} is missing — {UPDATE_HINT}"
        expected = path.read_text(encoding="utf-8")
        assert expected == captured[name], _diff(expected, captured[name], str(path.name))


def _event(step_path: str | None, marker: str) -> dict[str, object]:
    return {"type": "stream", "step_path": step_path, "marker": marker}


def test_the_corpus_does_not_pin_the_interleaving_of_concurrent_steps() -> None:
    """Sibling steps under `max_parallel:` finish in whatever order the event loop wakes them,
    so two runs of the same case interleave differently on the same machine. Comparing that
    would make the corpus red at random, and the diff would say nothing about the change."""
    started, finished = _event(None, "run.started"), _event(None, "run.finished")
    a = _event("api", "1"), _event("docs", "1"), _event("api", "2"), _event("docs", "2")
    b = a[1], a[0], a[3], a[2]  # the same records, the other way round
    assert canonical_order([started, *a, finished]) == canonical_order([started, *b, finished])
    assert canonical_order([started, *a, finished])[0] is started
    assert canonical_order([started, *a, finished])[-1] is finished


def test_the_corpus_still_pins_the_order_within_one_step() -> None:
    """What one step emitted, in the order it emitted it, is deterministic and is compared."""
    one, two = _event("api", "1"), _event("api", "2")
    assert canonical_order([one, two]) != canonical_order([two, one])


@pytest.mark.skipif(UPDATE, reason="the corpus is being regenerated")
def test_the_corpus_has_no_stale_entries() -> None:
    """A renamed or deleted case must not leave a directory behind."""
    expected = {golden_dir(suite, case) for suite, case in CASES}
    found = {p.parent for p in GOLDEN_DIR.rglob("run.json")}
    stale = sorted(str(p.relative_to(GOLDEN_DIR)) for p in found - expected)
    assert not stale, f"stale golden directories: {stale} — {UPDATE_HINT}"


@pytest.mark.skipif(UPDATE, reason="the corpus is being regenerated")
def test_masking_leaves_nothing_machine_specific() -> None:
    """No absolute path, run id, timestamp or hostname may survive into a committed file."""
    import re
    import socket

    offenders: list[str] = []
    patterns = [
        (re.compile(r"/(Users|home|private|var|tmp)/"), "absolute path"),
        (re.compile(r"\b\d{8}-\d{6}-[0-9a-z]{4}\b"), "run id"),
        (re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"), "timestamp"),
        (re.compile(re.escape(socket.gethostname())), "hostname"),
        # RunRecord.toolchain leaks the machine unless masked:
        # an OS/arch string, an interpreter version, or a semver where a version was recorded.
        (re.compile(r"\b(macOS|Linux|Windows|Darwin)-[\w.\-]+"), "platform string"),
        (re.compile(r'"python":\s*"\d+\.\d+'), "python version"),
        (re.compile(r'"(rayspec|sdk_version|cli_version)":\s*"\d+\.\d+'), "toolchain version"),
    ]
    for path in sorted(GOLDEN_DIR.rglob("*.json*")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, what in patterns:
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(GOLDEN_DIR)}:{lineno}: {what}")
    assert not offenders, offenders[:20]

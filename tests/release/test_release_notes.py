"""``scripts/release_notes.py`` turns one CHANGELOG section into the notes of one tag.

Boundary: text in, text out. The release workflow runs this script instead of hand-copying the
section, so the failure that matters is a tag whose version has no section — it must stop the
release before anything is published, not produce empty notes.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release_notes.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

SAMPLE = """# Changelog

Preamble that belongs to no version.

## [Unreleased]

### Added
- something not released yet

## [2.1.0] — 2026-09-01

### Added
- a thing

### Fixed
- another thing

## [2.0.0] — 2026-08-01

### Added
- the old thing

[2.1.0]: https://example.invalid/releases/tag/v2.1.0
"""


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release_notes = _load_script()


def test_the_notes_are_the_section_of_that_version() -> None:
    """The heading itself is dropped — GitHub renders the tag above the body."""
    notes = release_notes.notes_for("2.1.0", SAMPLE)
    assert notes.startswith("### Added")
    assert "- a thing" in notes
    assert "- another thing" in notes


def test_the_notes_stop_at_the_next_version() -> None:
    notes = release_notes.notes_for("2.1.0", SAMPLE)
    assert "the old thing" not in notes
    assert "not released yet" not in notes
    assert "## [" not in notes


def test_the_link_reference_of_the_version_is_not_part_of_the_notes() -> None:
    """The trailing ``[2.1.0]: …`` line is changelog plumbing, not a release note."""
    assert "https://example.invalid" not in release_notes.notes_for("2.1.0", SAMPLE)


def test_a_leading_v_is_accepted_because_that_is_what_the_tag_says() -> None:
    assert release_notes.notes_for("v2.1.0", SAMPLE) == release_notes.notes_for("2.1.0", SAMPLE)


def test_a_version_with_no_section_is_refused() -> None:
    """The release must stop here rather than publish a wheel with empty notes."""
    with pytest.raises(release_notes.NoSuchVersion) as excinfo:
        release_notes.notes_for("9.9.9", SAMPLE)
    assert "9.9.9" in str(excinfo.value)


def test_unreleased_is_not_a_version() -> None:
    with pytest.raises(release_notes.NoSuchVersion):
        release_notes.notes_for("Unreleased", SAMPLE)


def test_an_empty_section_is_refused() -> None:
    """A heading with nothing under it is the same failure as no heading at all."""
    with pytest.raises(release_notes.NoSuchVersion):
        release_notes.notes_for("3.0.0", "# Changelog\n\n## [3.0.0]\n\n## [2.0.0]\n\n- old\n")


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_cli_prints_the_notes_of_a_tag(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(SAMPLE, encoding="utf-8")
    done = _run("v2.1.0", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "- a thing" in done.stdout


def test_the_cli_writes_a_file_when_asked(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(SAMPLE, encoding="utf-8")
    out = tmp_path / "notes.md"
    done = _run("2.1.0", "--output", str(out), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "- another thing" in out.read_text(encoding="utf-8")


def test_the_cli_exits_two_and_names_the_missing_version(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(SAMPLE, encoding="utf-8")
    done = _run("v9.9.9", cwd=tmp_path)
    assert done.returncode == 2
    assert "9.9.9" in done.stderr
    assert not done.stdout.strip(), "nothing may reach stdout when the notes are unknown"


def test_a_missing_changelog_exits_two(tmp_path: Path) -> None:
    done = _run("1.0.0", cwd=tmp_path)
    assert done.returncode == 2
    assert "CHANGELOG.md" in done.stderr


def test_the_changelog_has_notes_for_the_version_that_would_be_released() -> None:
    """Release-day guard, run on every pull request.

    The version in ``pyproject.toml`` is what a tag would publish; if the changelog has no
    section for it, the release workflow stops — better to learn that here than at the tag.
    """
    import tomllib

    meta = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = meta["project"]["version"]
    notes = release_notes.notes_for(version, CHANGELOG.read_text(encoding="utf-8"))
    assert notes.strip(), f"CHANGELOG.md has no notes for {version}"

"""The workflow library bundled with rayspec (``src/rayspec/workflows/defaults/``), exercised the
way a fresh ``pip install rayspec`` meets it: a project root with **no** ``.rayspec/`` at all.

``checks.yaml`` + ``stubs/`` next to this file are the offline suite; they are copied into a
temporary root and driven through ``rayspec test`` so every case resolves its workflow from the
bundled copy (and, in the last test, from an ejected copy that must behave the same).
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec import __version__
from rayspec.cli.app import app
from rayspec.loader import load_workflow, validate_workflow
from rayspec.loader.bundled import (
    BUNDLED_LABEL_PREFIX,
    bundled_digest,
    bundled_dir,
    render_ejected,
)
from rayspec.providers.capabilities import BUILTIN_CAPABILITIES
from rayspec.testing import load_checks

HERE = Path(__file__).resolve().parent
BUNDLED = sorted(p.stem for p in bundled_dir().glob("*.yaml"))
runner = CliRunner()


@pytest.fixture
def empty_root(tmp_path: Path, home: Path) -> Path:
    """A project root without a ``.rayspec/`` — ``home`` exports a fresh ``RAYSPEC_HOME``."""
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _copy_suite(root: Path) -> None:
    shutil.copy(HERE / "checks.yaml", root / "checks.yaml")
    shutil.copytree(HERE / "stubs", root / "stubs")


def test_the_library_is_the_v1_set() -> None:
    assert BUNDLED == [
        "fix_issue",
        "pr_review",
        "release_check",
        "resolve_conflicts",
        "review_block",
    ]


def test_rayspec_test_passes_with_only_the_bundled_set(empty_root: Path) -> None:
    """Acceptance: `rayspec test --junit` passes with the bundled set exercised offline."""
    _copy_suite(empty_root)
    junit = empty_root / "junit.xml"
    res = runner.invoke(app, ["test", "--root", str(empty_root), "--junit", str(junit)])
    assert res.exit_code == 0, res.output
    suites = ET.parse(junit).getroot()
    assert suites.get("tests") == str(len(load_checks(HERE / "checks.yaml")))
    assert suites.get("failures") == "0" and suites.get("errors", "0") == "0"
    assert not (empty_root / ".rayspec").exists()  # nothing was scaffolded on the way


def test_pr_review_dry_runs_in_a_fresh_git_repo_without_a_rayspec_dir(empty_root: Path) -> None:
    """Acceptance: `cd <git repo> && rayspec run pr_review --input pr=1 --dry-run`, no stubs."""
    subprocess.run(["git", "init", "-q"], cwd=empty_root, check=True)
    res = runner.invoke(
        app, ["run", "pr_review", "--input", "pr=1", "--dry-run", "--root", str(empty_root)]
    )
    assert res.exit_code == 0, res.output
    assert "verdict" in res.output and "approve" in res.output


@pytest.mark.parametrize("name", BUNDLED)
def test_each_bundled_workflow_is_clean_self_contained_and_stable(
    name: str, empty_root: Path, home: Path, tmp_path: Path
) -> None:
    rw = load_workflow(name, project_root=empty_root, home=home)
    assert rw.workflow.name == name
    assert rw.label == f"{BUNDLED_LABEL_PREFIX}{name}.yaml"
    # self-contained: no prompt file, agent file or include outside the library
    assert all(p.is_relative_to(bundled_dir()) for p in rw.source_files), rw.source_files
    report = validate_workflow(
        rw, capabilities_for=BUILTIN_CAPABILITIES.get, provider_ids=sorted(BUILTIN_CAPABILITIES)
    )
    assert report.errors == [], report.errors
    assert report.warnings == [] and rw.warnings == [], (report.warnings, rw.warnings)
    # the hash does not depend on which project loaded it
    other = tmp_path / "other"
    other.mkdir()
    assert load_workflow(name, project_root=other, home=home).hash == rw.hash


def test_include_prefers_a_project_review_block(empty_root: Path, home: Path) -> None:
    """R2: `include:` inside a bundled workflow resolves a project-local override."""
    rw = load_workflow("pr_review", project_root=empty_root, home=home)
    assert rw.includes["review"].path == bundled_dir() / "review_block.yaml"
    local = empty_root / ".rayspec" / "workflows"
    local.mkdir(parents=True)
    shutil.copy(bundled_dir() / "review_block.yaml", local / "review_block.yaml")
    overridden = load_workflow("pr_review", project_root=empty_root, home=home)
    assert overridden.includes["review"].path == local / "review_block.yaml"
    assert overridden.hash != rw.hash


def test_an_ejected_copy_passes_the_same_cases(empty_root: Path) -> None:
    """Acceptance: `rayspec workflows eject pr_review` produces a file that runs identically."""
    _copy_suite(empty_root)
    source = bundled_dir() / "pr_review.yaml"
    target = empty_root / ".rayspec" / "workflows" / "pr_review.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        render_ejected(
            "pr_review",
            source.read_text(encoding="utf-8"),
            version=__version__,
            digest=bundled_digest(source),
        ),
        encoding="utf-8",
    )
    res = runner.invoke(app, ["test", "pr_review", "--root", str(empty_root)])
    assert res.exit_code == 0, res.output
    assert "pr_default_stubs" in res.output

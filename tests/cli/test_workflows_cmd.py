"""`rayspec workflows` / `rayspec validate` on an empty project + the docs-URL helper."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli._docs import DOCS_BASE, docs_url
from rayspec.cli.app import app

runner = CliRunner()


@pytest.fixture
def empty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    root = tmp_path / "proj"
    root.mkdir()
    return root


def test_docs_url_builds_a_full_github_url() -> None:
    assert DOCS_BASE.startswith("https://github.com/rayspec-labs/rayspec-py/blob/main/")
    assert docs_url("docs/providers.md#pricing") == DOCS_BASE + "docs/providers.md#pricing"
    assert docs_url("/docs/cli.md") == DOCS_BASE + "docs/cli.md"
    assert docs_url("examples/") == DOCS_BASE + "examples/"


def test_workflows_empty_project_hint_names_init_and_a_url(empty_project: Path) -> None:
    res = runner.invoke(app, ["workflows", "--root", str(empty_project)])
    assert res.exit_code == 0, res.output
    assert "no project workflows yet" in res.output
    assert "bundled" in res.output and "workflows eject" in res.output
    assert "rayspec init" in res.output
    assert docs_url("docs/examples.md") in res.output
    # repo-relative paths a tool-installed user does not have must not appear bare
    assert "see docs/examples.md" not in res.output
    assert "examples/ gallery" not in res.output


def test_validate_empty_project_hint_names_init(empty_project: Path) -> None:
    res = runner.invoke(app, ["validate", "--root", str(empty_project)])
    assert res.exit_code == 0, res.output
    assert "no workflows found" in res.output
    assert "rayspec init" in res.output
    res = runner.invoke(app, ["validate", "--json", "--root", str(empty_project)])
    assert res.exit_code == 0
    assert res.output.strip() == "[]"


def test_no_user_facing_hint_cites_a_bare_repo_relative_path() -> None:
    """Every CLI hint that points at docs uses ``docs_url`` (a full URL) or ``--help``."""
    import re

    src = Path(__file__).resolve().parents[2] / "src" / "rayspec"
    offenders: list[str] = []
    bare = re.compile(r"""["'][^"']*\bsee docs/""")
    for path in src.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if bare.search(line) and "docs_url(" not in line:
                offenders.append(f"{path.relative_to(src)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)

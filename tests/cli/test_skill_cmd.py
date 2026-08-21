"""`rayspec skill install [--global] [--force] [--root]`, `rayspec skill show`, `rayspec skill path`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec import __version__
from rayspec.cli.app import app
from rayspec.skill import content_digest, skill_dir, skill_files


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RAYSPEC_HOME", str(home / ".rayspec"))
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    return root


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_install_writes_the_project_skill_and_is_idempotent(fake_home: Path, project: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(project)])
    assert res.exit_code == 0, res.output
    target = project / ".claude" / "skills" / "rayspec"
    assert _files(target) == {rel for rel, _ in skill_files()}
    assert (target / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: rayspec\n")
    assert str(target) in res.output
    assert "fresh Claude Code session" in res.output
    # second run: nothing overwritten
    (target / "SKILL.md").write_text("edited\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "exists" in res.output and "--force" in res.output
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "edited\n"
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(project), "--force"])
    assert res.exit_code == 0, res.output
    assert (target / "SKILL.md").read_text(encoding="utf-8") != "edited\n"
    assert "overwrote" in res.output


def test_install_defaults_to_the_project_root_found_from_the_cwd(
    fake_home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project / "src")
    res = CliRunner().invoke(app, ["skill", "install"])
    assert res.exit_code == 0, res.output
    assert (project / ".claude" / "skills" / "rayspec" / "SKILL.md").is_file()
    assert not (project / "src" / ".claude").exists()


def test_install_global_writes_under_the_home_directory(fake_home: Path, project: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "install", "--global"])
    assert res.exit_code == 0, res.output
    target = fake_home / ".claude" / "skills" / "rayspec"
    assert (target / "SKILL.md").is_file()
    assert (target / "references" / "schema.md").is_file()
    assert str(target) in res.output
    assert not (project / ".claude").exists()


def test_install_root_that_is_a_file_is_a_usage_error(fake_home: Path, tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x")
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(not_a_dir)])
    assert res.exit_code == 2, res.output
    assert "error:" in res.output and "Traceback" not in res.output


def test_install_directory_at_skill_md_is_a_clean_error(fake_home: Path, project: Path) -> None:
    (project / ".claude" / "skills" / "rayspec" / "SKILL.md").mkdir(parents=True)
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "error:" in res.output and "Traceback" not in res.output


def test_show_reports_packaged_project_and_global_state(fake_home: Path, project: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "show", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert __version__ in res.output and content_digest() in res.output
    assert str(skill_dir()) in res.output
    assert "not installed" in res.output
    CliRunner().invoke(app, ["skill", "install", "--root", str(project)])
    res = CliRunner().invoke(app, ["skill", "show", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "up to date" in res.output
    (project / ".claude" / "skills" / "rayspec" / "SKILL.md").write_text("old\n")
    res = CliRunner().invoke(app, ["skill", "show", "--root", str(project)])
    assert "differs" in res.output and "--force" in res.output


def test_show_json_shape(fake_home: Path, project: Path) -> None:
    CliRunner().invoke(app, ["skill", "install", "--global"])
    res = CliRunner().invoke(app, ["skill", "show", "--root", str(project), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert set(data) == {"packaged", "project", "global"}
    assert data["packaged"] == {
        "path": str(skill_dir()),
        "rayspec_version": __version__,
        "digest": content_digest(),
        "files": [rel for rel, _ in skill_files()],
    }
    assert data["project"]["state"] == "missing" and data["project"]["digest"] is None
    assert data["global"]["state"] == "current"
    assert data["global"]["digest"] == content_digest()
    assert data["global"]["path"] == str(fake_home / ".claude" / "skills" / "rayspec")


def test_path_prints_the_packaged_directory(fake_home: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "path"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == str(skill_dir())
    assert (Path(res.stdout.strip()) / "SKILL.md").is_file()


def test_install_global_and_root_are_mutually_exclusive(fake_home: Path, project: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "install", "--global", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "error:" in res.output and "mutually exclusive" in res.output
    assert not (fake_home / ".claude").exists()
    assert not (project / ".claude").exists()

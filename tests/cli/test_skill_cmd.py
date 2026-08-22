"""`rayspec skill install|show|path [NAME] [--global] [--force] [--root]`.

Every subcommand takes an optional skill name: no name acts on **all** packaged skills, a name on
that one, an unknown name is a usage error with a did-you-mean.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec import __version__
from rayspec.cli.app import app
from rayspec.skill import SKILL_NAMES, SKILLS, content_digest, skill_dir, skill_files


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


def test_install_writes_every_project_skill_and_is_idempotent(
    fake_home: Path, project: Path
) -> None:
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(project)])
    assert res.exit_code == 0, res.output
    for one in SKILLS:
        target = project / ".claude" / "skills" / one.name
        assert _files(target) == {rel for rel, _ in skill_files(one)}, one.name
        assert (
            (target / "SKILL.md").read_text(encoding="utf-8").startswith(f"---\nname: {one.name}\n")
        )
        assert str(target) in res.output
    assert "fresh Claude Code session" in res.output
    # second run: nothing overwritten
    edited = project / ".claude" / "skills" / SKILL_NAMES[0] / "SKILL.md"
    edited.write_text("edited\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "exists" in res.output and "--force" in res.output
    assert edited.read_text(encoding="utf-8") == "edited\n"
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(project), "--force"])
    assert res.exit_code == 0, res.output
    assert edited.read_text(encoding="utf-8") != "edited\n"
    assert "overwrote" in res.output


def test_install_a_named_skill_writes_only_that_one(fake_home: Path, project: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "install", "rayspec-cli", "--root", str(project)])
    assert res.exit_code == 0, res.output
    skills_dir = project / ".claude" / "skills"
    assert {p.name for p in skills_dir.iterdir()} == {"rayspec-cli"}
    assert (skills_dir / "rayspec-cli" / "references" / "cli.md").is_file()


def test_an_unknown_skill_name_is_a_usage_error_that_lists_the_real_ones(
    fake_home: Path, project: Path
) -> None:
    for args in (["install", "--root", str(project)], ["show", "--root", str(project)], ["path"]):
        res = CliRunner().invoke(app, ["skill", args[0], "nope", *args[1:]])
        assert res.exit_code == 2, (args, res.output)
        assert "error:" in res.output and "Traceback" not in res.output
        assert "rayspec-workflows" in res.output and "rayspec-cli" in res.output
    assert not (project / ".claude").exists()


def test_the_retired_single_skill_name_is_not_an_alias(fake_home: Path, project: Path) -> None:
    """1.0.0 has never been published, so `rayspec` is simply not a skill any more — it must be
    a clean usage error, not a silent install of one of the two."""
    res = CliRunner().invoke(app, ["skill", "install", "rayspec", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "unknown skill 'rayspec'" in res.output
    assert not (project / ".claude").exists()


def test_install_defaults_to_the_project_root_found_from_the_cwd(
    fake_home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project / "src")
    res = CliRunner().invoke(app, ["skill", "install"])
    assert res.exit_code == 0, res.output
    for one in SKILLS:
        assert (project / ".claude" / "skills" / one.name / "SKILL.md").is_file()
    assert not (project / "src" / ".claude").exists()


def test_install_global_writes_under_the_home_directory(fake_home: Path, project: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "install", "--global"])
    assert res.exit_code == 0, res.output
    for one in SKILLS:
        target = fake_home / ".claude" / "skills" / one.name
        assert (target / "SKILL.md").is_file()
        assert (target / "references" / f"{one.references[0]}.md").is_file()
        assert str(target) in res.output
    assert not (project / ".claude").exists()


def test_install_root_that_is_a_file_is_a_usage_error(fake_home: Path, tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x")
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(not_a_dir)])
    assert res.exit_code == 2, res.output
    assert "error:" in res.output and "Traceback" not in res.output


def test_install_directory_at_skill_md_is_a_clean_error(fake_home: Path, project: Path) -> None:
    (project / ".claude" / "skills" / SKILL_NAMES[0] / "SKILL.md").mkdir(parents=True)
    res = CliRunner().invoke(app, ["skill", "install", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "error:" in res.output and "Traceback" not in res.output


def test_show_reports_packaged_project_and_global_state(fake_home: Path, project: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "show", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert __version__ in res.output
    for one in SKILLS:
        assert one.name in res.output
        assert content_digest(one) in res.output
        assert str(skill_dir(one)) in res.output
    assert "not installed" in res.output
    CliRunner().invoke(app, ["skill", "install", "--root", str(project)])
    res = CliRunner().invoke(app, ["skill", "show", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "up to date" in res.output
    (project / ".claude" / "skills" / SKILL_NAMES[0] / "SKILL.md").write_text("old\n")
    res = CliRunner().invoke(app, ["skill", "show", "--root", str(project)])
    assert "differs" in res.output and "--force" in res.output


def test_show_json_shape(fake_home: Path, project: Path) -> None:
    CliRunner().invoke(app, ["skill", "install", "--global"])
    res = CliRunner().invoke(app, ["skill", "show", "--root", str(project), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert set(data) == {"skills"}
    assert [entry["name"] for entry in data["skills"]] == list(SKILL_NAMES)
    for entry, one in zip(data["skills"], SKILLS, strict=True):
        assert set(entry) == {"name", "packaged", "project", "global"}
        assert entry["packaged"] == {
            "path": str(skill_dir(one)),
            "rayspec_version": __version__,
            "digest": content_digest(one),
            "files": [rel for rel, _ in skill_files(one)],
        }
        assert entry["project"]["state"] == "missing" and entry["project"]["digest"] is None
        assert entry["global"]["state"] == "current"
        assert entry["global"]["digest"] == content_digest(one)
        assert entry["global"]["path"] == str(fake_home / ".claude" / "skills" / one.name)


def test_show_json_with_a_name_reports_just_that_skill(fake_home: Path, project: Path) -> None:
    res = CliRunner().invoke(
        app, ["skill", "show", "rayspec-workflows", "--root", str(project), "--json"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert [entry["name"] for entry in data["skills"]] == ["rayspec-workflows"]


def test_path_prints_every_packaged_directory(fake_home: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "path"])
    assert res.exit_code == 0, res.output
    lines = res.stdout.strip().splitlines()
    assert lines == [str(skill_dir(one)) for one in SKILLS]
    for line in lines:
        assert (Path(line) / "SKILL.md").is_file()


def test_path_with_a_name_prints_one_directory(fake_home: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "path", "rayspec-cli"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == str(skill_dir(SKILLS[1]))


def test_install_global_and_root_are_mutually_exclusive(fake_home: Path, project: Path) -> None:
    res = CliRunner().invoke(app, ["skill", "install", "--global", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "error:" in res.output and "mutually exclusive" in res.output
    assert not (fake_home / ".claude").exists()
    assert not (project / ".claude").exists()

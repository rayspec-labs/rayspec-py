"""``rayspec.skill`` — the packaged Claude Code skill and its install helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.skill import (
    REFERENCE_NAMES,
    SKILL_NAME,
    content_digest,
    install_skill,
    installed_state,
    skill_dir,
    skill_files,
)


def test_packaged_skill_has_skill_md_and_every_reference() -> None:
    root = skill_dir()
    assert (root / "SKILL.md").is_file()
    names = {rel for rel, _ in skill_files()}
    assert "SKILL.md" in names
    for ref in REFERENCE_NAMES:
        assert f"references/{ref}.md" in names, names
    assert SKILL_NAME == "rayspec"


def test_skill_files_are_sorted_and_exclude_python_files() -> None:
    rels = [rel for rel, _ in skill_files()]
    assert rels == sorted(rels)
    assert not any(rel.endswith(".py") or "__pycache__" in rel for rel in rels)


def test_install_is_idempotent_and_force_overwrites(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / "skills" / "rayspec"
    first = install_skill(target)
    assert {f.action for f in first} == {"created"}
    assert (target / "SKILL.md").is_file()
    assert {f.relative for f in first} == {rel for rel, _ in skill_files()}
    (target / "SKILL.md").write_text("edited\n", encoding="utf-8")
    second = install_skill(target)
    assert {f.action for f in second} == {"skipped"}
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "edited\n"
    third = install_skill(target, force=True)
    assert {f.action for f in third} == {"overwritten"}
    assert (target / "SKILL.md").read_text(encoding="utf-8") != "edited\n"


def test_install_refuses_a_file_where_the_directory_goes(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "rayspec"
    target.parent.mkdir(parents=True)
    target.write_text("not a dir")
    with pytest.raises(NotADirectoryError):
        install_skill(target)


def test_installed_state_tracks_the_packaged_digest(tmp_path: Path) -> None:
    target = tmp_path / "rayspec"
    assert installed_state(target).state == "missing"
    install_skill(target)
    state = installed_state(target)
    assert state.state == "current"
    assert state.digest == content_digest()
    (target / "SKILL.md").write_text("edited\n", encoding="utf-8")
    assert installed_state(target).state == "stale"
    assert installed_state(target).digest != content_digest()


def test_content_digest_is_stable_and_short() -> None:
    a = content_digest()
    assert a == content_digest()
    assert len(a) == 12 and all(c in "0123456789abcdef" for c in a)


def test_contracts_md_documents_the_public_surface() -> None:
    """CONTRACTS.md is the working agreement between the modules of rayspec: every public helper of
    ``rayspec.skill``, the ``skill`` command group and ``init --no-skill`` must be in it."""
    import rayspec.skill as skill_pkg

    contracts = Path(__file__).resolve().parents[2] / "CONTRACTS.md"
    if not contracts.is_file():
        pytest.skip("not running from a repository checkout")
    text = contracts.read_text(encoding="utf-8")
    assert "### rayspec.skill + CLI `skill`" in text
    for name in skill_pkg.__all__:
        assert name in text, name
    for needle in (
        "cli/commands/skill.py",
        "cli/commands/_skill_common.py",
        "rayspec skill install [--global] [--force] [--root DIR]",
        "rayspec skill show [--root DIR] [--json]",
        "rayspec skill path",
        "rayspec init [--kind code|content] [--force] [--no-skill] [--root DIR]",
        "next_steps(kind, *,\n  skill=True)",
    ):
        assert needle in text, needle

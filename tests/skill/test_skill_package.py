"""``rayspec.skill`` — the registry of packaged Claude Code skills and its install helper.

Every test here runs over **both** skills (:data:`rayspec.skill.SKILLS`), so a third skill is
covered the moment it is registered and neither of the two can quietly stop being checked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.skill import (
    SKILL_NAMES,
    SKILLS,
    Skill,
    content_digest,
    find_skill,
    global_skill_dir,
    install_skill,
    installed_state,
    project_skill_dir,
    skill_dir,
    skill_files,
)


def test_the_registry_holds_the_two_skills_with_disjoint_references() -> None:
    assert SKILL_NAMES == ("rayspec-workflows", "rayspec-cli")
    assert [s.name for s in SKILLS] == list(SKILL_NAMES)
    seen: set[str] = set()
    for skill in SKILLS:
        assert skill.references, skill.name
        assert not seen & set(skill.references), (skill.name, seen & set(skill.references))
        seen |= set(skill.references)
        assert skill.summary and len(skill.summary) < 120


def test_find_skill_resolves_a_name_and_rejects_anything_else() -> None:
    for name in SKILL_NAMES:
        found = find_skill(name)
        assert found is not None and found.name == name
    assert find_skill("rayspec") is None  # the retired single-skill name is not an alias
    assert find_skill("nope") is None


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_packaged_skill_has_skill_md_and_every_reference(skill: Skill) -> None:
    root = skill_dir(skill)
    assert (root / "SKILL.md").is_file()
    names = {rel for rel, _ in skill_files(skill)}
    assert "SKILL.md" in names
    for ref in skill.references:
        assert f"references/{ref}.md" in names, names


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_skill_files_are_sorted_and_exclude_python_files(skill: Skill) -> None:
    rels = [rel for rel, _ in skill_files(skill)]
    assert rels == sorted(rels)
    assert not any(rel.endswith(".py") or "__pycache__" in rel for rel in rels)


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_install_is_idempotent_and_force_overwrites(skill: Skill, tmp_path: Path) -> None:
    target = project_skill_dir(skill, tmp_path)
    first = install_skill(skill, target)
    assert {f.action for f in first} == {"created"}
    assert (target / "SKILL.md").is_file()
    assert {f.relative for f in first} == {rel for rel, _ in skill_files(skill)}
    (target / "SKILL.md").write_text("edited\n", encoding="utf-8")
    second = install_skill(skill, target)
    assert {f.action for f in second} == {"skipped"}
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "edited\n"
    third = install_skill(skill, target, force=True)
    assert {f.action for f in third} == {"overwritten"}
    assert (target / "SKILL.md").read_text(encoding="utf-8") != "edited\n"


def test_install_refuses_a_file_where_the_directory_goes(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "rayspec-cli"
    target.parent.mkdir(parents=True)
    target.write_text("not a dir")
    with pytest.raises(NotADirectoryError):
        install_skill(SKILLS[1], target)


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_installed_state_tracks_the_packaged_digest(skill: Skill, tmp_path: Path) -> None:
    target = tmp_path / skill.name
    assert installed_state(skill, target).state == "missing"
    install_skill(skill, target)
    state = installed_state(skill, target)
    assert state.state == "current"
    assert state.digest == content_digest(skill)
    (target / "SKILL.md").write_text("edited\n", encoding="utf-8")
    assert installed_state(skill, target).state == "stale"
    assert installed_state(skill, target).digest != content_digest(skill)


def test_a_skill_installed_where_another_belongs_is_stale_not_current(tmp_path: Path) -> None:
    """The digest identifies *which* skill is there: installing rayspec-cli into the
    rayspec-workflows directory must not read as `current`."""
    workflows, cli = SKILLS
    target = project_skill_dir(workflows, tmp_path)
    install_skill(cli, target)
    assert installed_state(workflows, target).state == "stale"
    assert installed_state(cli, target).state == "current"


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_content_digest_is_stable_and_short(skill: Skill) -> None:
    a = content_digest(skill)
    assert a == content_digest(skill)
    assert len(a) == 12 and all(c in "0123456789abcdef" for c in a)


def test_the_two_skills_have_different_digests() -> None:
    assert content_digest(SKILLS[0]) != content_digest(SKILLS[1])


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_install_locations_end_in_the_skill_name(skill: Skill, tmp_path: Path) -> None:
    assert project_skill_dir(skill, tmp_path) == tmp_path / ".claude" / "skills" / skill.name
    assert global_skill_dir(skill, tmp_path) == tmp_path / ".claude" / "skills" / skill.name


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
        "rayspec skill install [NAME] [--global] [--force] [--root DIR]",
        "rayspec skill show [NAME] [--root DIR] [--json]",
        "rayspec skill path [NAME]",
        "rayspec init [--kind code|content | --from EXAMPLE] [--force] [--no-skill] [--root DIR]",
        "next_steps(kind, *,\n  skill=True)",
    ):
        assert needle in text, needle

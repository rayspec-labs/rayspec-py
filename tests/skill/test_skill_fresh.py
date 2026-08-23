"""The generated references and the ``.claude/skills/<name>`` mirrors of **both** skills are up
to date (``uv run python scripts/gen_skill.py`` regenerates them)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from rayspec.cli._docs import DOCS_BASE
from rayspec.skill import CLI_SKILL, SKILLS, WORKFLOWS_SKILL, Skill

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_skill.py"
IDS = [s.name for s in SKILLS]


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_skill", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_references_and_mirrors_are_fresh(gen: ModuleType) -> None:
    problems = gen.stale_items()
    assert not problems, "\n".join([*problems, "run `uv run python scripts/gen_skill.py`"])


def test_script_check_mode_passes_on_the_committed_tree() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_retired_single_skill_mirror_is_gone() -> None:
    """The old `.claude/skills/rayspec/` mirror is not a third skill an agent might load."""
    assert not (REPO_ROOT / ".claude" / "skills" / "rayspec").exists()
    assert {p.name for p in (REPO_ROOT / ".claude" / "skills").iterdir() if p.is_dir()} == set(IDS)


@pytest.mark.parametrize("skill", SKILLS, ids=IDS)
def test_every_reference_has_a_docs_source_and_the_header(gen: ModuleType, skill: Skill) -> None:
    for name in skill.references:
        assert (REPO_ROOT / "docs" / f"{name}.md").is_file(), name
        text = gen.render_reference(skill, name)
        head = text.splitlines()[:3]
        assert head[0] == (
            f"<!-- Generated from docs/{name}.md by scripts/gen_skill.py — do not edit here. -->"
        )
        assert f"{DOCS_BASE}docs/{name}.md" in head[1]
        assert all(f"{n}.md" in head[2] for n in skill.references)
        page = (REPO_ROOT / "docs" / f"{name}.md").read_text("utf-8")
        body = text.split("\n", 4)[4]
        assert body == _rewritten(gen, skill, gen.strip_markers(page))


def _rewritten(gen: ModuleType, skill: Skill, text: str) -> str:
    return gen._LINK_RE.sub(
        lambda m: f"{m.group(1)}{gen.rewrite_link(skill, m.group(2))}{m.group(3)}", text
    )


@pytest.mark.parametrize(
    ("skill", "target", "expected"),
    [
        (WORKFLOWS_SKILL, "schema.md#inputs", "schema.md#inputs"),
        (WORKFLOWS_SKILL, "concepts.md", "concepts.md"),
        # cli.md belongs to the *other* skill: linked, never duplicated
        (WORKFLOWS_SKILL, "cli.md", f"{DOCS_BASE}docs/cli.md"),
        (
            WORKFLOWS_SKILL,
            "runs-and-resume.md#resume",
            f"{DOCS_BASE}docs/runs-and-resume.md#resume",
        ),
        (CLI_SKILL, "cli.md", "cli.md"),
        (CLI_SKILL, "runs-and-resume.md#resume", "runs-and-resume.md#resume"),
        (CLI_SKILL, "schema.md#inputs", f"{DOCS_BASE}docs/schema.md#inputs"),
        (CLI_SKILL, "../CONTRACTS.md", f"{DOCS_BASE}CONTRACTS.md"),
        (CLI_SKILL, "../examples/fix_issue/", f"{DOCS_BASE}examples/fix_issue"),
        (CLI_SKILL, "https://example.com/x.md", "https://example.com/x.md"),
        (CLI_SKILL, "#durations", "#durations"),
    ],
)
def test_rewrite_link(gen: ModuleType, skill: Skill, target: str, expected: str) -> None:
    assert gen.rewrite_link(skill, target) == expected


@pytest.mark.parametrize("skill", SKILLS, ids=IDS)
def test_generated_references_contain_no_relative_links_outside_the_skill(
    gen: ModuleType, skill: Skill
) -> None:
    import re

    link_re = re.compile(r"(?<!\\)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    allowed = {f"{n}.md" for n in skill.references}
    for name in skill.references:
        text = gen.render_reference(skill, name)
        for target in link_re.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            file_part = target.partition("#")[0]
            assert file_part in allowed, f"{skill.name}/{name}.md links to {target!r}"


def test_script_depends_only_on_public_rayspec_names() -> None:
    import re

    source = SCRIPT.read_text(encoding="utf-8")
    private = re.findall(r"^from rayspec\S* import .*\b_\w+", source, re.MULTILINE)
    assert not private, private

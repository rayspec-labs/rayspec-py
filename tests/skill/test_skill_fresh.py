"""The generated references and the ``.claude/skills/rayspec`` mirror are up to date
(``uv run python scripts/gen_skill.py`` regenerates them)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from rayspec.cli._docs import DOCS_BASE
from rayspec.skill import REFERENCE_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_skill.py"


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_skill", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_references_and_mirror_are_fresh(gen: ModuleType) -> None:
    problems = gen.stale_items()
    assert not problems, "\n".join([*problems, "run `uv run python scripts/gen_skill.py`"])


def test_script_check_mode_passes_on_the_committed_tree() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_reference_has_a_docs_source_and_the_header(gen: ModuleType) -> None:
    for name in REFERENCE_NAMES:
        assert (REPO_ROOT / "docs" / f"{name}.md").is_file(), name
        text = gen.render_reference(name)
        head = text.splitlines()[:3]
        assert head[0] == (
            f"<!-- Generated from docs/{name}.md by scripts/gen_skill.py — do not edit here. -->"
        )
        assert f"{DOCS_BASE}docs/{name}.md" in head[1]
        assert all(f"{n}.md" in head[2] for n in REFERENCE_NAMES)
        page = (REPO_ROOT / "docs" / f"{name}.md").read_text("utf-8")
        body = text.split("\n", 4)[4]
        assert body == _rewritten(gen, gen.strip_markers(page))


def _rewritten(gen: ModuleType, text: str) -> str:
    return gen._LINK_RE.sub(
        lambda m: f"{m.group(1)}{gen.rewrite_link(m.group(2))}{m.group(3)}", text
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("schema.md#inputs", "schema.md#inputs"),
        ("cli.md", "cli.md"),
        ("runs-and-resume.md#resume", f"{DOCS_BASE}docs/runs-and-resume.md#resume"),
        ("isolation.md", f"{DOCS_BASE}docs/isolation.md"),
        ("../CONTRACTS.md", f"{DOCS_BASE}CONTRACTS.md"),
        ("../examples/fix_issue/", f"{DOCS_BASE}examples/fix_issue"),
        ("https://example.com/x.md", "https://example.com/x.md"),
        ("#durations", "#durations"),
    ],
)
def test_rewrite_link(gen: ModuleType, target: str, expected: str) -> None:
    assert gen.rewrite_link(target) == expected


def test_generated_references_contain_no_relative_links_outside_the_skill(gen: ModuleType) -> None:
    import re

    link_re = re.compile(r"(?<!\\)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    allowed = {f"{n}.md" for n in REFERENCE_NAMES}
    for name in REFERENCE_NAMES:
        text = gen.render_reference(name)
        for target in link_re.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            file_part = target.partition("#")[0]
            assert file_part in allowed, f"{name}.md links to {target!r}"


def test_script_depends_only_on_public_rayspec_names() -> None:
    import re

    source = SCRIPT.read_text(encoding="utf-8")
    private = re.findall(r"^from rayspec\S* import .*\b_\w+", source, re.MULTILINE)
    assert not private, private

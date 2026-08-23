"""Both ``SKILL.md`` files teach what the docs teach: their frontmatter, the cheat-sheet workflows
(they load, validate and dry-run), the stub snippet, the CLI tables and the reference lists are
checked against the real loader, stub parser and Typer app.

Everything that can run over both skills is parametrised over :data:`rayspec.skill.SKILLS`;
what is specific to one of them says so by name.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.main import get_command
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import _loader_common as common
from rayspec.loader import load_workflow, validate_workflow
from rayspec.loader.yaml import load_yaml
from rayspec.providers.stub import StubScript
from rayspec.schema import parse_step
from rayspec.skill import CLI_SKILL, SKILLS, WORKFLOWS_SKILL, Skill, skill_dir

_FENCE_RE = re.compile(r"```(?P<lang>[a-z]*)\n(?P<body>.*?)```", re.DOTALL)

#: Every skill's hand-written page, by name.
TEXT = {skill.name: (skill_dir(skill) / "SKILL.md").read_text(encoding="utf-8") for skill in SKILLS}
WORKFLOWS_MD = TEXT[WORKFLOWS_SKILL.name]
CLI_MD = TEXT[CLI_SKILL.name]

#: What each skill's page must be shaped like: the headings it owns and a size window. A section
#: moving from one skill to the other has to be a deliberate edit here, not a silent drift.
SHAPE: dict[str, tuple[tuple[str, ...], int, int]] = {
    WORKFLOWS_SKILL.name: (
        (
            "## Mental model",
            "## The authoring loop",
            "## YAML cheat-sheet",
            "## Field index",
            "## Templating rules that bite",
            "## Secrets",
            "## Best practices",
            "## Worked examples",
            "## CLI quick reference",
            "## Pitfalls and conventions",
            "## References",
        ),
        620,
        780,
    ),
    CLI_SKILL.name: (
        (
            "## Mental model",
            "## CLI quick reference",
            "## Stub file",
            "## Providers, capabilities, cost",
            "## Pitfalls and conventions",
            "## References",
        ),
        90,
        400,
    ),
}


def frontmatter(text: str) -> dict[str, object]:
    assert text.startswith("---\n")
    head = text.split("---\n", 2)[1]
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data


def yaml_blocks(text: str) -> list[str]:
    return [m.group("body") for m in _FENCE_RE.finditer(text) if m.group("lang") == "yaml"]


def workflow_blocks() -> dict[str, str]:
    found: dict[str, str] = {}
    for body in yaml_blocks(WORKFLOWS_MD):
        if "rayspec: 1" in body and "- id:" in body:
            found[yaml.safe_load(body)["name"]] = body
    return found


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_every_yaml_fence_is_valid_yaml_and_every_step_in_it_parses(skill: Skill) -> None:
    """Not only the full workflows: every ```yaml fence (the secret snippet, the stub file …)
    must load with PyYAML *and* the strict loader, and every `- id:` step mapping under a
    `steps:` list must be a valid step — an agent copies these verbatim."""
    blocks = yaml_blocks(TEXT[skill.name])
    assert blocks, skill.name
    for index, body in enumerate(blocks):
        data = yaml.safe_load(body)
        source = f"{skill.name}/SKILL.md#{index}"
        assert load_yaml(body, source=source) == data
        steps = data.get("steps") if isinstance(data, dict) else None
        if isinstance(steps, list):
            for step in steps:
                assert isinstance(step, dict) and "id" in step, (index, step)
                parse_step(step, source=source)


#: Every full workflow the authoring page shows, and what each one is there to teach. The set is
#: pinned so a pattern cannot quietly disappear (or a fourth one appear without a decision); the
#: tests below load, validate and dry-run every member, so none of them can rot either.
PAGE_WORKFLOWS: dict[str, str] = {
    "fix_issue": "the cheat-sheet: every step kind once, in one file",
    "review_block": "the block the cheat-sheet includes",
    "selfheal": "worked example 1 — a loop that ends on a signal, not on a count",
    "audit_sweep": "worked example 2 — fan-out with a tolerated item and a finally step",
    "quality_block": "worked example 3 — a reusable block with its own inputs and outputs",
    "pipeline": "worked example 3 — the caller that includes that block twice",
}


def test_the_workflows_skill_still_carries_the_full_cheat_sheet() -> None:
    assert len(yaml_blocks(WORKFLOWS_MD)) >= 3
    assert set(workflow_blocks()) == set(PAGE_WORKFLOWS)
    for name, why in PAGE_WORKFLOWS.items():
        assert len(why.strip()) > 20, name


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_frontmatter_is_a_valid_claude_code_skill_header(skill: Skill) -> None:
    data = frontmatter(TEXT[skill.name])
    assert data["name"] == skill.name == skill_dir(skill).name
    assert re.fullmatch(r"[a-z][a-z0-9-]*", str(data["name"]))
    description = str(data["description"])
    assert 40 < len(description) <= 1024
    for needle in ("rayspec", "workflow", "Claude Agent SDK", "Codex", ".rayspec/"):
        assert needle in description, (skill.name, needle)
    # each description names the *other* skill, so an agent that loaded one knows the other exists
    other = next(s for s in SKILLS if s is not skill)
    assert other.name in description, (skill.name, other.name)


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_skill_md_is_focused(skill: Skill) -> None:
    headings, low, high = SHAPE[skill.name]
    lines = TEXT[skill.name].splitlines()
    assert low <= len(lines) <= high, len(lines)
    for heading in headings:
        assert any(line.startswith(heading) for line in lines), heading
    # and nothing else: a new section is a deliberate change to SHAPE
    found = tuple(line.split("(")[0].strip() for line in lines if line.startswith("## "))
    assert len(found) == len(headings), found


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_every_reference_listed_exists_and_every_reference_is_listed(skill: Skill) -> None:
    listed = set(re.findall(r"`references/([a-z-]+)\.md`", TEXT[skill.name]))
    assert listed == set(skill.references), listed ^ set(skill.references)
    for name in skill.references:
        assert (skill_dir(skill) / "references" / f"{name}.md").is_file()


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_each_skill_points_at_the_other_one(skill: Skill) -> None:
    """The cross-link decision: an agent reading one skill must be told the other exists and
    when to load it."""
    other = next(s for s in SKILLS if s is not skill)
    assert other.name in TEXT[skill.name], skill.name
    assert re.search(rf"[Ll]oad (?:it|the)[^\n]*`?{other.name}`?|`{other.name}`", TEXT[skill.name])


def test_the_authoring_loop_hands_off_to_the_cli_skill() -> None:
    assert "validate, plan and dry-run" in WORKFLOWS_MD
    assert "`rayspec-cli` skill" in WORKFLOWS_MD


def test_cheat_sheet_covers_every_step_kind() -> None:
    text = workflow_blocks()["fix_issue"]
    for kind in ("prompt:", "shell:", "python:", "loop:", "each:", "approve:", "include:", "stop:"):
        assert kind in text, kind
    for needle in (
        "join: always",
        "allow_failure: true",
        "session:",
        "output_schema:",
        "when:",
        "until:",
        "has_signal",
        "iteration.prev",
        "iteration.first",
        "with:",
        "outputs:",
    ):
        assert needle in text, needle


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "prompts").mkdir()
    (root / ".rayspec" / "prompts" / "implementer.md").write_text("You implement fixes.\n")
    home.mkdir()
    for name, text in workflow_blocks().items():
        (root / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root, home


@pytest.mark.parametrize("name", sorted(workflow_blocks()))
def test_cheat_sheet_workflows_load_and_validate_without_warnings(
    name: str, tmp_path: Path
) -> None:
    root, home = _project(tmp_path)
    caps = common.capability_source()
    rw = load_workflow(name, project_root=root, home=home)
    report = validate_workflow(
        rw,
        capabilities_for=caps.capabilities_for,
        template_checker=common.template_checker(),
        provider_ids=caps.provider_ids,
    )
    assert report.ok, report.errors
    assert not rw.warnings and not report.warnings, rw.warnings + report.warnings


def test_cheat_sheet_workflow_dry_runs_with_an_edited_stub_scaffold(tmp_path: Path) -> None:
    """The authoring loop as written: validate → plan → --stubs-init → edit sequence → --stubs."""
    root, home = _project(tmp_path)
    env = {"RAYSPEC_HOME": str(home)}
    runner = CliRunner()
    res = runner.invoke(app, ["validate", "--root", str(root)], env=env)
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["plan", "fix_issue", "--root", str(root), "-i", "issue=7"], env=env)
    assert res.exit_code == 0, res.output
    stubs = tmp_path / "stubs.yaml"
    res = runner.invoke(
        app,
        [
            "run",
            "fix_issue",
            "--root",
            str(root),
            "--dry-run",
            "--stubs-init",
            str(stubs),
            "-i",
            "issue=7",
        ],
        env=env,
    )
    assert res.exit_code == 0, res.output
    data = yaml.safe_load(stubs.read_text())
    assert set(data["steps"]) == {
        "assess",
        "build[*]/implement",
        "build[*]/review",
        "second_opinion/review",
    }
    data["steps"]["build[*]/review"] = {"sequence": ["Fix the flaky test", "BUILD-CLEAN"]}
    stubs.write_text(yaml.safe_dump(data))
    res = runner.invoke(
        app,
        [
            "run",
            "fix_issue",
            "--root",
            str(root),
            "--dry-run",
            "--stubs",
            str(stubs),
            "-i",
            "issue=7",
            "-i",
            "labels=a",
            "-i",
            "labels=b",
            "--json",
        ],
        env=env,
    )
    assert res.exit_code == 0, res.output
    import json

    summary = json.loads(res.stdout.splitlines()[-1])
    assert summary["status"] == "succeeded"
    assert summary["outputs"]["verdict"] == "fix"
    assert summary["outputs"]["iterations"] == 2


def test_stub_snippet_parses() -> None:
    """The stub file moved to the operating skill; it still has to parse with the real parser."""
    [block] = [b for b in yaml_blocks(CLI_MD) if "prompt_regex" in b]
    script = StubScript.from_yaml(block, source="rayspec-cli/SKILL.md")
    assert {e.key for e in script.steps} >= {
        "assess",
        "build[*]/implement",
        "build[*]/review",
        "pr",
    }
    assert script.match


def cli_table_section(text: str) -> str:
    """The ``## CLI quick reference`` section of one page, up to the next ``## `` heading."""
    assert "## CLI quick reference" in text
    rest = text.split("## CLI quick reference", 1)[1]
    return rest.split("\n## ", 1)[0]


def table_rows(text: str) -> list[str]:
    return [line for line in cli_table_section(text).splitlines() if line.startswith("| `rayspec ")]


@pytest.mark.parametrize("skill", SKILLS, ids=[s.name for s in SKILLS])
def test_cli_table_names_only_real_commands_and_flags(skill: Skill) -> None:
    """Soundness: everything a table names exists. (Completeness — that every command is in
    exactly one of the tables — is ``test_skill_totality.py``.)"""
    rows = table_rows(TEXT[skill.name])
    assert rows, skill.name
    leaves = _leaf_commands()
    root: Any = get_command(app)
    for row in rows:
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", row.strip("|"))]
        cmd_cell, _purpose, flags_cell, _exit = cells
        # every `rayspec <a> [<b>]` token in the first cell is a command or a group with subcommands
        for match in re.finditer(r"`rayspec ([a-z]+)(?: ([a-z]+))?", cmd_cell):
            name, sub = match.group(1), match.group(2)
            group = root.commands.get(name)
            assert group is not None, name
            if hasattr(group, "commands"):
                assert sub is None or sub in group.commands, (name, sub)
            else:
                assert f"{name}" in leaves
        # every --flag listed exists on EVERY command named in the cell (a row such as
        # `rayspec workflows` · `agents` lists flags both accept); for groups, on one of the
        # subcommands. A `(… for `a`/`b`)` clause restricts the flags inside it to those commands.
        names = [re.match(r"`(?:rayspec )?([a-z]+)", c.strip()) for c in cmd_cell.split("·")]
        assert all(names), cmd_cell
        first_name = names[0].group(1) if names[0] else ""
        wanted: dict[str, set[str]] = {}
        for m in names:
            if m is None:
                continue
            # `rayspec worktrees list` · `clean`: a bare name may be a subcommand of the row's group
            key = m.group(1) if m.group(1) in root.commands else first_name
            wanted.setdefault(key, set())
        rest = flags_cell
        for clause in re.finditer(r"\(([^)]*?) for ([^)]*)\)", flags_cell):
            only = set(re.findall(r"`([a-z]+)`", clause.group(2)))
            assert only and only <= set(wanted), (cmd_cell, clause.group(0))
            for flag in re.findall(r"--[a-z-]+", clause.group(1)):
                for name in only:
                    wanted[name].add(flag)
            rest = rest.replace(clause.group(0), "")
        for flag in re.findall(r"--[a-z-]+", rest):
            for name in wanted:
                wanted[name].add(flag)
        for name, flags in wanted.items():
            group = root.commands[name]
            # a group carries its own options too (`rayspec runs --all` next to `runs diff`)
            cmds = [group, *group.commands.values()] if hasattr(group, "commands") else [group]
            known = {
                opt
                for cmd in cmds
                for param in cmd.params
                for opt in [*param.opts, *getattr(param, "secondary_opts", [])]
            }
            for flag in sorted(flags):
                assert flag in known, (cmd_cell, name, flag)


def _leaf_commands() -> set[str]:
    root: Any = get_command(app)
    found: set[str] = set()

    def walk(group: Any, prefix: str) -> None:
        for name, cmd in group.commands.items():
            if hasattr(cmd, "commands"):
                walk(cmd, f"{prefix}{name} ")
            else:
                found.add(f"{prefix}{name}")

    walk(root, "")
    return found


def test_exit_codes_are_stated_by_the_operating_skill() -> None:
    assert "`0` succeeded · `1` failed · `2` usage/validation error · `3` paused" in CLI_MD
    assert "`130` interrupted" in CLI_MD


def test_the_env_ref_rule_is_stated_by_the_authoring_skill() -> None:
    assert "${RAYSPEC_V<n>}" in WORKFLOWS_MD
    assert "RAYSPEC_INPUT_<NAME>" in WORKFLOWS_MD
    assert "{{# ... #}}" in WORKFLOWS_MD
    assert "{% raw %}" in WORKFLOWS_MD

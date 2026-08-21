"""``SKILL.md`` teaches what the docs teach: its frontmatter, the cheat-sheet workflows (they load,
validate and dry-run), the stub snippet, the CLI table and the reference list are checked against
the real loader, stub parser and Typer app."""

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
from rayspec.skill import REFERENCE_NAMES, skill_dir

SKILL_MD = (skill_dir() / "SKILL.md").read_text(encoding="utf-8")
_FENCE_RE = re.compile(r"```(?P<lang>[a-z]*)\n(?P<body>.*?)```", re.DOTALL)


def frontmatter() -> dict[str, object]:
    assert SKILL_MD.startswith("---\n")
    head = SKILL_MD.split("---\n", 2)[1]
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data


def yaml_blocks() -> list[str]:
    return [m.group("body") for m in _FENCE_RE.finditer(SKILL_MD) if m.group("lang") == "yaml"]


def workflow_blocks() -> dict[str, str]:
    found: dict[str, str] = {}
    for body in yaml_blocks():
        if "rayspec: 1" in body and "- id:" in body:
            found[yaml.safe_load(body)["name"]] = body
    return found


def test_every_yaml_fence_is_valid_yaml_and_every_step_in_it_parses() -> None:
    """Not only the full workflows: every ```yaml fence (the secret snippet, the stub file …)
    must load with PyYAML *and* the strict loader, and every `- id:` step mapping under a
    `steps:` list must be a valid step — an agent copies these verbatim."""
    blocks = yaml_blocks()
    assert len(blocks) >= 4
    for index, body in enumerate(blocks):
        data = yaml.safe_load(body)
        assert load_yaml(body, source=f"SKILL.md#{index}") == data
        steps = data.get("steps") if isinstance(data, dict) else None
        if isinstance(steps, list):
            for step in steps:
                assert isinstance(step, dict) and "id" in step, (index, step)
                parse_step(step, source=f"SKILL.md#{index}")


def test_frontmatter_is_a_valid_claude_code_skill_header() -> None:
    data = frontmatter()
    assert data["name"] == "rayspec" == skill_dir().name
    assert re.fullmatch(r"[a-z][a-z0-9-]*", str(data["name"]))
    description = str(data["description"])
    assert 40 < len(description) <= 1024
    for needle in ("rayspec", "workflow", "Claude Agent SDK", "Codex", ".rayspec/", "CLI"):
        assert needle in description, needle


def test_skill_md_is_focused() -> None:
    lines = SKILL_MD.splitlines()
    assert 250 <= len(lines) <= 450, len(lines)
    for heading in (
        "## Mental model",
        "## The authoring loop",
        "## YAML cheat-sheet",
        "## Templating rules that bite",
        "## CLI quick reference",
        "## Providers, capabilities, cost",
        "## Pitfalls and conventions",
        "## References",
    ):
        assert any(line.startswith(heading) for line in lines), heading


def test_every_reference_listed_exists_and_every_reference_is_listed() -> None:
    listed = set(re.findall(r"`references/([a-z-]+)\.md`", SKILL_MD))
    assert listed == set(REFERENCE_NAMES), listed ^ set(REFERENCE_NAMES)
    for name in REFERENCE_NAMES:
        assert (skill_dir() / "references" / f"{name}.md").is_file()


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
    [block] = [b for b in yaml_blocks() if "prompt_regex" in b]
    script = StubScript.from_yaml(block, source="SKILL.md")
    assert {e.key for e in script.steps} >= {
        "assess",
        "build[*]/implement",
        "build[*]/review",
        "pr",
    }
    assert script.match


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


def test_cli_table_names_only_real_commands_and_flags() -> None:
    table = SKILL_MD.split("## CLI quick reference", 1)[1].split("**Stub file**", 1)[0]
    rows = [line for line in table.splitlines() if line.startswith("| `rayspec ")]
    assert len(rows) >= 14
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
        # `rayspec workflows` · `agents` · `providers` lists flags all three accept); for
        # groups, on one of the subcommands
        # A `(… for `a`/`b`)` clause restricts the flags inside it to the named commands.
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


def test_exit_codes_and_env_ref_rule_are_stated() -> None:
    assert "`0` succeeded · `1` failed · `2` usage/validation error · `3` paused" in SKILL_MD
    assert "`130` interrupted" in SKILL_MD
    assert "${RAYSPEC_V<n>}" in SKILL_MD
    assert "RAYSPEC_INPUT_<NAME>" in SKILL_MD
    assert "{{# ... #}}" in SKILL_MD
    assert "{% raw %}" in SKILL_MD

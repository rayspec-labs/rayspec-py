"""The total rules: nothing may fall out of the skills unnoticed.

The skills went stale once because the only test over the CLI table asserted that everything the
table *named* existed, plus a `len(rows) >= 14` floor. Ten commands were added, none was listed,
and the suite stayed green. **Completeness is not a longer list — it is a test that fails when
the classification stops being total.** So each rule below derives its expected set from the code
(the Typer app, ``docs/``, the pydantic models) and demands that every member be assigned to
exactly one skill. A deliberate omission is not silence: it is a named entry in one of the
deny-lists here, with the reason written down, so *adding* something forces a decision.

The soundness direction — everything the skills name really exists — stays in
``test_skill_content.py``. Both directions must hold.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.main import get_command

from rayspec.cli.app import build_app
from rayspec.schema import STEP_MODELS, AgentDef, AgentOverride, InputSpec, Workflow
from rayspec.skill import CLI_SKILL, SKILLS, WORKFLOWS_SKILL, Skill, skill_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"

TEXT = {skill.name: (skill_dir(skill) / "SKILL.md").read_text(encoding="utf-8") for skill in SKILLS}


# --------------------------------------------------------------------------------------------
# a. every command of the builtin app is in exactly one skill's CLI table
# --------------------------------------------------------------------------------------------

#: Commands deliberately absent from both CLI tables, each with the reason. Empty on purpose:
#: every command of the app is worth telling an agent about. An entry here is a decision, and a
#: new command that nobody decided about fails the test instead of disappearing quietly.
UNLISTED_COMMANDS: dict[str, str] = {}


def _builtin_app() -> typer.Typer:
    """The app **without** installed CLI plugins — a plugin's commands are not ours to document."""
    return build_app(plugins=False)


def app_commands() -> set[str]:
    """Every command an operator can actually invoke: each leaf path, plus a group that is
    invokable on its own (``rayspec runs``). A group that only prints help is not a command."""
    root: Any = get_command(_builtin_app())
    found: set[str] = set()

    def walk(group: Any, prefix: str) -> None:
        for name, cmd in group.commands.items():
            path = f"{prefix}{name}"
            if hasattr(cmd, "commands"):
                if getattr(cmd, "invoke_without_command", False):
                    found.add(path)
                walk(cmd, f"{path} ")
            else:
                found.add(path)

    walk(root, "")
    return found


def _expand_cell(cell: str) -> list[str]:
    """The command paths one table cell names.

    A cell is one or more backticked tokens separated by ``·``. A token starting with ``rayspec``
    is an absolute path (``rayspec worktrees list``); a bare token replaces the tail of the
    previous path at its own depth, which is how the tables abbreviate (``· `clean``` after
    ``rayspec worktrees list`` = ``worktrees clean``; ``· `new agent``` after
    ``rayspec new workflow`` = ``new agent``). The last word may alternate
    (``rayspec skill install|show|path``, escaped in the table). Placeholders (``<run>``,
    ``[comment]``, ``…``) are dropped.
    """
    paths: list[str] = []
    previous: list[str] = []
    for chunk in cell.split("·"):
        match = re.search(r"`([^`]+)`", chunk)
        if match is None:
            continue
        words = [
            w.replace("\\", "") for w in match.group(1).split() if re.fullmatch(r"[a-z|\\]+", w)
        ]
        if words and words[0] == "rayspec":
            words, prefix = words[1:], []
        else:
            prefix = previous[: max(0, len(previous) - len(words))]
        if not words:
            continue
        head, last = [*prefix, *words[:-1]], words[-1]
        for alternative in last.split("|"):
            paths.append(" ".join([*head, alternative]))
        previous = [*head, last.split("|")[0]]
    return paths


def table_rows(text: str) -> list[str]:
    """The rows of one page's ``## CLI quick reference`` table (up to the next ``## `` heading)."""
    assert "## CLI quick reference" in text
    section = text.split("## CLI quick reference", 1)[1].split("\n## ", 1)[0]
    return [line for line in section.splitlines() if line.startswith("| `rayspec ")]


def listed_commands(skill: Skill) -> list[str]:
    """Every command path the CLI table of ``skill`` names, in order (duplicates kept)."""
    found: list[str] = []
    for row in table_rows(TEXT[skill.name]):
        # `\|` inside a cell is an escaped pipe (`install\|show\|path`), not a column break
        cell = re.split(r"(?<!\\)\|", row.strip("|"))[0]
        found.extend(_expand_cell(cell))
    return found


def test_every_command_appears_in_exactly_one_skills_cli_table() -> None:
    per_skill = {skill.name: listed_commands(skill) for skill in SKILLS}
    for name, listed in per_skill.items():
        assert len(listed) == len(set(listed)), f"{name} lists a command twice: {listed}"
    seen: dict[str, str] = {}
    for name, listed in per_skill.items():
        for path in listed:
            assert path not in seen, f"{path!r} is in both {seen[path]} and {name}"
            seen[path] = name
    expected = app_commands()
    missing = sorted(expected - set(seen) - set(UNLISTED_COMMANDS))
    assert not missing, (
        f"{len(missing)} command(s) documented by neither skill: {missing}. Add a row to one "
        "CLI table, or an entry with a reason to UNLISTED_COMMANDS."
    )
    unreal = sorted(set(seen) - expected)
    assert not unreal, f"CLI table names commands that do not exist: {unreal}"
    stale_denies = sorted(set(UNLISTED_COMMANDS) - expected)
    assert not stale_denies, f"UNLISTED_COMMANDS names commands that are gone: {stale_denies}"
    both = sorted(set(UNLISTED_COMMANDS) & set(seen))
    assert not both, f"UNLISTED_COMMANDS names commands that ARE listed: {both}"
    for path, reason in UNLISTED_COMMANDS.items():
        assert reason.strip(), path


def test_the_split_between_the_two_tables_is_the_stated_one() -> None:
    """The rule the two tables are split by: the authoring skill documents the commands that
    create or describe the authoring artifacts themselves; everything that executes, inspects or
    governs a run is the operating skill's."""
    assert set(listed_commands(WORKFLOWS_SKILL)) == {
        "init",
        "new workflow",
        "new agent",
        "schema",
    }
    assert "run" in listed_commands(CLI_SKILL)


# --------------------------------------------------------------------------------------------
# b. every docs page is assigned
# --------------------------------------------------------------------------------------------

#: Pages that ship with neither skill, and why. They are reachable from both skills' reference
#: lists as links to the published docs, which is what the link rewriter produces for them.
ONLINE_ONLY: dict[str, str] = {
    "README.md": "the docs index itself — a table of contents for humans browsing the site",
    "agent-skill.md": "documents these two skills for the person installing them, not for the "
    "agent that already has them loaded",
    "extending.md": "writing rayspec plugins (providers, stores, sinks) — a library-authoring "
    "job, not authoring or operating a workflow",
    "constitution.md": "why the DSL refuses fields; guidance for changing rayspec itself, and "
    "already summarised by the fields the schema does define",
}


def docs_pages() -> set[str]:
    return {path.name for path in DOCS_DIR.glob("*.md")}


def test_every_docs_page_is_assigned_to_one_skill_or_named_online_only() -> None:
    assigned: dict[str, str] = {}
    for skill in SKILLS:
        for name in skill.references:
            page = f"{name}.md"
            assert page not in assigned, f"{page} is in both {assigned[page]} and {skill.name}"
            assigned[page] = skill.name
    pages = docs_pages()
    unassigned = sorted(pages - set(assigned) - set(ONLINE_ONLY))
    assert not unassigned, (
        f"docs page(s) in no skill's references and not in ONLINE_ONLY: {unassigned}. Add the "
        "page to a skill's `references`, or to ONLINE_ONLY with a one-line reason."
    )
    ghosts = sorted(set(assigned) - pages)
    assert not ghosts, f"a skill references a docs page that does not exist: {ghosts}"
    stale = sorted(set(ONLINE_ONLY) - pages)
    assert not stale, f"ONLINE_ONLY names pages that are gone: {stale}"
    both = sorted(set(ONLINE_ONLY) & set(assigned))
    assert not both, f"ONLINE_ONLY names pages that a skill also ships: {both}"
    for page, reason in ONLINE_ONLY.items():
        assert len(reason.strip()) > 20, page


def test_each_skill_names_the_online_only_pages_it_does_not_ship() -> None:
    """An agent must be told where the unshipped pages are, not left to guess they exist."""
    for skill in SKILLS:
        text = TEXT[skill.name]
        assert "Online only" in text, skill.name
        for page in ("extending.md", "constitution.md"):
            assert page in text, (skill.name, page)


# --------------------------------------------------------------------------------------------
# c. every construct the schema defines appears in the authoring skill
# --------------------------------------------------------------------------------------------

#: Schema constructs deliberately absent from the authoring skill, and why.
UNDOCUMENTED_FIELDS: dict[str, str] = {
    "provider_options.<id>": "an opaque per-provider pass-through: the keys are the provider "
    "SDK's, not rayspec's, so there is nothing for the skill to enumerate",
}


def _field_names(model: type[Any]) -> set[str]:
    return {(field.alias or name) for name, field in model.model_fields.items()}


def schema_constructs() -> dict[str, set[str]]:
    """Every construct an author writes, grouped, derived from the pydantic models."""
    step_fields: set[str] = set()
    for model in STEP_MODELS.values():
        step_fields |= _field_names(model)
    return {
        "step kinds": set(STEP_MODELS),
        "step fields": step_fields,
        "input fields": _field_names(InputSpec),
        "agent fields": _field_names(AgentDef) | _field_names(AgentOverride),
        "workflow fields": _field_names(Workflow),
    }


def documented_tokens(text: str) -> set[str]:
    """Every backticked word of a page, normalised the way a field is written in prose.

    ``` `retry:` ```, ``` `steps.<id>.output` ```, ``` `--json` ``` and ``` `attempts` ``` all
    contribute the bare name, so a field counts as documented wherever it is actually mentioned —
    the Field index table, the cheat-sheet, or a sentence.
    """
    found: set[str] = set()
    # line by line: an inline span never crosses a newline, and pairing backticks across the
    # whole page would be thrown off by the first ``` fence marker and stay wrong after it
    spans = [m for line in text.splitlines() for m in re.findall(r"`([^`\n]+)`", line)]
    for span in spans:
        for word in re.split(r"[\s,;/|()\[\]{}·]+", span):
            word = word.strip().rstrip(":").strip()
            if not word:
                continue
            found.add(word)
            found.add(word.rsplit(".", 1)[-1])
    return found


@pytest.mark.parametrize("group", sorted(schema_constructs()))
def test_every_schema_construct_appears_in_the_authoring_skill(group: str) -> None:
    tokens = documented_tokens(TEXT[WORKFLOWS_SKILL.name])
    expected = schema_constructs()[group]
    missing = sorted(name for name in expected if name not in tokens)
    missing = [name for name in missing if name not in UNDOCUMENTED_FIELDS]
    assert not missing, (
        f"{group} the schema defines but rayspec-workflows never mentions: {missing}. Document "
        "them (the Field index table is the natural place), or add an entry with a reason to "
        "UNDOCUMENTED_FIELDS."
    )


def test_every_nested_spec_field_appears_in_the_authoring_skill() -> None:
    """The sub-fields of `retry:`, `loop:`, `each:`, `approve:`, `stop:`, `tools:`, `commands:`,
    `mcp.<server>:` and `defaults:` — a step field whose value is itself a mapping hides a whole
    vocabulary, which is exactly where the old skill was thinnest."""
    module = importlib.import_module("rayspec.schema")
    specs = {
        "RetryPolicy",
        "LoopSpec",
        "ApproveSpec",
        "StopSpec",
        "ToolsSpec",
        "CommandsSpec",
        "McpServerDef",
        "Defaults",
    }
    tokens = documented_tokens(TEXT[WORKFLOWS_SKILL.name])
    missing: list[str] = []
    for name in sorted(specs):
        model = getattr(module, name)
        for field in sorted(_field_names(model)):
            if field not in tokens and f"{name}.{field}" not in UNDOCUMENTED_FIELDS:
                missing.append(f"{name}.{field}")
    assert not missing, (
        f"nested spec field(s) rayspec-workflows never mentions: {missing}. Document them, or "
        "add an entry with a reason to UNDOCUMENTED_FIELDS."
    )


def test_the_deny_lists_are_named_and_justified() -> None:
    for reason in UNDOCUMENTED_FIELDS.values():
        assert len(reason.strip()) > 20, reason

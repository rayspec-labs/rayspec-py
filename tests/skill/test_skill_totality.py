"""The total rules: nothing may fall out of the skills unnoticed.

The skills went stale once because the only test over the CLI table asserted that everything the
table *named* existed, plus a `len(rows) >= 14` floor. Ten commands were added, none was listed,
and the suite stayed green. **Completeness is not a longer list — it is a test that fails when
the classification stops being total.** So each rule below derives its expected set from the code
(the Typer app, ``docs/``, the pydantic models) and demands that every member be assigned to
exactly one skill. A deliberate omission is not silence: it is a named entry in one of the
deny-lists here, with the reason written down, so *adding* something forces a decision. That
includes the sets a rule could be tempted to spell out — the schema models with sub-vocabularies
and the flags of every documented command are walked, not listed, because a written-down list
goes red when a member is removed and stays green when one is added, and adding is the direction
that actually happens.

The soundness direction — everything the skills name really exists — stays in
``test_skill_content.py``. Both directions must hold.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import pytest
import typer
from pydantic import BaseModel
from typer.main import get_command

import rayspec.schema
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
        assert len(reason.strip()) > 20, path


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
# a2. every flag of every listed command is in that command's row
# --------------------------------------------------------------------------------------------

#: Flags deliberately absent from the CLI tables, keyed ``<command path> <flag>``, each with the
#: reason. Empty on purpose: the "Key flags" columns are a complete inventory of what a command
#: accepts, not a curated subset, and the day one stops keeping up should be a red test rather
#: than a silent gap. ``--help`` is not listed here because the preamble of the operating skill's
#: table says it works on every command.
UNLISTED_FLAGS: dict[str, str] = {}


def _option_spellings(command: Any) -> list[tuple[str, ...]]:
    """One tuple per option *param* of ``command``: every long spelling it answers to.

    Per param, not per spelling, because the tables document options: a boolean pair
    (``--locked`` / ``--no-locked``, ``--worktree`` / ``--no-worktree``) is one decision, and a
    row that names either half has documented it. ``--help`` is dropped — the operating skill's
    table preamble states it works everywhere.
    """
    found: list[tuple[str, ...]] = []
    for param in command.params:
        if getattr(param, "hidden", False):
            continue  # a hidden internal option (e.g. --detached-child) is not user-facing
        spellings = tuple(
            opt
            for opt in [*param.opts, *getattr(param, "secondary_opts", [])]
            if opt.startswith("--") and opt != "--help"
        )
        if spellings:
            found.append(spellings)
    return found


def command_options() -> dict[str, list[tuple[str, ...]]]:
    """Every command path of the builtin app → its option params. Mirrors :func:`app_commands`."""
    root: Any = get_command(_builtin_app())
    found: dict[str, list[tuple[str, ...]]] = {}

    def walk(group: Any, prefix: str) -> None:
        for name, cmd in group.commands.items():
            path = f"{prefix}{name}"
            if hasattr(cmd, "commands"):
                if getattr(cmd, "invoke_without_command", False):
                    found[path] = _option_spellings(cmd)
                walk(cmd, f"{path} ")
            else:
                found[path] = _option_spellings(cmd)

    walk(root, "")
    return found


def listed_flags() -> dict[str, set[str]]:
    """Every command path the two CLI tables name → the flags its row attributes to it.

    A row that names several commands (``rayspec providers`` · ``plugins``) attributes its whole
    "Key flags" cell to each of them, which is how the tables are written and how the soundness
    rule in ``test_skill_content.py`` reads them.
    """
    found: dict[str, set[str]] = {}
    for skill in SKILLS:
        for row in table_rows(TEXT[skill.name]):
            cells = re.split(r"(?<!\\)\|", row.strip("|"))
            flags = set(re.findall(r"--[a-z-]+", cells[2]))
            for path in _expand_cell(cells[0]):
                found.setdefault(path, set()).update(flags)
    return found


def test_every_flag_of_every_listed_command_is_named_in_its_row() -> None:
    """The other half of the flag rule.

    ``test_cli_table_names_only_real_commands_and_flags`` proves every flag a table names exists;
    nothing proved the converse, so a new flag on ``rayspec run`` — the command where a flag
    decides whether an unattended agent spends money — could be added and the suite stay green.
    """
    listed = listed_flags()
    missing: list[str] = []
    for path, params in command_options().items():
        if path not in listed:
            # a command in neither table is the *command* rule's failure, not this one's; the
            # only paths that legitimately get here are the ones named in UNLISTED_COMMANDS
            assert path in UNLISTED_COMMANDS, path
            continue
        for spellings in params:
            if not set(spellings) & listed[path] and f"{path} {spellings[0]}" not in UNLISTED_FLAGS:
                missing.append(f"{path} {spellings[0]}")
    assert not missing, (
        f"{len(missing)} flag(s) named in no CLI table row: {sorted(missing)}. Add each to the "
        "Key flags cell of the row that names its command, or add an entry with a reason to "
        "UNLISTED_FLAGS."
    )
    real = {f"{path} {s[0]}" for path, params in command_options().items() for s in params}
    stale = sorted(set(UNLISTED_FLAGS) - real)
    assert not stale, f"UNLISTED_FLAGS names flags that are gone: {stale}"


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
    """Every page under ``docs/``, keyed by its path relative to ``docs/``.

    ``rglob``, not ``glob``: ``docs/`` is flat today, but the rule is "every page is assigned",
    and a top-level glob would silently exempt a whole shape of new page (``docs/guides/x.md``)
    instead of forcing a decision about it.
    """
    return {path.relative_to(DOCS_DIR).as_posix() for path in DOCS_DIR.rglob("*.md")}


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


@pytest.mark.parametrize("page", sorted(ONLINE_ONLY))
def test_each_skill_names_the_online_only_pages_it_does_not_ship(page: str) -> None:
    """An agent must be told where the unshipped pages are, not left to guess they exist.

    Over :data:`ONLINE_ONLY` itself rather than a literal list, so the two pages cannot drift
    apart about which pages are online-only — they did once — and a new entry has to be written
    into both References sections instead of into only the one whose author added it.
    """
    for skill in SKILLS:
        text = TEXT[skill.name]
        assert "Online only" in text, skill.name
        assert page in text, (skill.name, page)


# --------------------------------------------------------------------------------------------
# c. every construct the schema defines appears in the authoring skill
# --------------------------------------------------------------------------------------------

#: Schema constructs deliberately absent from the authoring skill, keyed ``<Model>.<field>``
#: (``RetryPolicy.attempts``), each with the reason. Empty on purpose: every field an author can
#: write is worth a mention. An entry here is a decision, and a field nobody decided about fails
#: the test instead of disappearing quietly. One key shape serves both rules below, so an
#: exemption a maintainer writes actually exempts something.
UNDOCUMENTED_FIELDS: dict[str, str] = {}


#: Models ``rayspec.schema`` exports that no walk from the roots reaches, and why. An abstract
#: base is never written on its own; every field it declares is checked through the concrete
#: models that inherit it.
NOT_AUTHORED: dict[str, str] = {
    "StrictModel": "the base every model inherits — it declares no field of its own, it only "
    "turns unknown keys into errors",
    "StepBase": "abstract: the fields every step shares, checked through the eight concrete step "
    "models that inherit them",
    "LeafStep": "abstract: the extra fields of prompt/shell/python steps, checked through those "
    "three concrete models",
}


def _field_names(model: type[Any]) -> set[str]:
    return {(field.alias or name) for name, field in model.model_fields.items()}


def _owned_fields(model: type[BaseModel]) -> set[tuple[str, str]]:
    return {(model.__name__, name) for name in _field_names(model)}


def schema_constructs() -> dict[str, set[tuple[str, str]]]:
    """Every construct an author writes, grouped, derived from the pydantic models.

    Each member is ``(owning model, name)`` so a deliberate omission is written the same way
    everywhere — ``RetryPolicy.attempts``, ``PromptStep.session`` — and the skill is searched for
    the bare name, which is how a field is actually written in prose.
    """
    step_fields: set[tuple[str, str]] = set()
    for model in STEP_MODELS.values():
        step_fields |= _owned_fields(model)
    return {
        "step kinds": {(model.__name__, kind) for kind, model in STEP_MODELS.items()},
        "step fields": step_fields,
        "input fields": _owned_fields(InputSpec),
        "agent fields": _owned_fields(AgentDef) | _owned_fields(AgentOverride),
        "workflow fields": _owned_fields(Workflow),
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
    missing = sorted(
        f"{owner}.{name}"
        for owner, name in schema_constructs()[group]
        if name not in tokens and f"{owner}.{name}" not in UNDOCUMENTED_FIELDS
    )
    assert not missing, (
        f"{group} the schema defines but rayspec-workflows never mentions: {missing}. Document "
        "them (the Field index table is the natural place), or add an entry with a reason to "
        "UNDOCUMENTED_FIELDS."
    )


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    """Every pydantic model one field annotation can hold — through unions, lists and dicts."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in get_args(annotation):
        yield from _models_in(arg)


def authorable_models() -> dict[str, type[BaseModel]]:
    """Every pydantic model an author can write a field of, **walked** from the roots.

    Derived rather than listed on purpose. A step field whose value is itself a mapping hides a
    whole sub-vocabulary (``retry:``, ``loop:``, ``mcp.<server>:``, ``defaults:``) — which is
    exactly where the old skill was thinnest — and a hand-kept list of those mappings goes red
    when one is *removed* but stays green when one is *added*. Adding is the realistic direction:
    a field that is a ``Literal`` today (``isolation:``, ``network:``, ``access:``) is the kind
    that grows into a mapping tomorrow. The walk reaches it the day it does.
    """
    found: dict[str, type[BaseModel]] = {}
    stack: list[type[BaseModel]] = [
        Workflow,
        AgentDef,
        AgentOverride,
        InputSpec,
        *STEP_MODELS.values(),
    ]
    while stack:
        model = stack.pop()
        if model.__name__ in found:
            continue
        found[model.__name__] = model
        for field in model.model_fields.values():
            stack.extend(_models_in(field.annotation))
    return found


def test_every_nested_spec_field_appears_in_the_authoring_skill() -> None:
    tokens = documented_tokens(TEXT[WORKFLOWS_SKILL.name])
    missing: list[str] = []
    for name, model in sorted(authorable_models().items()):
        for field in sorted(_field_names(model)):
            if field not in tokens and f"{name}.{field}" not in UNDOCUMENTED_FIELDS:
                missing.append(f"{name}.{field}")
    assert not missing, (
        f"nested spec field(s) rayspec-workflows never mentions: {missing}. Document them, or "
        "add an entry with a reason to UNDOCUMENTED_FIELDS."
    )


def test_the_walk_reaches_every_model_the_schema_package_exports() -> None:
    """The walk is only total if it actually arrives everywhere.

    Every model an author writes is reachable from one of the roots today, but the roots are a
    *choice* — ``AgentDef`` is one because agents live in their own files. A second hand-written
    file kind would arrive with a model that no field of ``Workflow`` points at, and the walk
    would skip it in silence. Comparing against what the package exports turns that into a
    decision instead.
    """
    exported = {
        name
        for name in rayspec.schema.__all__
        if isinstance(obj := getattr(rayspec.schema, name), type) and issubclass(obj, BaseModel)
    }
    unreachable = sorted(exported - set(authorable_models()) - set(NOT_AUTHORED))
    assert not unreachable, (
        f"rayspec.schema exports model(s) the walk never reaches: {unreachable}. Either a root "
        "is missing from authorable_models(), or the model is not something an author writes — "
        "in which case name it in NOT_AUTHORED with the reason."
    )
    stale = sorted(set(NOT_AUTHORED) - exported)
    assert not stale, f"NOT_AUTHORED names models that are gone: {stale}"


def test_the_deny_lists_are_named_and_justified() -> None:
    for reason in [*UNDOCUMENTED_FIELDS.values(), *NOT_AUTHORED.values(), *UNLISTED_FLAGS.values()]:
        assert len(reason.strip()) > 20, reason

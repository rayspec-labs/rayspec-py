"""Total rules over the operating skill's two classifications.

``test_skill_totality.py`` makes the *command table* total: every command of the app is in one of
the two skills. That leaves the two places inside ``rayspec-cli/SKILL.md`` where the page
classifies those same commands and could quietly stop being complete:

* the **Safety** table — read-only vs. writes-locally vs. executes-agents. An agent that has to
  decide whether it may run something unattended reads that table and nothing else, so a command
  missing from it is not a documentation gap, it is a command with no stated blast radius.
* the **``--json`` contract** table — JSONL stream vs. stored JSONL vs. one document. A caller
  parses stdout on the strength of that row.

Both rules derive their expected set from the code (the CLI table, and the Typer app's ``--json``
options) rather than from a list written here, so *adding* a command forces a decision instead of
passing in silence. A deliberate omission is a named entry with a reason in one of the deny-lists
below. **A test that cannot fail is worse than no test**, which is exactly how the single skill
went ten commands stale.
"""

from __future__ import annotations

import re
from typing import Any

from typer.main import get_command

from rayspec.cli.app import build_app
from rayspec.skill import CLI_SKILL, skill_dir

from .test_skill_totality import listed_commands

TEXT = (skill_dir(CLI_SKILL) / "SKILL.md").read_text(encoding="utf-8")

#: Commands the Safety table deliberately does not classify, and why. Empty on purpose: an agent
#: must be able to look up any command it is about to run.
UNCLASSIFIED_BY_SAFETY: dict[str, str] = {}

#: Commands that take ``--json`` but whose output shape the contract table deliberately omits.
#: Empty on purpose: the shape is what a caller has to know before it parses stdout.
UNSTATED_JSON_SHAPE: dict[str, str] = {}


def _section(heading: str) -> str:
    """The body of one ``## `` section of the page, up to the next ``## `` heading."""
    assert heading in TEXT, heading
    return TEXT.split(heading, 1)[1].split("\n## ", 1)[0]


def _classified_rows(section: str) -> dict[str, set[str]]:
    """``{class label: {command path, …}}`` from a table whose rows start ``| **<label>**``.

    A command path is a backticked span of lowercase words (``run``, ``trust list``); anything
    else in the cell — a flag, a prose aside — is not a path and is ignored.
    """
    found: dict[str, set[str]] = {}
    for line in section.splitlines():
        if not line.startswith("| **"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        label = cells[0].strip("* ")
        assert label not in found, label
        found[label] = set(re.findall(r"`([a-z]+(?: [a-z]+)*)`", cells[1]))
    return found


def _assert_total(
    classified: dict[str, set[str]],
    expected: set[str],
    denied: dict[str, str],
    what: str,
    *,
    unreal_hint: str = "are not in the CLI table",
) -> None:
    seen: dict[str, str] = {}
    for label, paths in classified.items():
        for path in paths:
            assert path not in seen, f"{path!r} is in both {seen[path]!r} and {label!r}"
            seen[path] = label
    missing = sorted(expected - set(seen) - set(denied))
    assert not missing, (
        f"{len(missing)} command(s) the {what} does not classify: {missing}. Put each in a row, "
        "or add an entry with a reason to the deny-list in this file."
    )
    unreal = sorted(set(seen) - expected)
    assert not unreal, f"the {what} classifies commands that {unreal_hint}: {unreal}"
    stale = sorted(set(denied) - expected)
    assert not stale, f"the deny-list names commands that are gone: {stale}"
    both = sorted(set(denied) & set(seen))
    assert not both, f"the deny-list names commands that ARE classified: {both}"
    for path, reason in denied.items():
        assert len(reason.strip()) > 20, path


def test_the_safety_table_classifies_every_command_the_skill_documents() -> None:
    classified = _classified_rows(_section("## Safety"))
    assert set(classified) == {"read-only", "writes locally", "executes agents"}, set(classified)
    _assert_total(
        classified, set(listed_commands(CLI_SKILL)), UNCLASSIFIED_BY_SAFETY, "Safety table"
    )


def json_commands() -> set[str]:
    """Every leaf command (and invokable group) of the builtin app that takes ``--json``."""
    root: Any = get_command(build_app(plugins=False))
    found: set[str] = set()

    def walk(group: Any, prefix: str) -> None:
        for name, cmd in group.commands.items():
            path = f"{prefix}{name}"
            if hasattr(cmd, "commands"):
                if getattr(cmd, "invoke_without_command", False) and _takes_json(cmd):
                    found.add(path)
                walk(cmd, f"{path} ")
            elif _takes_json(cmd):
                found.add(path)

    def _takes_json(cmd: Any) -> bool:
        return any("--json" in param.opts for param in cmd.params)

    walk(root, "")
    return found


def test_the_json_contract_states_a_shape_for_every_command_that_takes_json() -> None:
    classified = _classified_rows(_section("## Exit codes and the `--json` contract"))
    assert set(classified) == {"JSONL stream", "stored JSONL, verbatim", "one JSON document"}
    expected = json_commands() & set(listed_commands(CLI_SKILL))
    _assert_total(
        classified,
        expected,
        UNSTATED_JSON_SHAPE,
        "`--json` contract table",
        unreal_hint="do not take --json (or are in neither CLI table)",
    )

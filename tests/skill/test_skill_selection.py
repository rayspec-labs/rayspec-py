"""The operating skill's ``## Selecting a workflow from a request`` section (PRD-05).

The skill is the router — rayspec has none — so what it tells an agent to type has to stay true:
the discovery command exists, the rules are the ones this repository decided on, and every
``rayspec run <name>`` it shows names a workflow that ships and dry-runs with exactly the inputs
shown. The live half (headless Claude Code choosing for real) is ``test_skill_selection_live.py``.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.loader.bundled import bundled_dir
from rayspec.skill import CLI_SKILL, WORKFLOWS_SKILL

from .test_skill_content import SHAPE, TEXT, frontmatter

CLI_MD = TEXT[CLI_SKILL.name]
WORKFLOWS_MD = TEXT[WORKFLOWS_SKILL.name]
HEADING = "## Selecting a workflow from a request"
BUNDLED = {p.stem for p in bundled_dir().glob("*.yaml")}
#: A stub-less `fix_issue` dry run cannot converge (the loop waits for BUILD-CLEAN), so its row is
#: driven with the script `rayspec test` uses for it (tests/workflows/checks.yaml `fix_happy`).
STUBS = {
    "fix_issue": Path(__file__).resolve().parents[1] / "workflows" / "stubs" / "fix_issue.yaml"
}
#: A dry run that ends in a decision, not a defect: with every shell step a stand-in, architect
#: finds nothing to survey and stops (`cancelled`, exit 4 — checks.yaml `architect_empty`).
EXPECTED_EXIT = {"architect": 4}


def section() -> str:
    assert HEADING in CLI_MD, "the operating skill has no selection section"
    return CLI_MD.split(HEADING, 1)[1].split("\n## ", 1)[0]


def table_rows() -> list[tuple[str, str]]:
    """``(request, first backticked span of the command cell)`` per row of the examples table."""
    rows: list[tuple[str, str]] = []
    for line in section().splitlines():
        if not line.startswith("| ") or line.startswith(("| Request", "|---")):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        assert len(cells) == 2, line
        span = re.search(r"`([^`]+)`", cells[1])
        assert span is not None, line
        rows.append((cells[0], span.group(1)))
    return rows


ROWS = table_rows() if HEADING in CLI_MD else []


def test_the_section_is_pinned_between_the_json_contract_and_the_operating_loops() -> None:
    headings, _low, _high = SHAPE[CLI_SKILL.name]
    i = headings.index(HEADING)
    assert headings[i - 1] == "## Exit codes and the `--json` contract"
    assert headings[i + 1] == "## Operating loops"


def test_discovery_is_a_command_not_a_list() -> None:
    text = section()
    assert "`rayspec workflows --json`" in text
    assert "never from memory" in text
    for word in (
        "`project`",
        "`overridden`",
        "`bundled`",
        "`description`",
        "`inputs`",
        "`required`",
    ):
        assert word in text, word


def test_the_ask_rule_is_stated() -> None:
    text = section()
    assert "**Ask instead of guessing**" in text
    assert "two workflows fit" in text
    assert "`create_issue`" in text


def test_the_propose_or_run_rule_is_stated() -> None:
    text = section()
    assert "`rayspec plan <wf> --input … --json`" in text
    assert "`agents[].access`" in text and "`read-only`" in text
    assert "**state the command, then run it**" in text
    assert "**print the command and wait**" in text


def test_the_dry_run_first_rule_is_stated() -> None:
    text = section()
    assert "`isolation`" in text and "`worktree`" in text and "`workspace-write`" in text
    assert "`rayspec runs --json`" in text
    assert "**propose `--dry-run` first**" in text


def test_the_safety_lead_agrees_with_the_selection_rule() -> None:
    safety = CLI_MD.split("## Safety", 1)[1].split("\n## ", 1)[0]
    assert "**Ask the human before any run that writes**" in safety
    assert "run unasked" in safety
    assert "#selecting-a-workflow-from-a-request" in safety


def test_the_examples_table_covers_the_prd_acceptance_requests() -> None:
    requests = [r.lower() for r, _ in ROWS]
    assert any("security and performance" in r for r in requests), requests
    assert any("already broken" in r for r in requests), requests
    assert any(r.startswith("fix issue") for r in requests), requests
    assert any("fix the failing issue" in r for r in requests), requests


def test_every_run_command_in_the_section_names_a_bundled_workflow() -> None:
    """The staleness guard: a renamed or dropped bundled workflow goes red here, not in a
    user's session."""
    named = set(re.findall(r"`rayspec run ([a-z_]+)", section()))
    assert named, "the section shows no `rayspec run <name>`"
    assert named <= BUNDLED, named - BUNDLED
    for _request, command in ROWS:
        assert command.startswith("rayspec run "), command


@pytest.mark.parametrize(
    ("command",), [(c,) for _, c in ROWS], ids=[shlex.split(c)[2] for _, c in ROWS]
)
def test_each_complete_table_command_dry_runs_in_an_empty_project(
    command: str, tmp_path: Path
) -> None:
    """Exactly the inputs the table shows, no `.rayspec/`, no git, a fresh `RAYSPEC_HOME`: the
    command is accepted (never exit 2) and the graph runs to its documented end."""
    if "<" in command:
        pytest.skip("an ask row: the placeholder is the point")
    words = shlex.split(command)
    name = words[2]
    root, home = tmp_path / "proj", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    args = [*words[1:], "--dry-run", "--root", str(root), "--json"]
    if name in STUBS:
        args += ["--stubs", str(STUBS[name])]
    res = CliRunner().invoke(app, args, env={"RAYSPEC_HOME": str(home)})
    assert res.exit_code == EXPECTED_EXIT.get(name, 0), res.output
    summary = json.loads(res.stdout.splitlines()[-1])
    assert summary["run_id"] and summary["status"] in {"succeeded", "cancelled"}, summary


def test_the_cli_description_makes_claude_code_load_the_skill_for_a_plain_request() -> None:
    description = str(frontmatter(CLI_MD)["description"])
    assert "rayspec workflows --json" in description
    for needle in ("fix issue 42", "review PR 118", "names no workflow"):
        assert needle in description, needle


def test_the_authoring_skill_routes_selection_to_the_operating_skill() -> None:
    description = str(frontmatter(WORKFLOWS_MD)["description"])
    assert "fix issue 42" in description and "rayspec-cli" in description
    assert "`rayspec workflows --json`" in WORKFLOWS_MD
    assert HEADING not in WORKFLOWS_MD  # one home for the rule: the operating skill

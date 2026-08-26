"""Live: headless Claude Code, with both packaged skills installed into a scratch project, maps
the PRD-05 acceptance requests onto commands — and a project workflow no skill text mentions is
selectable without a skill edit.

Opt in with ``RAYSPEC_LIVE=1`` (needs a logged-in ``claude``); deselected by the gate's
``-m 'not live'``. A ``claude -p`` that inherits ``CLAUDECODE=1`` from an enclosing Claude Code
session hangs at startup, so the child environment below strips every ``CLAUDE*`` variable except
``CLAUDE_CONFIG_DIR``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.skill import SKILLS, install_skill, project_skill_dir

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("RAYSPEC_LIVE"), reason="set RAYSPEC_LIVE=1 to drive Claude Code"
    ),
    pytest.mark.skipif(shutil.which("claude") is None, reason="needs the claude CLI on PATH"),
]

MODEL = os.environ.get("RAYSPEC_LIVE_CLAUDE_MODEL", "sonnet")
SCHEMA = {
    "type": "object",
    "properties": {
        "workflow": {"type": "string"},
        "command": {"type": "string"},
        "needs_question": {"type": "boolean"},
        "question": {"type": "string"},
    },
    "required": ["workflow", "command", "needs_question"],
}
#: Exactly what the selection section tells the agent to run, and nothing that writes.
ALLOWED_TOOLS = "Bash(rayspec workflows *),Bash(rayspec plan *),Bash(rayspec runs *),Skill,Read"

#: A project workflow whose name appears in no skill text.
TRANSLATE_DOCS = """\
rayspec: 1
name: translate_docs
description: Translate every Markdown page under docs/ into a language.
inputs:
  language: { type: string, required: true, description: "Target language, e.g. German" }
agents:
  translator: { provider: claude, model: small, access: workspace-write }
steps:
  - id: translate
    agent: translator
    prompt: "Translate every Markdown file under docs/ into {{ inputs.language }}."
"""

PROMPT = """\
Load the rayspec-cli skill with the Skill tool and follow its section
"Selecting a workflow from a request" for this request from the human:

    {request}

Decide only — do NOT run any `rayspec run`. Use `rayspec workflows --json`,
`rayspec plan <workflow> --input ... --json` and `rayspec runs --json` as the skill says, as plain
commands (no pipes, no jq, no gh, no git). Then answer with the structured object: `workflow` (the
name you chose, or "" if you cannot choose), `command` (the complete `rayspec run ...` line you
would propose, every input you could fill, `--input name=value`), `needs_question` (true when a
required input or the choice itself is unresolved) and `question` (what you would ask, else "").
"""


@pytest.fixture
def scratch(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "translate_docs.yaml").write_text(TRANSLATE_DOCS)
    (root / "docs").mkdir()
    (root / "docs" / "index.md").write_text("# Docs\n")
    for skill in SKILLS:
        install_skill(skill, project_skill_dir(skill, root))
    home = tmp_path / "home"
    home.mkdir()
    # the fixture is sound before a token is spent
    res = CliRunner().invoke(
        app, ["validate", "translate_docs", "--root", str(root)], env={"RAYSPEC_HOME": str(home)}
    )
    assert res.exit_code == 0, res.output
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE") or k == "CLAUDE_CONFIG_DIR"
    }
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    env["RAYSPEC_HOME"] = str(home)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return root, env


def select(scratch: tuple[Path, dict[str, str]], request: str) -> dict[str, object]:
    root, env = scratch
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA),
        "--model",
        MODEL,
        "--max-turns",
        "12",
        "--max-budget-usd",
        "1.00",
        "--no-session-persistence",
        "--setting-sources",
        "project",  # <root>/.claude/skills only; never ~/.claude/skills
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ALLOWED_TOOLS,  # variadic: keep it last, the prompt goes on stdin
    ]
    proc = subprocess.run(
        cmd,
        cwd=root,
        env=env,
        input=PROMPT.format(request=request),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    data = json.loads(proc.stdout)
    assert data.get("subtype") == "success", data
    out = data.get("structured_output")
    assert isinstance(out, dict), data
    print(
        json.dumps(
            {
                "request": request,
                "decision": out,
                "turns": data.get("num_turns"),
                "usd": data.get("total_cost_usd"),
            }
        )
    )
    return out


def inputs_of(command: object) -> dict[str, str]:
    words = shlex.split(str(command))
    assert words[:2] == ["rayspec", "run"], command
    pairs = [
        words[i + 1] for i, w in enumerate(words) if w in ("--input", "-i") and i + 1 < len(words)
    ]
    return dict(p.split("=", 1) for p in pairs)


def test_a_security_and_performance_review_is_a_panel(scratch: tuple[Path, dict[str, str]]) -> None:
    out = select(scratch, "review PR 118 from a security and performance angle")
    assert out["workflow"] == "review_panel", out
    assert out["needs_question"] is False, out
    given = inputs_of(out["command"])
    assert given.get("pr") == "118", given
    lenses = json.loads(given["lenses"])
    assert {"security", "performance"} <= set(lenses), lenses


def test_was_it_already_broken_is_a_measurement(scratch: tuple[Path, dict[str, str]]) -> None:
    out = select(scratch, "Is PR 118 broken or was it already broken?")
    assert out["workflow"] == "validate_pr", out
    assert inputs_of(out["command"]).get("pr") == "118", out


def test_a_project_workflow_is_selectable_without_a_skill_edit(
    scratch: tuple[Path, dict[str, str]],
) -> None:
    out = select(scratch, "translate the docs into German")
    assert out["workflow"] == "translate_docs", out
    assert inputs_of(out["command"]).get("language", "").lower().startswith("german"), out


def test_a_missing_required_input_is_a_question_not_a_guess(
    scratch: tuple[Path, dict[str, str]],
) -> None:
    out = select(scratch, "fix the failing issue")
    assert out["needs_question"] is True, out
    assert out.get("question"), out
    assert out["workflow"] in ("", "fix_issue"), out

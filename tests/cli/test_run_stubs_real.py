"""``--stubs`` without ``--dry-run`` drives a REAL run when every resolved agent of the
workflow uses ``provider: stub``; a workflow that would run a non-stub agent is still refused
(exit 2) with a hint to switch the agents to the stub provider."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rayspec.cli.app import app

STUB_ONLY = """
rayspec: 1
name: example
isolation: none
agents:
  reviewer: {provider: stub}
steps:
  - {id: files, shell: echo README.md}
  - id: review
    needs: [files]
    agent: reviewer
    prompt: "review {{ steps.files.output }}"
    output_schema:
      type: object
      properties: {verdict: {type: string}, summary: {type: string}}
      required: [verdict, summary]
outputs:
  verdict: "{{ steps.review.output.verdict }}"
  summary: "{{ steps.review.output.summary }}"
"""

MIXED = """
rayspec: 1
name: mixed
isolation: none
agents:
  reviewer: {provider: stub}
  writer: {provider: claude}
steps:
  - {id: a, agent: reviewer, prompt: one}
  - {id: b, needs: [a], agent: writer, prompt: two}
  - {id: c, needs: [b], agent: writer, prompt: three}
  - {id: d, needs: [c], agent: claude, prompt: four}
  - {id: e, needs: [d], agent: {provider: codex}, prompt: five}
"""

STUBS = """
steps:
  review: {output: {verdict: approve, summary: "Small and readable; nothing blocks."}}
"""


def _write(project: Path, name: str, text: str) -> None:
    (project / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text, encoding="utf-8")


def test_stubs_drive_a_real_run_of_stub_agents(cli: CliRunner, home: Path, project: Path) -> None:
    _write(project, "example", STUB_ONLY)
    stubs = project / "stubs.yaml"
    stubs.write_text(STUBS, encoding="utf-8")
    res = cli.invoke(
        app, ["run", "example", "--root", str(project), "--stubs", str(stubs), "--json"]
    )
    assert res.exit_code == 0, res.output
    summary = json.loads(res.stdout.strip().splitlines()[-1])
    assert summary["outputs"] == {
        "verdict": "approve",
        "summary": "Small and readable; nothing blocks.",
    }
    # a real run: shell steps executed, not a dry-run rehearsal
    (run_json,) = home.rglob("runs/*/run.json")
    record = json.loads(run_json.read_text())
    assert record["dry_run"] is False
    assert record["steps"]["files"]["status"] == "succeeded"
    assert record["steps"]["review"]["provider"] == "stub"


def test_stubs_without_dry_run_refused_when_a_non_stub_agent_would_run(
    cli: CliRunner, home: Path, project: Path
) -> None:
    _write(project, "mixed", MIXED)
    stubs = project / "stubs.yaml"
    stubs.write_text("steps: {a: {text: x}}\n", encoding="utf-8")
    res = cli.invoke(app, ["run", "mixed", "--root", str(project), "--stubs", str(stubs)])
    assert res.exit_code == 2, res.output
    assert "--stubs requires --dry-run" in res.output
    assert "switch the agents to provider: stub" in res.output
    # user-facing agent names, de-duplicated — not the loader's internal keys
    assert "agents 'writer' (claude), 'claude' (claude), 'e (inline)' (codex)" in res.output
    assert "agents.writer" not in res.output and "provider:claude" not in res.output
    assert not list(home.rglob("runs/*/run.json"))

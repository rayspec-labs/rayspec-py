"""Fresh-agent simulation: a workflow authored from the installed skills alone (no repo access)
passes ``validate`` → ``plan`` → ``--dry-run --stubs-init`` → ``--dry-run --stubs`` first time.

The YAML below is what a coding agent wrote after reading only the installed skills: 5 root steps
(inputs incl. an enum, a Claude read-only agent with ``output_schema``, a ``loop:`` with
``until``/``has_signal``, a ``shell:`` step using the env-ref rule, an ``approve:`` gate,
``outputs:``). It is the acceptance criterion of the two skills and doubles as their regression test.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.skill import SKILLS, install_skill, project_skill_dir

FRESH_AGENT_WORKFLOW = """\
rayspec: 1
name: triage_and_fix
description: Classify a bug report, fix it in a review loop, confirm, and report.
inputs:
  report:   { type: string, required: true, description: "Bug report text" }
  severity: { type: string, enum: [low, high], default: low }
agents:
  classifier:
    provider: claude
    model: small
    access: read-only
    instructions: Classify bug reports and review fixes. Be terse.
  fixer:
    provider: claude
    model: medium
    access: workspace-write
steps:
  - id: classify
    agent: classifier
    prompt: |
      Bug report (severity {{ inputs.severity }}):
      {{ inputs.report }}
      Classify it.
    output_schema:
      type: object
      properties:
        area: { type: string, enum: [docs, code, infra] }
        summary: { type: string }
      required: [area, summary]
  - id: bail
    needs: [classify]
    when: steps.classify.output.area == 'infra'
    stop: { status: cancelled, reason: "infra issues are handled elsewhere: {{ steps.classify.output.summary }}" }
  - id: fix
    needs: [classify]
    when: steps.classify.output.area != 'infra'
    loop:
      max_iterations: 3
      until: steps.review.output | has_signal('LGTM')
      steps:
        - id: patch
          agent: fixer
          session: patch
          prompt: |
            {% if iteration.first %}Fix this {{ steps.classify.output.area }} bug: {{ steps.classify.output.summary }}
            {% else %}Address the review: {{ iteration.prev.review.output }}{% endif %}
        - id: review
          needs: [patch]
          agent: classifier
          prompt: |
            Review the change for "{{ steps.classify.output.summary }}".
            Reply with a whole line LGTM when nothing is left to fix.
  - id: report
    needs: [fix]
    shell: |
      printf 'area=%s iterations=%s severity=%s\\n' "{{ steps.classify.output.area }}" \\
        "{{ steps.fix.iterations }}" "$RAYSPEC_INPUT_SEVERITY"
  - id: confirm
    needs: [report]
    approve: "Ship the fix for '{{ steps.classify.output.summary }}'?"
outputs:
  area: "{{ steps.classify.output.area }}"
  report: "{{ steps.report.output }}"
  iterations: "{{ steps.fix.iterations }}"
"""


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    home.mkdir()
    (root / ".rayspec" / "workflows" / "triage_and_fix.yaml").write_text(FRESH_AGENT_WORKFLOW)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=root, check=True)
    # the only rayspec material the agent had: both installed skills
    for skill in SKILLS:
        install_skill(skill, project_skill_dir(skill, root))
    return root, home


def test_fresh_agent_workflow_passes_the_authoring_loop_first_try(tmp_path: Path) -> None:
    root, home = _project(tmp_path)
    env = {"RAYSPEC_HOME": str(home)}
    runner = CliRunner()
    for skill in SKILLS:
        assert (project_skill_dir(skill, root) / "SKILL.md").is_file()

    res = runner.invoke(app, ["validate", "--root", str(root)], env=env)
    assert res.exit_code == 0 and "OK" in res.stdout, res.output
    assert "warning" not in res.stdout.lower(), res.output

    res = runner.invoke(
        app, ["plan", "triage_and_fix", "--root", str(root), "-i", "report=crash on start"], env=env
    )
    assert res.exit_code == 0, res.output
    assert "classifier" in res.stdout and "read-only" in res.stdout

    stubs = tmp_path / "stubs.yaml"
    res = runner.invoke(
        app,
        [
            "run",
            "triage_and_fix",
            "--root",
            str(root),
            "--dry-run",
            "--stubs-init",
            str(stubs),
            "-i",
            "report=crash on start",
        ],
        env=env,
    )
    assert res.exit_code == 0 and stubs.is_file(), res.output
    data = yaml.safe_load(stubs.read_text())
    assert set(data["steps"]) == {"classify", "fix[*]/patch", "fix[*]/review"}
    # what the skill tells the agent to do: make the loop converge with a sequence
    data["steps"]["fix[*]/review"] = {"sequence": ["Please add a test.", "LGTM"]}
    data["steps"]["classify"] = {"output": {"area": "code", "summary": "null deref on start"}}
    stubs.write_text(yaml.safe_dump(data))

    res = runner.invoke(
        app,
        [
            "run",
            "triage_and_fix",
            "--root",
            str(root),
            "--dry-run",
            "--stubs",
            str(stubs),
            "-i",
            "report=crash on start",
            "-i",
            "severity=high",
            "--json",
        ],
        env=env,
    )
    assert res.exit_code == 0, res.output
    summary = json.loads(res.stdout.splitlines()[-1])
    assert summary["status"] == "succeeded" and summary["exit_code"] == 0
    assert summary["outputs"]["area"] == "code"
    assert summary["outputs"]["iterations"] == 2
    events = [json.loads(line) for line in res.stdout.splitlines()[:-1]]
    finished = {e["step_path"]: e["data"]["status"] for e in events if e["type"] == "step.finished"}
    assert finished["bail"] == "skipped"
    assert finished["confirm"] == "succeeded"  # dry run auto-approves the gate
    assert finished["report"] == "succeeded"  # shell steps are skipped-as-success in a dry run


def test_fresh_agent_workflow_stops_on_the_infra_branch(tmp_path: Path) -> None:
    root, home = _project(tmp_path)
    stubs = tmp_path / "stubs.yaml"
    stubs.write_text(
        yaml.safe_dump({"steps": {"classify": {"output": {"area": "infra", "summary": "dns"}}}})
    )
    res = CliRunner().invoke(
        app,
        [
            "run",
            "triage_and_fix",
            "--root",
            str(root),
            "--dry-run",
            "--stubs",
            str(stubs),
            "-i",
            "report=dns down",
            "--json",
        ],
        env={"RAYSPEC_HOME": str(home)},
    )
    assert res.exit_code == 4, res.output
    summary = json.loads(res.stdout.splitlines()[-1])
    assert summary["status"] == "cancelled"
    assert "infra issues" in summary["reason"]

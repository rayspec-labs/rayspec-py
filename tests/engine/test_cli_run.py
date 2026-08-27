"""e2e: `rayspec run` on a temp project (shell → prompt(stub) → when/stop → loop with shell check)
in --dry-run and with a stub script; run.json / outputs / events.jsonl / exit codes / --resume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.store.file import FileRunStore

WORKFLOW = """
rayspec: 1
name: fixit
description: shell → prompt → when/stop → loop
inputs:
  issue: {type: integer, required: true}
  mode: {type: string, enum: [fix, skip], default: fix}
isolation: none
steps:
  - id: fetch
    shell: echo "issue $RAYSPEC_INPUT_ISSUE"
  - id: assess
    needs: [fetch]
    prompt: |
      {{ steps.fetch.output }}
      mode={{ inputs.mode }}
    output_schema:
      type: object
      properties: {verdict: {enum: [fix, skip]}, reason: {type: string}}
      required: [verdict, reason]
  - id: bail
    needs: [assess]
    when: steps.assess.output.verdict == 'skip'
    stop: {status: cancelled, reason: "skipping: {{ steps.assess.output.reason }}"}
  - id: build
    needs: [assess]
    when: steps.assess.output.verdict == 'fix'
    loop:
      max_iterations: 3
      until: steps.check.ok
      steps:
        - id: implement
          prompt: "fix it (attempt {{ iteration.n }})"
        - id: check
          needs: [implement]
          shell: |
            test {{ iteration.n }} -ge {{ inputs.issue }} && echo ok
          allow_failure: true
outputs:
  verdict: "{{ steps.assess.output.verdict }}"
  iterations: "{{ steps.build.iterations }}"
"""

STUBS_FIX = """
steps:
  assess: {output: {verdict: fix, reason: "real bug"}}
  "build[*]/implement": {sequence: ["try 1", "try 2", "try 3"]}
"""

STUBS_SKIP = """
steps:
  assess: {output: {verdict: skip, reason: "not worth it"}}
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "fixit.yaml").write_text(WORKFLOW, encoding="utf-8")
    (root / "stubs_fix.yaml").write_text(STUBS_FIX, encoding="utf-8")
    (root / "stubs_skip.yaml").write_text(STUBS_SKIP, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    return root


def _store(tmp_path: Path) -> FileRunStore:
    projects = tmp_path / "home" / "projects"
    (slug_dir,) = [p for p in projects.glob("*/*") if (p / "runs").is_dir()]
    return FileRunStore(slug_dir)


def test_dry_run_with_stub_script_and_exec_shell(project: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "fixit",
            "--root",
            str(project),
            "--input",
            "issue=2",
            "--dry-run",
            "--exec-shell",
            "--stubs",
            str(project / "stubs_fix.yaml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output
    store = _store(tmp_path)
    (run_id,) = store.list_run_ids()
    run = store.load(run_id)
    assert run.status.value == "succeeded"
    assert run.outputs == {"verdict": "fix", "iterations": 2}
    assert run.inputs == {"issue": 2, "mode": "fix"}
    assert run.steps["bail"].status.value == "skipped"
    assert run.steps["build"].loop is not None and run.steps["build"].loop.iterations == 2
    assert run.steps["build[1]/check"].status.value == "failed"
    assert run.steps["build[1]/check"].tolerated is True
    assert run.steps["build[2]/check"].status.value == "succeeded"
    assert run.steps["assess"].provider == "stub"
    assert run.steps["build[2]/implement"].output_ref
    assert store.read_output(run_id, run.steps["build[2]/implement"].output_ref or "") == "try 2"
    events = list(store.read_events(run_id))
    types = [e.type.value for e in events]
    assert types[0] == "run.started" and types[-1] == "run.finished"
    assert "loop.iteration" in types and types.count("step.finished") == len(run.steps)
    assert (store.run_dir(run_id) / "events.jsonl").is_file()
    assert (store.run_dir(run_id) / "steps" / "fetch" / "stdout.log").read_text() == "issue 2\n"


def test_dry_run_without_exec_shell_skips_shell_and_auto_approves(
    project: Path, tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        app, ["run", "fixit", "--root", str(project), "-i", "issue=1", "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert lines[0]["type"] == "run.started"
    finished = [
        ln for ln in lines if ln.get("type") == "step.finished" and ln["step_path"] == "fetch"
    ]
    assert finished and finished[0]["data"].get("dry_run") is True
    assert lines[-1]["status"] == "succeeded"  # the summary line
    assert set(lines[-1]["usage"]) == {
        "input",
        "cached_input",
        "cache_write",
        "output",
        "reasoning",
    }
    store = _store(tmp_path)
    run = store.load(store.list_run_ids()[0])
    # no stub script: the stub returns a minimal schema instance → verdict 'fix' (first enum)
    assert run.outputs is not None and run.outputs["verdict"] == "fix"


def test_stop_branch_exits_4(project: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "fixit",
            "--root",
            str(project),
            "-i",
            "issue=1",
            "--dry-run",
            "--stubs",
            str(project / "stubs_skip.yaml"),
        ],
    )
    assert result.exit_code == 4, result.output
    assert "skipping: not worth it" in result.output
    store = _store(tmp_path)
    run = store.load(store.list_run_ids()[0])
    assert run.status.value == "cancelled" and run.reason == "skipping: not worth it"
    assert run.steps["bail"].status.value == "succeeded"
    assert run.steps["build"].status.value == "skipped"


def test_validation_and_input_errors_exit_2(project: Path) -> None:
    result = CliRunner().invoke(app, ["run", "fixit", "--root", str(project), "--dry-run"])
    assert result.exit_code == 2
    assert "issue" in result.output
    result = CliRunner().invoke(app, ["run", "nope", "--root", str(project)])
    assert result.exit_code == 2
    assert "unknown workflow" in result.output


def test_resume_after_crafted_failure(project: Path, tmp_path: Path) -> None:
    flag = tmp_path / "flag"
    (project / ".rayspec" / "workflows" / "crash.yaml").write_text(
        f"""
rayspec: 1
name: crash
isolation: none
steps:
  - {{id: a, shell: echo a}}
  - {{id: b, needs: [a], shell: "test -f {flag}"}}
  - {{id: c, needs: [b], shell: echo c}}
outputs:
  c: "{{{{ steps.c.output }}}}"
""",
        encoding="utf-8",
    )
    first = CliRunner().invoke(app, ["run", "crash", "--root", str(project)])
    assert first.exit_code == 1, first.output
    store = _store(tmp_path)
    (run_id,) = store.list_run_ids()
    assert store.load(run_id).steps["b"].status.value == "failed"
    flag.write_text("1")
    # inputs are fixed on resume
    bad = CliRunner().invoke(
        app, ["run", "crash", "--root", str(project), "--resume", run_id, "-i", "x=1"]
    )
    assert bad.exit_code == 2
    second = CliRunner().invoke(
        app, ["run", "crash", "--root", str(project), "--resume", run_id[:12]]
    )
    assert second.exit_code == 0, second.output
    assert "reused 1 step" in second.output
    run = store.load(run_id)
    assert run.status.value == "succeeded" and run.outputs == {"c": "c"}
    assert run.steps["b"].attempts == 2 and run.resume_count == 1
    # hash mismatch → refuse (exit 2) unless --force
    (project / ".rayspec" / "workflows" / "crash.yaml").write_text(
        "rayspec: 1\nname: crash\nisolation: none\nsteps:\n  - {id: a, shell: echo a2}\n",
        encoding="utf-8",
    )
    refused = CliRunner().invoke(app, ["run", "crash", "--root", str(project), "--resume", run_id])
    assert refused.exit_code == 2 and "changed since run" in refused.output
    forced = CliRunner().invoke(
        app, ["run", "crash", "--root", str(project), "--resume", run_id, "--force"]
    )
    assert forced.exit_code == 0, forced.output


def test_no_interactive_gate_pauses_exit_3_then_approve_resume(
    project: Path, tmp_path: Path
) -> None:
    (project / ".rayspec" / "workflows" / "gate.yaml").write_text(
        """
rayspec: 1
name: gate
isolation: none
steps:
  - {id: a, shell: echo a}
  - {id: ok, needs: [a], approve: "ship {{ steps.a.output }}?"}
  - {id: b, needs: [ok], shell: "echo {{ steps.ok.output }}"}
""",
        encoding="utf-8",
    )
    paused = CliRunner().invoke(app, ["run", "gate", "--root", str(project), "--no-interactive"])
    assert paused.exit_code == 3, paused.output
    assert "rayspec approve" in paused.output
    store = _store(tmp_path)
    (run_id,) = store.list_run_ids()
    run = store.load(run_id)
    assert run.status.value == "paused" and run.pause is not None and run.pause.token == "ok#1"
    # what `rayspec approve` (another scope) does: record the decision, then resume
    from rayspec.store.model import Decision

    run.pause.decision = Decision(approved=True, comment="yes", by="cli")
    store.save(run)
    resumed = CliRunner().invoke(
        app, ["run", "gate", "--root", str(project), "--resume", run_id, "--no-interactive"]
    )
    assert resumed.exit_code == 0, resumed.output
    run = store.load(run_id)
    assert run.status.value == "succeeded"
    assert store.read_output(run_id, run.steps["b"].output_ref or "") == "yes"
    # --yes auto-approves a fresh run
    auto = CliRunner().invoke(app, ["run", "gate", "--root", str(project), "--yes"])
    assert auto.exit_code == 0, auto.output


def test_stubs_init_writes_scaffold(project: Path) -> None:
    target = project / "scaffold.yaml"
    result = CliRunner().invoke(
        app, ["run", "fixit", "--root", str(project), "-i", "issue=1", "--stubs-init", str(target)]
    )
    assert result.exit_code == 0, result.output
    import yaml

    data = yaml.safe_load(target.read_text())
    assert data["steps"]["assess"] == {"output": {"verdict": "fix", "reason": ""}}
    # loop-body steps are keyed the way the engine names them at run time (a glob)
    assert data["steps"]["build[*]/implement"] == {"text": "[stub] implement"}
    assert "build/implement" not in data["steps"]


def test_stubs_without_dry_run_is_a_usage_error(project: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "fixit",
            "--root",
            str(project),
            "--input",
            "issue=2",
            "--stubs",
            str(project / "stubs_fix.yaml"),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--stubs requires --dry-run" in result.output


def test_quiet_hides_per_step_success_lines(project: Path) -> None:
    args = ["run", "fixit", "--root", str(project), "-i", "issue=2", "--dry-run", "--exec-shell"]
    args += ["--stubs", str(project / "stubs_fix.yaml"), "--quiet"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "fetch succeeded" not in result.output and "bail skipped" not in result.output
    # a tolerated failure is still a problem worth a line
    assert "build[1]/check failed (tolerated)" in result.output
    assert "succeeded" in result.output  # the run line / summary
    loud = CliRunner().invoke(app, args[:-1]).output
    assert "fetch succeeded" in loud

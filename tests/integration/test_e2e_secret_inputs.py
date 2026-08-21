"""End to end through the CLI: a ``secret: true`` input never lands under ``RAYSPEC_HOME``,
``plan``/``validate``/``show`` print ``<secret>`` / ``(secret)``, and every resume entry
(``resume``, ``approve``, ``reject``, ``run --resume``) re-obtains the value."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from ._helpers import invoke, run_records

SECRET = "ghp_SECRETTOKEN_ABCDEF"

WORKFLOW = """
rayspec: 1
name: sec
isolation: none
inputs:
  token: { type: string, secret: true, required: true, description: "API token" }
  issue: { type: integer, default: 7 }
agents:
  r: { provider: stub }
steps:
  - id: use
    shell: echo "len=${#RAYSPEC_INPUT_TOKEN}"
  - id: gate
    needs: [use]
    approve: "ship?"
  - id: after
    needs: [gate]
    shell: echo "again=${#RAYSPEC_INPUT_TOKEN} t=${#T}"
    env: { T: "{{ inputs.token }}" }
  - id: ask
    needs: [after]
    agent: r
    prompt: "issue {{ inputs.issue }}"
outputs:
  v: "{{ steps.after.output }}"
"""

STUBS = "steps:\n  ask: {text: scripted-answer}\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "sec.yaml").write_text(textwrap.dedent(WORKFLOW))
    (root / "stubs.yaml").write_text(STUBS)
    return root


def _grep(root: Path, needle: str) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and needle in p.read_text(errors="replace")
    )


def _paused_run(project: Path, home: Path) -> str:
    res = invoke(
        [
            "run",
            "sec",
            "--root",
            str(project),
            "--input",
            f"token={SECRET}",
            "--no-worktree",
            "--no-interactive",
            "--stubs",
            str(project / "stubs.yaml"),
        ],
        home,
    )
    assert res.exit_code == 3, res.output
    (record,) = run_records(home)
    assert record["status"] == "paused"
    return record["run_id"]


def test_secret_is_never_persisted_and_printed_as_placeholder(project: Path, home: Path) -> None:
    run_id = _paused_run(project, home)
    assert _grep(home, SECRET) == []
    (record,) = run_records(home)
    assert record["inputs"] == {"token": "<secret>", "issue": 7}
    assert record["secret_inputs"] == ["token"]
    assert record["stubs_path"] == str((project / "stubs.yaml").resolve())
    ctx = json.loads(next(home.rglob("steps/use/context.json")).read_text())
    assert ctx["inputs"]["token"] == "<secret>"
    # the shell step saw the real value (length 22), not the placeholder
    assert next(home.rglob("steps/use/output.txt")).read_text().strip() == f"len={len(SECRET)}"

    show = invoke(["show", run_id, "--root", str(project)], home)
    assert show.exit_code == 0, show.output
    assert "<secret>" in show.output and SECRET not in show.output
    show_json = invoke(["show", run_id, "--root", str(project), "--json"], home)
    assert json.loads(show_json.stdout)["inputs"] == {"token": "<secret>", "issue": 7}


def test_plan_and_validate_mark_secret_inputs(project: Path, home: Path) -> None:
    plan = invoke(["plan", "sec", "--root", str(project), "--input", f"token={SECRET}"], home)
    assert plan.exit_code == 0, plan.output
    assert "token = <secret>" in plan.output and "(string, secret)" in plan.output
    assert SECRET not in plan.output
    plan_json = invoke(
        ["plan", "sec", "--root", str(project), "--input", f"token={SECRET}", "--json"], home
    )
    row = json.loads(plan_json.stdout)["inputs"]["token"]
    assert row["value"] == "<secret>" and row["secret"] is True and row["state"] == "ok"
    assert SECRET not in plan_json.stdout
    # a missing required secret is reported like any other missing input
    missing = invoke(["plan", "sec", "--root", str(project)], home)
    assert missing.exit_code == 2 and "missing (required)" in missing.output

    validate = invoke(["validate", "sec", "--root", str(project)], home)
    assert validate.exit_code == 0, validate.output
    assert "secret inputs: token" in validate.output
    validate_json = invoke(["validate", "sec", "--root", str(project), "--json"], home)
    (row,) = json.loads(validate_json.stdout)
    assert row["secret_inputs"] == ["token"]


def test_smuggling_a_secret_into_a_prompt_is_a_validation_error(project: Path, home: Path) -> None:
    wf = project / ".rayspec" / "workflows" / "sec.yaml"
    wf.write_text(
        wf.read_text().replace('prompt: "issue {{ inputs.issue }}"', 'prompt: "{{ inputs.token }}"')
    )
    res = invoke(["run", "sec", "--root", str(project), "--input", "token=x", "--dry-run"], home)
    assert res.exit_code == 2, res.output
    assert "steps.ask.prompt" in res.output
    assert "secret inputs can only reach shell/python steps via RAYSPEC_INPUT_TOKEN" in res.output
    assert run_records(home) == []


def test_approve_requires_the_secret_again(project: Path, home: Path) -> None:
    run_id = _paused_run(project, home)
    refused = invoke(["approve", run_id, "--root", str(project)], home)
    assert refused.exit_code == 2, refused.output
    assert "missing secret input(s): token" in refused.output
    assert "--input token=" in refused.output and "RAYSPEC_INPUT_TOKEN" in refused.output
    (record,) = run_records(home)
    assert record["status"] == "paused"  # nothing was written
    # the environment variable is the second source
    ok = invoke(["approve", run_id, "--root", str(project)], home, RAYSPEC_INPUT_TOKEN=SECRET)
    assert ok.exit_code == 0, ok.output
    (record,) = run_records(home)
    assert record["status"] == "succeeded"
    assert record["outputs"] == {"v": f"again={len(SECRET)} t={len(SECRET)}"}
    assert record["inputs"] == {"token": "<secret>", "issue": 7}
    # the recorded stubs file drove the stub agent on the resume
    assert next(home.rglob("steps/ask/output.txt")).read_text().strip() == "scripted-answer"
    assert _grep(home, SECRET) == []


def test_reject_and_resume_accept_input_for_secrets_only(project: Path, home: Path) -> None:
    run_id = _paused_run(project, home)
    bad = invoke(["resume", run_id, "--root", str(project), "--yes", "--input", "issue=2"], home)
    assert bad.exit_code == 2, bad.output
    assert "inputs are fixed per run" in bad.output and "issue" in bad.output
    ok = invoke(
        ["resume", run_id, "--root", str(project), "--yes", "--input", f"token={SECRET}"], home
    )
    assert ok.exit_code == 0, ok.output
    (record,) = run_records(home)
    assert record["status"] == "succeeded" and record["outputs"]["v"].startswith("again=")
    assert _grep(home, SECRET) == []


def test_reject_with_secret_input(project: Path, home: Path) -> None:
    run_id = _paused_run(project, home)
    res = invoke(
        ["reject", run_id, "nope", "--root", str(project), "--input", f"token={SECRET}"], home
    )
    assert res.exit_code == 4, res.output  # on_reject: cancel
    (record,) = run_records(home)
    assert record["status"] == "cancelled"


def test_run_resume_accepts_input_for_secrets_only(project: Path, home: Path) -> None:
    run_id = _paused_run(project, home)
    base = ["run", "sec", "--root", str(project), "--resume", run_id, "--yes"]
    bad = invoke([*base, "--input", "issue=2"], home)
    assert bad.exit_code == 2 and "inputs are fixed per run" in bad.output, bad.output
    missing = invoke(base, home)
    assert missing.exit_code == 2 and "missing secret input(s): token" in missing.output
    ok = invoke([*base, "--input", f"token={SECRET}"], home)
    assert ok.exit_code == 0, ok.output
    (record,) = run_records(home)
    assert record["status"] == "succeeded"
    assert _grep(home, SECRET) == []


def test_a_step_that_echoes_the_secret_no_longer_persists_it(project: Path, home: Path) -> None:
    """A script that deliberately prints its secret used to persist it in the step output,
    the stdout log and the stream — the Redactor at every writer closes that hole."""
    wf = project / ".rayspec" / "workflows" / "sec.yaml"
    wf.write_text(
        wf.read_text().replace('echo "len=${#RAYSPEC_INPUT_TOKEN}"', 'echo "$RAYSPEC_INPUT_TOKEN"')
    )
    _paused_run(project, home)
    assert _grep(home, SECRET) == []
    assert _grep(home, "[REDACTED:token]") != []
    (record,) = run_records(home)
    assert record["inputs"]["token"] == "<secret>"  # the input itself is still redacted


def test_an_invalid_secret_value_is_not_echoed_by_run_or_plan(project: Path, home: Path) -> None:
    wf = project / ".rayspec" / "workflows" / "sec.yaml"
    wf.write_text(
        wf.read_text().replace("type: string, secret: true", "type: integer, secret: true")
    )
    bad = "notanint_SECRETVAL"
    run = invoke(["run", "sec", "--root", str(project), "--input", f"token={bad}"], home)
    assert run.exit_code == 2, run.output
    assert "SECRETVAL" not in run.output and "<secret>" in run.output
    plan = invoke(
        ["plan", "sec", "--root", str(project), "--input", f"token={bad}", "--json"], home
    )
    assert plan.exit_code == 2, plan.output
    assert "SECRETVAL" not in plan.output and "SECRETVAL" not in plan.stdout


def test_resume_of_a_paused_run_reports_the_gate_before_missing_secrets(
    project: Path, home: Path
) -> None:
    """Non-interactive ``resume`` of a run paused at a gate points at approve/reject (exit 3)
    — the secret check would only be the next thing the user hits after that."""
    run_id = _paused_run(project, home)
    res = invoke(["resume", run_id, "--root", str(project), "--no-interactive"], home)
    assert res.exit_code == 3, res.output
    assert "is paused" in res.output and "rayspec approve" in res.output
    assert "missing secret" not in res.output
    (record,) = run_records(home)
    assert record["status"] == "paused"

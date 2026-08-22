# SPDX-License-Identifier: Apache-2.0
"""``rayspec resume --fail-fast`` and the flag ``run.json`` remembers.

A resume continues a run; it must be able to continue it with the same blast radius, and to
tighten it when the first half taught the operator something. Both halves of that are checked
through the real CLI, because the flag is a command-line thing and the wiring is where it got
lost before.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rayspec.cli.app import app

FAILING = """rayspec: 1
name: ff
isolation: none
agents:
  bot: {provider: stub}
steps:
  - {id: a, agent: bot, prompt: "x"}
"""

STUBS = """steps:
  a: { fail: { kind: api, message: "nope", transient: false } }
"""


def _record(home: Path, run_id: str) -> dict:
    (run_json,) = home.rglob(f"runs/{run_id}/run.json")
    return json.loads(run_json.read_text(encoding="utf-8"))


def _start(cli: CliRunner, project: Path, *extra: str) -> str:
    (project / ".rayspec" / "workflows" / "ff.yaml").write_text(FAILING, encoding="utf-8")
    stubs = project / "stubs.yaml"
    stubs.write_text(STUBS, encoding="utf-8")
    result = cli.invoke(
        app, ["run", "ff", "--root", str(project), "--json", "--stubs", str(stubs), *extra]
    )
    assert result.exit_code == 1, result.output
    return json.loads(result.stdout.strip().splitlines()[-1])["run_id"]


def test_run_records_the_flag_and_a_resume_keeps_it(
    cli: CliRunner, home: Path, project: Path
) -> None:
    run_id = _start(cli, project, "--fail-fast")
    assert _record(home, run_id)["fail_fast"] is True
    result = cli.invoke(app, ["resume", run_id, "--root", str(project), "--json"])
    assert result.exit_code == 1, result.output
    assert _record(home, run_id)["fail_fast"] is True, (
        "the resumed run dropped the failure policy the run started with"
    )


def test_a_resume_can_tighten_a_run_that_started_without_the_flag(
    cli: CliRunner, home: Path, project: Path
) -> None:
    run_id = _start(cli, project)
    assert _record(home, run_id)["fail_fast"] is False
    result = cli.invoke(app, ["resume", run_id, "--fail-fast", "--root", str(project), "--json"])
    assert result.exit_code == 1, result.output
    assert _record(home, run_id)["fail_fast"] is True


GATED = """rayspec: 1
name: gate
isolation: none
steps:
  - {id: a, shell: echo a}
  - {id: ok, needs: [a], approve: "ship?"}
  - {id: b, needs: [ok], shell: echo b}
"""


def _pause(cli: CliRunner, home: Path, project: Path) -> str:
    (project / ".rayspec" / "workflows" / "gate.yaml").write_text(GATED, encoding="utf-8")
    result = cli.invoke(app, ["run", "gate", "--root", str(project), "--no-interactive"])
    assert result.exit_code == 3, result.output
    (run_json,) = home.rglob("runs/*/run.json")
    return json.loads(run_json.read_text(encoding="utf-8"))["run_id"]


def test_a_run_paused_at_a_gate_is_tightened_before_the_gate_is_decided(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """The blast radius must be narrowable while the run waits for a decision.

    A run paused at an approval gate is exactly where an operator learns that the next half
    should not fan out — and the only entry point the docs point at for it is `resume`. The
    short-circuit that reports "still paused" must therefore record the tightening on its way
    out, not drop it.
    """
    run_id = _pause(cli, home, project)
    assert _record(home, run_id)["fail_fast"] is False
    result = cli.invoke(
        app, ["resume", run_id, "--fail-fast", "--no-interactive", "--root", str(project)]
    )
    assert result.exit_code == 3, result.output
    assert _record(home, run_id)["fail_fast"] is True, (
        "--fail-fast was accepted and dropped: the run still carries the wider blast radius"
    )
    assert "--fail-fast" in result.output, result.output


def test_the_gate_decision_after_a_tightening_keeps_it(
    cli: CliRunner, home: Path, project: Path
) -> None:
    run_id = _pause(cli, home, project)
    cli.invoke(app, ["resume", run_id, "--fail-fast", "--no-interactive", "--root", str(project)])
    result = cli.invoke(app, ["approve", run_id, "ok", "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert _record(home, run_id)["fail_fast"] is True


def test_the_recorded_policy_is_visible_without_reading_run_json(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """A control nobody can see is a control nobody checks."""
    run_id = _start(cli, project, "--fail-fast")
    rows = json.loads(cli.invoke(app, ["runs", "--json", "--root", str(project)]).output)
    assert next(row for row in rows if row["run_id"] == run_id)["fail_fast"] is True
    shown = json.loads(cli.invoke(app, ["show", run_id, "--json", "--root", str(project)]).output)
    assert shown["fail_fast"] is True
    text = cli.invoke(app, ["show", run_id, "--root", str(project)]).output
    assert "fail-fast" in text, text

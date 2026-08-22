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

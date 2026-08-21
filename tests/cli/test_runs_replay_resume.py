# SPDX-License-Identifier: Apache-2.0
"""``--stubs-from`` across a pause and against a recorded ``--stubs`` file.

A replay that loses its script at an approval gate answers the remaining prompt steps with the
stub provider's built-in default and still reports success — fabricated outputs sold as a
faithful replay. These tests pin that the replay source survives the pause (it is recorded as
``run:<id>`` in ``run.json``) and that an explicit ``--stubs-from`` outranks the recorded path.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from rayspec.cli.app import app

GATED = """
rayspec: 1
name: rep
isolation: none
agents:
  bot: {provider: stub}
steps:
  - id: a
    agent: bot
    prompt: "first?"
  - id: gate
    needs: [a]
    approve:
      message: "ship it?"
  - id: b
    needs: [gate]
    agent: bot
    prompt: "again?"
outputs:
  a: "{{ steps.a.output }}"
  b: "{{ steps.b.output }}"
"""

DONOR_STUBS = """
steps:
  a: {text: XYZZY}
  b: {text: PLUGH}
"""

OTHER_STUBS = """
steps:
  a: {text: FROMFILE}
  b: {text: FROMFILE2}
"""


def _write(project: Path, name: str, text: str) -> None:
    (project / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text, encoding="utf-8")


def _summary(result_output: str) -> dict:
    return json.loads(result_output.strip().splitlines()[-1])


def _run(cli: CliRunner, project: Path, *extra: str) -> tuple[int, dict]:
    result = cli.invoke(app, ["run", "rep", "--root", str(project), "--json", *extra])
    return result.exit_code, _summary(result.stdout)


def _donor(cli: CliRunner, project: Path) -> dict:
    """A finished run whose recorded answers are XYZZY / PLUGH."""
    stubs = project / "donor.yaml"
    stubs.write_text(DONOR_STUBS, encoding="utf-8")
    code, summary = _run(cli, project, "--stubs", str(stubs), "--yes")
    assert code == 0, summary
    assert summary["outputs"] == {"a": "XYZZY", "b": "PLUGH"}
    return summary


def test_stubs_from_survives_a_pause(cli: CliRunner, home: Path, project: Path) -> None:
    """`run --stubs-from` → pause at the gate → `approve` must still answer from the donor."""
    _write(project, "rep", GATED)
    donor = _donor(cli, project)

    code, paused = _run(cli, project, "--stubs-from", donor["run_id"])
    assert code == 3, paused
    run_id = paused["run_id"]

    approved = cli.invoke(app, ["approve", run_id, "--root", str(project), "--json"])
    assert approved.exit_code == 0, approved.output
    summary = _summary(approved.stdout)
    assert summary["outputs"] == {"a": "XYZZY", "b": "PLUGH"}, (
        f"replay lost its script across the pause: {summary['outputs']}"
    )


def test_stubs_from_is_recorded_as_a_run_reference(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """The donor is persisted in ``run.json`` so every resume entry can rebuild the script."""
    _write(project, "rep", GATED)
    donor = _donor(cli, project)
    code, paused = _run(cli, project, "--stubs-from", donor["run_id"])
    assert code == 3, paused
    (run_json,) = home.rglob(f"runs/{paused['run_id']}/run.json")
    record = json.loads(run_json.read_text(encoding="utf-8"))
    assert record["stubs_path"] == f"run:{donor['run_id']}"


def test_stubs_from_beats_the_recorded_stubs_path_on_resume(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """An explicit ``--stubs-from`` on ``run --resume`` outranks the recorded ``--stubs`` file."""
    _write(project, "rep", GATED)
    donor = _donor(cli, project)
    other = project / "other.yaml"
    other.write_text(OTHER_STUBS, encoding="utf-8")
    code, paused = _run(cli, project, "--stubs", str(other))
    assert code == 3, paused

    result = cli.invoke(
        app,
        [
            "run",
            "rep",
            "--root",
            str(project),
            "--json",
            "--yes",
            "--resume",
            paused["run_id"],
            "--stubs-from",
            donor["run_id"],
        ],
    )
    assert result.exit_code == 0, result.output
    summary = _summary(result.stdout)
    assert summary["outputs"]["b"] == "PLUGH", (
        f"--stubs-from was silently ignored on --resume: {summary['outputs']}"
    )


def test_recorded_replay_source_is_rebuilt_by_run_resume(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """`run --resume` with no stub flag rebuilds the script from the recorded donor run."""
    _write(project, "rep", GATED)
    donor = _donor(cli, project)
    code, paused = _run(cli, project, "--stubs-from", donor["run_id"])
    assert code == 3, paused

    result = cli.invoke(
        app,
        ["run", "rep", "--root", str(project), "--json", "--yes", "--resume", paused["run_id"]],
    )
    assert result.exit_code == 0, result.output
    assert _summary(result.stdout)["outputs"] == {"a": "XYZZY", "b": "PLUGH"}


def test_a_deleted_donor_run_is_a_usage_error_on_resume(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """A replay whose donor is gone refuses loudly (exit 2) instead of inventing answers."""
    _write(project, "rep", GATED)
    donor = _donor(cli, project)
    code, paused = _run(cli, project, "--stubs-from", donor["run_id"])
    assert code == 3, paused
    (donor_dir,) = home.rglob(f"runs/{donor['run_id']}")
    shutil.rmtree(donor_dir)

    result = cli.invoke(app, ["approve", paused["run_id"], "--root", str(project)])
    assert result.exit_code == 2, result.output
    assert donor["run_id"] in result.output

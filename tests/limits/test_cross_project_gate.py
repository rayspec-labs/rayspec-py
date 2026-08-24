"""`resume` / `approve` / `reject` against a run recorded in ANOTHER project.

One run, one project: every project-scoped input of a resume entry — the workflow, the config,
the lockfile — has to come from the project the RUN belongs to, never from the directory the
command happened to be typed in. `rayspec resume` finds a run in any project, and `--locked` is
on by default under `CI`, so a poll-then-approve CI job is exactly where the two meet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from rayspec.cli.app import app
from rayspec.store.file import FileRunStore

runner = CliRunner()

GATE = """\
rayspec: 1
name: g
isolation: none
agents:
  worker:
    provider: stub
    model: medium
steps:
  - {id: plan, prompt: "hi", agent: worker}
  - {id: ok, needs: [plan], approve: "ship?"}
  - {id: after, needs: [ok], shell: 'echo "${TOK:-none}"'}
"""


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


def config_text(*, tier_model: str, secret: bool = False) -> str:
    text = f"tiers:\n  stub:\n    medium: {{model: {tier_model}}}\n"
    if secret:
        text += "secrets:\n  TOK: {env: SRC_TOKEN}\n"
    return text


#: Model names for the three configurations these tests tell apart. They are long on purpose.
#: The assertions below ask whether a model name is present in or absent from command output, and
#: that output carries a run id -- ``YYYYMMDD-HHMMSS-`` plus four characters drawn from
#: ``abcdefghijklmnopqrstuvwxyz234567`` (``store/model.py``). A two-character probe like ``m2``
#: therefore appears in roughly one run id in 341 by chance alone, which is what turned
#: ``test (3.14)`` red on the 1.0.3 release commit while the other three interpreters passed.
#: An absent-probe assertion fails when the dice say so; a present-probe assertion is worse, since
#: it PASSES when the dice say so and the behaviour it guards can rot unnoticed. Names containing a
#: hyphen and a whole word cannot occur in a run id at all, so the dice are out of it.
RUNS_MODEL = "model-in-the-run"
CALLERS_MODEL = "model-in-the-caller"
DRIFTED_MODEL = "model-after-drift"


def make_project(path: Path, *, tier_model: str, secret: bool = False) -> Path:
    (path / ".rayspec" / "workflows").mkdir(parents=True)
    (path / ".rayspec" / "workflows" / "g.yaml").write_text(GATE, encoding="utf-8")
    (path / ".rayspec" / "config.yaml").write_text(
        config_text(tier_model=tier_model, secret=secret), encoding="utf-8"
    )
    return path


def run_store(home: Path) -> FileRunStore:
    (store_dir,) = [p for p in (home / "projects").glob("*/*") if (p / "runs").is_dir()]
    return FileRunStore(store_dir)


@pytest.fixture
def elsewhere(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    """``(run's project, caller's project, paused run id)``.

    Both projects define the tier ``stub.medium`` and point it at a DIFFERENT model, so a command
    that resolves the run's agents against the caller's config cannot accidentally agree. Only
    the run's project declares the ``TOK`` secret, and only its workflow is pinned.
    """
    monkeypatch.setenv("SRC_TOKEN", "sekrit-token-value")
    project = make_project(tmp_path / "proj", tier_model=RUNS_MODEL, secret=True)
    assert invoke("lock", "--root", str(project)).exit_code == 0
    caller = make_project(tmp_path / "caller", tier_model=CALLERS_MODEL)
    started = invoke("run", "g", "--root", str(project), "--no-interactive")
    assert started.exit_code == 3, started.output
    (run_id,) = run_store(home).list_run_ids()
    return project, caller, run_id


def test_approve_from_another_project_resolves_models_in_the_runs_project(
    elsewhere: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing has drifted, so the CI default must let the run through.

    Resolving the run's agents against the CALLER's config invents a drift that names a model
    nobody configured — and the documented way out of a false refusal is `--no-locked`, i.e.
    switching the gate off for good.
    """
    _project, caller, run_id = elsewhere
    monkeypatch.setenv("CI", "true")  # --locked is on by default here
    result = invoke("approve", run_id, "ship it", "--root", str(caller))
    assert result.exit_code == 0, result.output
    assert "lockfile" not in result.output and CALLERS_MODEL not in result.output


def test_resume_from_another_project_reports_paused_not_a_lockfile_error(
    elsewhere: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rayspec resume` is documented to find the run in any project; exit 3 is "still paused"."""
    _project, caller, run_id = elsewhere
    monkeypatch.setenv("CI", "true")
    result = invoke("resume", run_id, "--no-interactive", "--root", str(caller))
    assert result.exit_code == 3, result.output
    assert "awaiting approval" in result.output


def test_reject_from_another_project_is_not_refused_by_the_gate(
    elsewhere: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _project, caller, run_id = elsewhere
    monkeypatch.setenv("CI", "true")
    result = invoke("reject", run_id, "not now", "--root", str(caller))
    assert result.exit_code == 4, result.output


def test_a_drift_in_the_runs_project_is_still_refused_from_anywhere(
    elsewhere: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoping the gate to the run's project must not weaken it: re-point the tier THERE and the
    same command refuses, naming the pin and what the agent resolves to now."""
    project, caller, run_id = elsewhere
    (project / ".rayspec" / "config.yaml").write_text(
        config_text(tier_model=DRIFTED_MODEL, secret=True), encoding="utf-8"
    )
    monkeypatch.setenv("CI", "true")
    result = invoke("approve", run_id, "--root", str(caller))
    assert result.exit_code == 2, result.output
    assert "agents.worker" in result.output
    assert RUNS_MODEL in result.output and DRIFTED_MODEL in result.output


def test_the_resumed_half_reads_the_runs_config_secrets(
    elsewhere: tuple[Path, Path, str], home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`config.secrets` is project-scoped too: the shell step after the gate belongs to the run's
    project, so it gets the run project's table — the caller's declares nothing."""
    _project, caller, run_id = elsewhere
    result = invoke("approve", run_id, "ship it", "--root", str(caller))
    assert result.exit_code == 0, result.output
    store = run_store(home)
    record = store.load(run_id)
    output = store.read_output(run_id, record.steps["after"].output_ref or "")
    assert output.strip() == "[REDACTED:TOK]"  # resolved, then kept out of the store

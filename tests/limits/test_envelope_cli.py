"""End to end through the CLI: a ceiling pauses `rayspec run`, `rayspec resume` continues it."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.limits import BudgetEnvelope, LimitsPolicy, SpendLedger, ledger_path
from rayspec.store.file import FileRunStore

runner = CliRunner()

WORKFLOW = """\
rayspec: 1
name: t
agents:
  worker: {provider: stub, model: m1}
steps:
  - {id: a, prompt: "one", agent: worker}
  - {id: b, needs: [a], prompt: "two", agent: worker}
"""

CONFIG = """\
pricing:
  m1: {input: 2.0, cached_input: 0, output: 0}
"""

STUBS = """\
defaults:
  usage: {input: 1000, output: 0}
"""


@pytest.fixture
def root(tmp_path: Path, home: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".rayspec" / "workflows").mkdir(parents=True)
    (project / ".rayspec" / "workflows" / "t.yaml").write_text(WORKFLOW, encoding="utf-8")
    (project / ".rayspec" / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (project / "stubs.yaml").write_text(STUBS, encoding="utf-8")
    return project


def with_budget(monkeypatch: pytest.MonkeyPatch, **caps: float) -> None:
    """Stand in for the policy layer, which both entry points read through one accessor."""
    policy = LimitsPolicy(budget=BudgetEnvelope(**caps))  # type: ignore[arg-type]
    # `run` binds the accessor at import; the in-process resume imports it inside the function,
    # so the package attribute is what that call resolves.
    for where in ("rayspec.cli.commands.run", "rayspec.limits"):
        monkeypatch.setattr(f"{where}.limits_policy", lambda *_a, **_k: policy)


def store_of(root: Path, home: Path) -> FileRunStore:
    return FileRunStore(home / "projects" / project_slug_for(root))


def test_run_pauses_on_the_ceiling_and_resume_continues(
    root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # $0.002 per step; the first step already exceeds a $0.001 ceiling
    with_budget(monkeypatch, per_day=0.001)
    result = runner.invoke(
        app,
        ["run", "t", "--stubs", str(root / "stubs.yaml"), "--no-interactive", "--root", str(root)],
    )
    assert result.exit_code == 3, result.output
    assert "spending envelope reached" in result.output

    store = store_of(root, home)
    (run_id,) = store.list_run_ids()
    record = store.load(run_id)
    assert record.pause is not None and record.pause.reason == "budget"
    assert record.status.value == "paused"
    assert record.steps["b"].skip_reason == "budget_exceeded"
    ledger = SpendLedger(ledger_path(home / "projects" / project_slug_for(root)))
    assert ledger.read().day_usd == pytest.approx(0.002)

    # still exceeded: resuming pauses again rather than slipping past the ceiling
    again = runner.invoke(app, ["resume", run_id, "--no-interactive", "--root", str(root)])
    assert again.exit_code == 3, again.output
    assert "awaiting approval" not in again.output

    # the operator raises it and the run finishes
    with_budget(monkeypatch, per_day=10.0)
    done = runner.invoke(app, ["resume", run_id, "--no-interactive", "--root", str(root)])
    assert done.exit_code == 0, done.output
    finished = store.load(run_id)
    assert finished.status.value == "succeeded"
    assert finished.pause is None
    assert set(finished.steps) == {"a", "b"}


def test_approve_continues_a_paused_run_without_raising_the_ceiling(
    root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_budget(monkeypatch, per_run=0.001)
    assert (
        runner.invoke(
            app,
            [
                "run",
                "t",
                "--stubs",
                str(root / "stubs.yaml"),
                "--no-interactive",
                "--root",
                str(root),
            ],
        ).exit_code
        == 3
    )
    store = store_of(root, home)
    (run_id,) = store.list_run_ids()
    approved = runner.invoke(app, ["approve", run_id, "looked at it", "--root", str(root)])
    assert approved.exit_code == 0, approved.output
    assert store.load(run_id).status.value == "succeeded"


def test_without_a_policy_nothing_pauses(root: Path, home: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "t", "--stubs", str(root / "stubs.yaml"), "--no-interactive", "--root", str(root)],
    )
    assert result.exit_code == 0, result.output
    assert not ledger_path(home / "projects" / project_slug_for(root)).exists()


def test_a_dry_run_is_never_accounted(
    root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_budget(monkeypatch, per_run=0.0000001)
    result = runner.invoke(
        app, ["run", "t", "--dry-run", "--stubs", str(root / "stubs.yaml"), "--root", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert not ledger_path(home / "projects" / project_slug_for(root)).exists()


def test_the_failure_breaker_stops_the_next_run(
    root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / ".rayspec" / "workflows" / "f.yaml").write_text(
        textwrap.dedent(
            """\
            rayspec: 1
            name: f
            steps:
              - {id: a, shell: "exit 4"}
            """
        ),
        encoding="utf-8",
    )
    with_budget(monkeypatch, max_consecutive_failures=2)
    for _ in range(2):
        assert runner.invoke(app, ["run", "f", "--root", str(root)]).exit_code == 1
    blocked = runner.invoke(app, ["run", "f", "--root", str(root)])
    assert blocked.exit_code == 3
    assert "circuit breaker open" in blocked.output


def test_a_ceiling_pause_leads_with_resume_not_approve(
    root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`approve` on a ceiling pause WAIVES the operator's ceiling — it does not answer a gate,
    so the console must not offer it first."""
    with_budget(monkeypatch, per_day=0.001)
    result = runner.invoke(
        app,
        ["run", "t", "--stubs", str(root / "stubs.yaml"), "--no-interactive", "--root", str(root)],
    )
    assert result.exit_code == 3, result.output
    footer = next(
        line
        for line in result.output.splitlines()
        if "rayspec resume" in line and "warning" not in line
    )
    assert footer.index("rayspec resume") < footer.index("rayspec approve")
    assert "waiv" in footer
    assert "rayspec reject" not in footer  # rejecting a ceiling does nothing at all

    store = store_of(root, home)
    (run_id,) = store.list_run_ids()
    shown = runner.invoke(app, ["show", run_id, "--root", str(root)])
    assert shown.exit_code == 0, shown.output
    shown_footer = next(
        line
        for line in shown.output.splitlines()
        if "rayspec resume" in line and "warning" not in line
    )
    assert shown_footer.index("rayspec resume") < shown_footer.index("rayspec approve")


def test_a_dry_run_with_exec_shell_spends_nothing(
    root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule for the envelope as for the slot: `--exec-shell` runs shell steps for real,
    but every prompt step is the stub, so there is no money to account."""
    with_budget(monkeypatch, per_day=0.000001)
    result = runner.invoke(
        app,
        [
            "run",
            "t",
            "--dry-run",
            "--exec-shell",
            "--stubs",
            str(root / "stubs.yaml"),
            "--no-interactive",
            "--root",
            str(root),
        ],
    )
    assert result.exit_code == 0, result.output
    ledger = SpendLedger(ledger_path(home / "projects" / project_slug_for(root)))
    assert ledger.read().day_usd == 0.0

"""A `pricing:` section too malformed to read must not take the spending envelope with it.

``PriceTable.from_config`` raises ``PricingConfigError`` on a malformed section and both run
entry points caught it and set the table to ``None``. Nothing was printed. For every provider that
reports no cost of its own the table IS the cost, so the operator's ``budget:`` ceilings were then
measured against nothing at all: a run that spent money looked free, and the ceiling that was
supposed to pause it never tripped.

It is the failure mode this whole area exists to prevent, and the limits layer already answers it
one file over — an unreadable ``budget.per_run`` is dropped and NAMED in ``LimitsPolicy.warnings``,
which the CLI prints before the run starts. A guardrail may be unavailable; it may not be silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.limits import BudgetEnvelope, LimitsPolicy
from rayspec.providers.pricing import PriceTable, price_table_of
from rayspec.store.file import FileRunStore

runner = CliRunner()

WORKFLOW = """\
rayspec: 1
name: t
agents:
  worker: {provider: stub, model: m1}
steps:
  - {id: a, prompt: "one", agent: worker}
"""

BROKEN = """\
pricing:
  m1: {input: free, cached_input: 0, output: 0}
"""

GOOD = """\
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
    (project / ".rayspec" / "config.yaml").write_text(BROKEN, encoding="utf-8")
    (project / "stubs.yaml").write_text(STUBS, encoding="utf-8")
    return project


def with_budget(monkeypatch: pytest.MonkeyPatch, **caps: float) -> None:
    policy = LimitsPolicy(budget=BudgetEnvelope(**caps))  # type: ignore[arg-type]
    for where in ("rayspec.cli.commands.run", "rayspec.limits"):
        monkeypatch.setattr(f"{where}.limits_policy", lambda *_a, **_k: policy)


def test_the_helper_reports_the_problem_instead_of_raising() -> None:
    """One reader for both entry points: an empty table plus the line naming what was dropped."""
    table, problem = price_table_of({"m1": {"input": "free", "cached_input": 0, "output": 0}})
    assert table == PriceTable()
    assert problem is not None
    assert "pricing.m1" in problem
    assert "budget" in problem  # what it costs the operator, not just what is malformed


def test_a_readable_table_reports_nothing() -> None:
    table, problem = price_table_of({"m1": {"input": 2.0, "cached_input": 0, "output": 0}})
    assert problem is None
    assert table.lookup("m1") is not None


def test_run_says_the_pricing_table_was_dropped(
    root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run still starts — an unreadable table is not a reason to refuse — but it says so."""
    with_budget(monkeypatch, per_day=1.0)
    result = runner.invoke(
        app,
        ["run", "t", "--stubs", str(root / "stubs.yaml"), "--no-interactive", "--root", str(root)],
    )
    assert result.exit_code == 0, result.output
    assert "pricing.m1" in result.output
    assert "budget" in result.output


def test_resume_says_it_too(root: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second half of a run is measured in the same prices, so it warns the same way.

    Written the way it actually happens: the run pauses on a ceiling it could still see, the
    table is edited while it is paused, and the resume — the half a scheduler drives unattended —
    would otherwise carry on with no prices and no ceiling and say nothing about either.
    """
    (root / ".rayspec" / "config.yaml").write_text(GOOD, encoding="utf-8")
    with_budget(monkeypatch, per_day=0.001)  # $0.002 for the step: the ceiling trips
    first = runner.invoke(
        app,
        ["run", "t", "--stubs", str(root / "stubs.yaml"), "--no-interactive", "--root", str(root)],
    )
    assert first.exit_code == 3, first.output
    assert "spending envelope reached" in first.output
    run_id = next(iter(FileRunStore(home / "projects" / project_slug_for(root)).list_runs())).run_id

    (root / ".rayspec" / "config.yaml").write_text(BROKEN, encoding="utf-8")
    with_budget(monkeypatch, per_day=10.0)
    resumed = runner.invoke(app, ["resume", run_id, "--no-interactive", "--root", str(root)])
    assert "pricing.m1" in resumed.output, resumed.output

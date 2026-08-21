"""``rayspec run`` and the host run slots: the policy limit, ``--wait-slot``, and dry runs."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.limits import LimitsPolicy, RunSlot, SlotBusyError
from rayspec.limits.policy import wait_seconds

runner = CliRunner()

WORKFLOW = """\
rayspec: 1
name: t
agents:
  reviewer: {provider: claude, model: claude-sonnet-4-6}
steps:
  - {id: a, prompt: "hi", agent: reviewer}
"""


@pytest.fixture
def root(tmp_path: Path, home: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".rayspec" / "workflows").mkdir(parents=True)
    (project / ".rayspec" / "workflows" / "t.yaml").write_text(WORKFLOW, encoding="utf-8")
    return project


def with_policy(monkeypatch: pytest.MonkeyPatch, **limits: int) -> None:
    """Stand in for the policy layer: ``rayspec run`` reads limits through the accessor."""
    monkeypatch.setattr(
        "rayspec.cli.commands.run.limits_policy",
        lambda *_a, **_k: LimitsPolicy(max_concurrent_runs=dict(limits)),
    )


def test_wait_seconds_reads_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert wait_seconds(None) is None
    assert wait_seconds("forever") == 0.0
    assert wait_seconds("0") == 0.0
    assert wait_seconds("30m") == 1800.0
    assert wait_seconds("90") == 90.0


def test_a_taken_slot_stops_a_second_run(root: Path, home: Path, monkeypatch) -> None:
    with_policy(monkeypatch, claude=1)
    held = RunSlot(home, "claude", 1, run_id="other").acquire()
    try:
        result = runner.invoke(app, ["run", "t", "--exec-shell", "--root", str(root)])
        assert result.exit_code == 2
        assert "claude run slot" in result.output and "other" in result.output
        assert "--wait-slot" in result.output
    finally:
        held.release()


def test_waiting_gives_up_with_a_clear_message(root: Path, home: Path, monkeypatch) -> None:
    with_policy(monkeypatch, claude=1)
    held = RunSlot(home, "claude", 1, run_id="other").acquire()
    try:
        result = runner.invoke(
            app, ["run", "t", "--exec-shell", "--wait-slot", "1s", "--root", str(root)]
        )
        assert result.exit_code == 2
        assert "after waiting" in result.output
    finally:
        held.release()


def test_a_bad_wait_slot_value_is_a_usage_error(root: Path, monkeypatch) -> None:
    with_policy(monkeypatch, claude=1)
    result = runner.invoke(
        app, ["run", "t", "--dry-run", "--wait-slot", "soon", "--root", str(root)]
    )
    assert result.exit_code == 2
    assert "--wait-slot" in result.output


def test_a_dry_run_takes_no_slot(root: Path, home: Path, monkeypatch) -> None:
    with_policy(monkeypatch, claude=1)
    held = RunSlot(home, "claude", 1, run_id="other").acquire()
    try:
        result = runner.invoke(app, ["run", "t", "--dry-run", "--root", str(root)])
        assert result.exit_code == 0, result.output
    finally:
        held.release()


def test_the_slot_is_given_back_when_the_run_ends(root: Path, home: Path, monkeypatch) -> None:
    with_policy(monkeypatch, claude=1)
    stubs = root / "stubs.yaml"
    stubs.write_text(
        textwrap.dedent(
            """
            steps:
              a: {text: "done"}
            """
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["run", "t", "--dry-run", "--exec-shell", "--stubs", str(stubs), "--root", str(root)]
    )
    assert result.exit_code == 0, result.output
    # nothing is left holding it
    slot = RunSlot(home, "claude", 1, run_id="after").acquire()
    slot.release()


def test_without_a_policy_nothing_is_limited(root: Path, home: Path) -> None:
    for index in range(1, 4):
        RunSlot(home, "claude", 8, run_id=f"r{index}").acquire()
    result = runner.invoke(app, ["run", "t", "--dry-run", "--root", str(root)])
    assert result.exit_code == 0, result.output


def test_slot_busy_error_carries_the_provider_and_limit(home: Path) -> None:
    held = RunSlot(home, "codex", 1, run_id="r1").acquire()
    try:
        with pytest.raises(SlotBusyError) as exc:
            RunSlot(home, "codex", 1, run_id="r2").acquire()
        assert exc.value.provider == "codex" and exc.value.limit == 1
    finally:
        held.release()

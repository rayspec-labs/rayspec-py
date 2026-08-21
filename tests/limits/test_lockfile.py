"""``.rayspec/rayspec.lock``: writing it, reading it and refusing a run that drifted."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.errors import RayspecError
from rayspec.limits import (
    LOCKFILE_NAME,
    LockDrift,
    Lockfile,
    LockfileError,
    check_locked,
    load_lockfile,
    lock_entries_for,
    locked_default,
    lockfile_path,
    write_lockfile,
)

from .conftest import Project

WORKFLOW = """
rayspec: 1
name: t
agents:
  reviewer:
    provider: claude
    model: claude-sonnet-4-6
    effort: high
steps:
  - {id: a, prompt: "hi", agent: reviewer}
"""


def test_lockfile_path_is_under_the_rayspec_dir(tmp_path: Path) -> None:
    assert lockfile_path(tmp_path) == tmp_path / ".rayspec" / LOCKFILE_NAME
    assert LOCKFILE_NAME == "rayspec.lock"


def test_lock_entries_pin_literal_model_and_effort(project: Project) -> None:
    project.workflow("t", WORKFLOW)
    entries = lock_entries_for(project.load("t"))
    assert list(entries) == ["agents.reviewer"]
    entry = entries["agents.reviewer"]
    assert (entry.provider, entry.model, entry.effort) == ("claude", "claude-sonnet-4-6", "high")


def test_write_then_load_round_trips(project: Project) -> None:
    project.workflow("t", WORKFLOW)
    resolved = project.load("t")
    path = write_lockfile(project.root, {"t": lock_entries_for(resolved)})
    assert path == lockfile_path(project.root)
    assert path.read_text(encoding="utf-8").startswith("#")
    lock = load_lockfile(project.root)
    assert lock is not None
    assert lock.workflows["t"]["agents.reviewer"].model == "claude-sonnet-4-6"
    assert check_locked(resolved, lock) == []


def test_missing_lockfile_reads_as_none(project: Project) -> None:
    assert load_lockfile(project.root) is None


def test_a_drifted_model_names_agent_pinned_and_resolved(project: Project) -> None:
    project.workflow("t", WORKFLOW)
    write_lockfile(project.root, {"t": lock_entries_for(project.load("t"))})
    project.workflow("t", WORKFLOW.replace("claude-sonnet-4-6", "claude-opus-4-9"))
    drifts = check_locked(project.load("t"), load_lockfile(project.root))
    assert len(drifts) == 1
    drift = drifts[0]
    assert drift.agent == "agents.reviewer" and drift.field == "model"
    assert drift.pinned == "claude-sonnet-4-6" and drift.resolved == "claude-opus-4-9"
    message = drift.message()
    assert "agents.reviewer" in message
    assert "claude-sonnet-4-6" in message and "claude-opus-4-9" in message


def test_effort_and_provider_drift_are_reported(project: Project) -> None:
    project.workflow("t", WORKFLOW)
    write_lockfile(project.root, {"t": lock_entries_for(project.load("t"))})
    project.workflow("t", WORKFLOW.replace("effort: high", "effort: low"))
    drifts = check_locked(project.load("t"), load_lockfile(project.root))
    assert [(d.field, d.pinned, d.resolved) for d in drifts] == [("effort", "high", "low")]


def test_an_unpinned_agent_and_an_unpinned_workflow_are_drift(project: Project) -> None:
    project.workflow("t", WORKFLOW)
    write_lockfile(project.root, {"t": {}})
    drifts = check_locked(project.load("t"), load_lockfile(project.root))
    assert [d.field for d in drifts] == ["agent"]
    assert "not pinned" in drifts[0].message()

    write_lockfile(project.root, {"other": {}})
    drifts = check_locked(project.load("t"), load_lockfile(project.root))
    assert [d.field for d in drifts] == ["workflow"]
    assert "rayspec lock" in drifts[0].message()


def test_no_lockfile_is_no_drift(project: Project) -> None:
    project.workflow("t", WORKFLOW)
    assert check_locked(project.load("t"), None) == []


def test_a_malformed_lockfile_is_a_loud_error(project: Project) -> None:
    lockfile_path(project.root).parent.mkdir(parents=True, exist_ok=True)
    lockfile_path(project.root).write_text("workflows: 3\n", encoding="utf-8")
    with pytest.raises(LockfileError) as exc:
        load_lockfile(project.root)
    assert isinstance(exc.value, RayspecError)
    assert "workflows" in str(exc.value)


def test_an_unpinnable_agent_is_recorded_as_the_provider_default(project: Project) -> None:
    project.workflow(
        "t",
        """
        rayspec: 1
        name: t
        steps:
          - {id: a, prompt: "hi", agent: {provider: mystery}}
        """,
    )
    entries = lock_entries_for(project.load("t"))
    (entry,) = entries.values()
    assert entry.model is None
    lock = Lockfile(version=1, workflows={"t": entries})
    assert check_locked(project.load("t"), lock) == []


def test_locked_default_follows_ci() -> None:
    assert locked_default({}) is False
    assert locked_default({"CI": ""}) is False
    assert locked_default({"CI": "false"}) is False
    assert locked_default({"CI": "0"}) is False
    assert locked_default({"CI": "true"}) is True
    assert locked_default({"CI": "1"}) is True


def test_lock_drift_is_hashable_data() -> None:
    drift = LockDrift(agent="a", field="model", pinned="x", resolved="y")
    assert drift == LockDrift(agent="a", field="model", pinned="x", resolved="y")

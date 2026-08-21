"""Shared helpers for the end-to-end CLI tests (real modules, stub provider, isolated home)."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from typer.testing import CliRunner, Result

from rayspec.cli.app import app
from rayspec.cli.commands.run import SUMMARY_KEYS

EVENT_KEYS = {"type", "run_id", "ts", "step_path", "data"}
EVENT_TYPES = {
    "run.started",
    "run.resumed",
    "run.paused",
    "run.decision",
    "run.finished",
    "step.started",
    "step.retry",
    "step.finished",
    "loop.iteration",
    "each.item",
    "workspace.created",
    "warning",
}
STREAM_KEYS = {"kind", "ts", "attempt", "text", "name", "call_id", "nested", "data"}


def git(*args: str, cwd: Path) -> str:
    """Run git with a fixed identity; return stdout."""
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def git_project(
    tmp_path: Path,
    workflows: dict[str, str],
    *,
    name: str = "proj",
    extra: dict[str, str] | None = None,
) -> Path:
    """A committed git repo (branch ``main``) with ``.rayspec/workflows/<name>.yaml`` files."""
    root = tmp_path / name
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    for wf_name, text in workflows.items():
        (root / ".rayspec" / "workflows" / f"{wf_name}.yaml").write_text(textwrap.dedent(text))
    for rel, text in (extra or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    git("init", "-q", "-b", "main", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-qm", "init", cwd=root)
    return root


def invoke(args: list[str], home: Path, **env: str) -> Result:
    """Drive the Typer app in process under an isolated ``RAYSPEC_HOME``."""
    return CliRunner().invoke(
        app, args, env={"RAYSPEC_HOME": str(home), "NO_COLOR": "1", **env}, catch_exceptions=False
    )


def run_records(home: Path) -> list[dict[str, Any]]:
    """Every ``run.json`` under the home (newest last)."""
    paths = sorted(home.rglob("runs/*/run.json"), key=lambda p: p.parent.name)
    return [json.loads(p.read_text()) for p in paths]


def jsonl(text: str) -> list[dict[str, Any]]:
    """Parse JSON lines (every non-blank line must be JSON)."""
    lines: list[dict[str, Any]] = []
    for raw in text.splitlines():
        if raw.strip():
            lines.append(json.loads(raw))
    return lines


def assert_json_stream(stdout: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Check the ``--json`` stdout contract; return ``(events_and_streams, summary)``."""
    lines = jsonl(stdout)
    assert len(lines) >= 3, stdout
    summary = lines[-1]
    assert set(summary) == SUMMARY_KEYS, summary
    body = lines[:-1]
    run_ids = set()
    for line in body:
        assert "type" in line, line
        if line["type"] == "stream":
            assert set(line) == {"type", "step_path", "record"}, line
            assert set(line["record"]) == STREAM_KEYS, line
            continue
        assert set(line) == EVENT_KEYS, line
        assert line["type"] in EVENT_TYPES, line
        run_ids.add(line["run_id"])
    assert run_ids == {summary["run_id"]}, (run_ids, summary["run_id"])
    assert body[0]["type"] in {"run.started", "run.resumed", "workspace.created"}, body[0]
    assert body[-1]["type"] == "run.finished", body[-1]
    assert body[-1]["data"]["status"] == summary["status"]
    return body, summary

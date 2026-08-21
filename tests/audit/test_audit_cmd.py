"""`rayspec audit <run> [--commands]` — the run's own ledger, rendered read-only."""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.store.file import FileRunStore

from .conftest import only_store

WORKFLOW = """
rayspec: 1
name: work
isolation: none
agents:
  r: { provider: stub }
steps:
  - id: build
    shell: 'echo built'
  - id: ask
    needs: [build]
    agent: r
    prompt: "go"
  - id: gate
    needs: [ask]
    approve: "ship?"
"""

STUBS = """
steps:
  ask:
    text: done
    events:
      - {command_start: {command: "pytest -q"}}
      - {tool_call: {name: Edit, call_id: c1, input: {path: "src/a.py"}}}
      - {file_change: {name: "src/a.py"}}
      - {warning: {text: "rate limited, retrying"}}
"""


@pytest.fixture
def work_project(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "work.yaml").write_text(textwrap.dedent(WORKFLOW))
    (root / "stubs.yaml").write_text(textwrap.dedent(STUBS))
    return root


@pytest.fixture
def finished(
    cli: CliRunner, work_project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, FileRunStore]:
    monkeypatch.setenv("RAYSPEC_ACTOR", "launcher@example.invalid")
    result = cli.invoke(
        app,
        [
            "run",
            "work",
            "--root",
            str(work_project),
            "--yes",
            "--stubs",
            str(work_project / "stubs.yaml"),
        ],
    )
    assert result.exit_code == 0, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    return run_id, store


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_audit_lists_commands_tools_files_and_approvals(
    cli: CliRunner, work_project: Path, finished: tuple[str, FileRunStore]
) -> None:
    run_id, _store = finished
    result = cli.invoke(app, ["audit", run_id, "--root", str(work_project)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "launcher@example.invalid" in out
    assert "pytest -q" in out
    assert "Edit" in out
    assert "src/a.py" in out
    assert "approved" in out
    assert "rate limited" in out


def test_commands_narrows_to_what_was_executed(
    cli: CliRunner, work_project: Path, finished: tuple[str, FileRunStore]
) -> None:
    run_id, _store = finished
    result = cli.invoke(app, ["audit", run_id, "--commands", "--root", str(work_project)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "pytest -q" in out
    assert "build" in out  # the shell step is a command the run executed
    assert "Edit" not in out and "approved" not in out


def test_audit_never_writes_to_the_store(
    cli: CliRunner, work_project: Path, finished: tuple[str, FileRunStore]
) -> None:
    run_id, store = finished
    before = _tree_hash(store.root)
    for args in ([], ["--commands"], ["--json"]):
        result = cli.invoke(app, ["audit", run_id, *args, "--root", str(work_project)])
        assert result.exit_code == 0, result.output
    assert _tree_hash(store.root) == before


def test_json_payload(
    cli: CliRunner, work_project: Path, finished: tuple[str, FileRunStore]
) -> None:
    run_id, _store = finished
    result = cli.invoke(app, ["audit", run_id, "--json", "--root", str(work_project)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == run_id
    assert payload["actor"]["id"] == "launcher@example.invalid"
    kinds = {row["kind"] for row in payload["rows"]}
    assert {"run", "step", "command", "tool", "file", "approval"} <= kinds
    assert all(set(row) == {"ts", "kind", "step", "detail", "data"} for row in payload["rows"])


def test_unknown_run_is_exit_2(cli: CliRunner, work_project: Path, home: Path) -> None:
    result = cli.invoke(app, ["audit", "nope", "--root", str(work_project)])
    assert result.exit_code == 2


def test_escape_sequences_never_reach_the_terminal(
    cli: CliRunner, work_project: Path, finished: tuple[str, FileRunStore]
) -> None:
    from rayspec.events.model import StreamRecord

    run_id, store = finished
    store.append_stream(
        run_id, "ask", StreamRecord(kind="command_start", text="clear\x1b[2Jrm -rf /")
    )
    result = cli.invoke(app, ["audit", run_id, "--root", str(work_project)])
    assert result.exit_code == 0, result.output
    assert "\x1b[2J" not in result.output
    assert "rm -rf /" in result.output

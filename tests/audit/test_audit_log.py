"""The optional local ``audit.jsonl`` — written through the store, so redaction applies."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.store.file import AUDIT_ENV, AUDIT_JSONL, FileRunStore

from .conftest import only_store

SECRET = "ghp_SECRETTOKEN_ABCDEF"

WORKFLOW = """
rayspec: 1
name: sec
isolation: none
inputs:
  token: { type: string, secret: true, required: true }
agents:
  r: { provider: stub }
steps:
  - id: leak
    shell: 'echo "token=$RAYSPEC_INPUT_TOKEN"'
  - id: ask
    needs: [leak]
    agent: r
    prompt: "go"
  - id: gate
    needs: [ask]
    approve: "ship?"
outputs:
  v: "{{ steps.ask.output }}"
"""

STUBS = f"""
steps:
  ask:
    text: done
    events:
      - {{command_start: {{command: "curl -H 'Authorization: {SECRET}' https://example.invalid"}}}}
      - {{tool_call: {{name: Bash, call_id: c1, input: {{cmd: "ls -la"}}}}}}
      - {{tool_result: {{call_id: c1, output: "a.py"}}}}
      - {{file_change: {{name: "src/a.py"}}}}
"""


@pytest.fixture
def secret_project(tmp_path: Path) -> Path:
    root = tmp_path / "secproj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "sec.yaml").write_text(textwrap.dedent(WORKFLOW))
    (root / "stubs.yaml").write_text(textwrap.dedent(STUBS))
    return root


def _run_sec(cli: CliRunner, project: Path) -> None:
    result = cli.invoke(
        app,
        [
            "run",
            "sec",
            "--root",
            str(project),
            "--input",
            f"token={SECRET}",
            "--no-interactive",
            "--stubs",
            str(project / "stubs.yaml"),
        ],
    )
    assert result.exit_code == 3, result.output


def _entries(store: FileRunStore, run_id: str) -> list[dict]:
    path = store.run_dir(run_id) / AUDIT_JSONL
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_no_audit_log_unless_it_is_asked_for(
    cli: CliRunner, secret_project: Path, home: Path
) -> None:
    _run_sec(cli, secret_project)
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    assert not (store.run_dir(run_id) / AUDIT_JSONL).exists()


def test_the_ledger_covers_the_run_its_steps_and_the_gate(
    cli: CliRunner, secret_project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(AUDIT_ENV, "1")
    monkeypatch.setenv("RAYSPEC_ACTOR", "launcher@example.invalid")
    _run_sec(cli, secret_project)
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    entries = _entries(store, run_id)
    kinds = [e["kind"] for e in entries]
    assert kinds[0] == "run" and entries[0]["detail"] == "created"
    assert entries[0]["data"]["actor"]["id"] == "launcher@example.invalid"
    assert "step" in kinds and "command" in kinds and "tool" in kinds and "file" in kinds
    commands = [e["detail"] for e in entries if e["kind"] == "command"]
    assert any("curl" in c for c in commands)
    tools = [e["detail"] for e in entries if e["kind"] == "tool"]
    assert "Bash" in tools
    files = [e["detail"] for e in entries if e["kind"] == "file"]
    assert "src/a.py" in files
    paused = [e for e in entries if e["kind"] == "run" and e["detail"] == "paused"]
    assert paused and paused[-1]["step"] == "gate"
    assert entries[-1]["detail"] == "finished (paused)"
    assert all(set(e) == {"ts", "kind", "step", "detail", "data"} for e in entries)


def test_an_approval_is_in_the_ledger_with_its_actor(
    cli: CliRunner, secret_project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(AUDIT_ENV, "1")
    monkeypatch.setenv("RAYSPEC_ACTOR", "launcher@example.invalid")
    _run_sec(cli, secret_project)
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    monkeypatch.setenv("RAYSPEC_ACTOR", "reviewer@example.invalid")
    result = cli.invoke(
        app,
        [
            "approve",
            run_id,
            "ship it",
            "--root",
            str(secret_project),
            "--input",
            f"token={SECRET}",
            "--stubs",
            str(secret_project / "stubs.yaml"),
        ],
    )
    assert result.exit_code == 0, result.output
    approvals = [e for e in _entries(store, run_id) if e["kind"] == "approval"]
    assert approvals and approvals[-1]["detail"] == "approved"
    assert approvals[-1]["data"]["by"] == "cli"
    assert approvals[-1]["data"]["actor"]["id"] == "reviewer@example.invalid"


def test_the_ledger_never_carries_a_secret(
    cli: CliRunner, secret_project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(AUDIT_ENV, "1")
    _run_sec(cli, secret_project)
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    raw = (store.run_dir(run_id) / AUDIT_JSONL).read_text()
    assert SECRET not in raw
    assert "[REDACTED:token]" in raw  # the scripted command carried it and was caught
    # and nothing else under the home leaked it either
    leaked = [p for p in home.rglob("*") if p.is_file() and SECRET in p.read_text(errors="replace")]
    assert leaked == []


def test_a_long_detail_is_capped(home: Path, tmp_path: Path) -> None:
    from rayspec.events.model import StreamRecord
    from rayspec.store.file import AUDIT_DETAIL_CAP, audit_entry_for_stream

    entry = audit_entry_for_stream("a", StreamRecord(kind="command_start", text="x" * 5000))
    assert entry is not None
    assert len(entry["detail"]) <= AUDIT_DETAIL_CAP


def test_read_audit_round_trips_and_tolerates_a_torn_line(
    cli: CliRunner, secret_project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(AUDIT_ENV, "1")
    _run_sec(cli, secret_project)
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    rows = list(store.read_audit(run_id))
    assert rows == _entries(store, run_id)
    path = store.run_dir(run_id) / AUDIT_JSONL
    with path.open("a") as fh:  # a crash mid-write leaves half a line behind
        fh.write('{"ts": "2026-01-01T00:00:00+00:00", "kind": "run"')
    assert list(store.read_audit(run_id)) == rows


def test_the_ledger_is_off_for_an_explicitly_disabled_store(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rayspec.schema import RunStatus
    from rayspec.store.model import RunRecord

    monkeypatch.setenv(AUDIT_ENV, "1")
    store = FileRunStore(home / "explicit", audit=False)
    run = RunRecord(
        run_id="20260821-000000-aaaa",
        workflow_name="w",
        workflow_path="w.yaml",
        workflow_hash="a" * 64,
        project_slug="local/x",
        project_root=str(home),
        status=RunStatus.RUNNING,
    )
    store.create(run)
    assert not (store.run_dir(run.run_id) / AUDIT_JSONL).exists()

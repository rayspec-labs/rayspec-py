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
      - {{tool_call: {{name: Read, call_id: c2, input: {{path: "src/a.py"}}}}}}
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
    assert "ls -la" in commands  # a Bash tool call is a command, not just a tool name
    tools = [e["detail"] for e in entries if e["kind"] == "tool"]
    assert "Read" in tools
    files = [e["detail"] for e in entries if e["kind"] == "file"]
    assert "src/a.py" in files
    paused = [e for e in entries if e["kind"] == "run" and e["detail"] == "paused"]
    assert paused and paused[-1]["step"] == "gate"
    assert entries[-1]["detail"] == "finished (paused)"
    assert all(set(e) == {"ts", "kind", "event", "step", "detail", "data"} for e in entries)


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
    assert approvals and approvals[-1]["detail"] == "approved by reviewer@example.invalid (cli)"
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
    from rayspec.store.file import AUDIT_DETAIL_CAP, audit_entry_for_stream, finish_audit_row

    entry = audit_entry_for_stream("a", StreamRecord(kind="command_start", text="x" * 5000))
    assert entry is not None
    assert len(entry["detail"]) == 5000  # the row is raw until it is finished
    assert len(finish_audit_row(entry)["detail"]) <= AUDIT_DETAIL_CAP


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


# A secret is not always a short single-line token: a PEM key has newlines, a service-account
# blob has runs of whitespace, and both are longer than a row's detail is allowed to be.
MULTILINE_SECRET = "-----BEGIN KEY-----\nAAAABBBBCCCC\n-----END KEY-----"
SPACED_SECRET = "service   account    key    value"
OVERLONG_SECRET = "sk-" + "y" * 1300


@pytest.fixture
def ledger_store(home: Path) -> FileRunStore:
    """A store with the ledger pinned on and a run to append to."""
    from rayspec.schema import RunStatus
    from rayspec.store.model import RunRecord

    store = FileRunStore(home / "shapes", audit=True)
    store.create(
        RunRecord(
            run_id="20260821-000000-bbbb",
            workflow_name="w",
            workflow_path="w.yaml",
            workflow_hash="a" * 64,
            project_slug="local/x",
            project_root=str(home),
            status=RunStatus.RUNNING,
        )
    )
    return store


@pytest.mark.parametrize(
    "secret", [SECRET, MULTILINE_SECRET, SPACED_SECRET, OVERLONG_SECRET], ids=lambda s: str(len(s))
)
def test_the_ledger_redacts_a_secret_of_any_shape(ledger_store: FileRunStore, secret: str) -> None:
    from rayspec.events.model import StreamRecord
    from rayspec.redact import Redactor

    run_id = "20260821-000000-bbbb"
    ledger_store.redactor = Redactor.build({"token": secret})
    ledger_store.append_stream(
        run_id, "leak", StreamRecord(kind="error", text=f"boom {secret} end")
    )
    raw = (ledger_store.run_dir(run_id) / AUDIT_JSONL).read_text()
    assert secret not in raw
    assert "".join(secret.split()) not in "".join(raw.split())
    assert "[REDACTED:token]" in raw


def test_a_capped_detail_is_still_redacted(ledger_store: FileRunStore) -> None:
    from rayspec.events.model import StreamRecord
    from rayspec.redact import Redactor
    from rayspec.store.file import AUDIT_DETAIL_CAP

    run_id = "20260821-000000-bbbb"
    ledger_store.redactor = Redactor.build({"token": OVERLONG_SECRET})
    ledger_store.append_stream(
        run_id, "leak", StreamRecord(kind="error", text=f"{OVERLONG_SECRET} tail")
    )
    (row,) = [r for r in ledger_store.read_audit(run_id) if r["kind"] == "warning"]
    assert row["detail"] == "[REDACTED:token] tail"
    assert len(row["detail"]) <= AUDIT_DETAIL_CAP


def test_a_tool_argument_is_redacted_before_it_is_capped(ledger_store: FileRunStore) -> None:
    from rayspec.events.model import StreamRecord
    from rayspec.redact import Redactor

    run_id = "20260821-000000-bbbb"
    ledger_store.redactor = Redactor.build({"token": OVERLONG_SECRET})
    ledger_store.append_stream(
        run_id,
        "leak",
        StreamRecord(kind="tool_call", name="Read", data={"input": {"key": OVERLONG_SECRET}}),
    )
    raw = (ledger_store.run_dir(run_id) / AUDIT_JSONL).read_text()
    assert OVERLONG_SECRET[:200] not in raw
    assert "[REDACTED:token]" in raw


def test_a_broken_ledger_never_costs_the_run_its_own_files(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from pathlib import Path as _Path

    from rayspec.events.model import EventType, RunEvent, StreamRecord
    from rayspec.schema import RunStatus
    from rayspec.store.file import EVENTS_JSONL, RUN_JSON, STREAM_JSONL
    from rayspec.store.model import RunRecord

    store = FileRunStore(home / "brittle", audit=True)
    original = FileRunStore._append_line

    def refuse_the_ledger(self: FileRunStore, path: _Path, line: str) -> None:
        if path.name == AUDIT_JSONL:  # a full disk, a read-only mount, a bad mode
            raise OSError("no space left on device")
        original(self, path, line)

    monkeypatch.setattr(FileRunStore, "_append_line", refuse_the_ledger)
    run = RunRecord(
        run_id="20260821-000000-cccc",
        workflow_name="w",
        workflow_path="w.yaml",
        workflow_hash="a" * 64,
        project_slug="local/x",
        project_root=str(home),
        status=RunStatus.RUNNING,
    )
    with caplog.at_level("WARNING"):
        store.create(run)
        store.append_event(run.run_id, RunEvent(run_id=run.run_id, type=EventType.RUN_STARTED))
        store.append_stream(run.run_id, "a", StreamRecord(kind="error", text="boom"))
    run_dir = store.run_dir(run.run_id)
    assert (run_dir / RUN_JSON).is_file()
    assert (run_dir / EVENTS_JSONL).is_file()
    assert "boom" in (store.step_dir(run.run_id, "a") / STREAM_JSONL).read_text()
    assert any(AUDIT_JSONL in record.getMessage() for record in caplog.records)

"""Step ``artifacts:`` — the files a step promises to write.

Load-time validation of the declared paths, the check after a step succeeds (a missing artifact
is a step failure naming the path), persistence through the store (a copy in the run directory,
``path``/``sha256`` on the record, redacted and ``0600`` like every other write) and what
``rayspec show`` reports. An artifact is a PATH the step promises: its CONTENT never reaches a
record, an event, a template context or an output.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from rich.console import Console

from rayspec.cli.commands.show import print_show, show_payload
from rayspec.engine.context import RunOptions
from rayspec.events.model import EventType
from rayspec.redact import Redactor
from rayspec.schema import RunStatus, SchemaError, StepStatus, parse_step
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


# -- schema / load time --------------------------------------------------------------------------


def test_artifacts_default_to_nothing_and_accept_relative_paths() -> None:
    assert parse_step({"id": "a", "shell": "true"}).artifacts == []
    step = parse_step({"id": "a", "shell": "true", "artifacts": ["build/report.md", "out.json"]})
    assert step.artifacts == ["build/report.md", "out.json"]


@pytest.mark.parametrize(
    ("declared", "needle"),
    [
        ("/etc/passwd", "must be relative"),
        ("~/notes.md", "must be relative"),
        ("../outside.txt", "'..'"),
        ("build/../../outside.txt", "'..'"),
        ("", "empty"),
        ("build/", "must name a file"),
        ("out/{{ item }}.txt", "not templated"),
        ("out/{% if x %}a{% endif %}.txt", "not templated"),
        ("we\nird.txt", "control character"),
    ],
)
def test_escaping_artifact_paths_are_refused_at_load_time(
    harness: Harness, declared: str, needle: str
) -> None:
    harness.workflow(
        "t",
        "rayspec: 1\nname: t\nsteps:\n"
        '  - {id: build, shell: "true"}\n'
        f'  - {{id: report, needs: [build], shell: "true", artifacts: [{declared!r}]}}\n',
    )
    with pytest.raises(SchemaError) as exc:
        harness.load("t")
    message = str(exc.value)
    assert ".rayspec/workflows/t.yaml:5" in message  # file:line of the offending step
    assert "artifacts" in message and needle in message
    assert "build/report.md" in message  # the fix hint shows a good path


def test_a_declared_path_is_normalised() -> None:
    """``./a.txt`` and ``a.txt`` name the same file: the stored path must not disagree with the
    ref the store computes from it."""
    step = parse_step({"id": "a", "shell": "true", "artifacts": ["./build/report.md", "b//c.txt"]})
    assert step.artifacts == ["build/report.md", "b/c.txt"]


def test_artifacts_are_declarable_on_every_kind() -> None:
    for step in (
        {"id": "a", "prompt": "write the report", "artifacts": ["report.md"]},
        {"id": "b", "python": "pass", "artifacts": ["report.md"]},
        {"id": "c", "each": "[1]", "steps": [{"id": "d", "shell": "true"}], "artifacts": ["r.md"]},
    ):
        assert parse_step(step).artifacts


# -- the promise ---------------------------------------------------------------------------------


def wf(steps: str) -> str:
    return f"rayspec: 1\nname: t\nsteps:\n{steps}"


async def test_a_written_artifact_is_recorded_and_copied(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            """
  - id: report
    shell: "mkdir -p build && printf 'hello\\n' > build/report.md"
    artifacts: [build/report.md]
"""
        ),
    )
    result = await harness.run("t")
    assert result.status is RunStatus.SUCCEEDED, result.reason
    record = harness.record(result.run_id).steps["report"]
    assert len(record.artifacts) == 1
    artifact = record.artifacts[0]
    assert artifact.path == "build/report.md"
    assert artifact.ref == "artifacts/report/build/report.md"
    assert artifact.sha256 == hashlib.sha256(b"hello\n").hexdigest()
    assert artifact.size == 6
    copy = harness.store.run_dir(result.run_id) / artifact.ref
    assert copy.read_text() == "hello\n"
    assert oct(copy.stat().st_mode)[-3:] == "600"
    # the step's own output is untouched (stdout, not the artifact)
    assert harness.record(result.run_id).steps["report"].output_ref == "steps/report/output.txt"


async def test_a_missing_artifact_fails_the_step(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            """
  - {id: report, shell: "echo working", artifacts: [build/report.md, notes.md]}
  - {id: after, needs: [report], shell: "echo never"}
"""
        ),
    )
    result = await harness.run("t")
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    record = harness.record(result.run_id).steps["report"]
    assert record.status is StepStatus.FAILED
    assert record.error is not None and record.error.type == "artifact"
    assert "build/report.md" in record.error.message
    assert not record.error.transient
    assert record.artifacts == []
    assert harness.statuses(result.run_id)["after"] == "skipped"


async def test_a_directory_is_not_an_artifact(harness: Harness) -> None:
    harness.workflow("t", wf('  - {id: r, shell: "mkdir -p build", artifacts: [build]}\n'))
    result = await harness.run("t")
    assert result.status is RunStatus.FAILED
    record = harness.record(result.run_id).steps["r"]
    assert record.error is not None and "directory" in record.error.message


async def test_a_symlink_out_of_the_working_directory_is_refused(harness: Harness) -> None:
    outside = harness.tmp / "outside.txt"
    outside.write_text("not yours\n")
    harness.workflow(
        "t", wf(f'  - {{id: r, shell: "ln -s {outside} report.md", artifacts: [report.md]}}\n')
    )
    result = await harness.run("t")
    assert result.status is RunStatus.FAILED
    record = harness.record(result.run_id).steps["r"]
    assert record.error is not None and record.error.type == "artifact"
    assert "working directory" in record.error.message
    assert "not yours" not in json.dumps(record.model_dump(mode="json"))


async def test_artifact_content_never_leaks_into_the_run_record(harness: Harness) -> None:
    payload = "PAYLOAD-THAT-MUST-NOT-BE-RECORDED"
    harness.workflow(
        "t",
        wf(
            f"""
  - {{id: write, shell: "printf '{payload}' > secret.bin", artifacts: [secret.bin]}}
  - {{id: after, needs: [write], shell: "echo done"}}
"""
        ),
    )
    result = await harness.run("t")
    assert result.status is RunStatus.SUCCEEDED, result.reason
    run_dir = harness.store.run_dir(result.run_id)
    copy = run_dir / "artifacts" / "write" / "secret.bin"
    assert copy.read_text() == payload  # the copy is the ONLY place it may appear
    leaked = [
        str(path.relative_to(run_dir))
        for path in run_dir.rglob("*")
        if path.is_file() and path != copy and payload in path.read_text(errors="replace")
    ]
    assert leaked == []
    assert all(payload not in json.dumps(e.data) for e in harness.events())


async def test_a_dry_run_does_not_check_artifacts(harness: Harness) -> None:
    harness.workflow("t", wf('  - {id: r, shell: "echo hi > report.md", artifacts: [report.md]}\n'))
    result = await harness.run("t", options=RunOptions(dry_run=True))
    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert harness.record(result.run_id).steps["r"].artifacts == []
    assert not (harness.root / "report.md").exists()


async def test_a_replayed_step_keeps_its_artifacts(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            """
  - {id: r, shell: "printf 'v1' > report.md", artifacts: [report.md]}
  - {id: after, needs: [r], shell: "exit 1"}
"""
        ),
    )
    first = await harness.run("t")
    assert first.status is RunStatus.FAILED
    (harness.root / "report.md").unlink()  # the workdir moved on; the record must not

    harness.sink.clear()
    again = await harness.run("t", resume=first.run_id)
    assert again.reused == ["r"]
    record = harness.record(first.run_id).steps["r"]
    assert [a.path for a in record.artifacts] == ["report.md"]


# -- the store ------------------------------------------------------------------------------------


def _store(tmp_path: Path, *, redactor: Redactor | None = None) -> tuple[FileRunStore, RunRecord]:
    store = FileRunStore(tmp_path / "store")
    if redactor is not None:
        store.redactor = redactor
    run = RunRecord(
        run_id="20260821-101010-aaaa",
        workflow_name="t",
        workflow_path=".rayspec/workflows/t.yaml",
        workflow_hash="h",
        project_slug="local/test",
        project_root=str(tmp_path),
    )
    store.create(run)
    return store, run


def test_write_artifact_is_private_atomic_and_redacted(tmp_path: Path) -> None:
    store, run = _store(tmp_path, redactor=Redactor.build({"token": "hunter2"}))
    source = tmp_path / "report.md"
    source.write_text("password is hunter2\n")
    written = store.write_artifact(run.run_id, "build[1]/report", "docs/report.md", source)
    assert written.artifact_ref == "artifacts/build[1]/report/docs/report.md"
    copy = store.run_dir(run.run_id) / written.artifact_ref
    assert copy.read_text() == "password is [REDACTED:token]\n"
    assert written.sha256 == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert written.size == copy.stat().st_size
    assert oct(copy.stat().st_mode)[-3:] == "600"
    assert oct(copy.parent.stat().st_mode)[-3:] == "700"
    assert not list(copy.parent.glob("*.tmp"))


def test_write_artifact_keeps_bytes_that_are_not_text(tmp_path: Path) -> None:
    store, run = _store(tmp_path)
    blob = bytes(range(256)) * 8
    source = tmp_path / "image.png"
    source.write_bytes(blob)
    written = store.write_artifact(run.run_id, "r", "image.png", source)
    copy = store.run_dir(run.run_id) / written.artifact_ref
    assert copy.read_bytes() == blob
    assert written.sha256 == hashlib.sha256(blob).hexdigest()


def test_write_artifact_refuses_a_ref_that_escapes_the_run_dir(tmp_path: Path) -> None:
    store, run = _store(tmp_path)
    source = tmp_path / "x.txt"
    source.write_text("x")
    for bad in ("/etc/passwd", "../escape.txt", "a/../../escape.txt"):
        with pytest.raises(ValueError, match="artifact"):
            store.write_artifact(run.run_id, "r", bad, source)


# -- a store that keeps no copies ------------------------------------------------------------------


class _NoArtifactStore:
    """A run store that implements the protocol but has no ``write_artifact``."""

    def __init__(self, inner: FileRunStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        if name == "write_artifact":
            raise AttributeError(name)
        return getattr(self._inner, name)


class _FailingArtifactStore(_NoArtifactStore):
    """A store whose artifact copy always fails (a full disk, a vanished file)."""

    def write_artifact(self, *args: object, **kwargs: object) -> object:
        raise OSError("no space left on device")


@pytest.mark.parametrize("store_class", [_NoArtifactStore, _FailingArtifactStore])
async def test_a_store_that_keeps_no_copy_still_records_the_promise(
    harness: Harness, store_class: type[_NoArtifactStore]
) -> None:
    harness.workflow("t", wf('  - {id: r, shell: "true", artifacts: [report.md]}\n'))
    graph = make_graph_harness(harness, harness.load("t"))
    graph.ctx.store = store_class(harness.store)  # type: ignore[assignment]
    source = harness.root / "report.md"
    source.write_text("kept anyway\n")
    record = graph.ctx.new_record(harness.load("t").workflow.steps[0], graph.scope)

    await graph.ctx.write_artifacts(record, [("report.md", source)])

    assert [(a.path, a.ref) for a in record.artifacts] == [("report.md", None)]
    assert record.artifacts[0].sha256 == hashlib.sha256(b"kept anyway\n").hexdigest()
    warned = [e.data["message"] for e in harness.sink.events_of(EventType.WARNING)]
    assert bool(warned) is (store_class is _FailingArtifactStore)


# -- rayspec show ---------------------------------------------------------------------------------


async def test_show_reports_the_artifacts_of_a_run(harness: Harness) -> None:
    harness.workflow(
        "t", wf('  - {id: r, shell: "printf hi > report.md", artifacts: [report.md]}\n')
    )
    result = await harness.run("t")
    assert result.status is RunStatus.SUCCEEDED, result.reason
    record = harness.record(result.run_id)

    payload = show_payload(harness.store, record)
    assert payload["artifacts"] == [
        {
            "step": "r",
            "path": "report.md",
            "ref": "artifacts/r/report.md",
            "sha256": hashlib.sha256(b"hi").hexdigest(),
            "size": 2,
        }
    ]

    console = Console(record=True, width=200, force_terminal=False, no_color=True)
    print_show(console, harness.store, record)
    text = console.export_text()
    assert "artifacts" in text and "report.md" in text
    assert hashlib.sha256(b"hi").hexdigest()[:12] in text


def test_the_run_dir_layout_keeps_artifacts_next_to_the_steps(tmp_path: Path) -> None:
    store, run = _store(tmp_path)
    source = tmp_path / "a.txt"
    source.write_text("a")
    store.write_artifact(run.run_id, "r", "a.txt", source)
    run_dir = store.run_dir(run.run_id)
    assert (run_dir / "artifacts" / "r" / "a.txt").is_file()
    assert os.path.commonpath([run_dir, (run_dir / "artifacts" / "r" / "a.txt").resolve()]) == str(
        run_dir
    )

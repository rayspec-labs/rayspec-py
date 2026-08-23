# SPDX-License-Identifier: Apache-2.0
"""Redaction must not rewrite the identifiers a run is LOOKED UP by.

``Redactor.redact_dump`` already leaves a record's structure alone where rewriting it would
destroy the record — field names, and the step paths a run's ``steps`` mapping is keyed by. The
run's own identity is the same class one level up: ``run_id`` names the directory the record
lives in, and ``workflow_name`` / ``workflow_path`` are what ``resume``/``approve``/``reject``/
``explain`` re-load the workflow by. A ``secret: true`` value that happens to equal one of them
used to rewrite it, and the run was permanently unreachable — ``unknown workflow
'[REDACTED:token]'`` — with no way to undo it.

Everything free-form (inputs, outputs, step data) stays redacted — **except** when the secret's
value IS one of the run's own addresses. Then it is not redacted anywhere, and the run says so
(``warning: token is one of the names this run is recorded under …``). Redaction cannot remove
that string from a run that has to keep it, so hiding it in ``events.jsonl`` and the console
while ``run.json``/``show``/``runs``/``audit`` print it protects nothing and discloses something:
a ``[REDACTED:token]`` standing where the reader can look the true content up one file over says
exactly which public string the secret is. Same answer as :data:`MIN_REDACTABLE_LEN` gives to the
other value redaction cannot help with — do not pretend, and name it.

A secret equal to a **step** id is a different case and stays redacted: a step's address is
structural on both sides already (``RunEvent.step_path`` is a field of its own, not a key in the
free-form ``data``), so nothing about it ever disagreed. These tests pin all of it.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.config import Config
from rayspec.engine.runner import Runner
from rayspec.loader import ResolvedWorkflow, load_workflow
from rayspec.redact import Redactor
from rayspec.schema import StepStatus
from rayspec.store.file import RUN_IDENTITY_FIELDS, FileRunStore
from rayspec.store.model import ErrorInfo, RunRecord, StepRecord
from rayspec.store.redacting import RedactingStore


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


WORKFLOW = """
rayspec: 1
name: {name}
isolation: none
inputs:
  token: {{ type: string, secret: true, required: true }}
steps:
  - id: echo
    shell: 'printf "%s" "$RAYSPEC_INPUT_TOKEN"'
outputs:
  v: "{{{{ steps.echo.output }}}}"
"""


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".rayspec" / "workflows" / f"{name}.yaml").write_text(
        textwrap.dedent(WORKFLOW.format(name=name))
    )
    return root


def _resolved(project: Path, home: Path, name: str) -> ResolvedWorkflow:
    return load_workflow(name, project_root=project, home=home, config=Config())


def test_a_secret_equal_to_the_workflow_name_does_not_brick_the_run(tmp_path: Path) -> None:
    """The canonical break: ``workflow_name`` survives, and a resume works."""
    name = "deploy_tool"
    project = _project(tmp_path, name)
    home = tmp_path / "home"
    home.mkdir()
    store = FileRunStore(tmp_path / "store")
    result = Runner(
        _resolved(project, home, name),
        inputs={"token": name},  # the secret value IS the workflow name
        store=store,
        project_root=project,
    ).run_sync()
    assert result.exit_code == 0, result.reason

    raw = json.loads((store.run_dir(result.run_id) / "run.json").read_text(encoding="utf-8"))
    assert raw["workflow_name"] == name, "the record must keep naming its own workflow"
    assert raw["run_id"] == result.run_id, "the record must keep naming its own directory"
    assert raw["workflow_path"].endswith(f"{name}.yaml")
    # ... and the value is not redacted ANYWHERE else either. rayspec cannot take this string
    # out of a run whose record has to keep it, so it does not half-take it out: `[REDACTED:…]`
    # in `events.jsonl` next to `deploy_tool` in `run.json` is the reader's answer key.
    assert raw["outputs"] == {"v": name}
    events = (store.run_dir(result.run_id) / "events.jsonl").read_text(encoding="utf-8")
    assert f'"workflow":"{name}"' in events
    assert "REDACTED" not in events

    # and the run is still resumable: the engine refuses a run whose recorded workflow name
    # does not match the workflow it was handed
    resumed = Runner(
        _resolved(project, home, name),
        inputs={"token": name},
        store=store,
        project_root=project,
        resume_run_id=result.run_id,
    ).run_sync()
    assert resumed.exit_code == 0, resumed.reason


def test_the_identity_fields_survive_a_plain_dump() -> None:
    """Unit level: the three identifiers are preserved, everything free-form is not."""
    secret = "deploy_tool"
    record = RunRecord(
        run_id="20260822-000000-abcd",
        workflow_name=secret,
        workflow_path=f".rayspec/workflows/{secret}.yaml",
        workflow_hash="0" * 64,
        project_slug="local/test",
        project_root="/tmp/proj",
        inputs={"note": secret},
    )
    dump = Redactor.build({"token": secret}).redact_dump(
        record, preserve=("run_id", "workflow_name", "workflow_path")
    )
    assert dump["workflow_name"] == secret
    assert dump["workflow_path"] == f".rayspec/workflows/{secret}.yaml"
    assert dump["run_id"] == "20260822-000000-abcd"
    assert dump["inputs"] == {"note": "[REDACTED:token]"}


def test_a_secret_equal_to_the_run_id_keeps_the_record_addressable(tmp_path: Path) -> None:
    """``run_id`` is how the record names its own directory; rewriting it loses the run."""
    name = "leaky"
    project = _project(tmp_path, name)
    home = tmp_path / "home"
    home.mkdir()
    store = FileRunStore(tmp_path / "store")
    run_id = "20260822-000000-abcd"
    result = Runner(
        _resolved(project, home, name),
        inputs={"token": run_id},  # the secret value IS the run id
        store=store,
        project_root=project,
        run_id=run_id,
    ).run_sync()
    assert result.exit_code == 0, result.reason
    assert store.load(run_id).run_id == run_id
    assert store.list_run_ids() == [run_id]


def test_the_plugin_store_boundary_preserves_the_same_fields(tmp_path: Path) -> None:
    """``RedactingStore`` hands the wrapped store a record, so it must preserve them too —
    otherwise a third-party store is handed a run that cannot name its own workflow."""
    secret = "deploy_tool"
    inner = FileRunStore(tmp_path / "store")
    store = RedactingStore(inner)
    store.redactor = Redactor.build({"token": secret})
    record = RunRecord(
        run_id="20260822-000000-abcd",
        workflow_name=secret,
        workflow_path=f".rayspec/workflows/{secret}.yaml",
        workflow_hash="0" * 64,
        project_slug="local/test",
        project_root=str(tmp_path),
        inputs={"note": secret},
    )
    store.create(record)
    saved = inner.load("20260822-000000-abcd")
    assert saved.workflow_name == secret and saved.run_id == "20260822-000000-abcd"
    assert saved.inputs == {"note": "[REDACTED:token]"}


@pytest.mark.parametrize("field", ["workflow_hash", "project_slug"])
def test_other_fields_are_still_redacted(field: str) -> None:
    """The exemption is the identity fields and nothing else — no blanket structural pass."""
    secret = "collides-here"
    fields: dict[str, str] = {
        "run_id": "20260822-000000-abcd",
        "workflow_name": "wf",
        "workflow_path": "wf.yaml",
        "workflow_hash": "0" * 64,
        "project_slug": "local/test",
        "project_root": "/tmp/proj",
    }
    record = RunRecord.model_validate({**fields, field: secret})
    dump = Redactor.build({"token": secret}).redact_dump(
        record, preserve=("run_id", "workflow_name", "workflow_path")
    )
    assert dump[field] == "[REDACTED:token]"


# --------------------------------------------------------------------------------------------------
# the same class, one level down: what a NESTED record is addressed by
# --------------------------------------------------------------------------------------------------

STEP_WORKFLOW = """
rayspec: 1
name: stepwise
isolation: none
inputs:
  token: {{ type: string, secret: true, required: true }}
steps:
  - id: {step}
    shell: 'printf "%s" "$RAYSPEC_INPUT_TOKEN"'
"""

GATED_WORKFLOW = """
rayspec: 1
name: gated
isolation: none
inputs:
  token: {type: string, secret: true, required: true}
steps:
  - {id: first, shell: "echo one"}
  - {id: gate, needs: [first], approve: "ship it?"}
  - {id: second, needs: [gate], shell: "echo two"}
outputs:
  v: "done"
"""


def _write(root: Path, name: str, text: str) -> Path:
    (root / ".rayspec" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".rayspec" / "workflows" / f"{name}.yaml").write_text(textwrap.dedent(text))
    return root


def test_a_secret_equal_to_a_step_id_keeps_the_step_addressable(
    tmp_path: Path, home: Path, cli: CliRunner
) -> None:
    """``preserve`` is the writer's word about the TOP-level record, and a step is one level down.

    A step's ``path`` is the key its own record is already filed under, so redacting it hides
    nothing — and it is what every later command parses to find the step: ``rayspec explain``
    died on ``invalid step path '[REDACTED:token]'``, a stack trace rather than an answer, for a
    run that had done nothing wrong. The two ``_ref`` fields the store built out of that path
    pointed at files that were not there for the same reason.
    """
    step = "deploy_tool"  # the secret value IS the step id
    project = _write(tmp_path / "proj", "stepwise", STEP_WORKFLOW.format(step=step))
    run = cli.invoke(app, ["run", "stepwise", "--root", str(project), "-i", f"token={step}"])
    assert run.exit_code == 0, run.output

    store = FileRunStore(home / "projects" / project_slug_for(project))
    run_id = store.list_run_ids()[0]
    raw = json.loads((store.run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
    record = raw["steps"][step]
    assert record["path"] == step and record["id"] == step
    assert record["output_ref"] == f"steps/{step}/output.txt"
    # the ref still resolves, and what it resolves TO — the secret itself — is still gone
    assert (store.run_dir(run_id) / record["output_ref"]).read_text() == "[REDACTED:token]"

    explained = cli.invoke(app, ["explain", run_id, step, "--root", str(project)])
    assert explained.exit_code == 0, explained.output
    assert explained.exception is None


def test_a_secret_equal_to_the_project_root_keeps_the_resumed_half_running(
    tmp_path: Path, home: Path, cli: CliRunner
) -> None:
    """``project_root`` and the workspace's ``workdir`` are where the rest of the run happens.

    A paused run is finished by a SECOND command, which rebuilds the engine's workspace from the
    record: with the directory rewritten, every remaining step failed ``cwd does not exist:
    [REDACTED:token]`` — half a run, unresumable, and the reason on the record names a directory
    that never existed. ``workdir`` sits inside a nested model, which is why the writer's
    ``preserve`` list could not reach it.
    """
    project = _write(tmp_path / "proj", "gated", GATED_WORKFLOW)
    secret = str(project)  # the secret value IS the project directory
    run = cli.invoke(app, ["run", "gated", "--root", str(project), "-i", f"token={secret}"])
    assert run.exit_code == 3, run.output  # paused at the gate

    store = FileRunStore(home / "projects" / project_slug_for(project))
    run_id = store.list_run_ids()[0]
    raw = json.loads((store.run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
    assert raw["project_root"] == secret
    assert raw["workspace"]["workdir"] == secret

    approved = cli.invoke(app, ["approve", run_id, "--root", str(project), "-i", f"token={secret}"])
    assert approved.exit_code == 0, approved.output
    assert store.load(run_id).steps["second"].status is StepStatus.SUCCEEDED


def test_a_nested_record_declares_its_own_identity_and_nothing_more() -> None:
    """Unit level: the declaration travels with the model, and only over the fields it names."""
    secret = "deploy_tool"
    record = RunRecord(
        run_id="20260822-000000-abcd",
        workflow_name="wf",
        workflow_path="wf.yaml",
        workflow_hash="0" * 64,
        project_slug="local/test",
        project_root="/tmp/proj",
        steps={
            secret: StepRecord(
                path=secret,
                id=secret,
                kind="shell",
                output_ref=f"steps/{secret}/output.txt",
                error=ErrorInfo(type="exit", message=f"{secret} exploded"),
            )
        },
    )
    dump = Redactor.build({"token": secret}).redact_dump(record, preserve=RUN_IDENTITY_FIELDS)
    step = dump["steps"][secret]
    assert step["path"] == secret and step["id"] == secret
    assert step["output_ref"] == f"steps/{secret}/output.txt"
    # everything the step holds that is CONTENT is still redacted
    assert step["error"]["message"] == "[REDACTED:token] exploded"


def test_the_identity_list_the_store_passes_is_the_one_the_record_declares() -> None:
    """One list, so a writer cannot preserve a field the record does not call its own."""
    assert RunRecord.redaction_identity == RUN_IDENTITY_FIELDS
    assert "project_root" in RUN_IDENTITY_FIELDS

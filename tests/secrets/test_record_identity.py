# SPDX-License-Identifier: Apache-2.0
"""Redaction must not rewrite the identifiers a run is LOOKED UP by.

``Redactor.redact_dump`` already leaves a record's structure alone where rewriting it would
destroy the record — field names, and the step paths a run's ``steps`` mapping is keyed by. The
run's own identity is the same class one level up: ``run_id`` names the directory the record
lives in, and ``workflow_name`` / ``workflow_path`` are what ``resume``/``approve``/``reject``/
``explain`` re-load the workflow by. A ``secret: true`` value that happens to equal one of them
used to rewrite it, and the run was permanently unreachable — ``unknown workflow
'[REDACTED:token]'`` — with no way to undo it.

Everything free-form (inputs, outputs, step data) stays redacted: these tests pin both halves.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from rayspec.config import Config
from rayspec.engine.runner import Runner
from rayspec.loader import ResolvedWorkflow, load_workflow
from rayspec.redact import Redactor
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord
from rayspec.store.redacting import RedactingStore

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
    """The canonical break: the value redacts, ``workflow_name`` does not, and a resume works."""
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
    # the step's OUTPUT — free-form — is still redacted: nothing was weakened
    assert raw["outputs"] == {"v": "[REDACTED:token]"}

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

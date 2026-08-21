"""The redaction boundary must not depend on the caller having wired it.

``docs/extending.md`` § Embedding the engine builds a store and hands it to :class:`Runner`.
Nothing there assigns ``store.redactor`` — so if the boundary only worked when a caller
remembered it, every embedder would write ``secret: true`` values straight into ``run.json``,
``events.jsonl`` and any plugin store. These tests pin the property the CLI's own wiring
happens to give it: the run installs what it needs itself, and refuses to start when it cannot.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, cast

import pytest

from rayspec.config import Config
from rayspec.engine.context import RunOptions
from rayspec.engine.errors import EngineError
from rayspec.engine.runner import Runner
from rayspec.loader import ResolvedWorkflow, load_workflow
from rayspec.redact import NULL_REDACTOR, Redactor
from rayspec.store.base import RunStore
from rayspec.store.file import FileRunStore

SECRET = "ghp_SECRETTOKEN_ABCDEF"

WORKFLOW = """
rayspec: 1
name: leak
isolation: none
inputs:
  token: { type: string, secret: true, required: true }
steps:
  - id: echo
    shell: 'printf "%s" "$RAYSPEC_INPUT_TOKEN"'
outputs:
  v: "{{ steps.echo.output }}"
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "leak.yaml").write_text(textwrap.dedent(WORKFLOW))
    return root


def _resolved(project: Path, home: Path) -> ResolvedWorkflow:
    return load_workflow("leak", project_root=project, home=home, config=Config())


class _FrozenRedactorStore:
    """A store whose ``redactor`` cannot be assigned — a plugin store rayspec cannot wire."""

    def __init__(self, inner: FileRunStore) -> None:
        self._inner = inner

    @property
    def redactor(self) -> Redactor:
        """Always the no-op; there is no setter, so the boundary cannot be installed."""
        return NULL_REDACTOR

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _files_containing(root: Path, needle: str) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and needle in path.read_text(errors="replace")
    )


def test_the_embedding_recipe_cannot_persist_a_secret(tmp_path: Path, project: Path) -> None:
    """The recipe verbatim: load, resolve, `Runner(..., store=FileRunStore(...)).run_sync()`."""
    home = tmp_path / "home"
    home.mkdir()
    store = FileRunStore(tmp_path / "store")
    assert store.redactor is NULL_REDACTOR  # nobody wired anything
    result = Runner(
        _resolved(project, home),
        inputs={"token": SECRET},
        store=store,
        project_root=project,
    ).run_sync()
    assert result.exit_code == 0, result.reason
    assert _files_containing(store.run_dir(result.run_id), SECRET) == []
    assert store.load(result.run_id).outputs == {"v": "[REDACTED:token]"}


def test_a_redactor_the_caller_installed_is_kept(tmp_path: Path, project: Path) -> None:
    """The CLI's redactor also covers ``config.secrets`` and the opt-in detectors — the run may
    only ADD to it, never replace it."""
    home = tmp_path / "home"
    home.mkdir()
    store = FileRunStore(tmp_path / "store")
    store.redactor = Redactor.build({"other": "unrelated-value"})
    result = Runner(
        _resolved(project, home),
        inputs={"token": SECRET},
        store=store,
        project_root=project,
    ).run_sync()
    assert result.exit_code == 0, result.reason
    assert store.redactor.redact("unrelated-value") == "[REDACTED:other]"
    assert store.redactor.redact(SECRET) == "[REDACTED:token]"


def test_config_secrets_handed_to_the_engine_are_covered_too(tmp_path: Path, project: Path) -> None:
    """``RunOptions.config_secrets`` reaches shell steps; a caller that sets it without a
    redactor would otherwise persist those values as well."""
    home = tmp_path / "home"
    home.mkdir()
    store = FileRunStore(tmp_path / "store")
    Runner(
        _resolved(project, home),
        inputs={"token": SECRET},
        store=store,
        project_root=project,
        options=RunOptions(config_secrets={"GH_TOKEN": "cfg-secret-value"}),
    ).run_sync()
    assert store.redactor.redact("cfg-secret-value") == "[REDACTED:GH_TOKEN]"


def test_a_store_that_cannot_hold_a_redactor_refuses_to_start(
    tmp_path: Path, project: Path
) -> None:
    """A store rayspec cannot install the boundary on must not run a workflow with secrets."""
    home = tmp_path / "home"
    home.mkdir()

    store = cast(RunStore, _FrozenRedactorStore(FileRunStore(tmp_path / "store")))
    runner = Runner(
        _resolved(project, home),
        inputs={"token": SECRET},
        store=store,
        project_root=project,
    )
    with pytest.raises(EngineError) as exc:
        runner.run_sync()
    assert "secret" in str(exc.value)
    assert not (tmp_path / "store" / "runs").exists()


def test_a_workflow_without_secrets_leaves_the_store_alone(tmp_path: Path, project: Path) -> None:
    """No secret, no redactor: a run that has nothing to hide pays nothing for the boundary."""
    home = tmp_path / "home"
    home.mkdir()
    (project / ".rayspec" / "workflows" / "plain.yaml").write_text(
        textwrap.dedent(
            """
            rayspec: 1
            name: plain
            isolation: none
            steps:
              - id: echo
                shell: 'printf hello'
            """
        )
    )
    store = FileRunStore(tmp_path / "store")
    Runner(
        load_workflow("plain", project_root=project, home=home, config=Config()),
        inputs={},
        store=store,
        project_root=project,
    ).run_sync()
    assert store.redactor is NULL_REDACTOR


def test_a_value_too_short_to_redact_is_recorded_and_announced(
    tmp_path: Path, project: Path
) -> None:
    """A short value is deliberately never redacted. The CLI says so at run start; an embedded
    run used to be silent about it, because the run returned before it installed anything."""
    home = tmp_path / "home"
    home.mkdir()
    store = FileRunStore(tmp_path / "store")
    result = Runner(
        _resolved(project, home),
        inputs={"token": "ab"},
        store=store,
        project_root=project,
    ).run_sync()
    assert store.redactor.skipped == ("token",)
    warnings = [
        event.data.get("message", "")
        for event in store.read_events(result.run_id)
        if event.type.value == "warning"
    ]
    assert any("token" in message and "not redacted" in message for message in warnings)


def test_a_config_secret_and_an_input_of_the_same_name_are_both_covered(
    tmp_path: Path, project: Path
) -> None:
    """``config.secrets`` and the workflow's inputs are independent namespaces that can use the
    same name for different values; merging them by name drops one value from the redactor
    while the step still gets both."""
    home = tmp_path / "home"
    home.mkdir()
    (project / ".rayspec" / "workflows" / "both.yaml").write_text(
        textwrap.dedent(
            """
            rayspec: 1
            name: both
            isolation: none
            inputs:
              token: { type: string, secret: true, required: true }
            steps:
              - id: echo
                shell: 'printf "%s %s" "$token" "$RAYSPEC_INPUT_TOKEN"'
            outputs:
              v: "{{ steps.echo.output }}"
            """
        )
    )
    store = FileRunStore(tmp_path / "store")
    result = Runner(
        load_workflow("both", project_root=project, home=home, config=Config()),
        inputs={"token": "INPUTVALUE_BBBBBBB"},
        store=store,
        project_root=project,
        options=RunOptions(config_secrets={"token": "CONFIGVALUE_AAAAAAA"}),
    ).run_sync()
    assert result.exit_code == 0, result.reason
    assert _files_containing(store.run_dir(result.run_id), "CONFIGVALUE_AAAAAAA") == []
    assert _files_containing(store.run_dir(result.run_id), "INPUTVALUE_BBBBBBB") == []

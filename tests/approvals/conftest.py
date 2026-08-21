"""Fixtures for the approval-class suite: a temp project, a loader check and a gate runner.

The suite deliberately keeps its own small harness instead of borrowing the engine suite's:
these tests are about *who may answer a gate*, so they need to drive one workflow to its gate
and inspect the record, and nothing else. ``home`` here is the shared one from
``tests/conftest.py`` (it exports ``RAYSPEC_HOME``), because the CLI tests in this package
invoke commands the way a user's shell would.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from rayspec.config import Config
from rayspec.engine.context import RunOptions
from rayspec.engine.runner import Runner, RunResult
from rayspec.events.model import EventType, RunEvent
from rayspec.events.sinks import CollectingSink
from rayspec.loader import ResolvedWorkflow, ValidationReport, load_workflow, validate_workflow
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord
from rayspec.templating import TemplateEngine


@dataclass
class Tree:
    """A temp project (``<root>/.rayspec``) plus an isolated rayspec home."""

    root: Path
    home: Path

    def write(self, rel: str, text: str) -> Path:
        path = self.root / ".rayspec" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return path

    def workflow(self, name: str, text: str) -> Path:
        return self.write(f"workflows/{name}.yaml", text)

    def load(self, name: str) -> ResolvedWorkflow:
        return load_workflow(name, project_root=self.root, home=self.home, config=Config())

    def check(self, name: str) -> ValidationReport:
        """Validate a workflow with the real template checker (capability checks skipped)."""
        return validate_workflow(self.load(name), template_checker=TemplateEngine())


@pytest.fixture
def tree(tmp_path: Path, home: Path) -> Tree:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    return Tree(root=root, home=home)


@dataclass
class GateRun:
    """One workflow driven through the engine, with the record and events it produced."""

    store: FileRunStore
    sink: CollectingSink
    result: RunResult

    @property
    def record(self) -> RunRecord:
        return self.store.load(self.result.run_id)

    def statuses(self) -> dict[str, str]:
        return {p: r.status.value for p, r in self.record.steps.items()}

    def events(self, type_: EventType) -> list[RunEvent]:
        return self.sink.events_of(type_)

    def decision(self, step_path: str) -> dict[str, Any]:
        for event in self.events(EventType.RUN_DECISION):
            if event.step_path == step_path:
                return dict(event.data)
        raise AssertionError(f"no run.decision for {step_path}")

    def warnings(self) -> list[str]:
        return [str(e.data.get("message", "")) for e in self.events(EventType.WARNING)]


@dataclass
class Gates:
    """Runs one workflow of ``tree`` and returns what the engine recorded."""

    tree: Tree
    store: FileRunStore
    engine: TemplateEngine = field(default_factory=TemplateEngine)

    async def run(
        self,
        name: str,
        *,
        options: RunOptions | None = None,
        prompt: Any = None,
        resume: str | None = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> GateRun:
        sink = CollectingSink()
        runner = Runner(
            self.tree.load(name),
            inputs=dict(inputs or {}),
            store=self.store,
            sinks=sink,
            project_root=self.tree.root,
            project_slug="local/test",
            options=options,
            approval_prompt=prompt,
            engine=self.engine,
            resume_run_id=resume,
            run_id=resume,
            handle_signals=True,
        )
        return GateRun(store=self.store, sink=sink, result=await runner.run())


@pytest.fixture
def gates(tree: Tree, tmp_path: Path) -> Gates:
    return Gates(tree=tree, store=FileRunStore(tmp_path / "store"))

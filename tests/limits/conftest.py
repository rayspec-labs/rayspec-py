"""Fixtures for the operational-limits tests: a temp project, a store and a run harness.

Deliberately its own harness rather than a shared one: these tests exercise cross-run state
under ``$RAYSPEC_HOME`` and need a project whose files they can rewrite between runs.
The ``home`` fixture from ``tests/conftest.py`` (which exports ``RAYSPEC_HOME``) applies here —
the CLI tests in this package want the command under test to find the store the way a shell does.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest

from rayspec.config import Config
from rayspec.engine.context import RunOptions
from rayspec.engine.runner import Runner, RunResult, Workspace
from rayspec.events.model import EventType, RunEvent
from rayspec.events.sinks import CollectingSink
from rayspec.loader import ResolvedWorkflow, load_workflow
from rayspec.providers.base import Provider
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord

#: The instant the ledger's own clock is stopped at for every test in this package.
#:
#: Deliberately NOT the ``WHEN`` those tests pass explicitly: if a ledger method reached for the
#: clock where it should have honoured a passed-in ``when=``, a frozen clock set to the same
#: instant would hide it, and every ``when=WHEN`` assertion would go on passing.
LEDGER_NOW = datetime(2026, 3, 4, 5, 6, tzinfo=UTC)


class _FrozenNow(datetime):
    """A ``datetime`` whose ``now()`` is :data:`LEDGER_NOW`. Everything else is a real one."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        return LEDGER_NOW if tz is None else LEDGER_NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def frozen_ledger_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Stop ``rayspec.limits.ledger``'s clock, for every test in this package.

    ``commit`` and ``read`` each take their OWN ``datetime.now(UTC)`` when the caller passes no
    ``when=``, and each turns its moment into one day/month bucket key. A test that commits and
    then reads is therefore asking the clock twice for a day, and 00:00 UTC landing in between
    puts the money in one bucket and the question in another. The gap is milliseconds wide, so
    this is not something a run will hit often — but the failures it produces are the exact
    symptoms these tests exist to detect: a ceiling that does not fire, a day total that reads
    as zero, a pause message quoting the wrong dollar figure. One of those in CI sends somebody
    hunting a product bug that is not there.

    The name is resolved on the module at call time, so this reaches the in-process ``CliRunner``
    commands and the engine's ``anyio.to_thread`` calls as well as direct use.
    """
    monkeypatch.setattr("rayspec.limits.ledger.datetime", _FrozenNow)
    return LEDGER_NOW


@dataclass
class Project:
    """A temp project (``root/.rayspec``), a file store and a collecting sink."""

    root: Path
    home: Path
    store: FileRunStore
    sink: CollectingSink = field(default_factory=CollectingSink)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / ".rayspec" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return path

    def workflow(self, name: str, text: str) -> Path:
        return self.write(f"workflows/{name}.yaml", text)

    def load(self, name: str) -> ResolvedWorkflow:
        return load_workflow(name, project_root=self.root, home=self.home, config=Config())

    def runner(
        self,
        resolved: ResolvedWorkflow | str,
        *,
        inputs: Mapping[str, Any] | None = None,
        options: RunOptions | None = None,
        providers: Mapping[str, Provider] | None = None,
        envelope: Any = None,
        resume: str | None = None,
        run_id: str | None = None,
    ) -> Runner:
        if isinstance(resolved, str):
            resolved = self.load(resolved)
        return Runner(
            resolved,
            inputs=inputs or {},
            store=self.store,
            sinks=self.sink,
            project_root=self.root,
            project_slug="local/test",
            options=options,
            providers=providers,
            envelope=envelope,
            resume_run_id=resume,
            run_id=run_id,
            workspace=Workspace.in_place(self.root),
            handle_signals=False,
        )

    async def run(self, resolved: ResolvedWorkflow | str, **kw: Any) -> RunResult:
        return await self.runner(resolved, **kw).run()

    def record(self, run_id: str) -> RunRecord:
        return self.store.load(run_id)

    def statuses(self, run_id: str) -> dict[str, str]:
        return {p: r.status.value for p, r in self.record(run_id).steps.items()}

    def events(self, type_: EventType | None = None) -> list[RunEvent]:
        if type_ is None:
            return list(self.sink.events)
        return self.sink.events_of(type_)


@pytest.fixture
def project(tmp_path: Path, home: Path) -> Project:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    return Project(root=root, home=home, store=FileRunStore(home / "projects" / "local-test"))

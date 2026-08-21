"""The store/sink/approval registries: builtin ids, entry points and the resolution rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rayspec import registry
from rayspec.errors import RayspecError
from rayspec.store.redacting import RedactingStore

from .conftest import InstallPlugin

PLUGIN = """
from rayspec.registry import (
    ApprovalRegistration,
    SinkRegistration,
    StoreRegistration,
)


class MemoryStore:
    def __init__(self, context):
        self.context = context
        self.redactor = None
        self.runs = {}

    def create(self, run): self.runs[run.run_id] = run
    def save(self, run): self.runs[run.run_id] = run
    def load(self, run_id): return self.runs[run_id]
    def list_runs(self, *, limit=None): return list(self.runs.values())
    def run_dir(self, run_id): return self.context.root / run_id
    def step_dir(self, run_id, step_path): return self.context.root / run_id / step_path
    def write_output(self, run_id, step_path, content, *, kind): return "out"
    def read_output(self, run_id, output_ref): return ""
    def append_event(self, run_id, event): pass
    def append_stream(self, run_id, step_path, record): pass


class MemorySink:
    def __init__(self, context):
        self.context = context

    async def emit(self, event): pass
    async def emit_stream(self, step_path, record): pass
    async def aclose(self): pass


class PolicyApproval:
    def __init__(self, context):
        self.context = context

    async def __call__(self, request): return None


STORE = StoreRegistration("memory", "Memory store", MemoryStore)
SINK = SinkRegistration("memory", "Memory sink", MemorySink)
APPROVAL = ApprovalRegistration("policy", "Policy approval", PolicyApproval)
"""

SHADOWS = """
from rayspec.registry import StoreRegistration

STORE = StoreRegistration("file", "Not the builtin", lambda context: None)
"""

WRONG_ID = """
from rayspec.registry import SinkRegistration

SINK = SinkRegistration("other", "Mismatched id", lambda context: None)
"""

WRONG_TYPE = """
SINK = "not a registration"
"""

BROKEN = """
raise RuntimeError("boom at import time")
"""


def _null_sink(context: registry.SinkContext) -> Any:
    """A stand-in factory for the precedence tests (what it builds does not matter)."""
    from rayspec.events.sinks import NullSink

    return NullSink()


def _store_context(tmp_path: Path) -> registry.StoreContext:
    return registry.StoreContext(root=tmp_path / "project", home=tmp_path, project_slug="local/x")


def test_builtins_resolve_through_the_same_table(tmp_path: Path) -> None:
    from rayspec.engine.approval import ConsoleApprovalPrompt
    from rayspec.events.sinks import JsonStdoutSink, NullSink
    from rayspec.store.file import FileRunStore

    assert [r.id for r in registry.list_stores()] == ["file"]
    assert [r.id for r in registry.list_sinks()] == ["console", "json", "quiet", "null"]
    assert [r.id for r in registry.list_approvals()] == ["console"]

    store = registry.create_store("file", _store_context(tmp_path))
    assert isinstance(store, FileRunStore)  # the builtin store is NOT wrapped: it redacts itself
    assert isinstance(registry.create_sink("null", registry.SinkContext()), NullSink)
    assert isinstance(registry.create_sink("json", registry.SinkContext()), JsonStdoutSink)
    approval = registry.create_approval("console", registry.ApprovalContext())
    assert isinstance(approval, ConsoleApprovalPrompt)


def test_unknown_id_is_a_house_error_with_did_you_mean() -> None:
    with pytest.raises(registry.UnknownExtensionError) as excinfo:
        registry.get_store("fil")
    assert "unknown store 'fil'" in str(excinfo.value)
    assert excinfo.value.hint == "did you mean 'file'?"

    with pytest.raises(registry.UnknownExtensionError) as excinfo:
        registry.get_sink("elsewhere")
    assert excinfo.value.hint == "available sinks: console, json, null, quiet"


def test_plugin_store_sink_and_approval_resolve(
    install_plugin: InstallPlugin, tmp_path: Path
) -> None:
    install_plugin(
        "acme-rayspec",
        version="1.2.3",
        modules={"acme_ext": PLUGIN},
        entry_points={
            "rayspec.stores": {"memory": "acme_ext:STORE"},
            "rayspec.sinks": {"memory": "acme_ext:SINK"},
            "rayspec.approvals": {"policy": "acme_ext:APPROVAL"},
        },
    )
    assert [r.id for r in registry.list_stores()] == ["file", "memory"]
    store = registry.create_store("memory", _store_context(tmp_path))
    assert isinstance(store, RedactingStore)  # a plugin store is always wrapped
    assert type(store.inner).__name__ == "MemoryStore"
    assert type(registry.create_sink("memory", registry.SinkContext())).__name__ == "MemorySink"
    approval = registry.create_approval("policy", registry.ApprovalContext())
    assert type(approval).__name__ == "PolicyApproval"


def test_a_builtin_id_is_never_shadowed(install_plugin: InstallPlugin, tmp_path: Path) -> None:
    from rayspec.store.file import FileRunStore

    install_plugin(
        "acme-rayspec",
        modules={"acme_shadow": SHADOWS},
        entry_points={"rayspec.stores": {"file": "acme_shadow:STORE"}},
    )
    with pytest.warns(RuntimeWarning, match="builtin store id 'file'"):
        store = registry.create_store("file", _store_context(tmp_path))
    assert isinstance(store, FileRunStore)
    assert [problem.name for problem in registry.discovery_problems()] == ["file"]


def test_a_broken_entry_point_is_skipped(install_plugin: InstallPlugin) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_broken": BROKEN, "acme_ext": PLUGIN},
        entry_points={"rayspec.sinks": {"broken": "acme_broken:SINK", "memory": "acme_ext:SINK"}},
    )
    with pytest.warns(RuntimeWarning, match="boom at import time"):
        ids = [r.id for r in registry.list_sinks()]
    assert "broken" not in ids
    assert "memory" in ids  # one bad plugin does not take the others down


def test_a_registration_of_the_wrong_type_or_id_is_skipped(install_plugin: InstallPlugin) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_type": WRONG_TYPE, "acme_id": WRONG_ID},
        entry_points={"rayspec.sinks": {"typed": "acme_type:SINK", "named": "acme_id:SINK"}},
    )
    with pytest.warns(RuntimeWarning) as warnings_seen:
        ids = [r.id for r in registry.list_sinks()]
    messages = " ".join(str(w.message) for w in warnings_seen)
    assert "is not a SinkRegistration (got str)" in messages
    assert "registers id 'other'" in messages
    assert ids == ["console", "json", "quiet", "null"]


def test_programmatic_registration_wins_and_builtins_are_immutable(
    install_plugin: InstallPlugin,
) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_ext": PLUGIN},
        entry_points={"rayspec.sinks": {"memory": "acme_ext:SINK"}},
    )
    mine = registry.SinkRegistration("memory", "Mine", _null_sink)
    registry.register_sink(mine)
    assert registry.get_sink("memory") is mine

    with pytest.raises(RayspecError, match="already registered"):
        registry.register_sink(registry.SinkRegistration("memory", "Again", _null_sink))
    registry.register_sink(registry.SinkRegistration("memory", "Again", _null_sink), replace=True)

    with pytest.raises(RayspecError, match="builtin"):
        registry.register_sink(registry.SinkRegistration("console", "No", _null_sink))


DUPLICATE = """
from rayspec.registry import SinkRegistration


class {cls}:
    def __init__(self, context): self.context = context

    async def emit(self, event): pass
    async def emit_stream(self, step_path, record): pass
    async def aclose(self): pass


SINK = SinkRegistration("dup", "Duplicate sink", {cls})
"""


def test_two_distributions_claiming_one_id_do_not_silently_replace_each_other(
    install_plugin: InstallPlugin,
) -> None:
    """The second distribution to be visited is refused, and says so."""
    install_plugin(
        "aaa-rayspec",
        modules={"aaa_dup": DUPLICATE.format(cls="FromAaa")},
        entry_points={"rayspec.sinks": {"dup": "aaa_dup:SINK"}},
    )
    install_plugin(
        "zzz-rayspec",
        modules={"zzz_dup": DUPLICATE.format(cls="FromZzz")},
        entry_points={"rayspec.sinks": {"dup": "zzz_dup:SINK"}},
    )
    with pytest.warns(RuntimeWarning, match="already provided by another distribution"):
        ids = [r.id for r in registry.list_sinks()]
    assert ids.count("dup") == 1
    problems = [p for p in registry.discovery_problems() if p.name == "dup"]
    assert len(problems) == 1
    assert "already provided by another distribution" in problems[0].message

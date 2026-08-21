from __future__ import annotations

import inspect

from rayspec.store.base import RunStore


def test_run_store_protocol_has_minimal_surface():
    names = {n for n, _ in inspect.getmembers(RunStore) if not n.startswith("_")}
    assert {
        "create",
        "save",
        "load",
        "list_runs",
        "write_output",
        "read_output",
        "append_event",
        "append_stream",
        "step_dir",
        "run_dir",
    } <= names

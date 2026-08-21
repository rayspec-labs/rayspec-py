"""The discovery helpers are reachable from the packages they belong to, and stay lazy."""

from __future__ import annotations

import subprocess
import sys


def test_store_package_exports_the_store_seam() -> None:
    import rayspec.registry as registry
    import rayspec.store as store

    assert store.create_store is registry.create_store
    assert store.list_stores is registry.list_stores
    assert store.register_store is registry.register_store
    assert store.StoreContext is registry.StoreContext
    assert store.StoreRegistration is registry.StoreRegistration
    for name in ("create_store", "list_stores", "register_store", "RedactingStore"):
        assert name in store.__all__


def test_events_package_exports_the_sink_seam() -> None:
    import rayspec.events as events
    import rayspec.registry as registry

    assert events.create_sink is registry.create_sink
    assert events.list_sinks is registry.list_sinks
    assert events.register_sink is registry.register_sink
    assert events.SinkContext is registry.SinkContext
    assert events.SinkRegistration is registry.SinkRegistration


def test_the_seam_does_not_drag_rich_into_the_model_import_path() -> None:
    """``rayspec.events`` stays importable without loading ``rich`` — the registry included."""
    code = (
        "import sys; import rayspec.events as e; e.create_sink; e.SinkContext; "
        "import rayspec.registry; "
        "assert 'rich' not in sys.modules, sorted(m for m in sys.modules if 'rich' in m)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)

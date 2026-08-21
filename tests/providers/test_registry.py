"""Provider registry: builtins, lazy adapter import, entry-point discovery, did-you-mean."""

from __future__ import annotations

import sys
import types
import warnings
from collections.abc import Mapping
from importlib.metadata import EntryPoint
from typing import Any

import pytest

from rayspec.errors import RayspecError
from rayspec.providers import registry
from rayspec.providers.base import (
    ProviderCapabilities,
    ProviderError,
    ProviderNotInstalledError,
    ProviderRegistration,
)
from rayspec.providers.capabilities import (
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    STUB_CAPABILITIES,
)
from rayspec.providers.registry import (
    ENTRY_POINT_GROUP,
    UnknownProviderError,
    create_provider,
    get_registration,
    list_registrations,
    register,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    # isolate from whatever entry points the environment has installed
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    reset_registry()
    yield
    reset_registry()


def test_builtins_are_registered_without_importing_adapters(monkeypatch):
    # other test modules (tests/providers/test_claude.py, ...) import the adapters; forget them so
    # this test still proves that the registry itself never imports them
    monkeypatch.delitem(sys.modules, "rayspec.providers.claude", raising=False)
    monkeypatch.delitem(sys.modules, "rayspec.providers.codex", raising=False)
    ids = [r.id for r in list_registrations()]
    assert ids == ["claude", "codex", "stub"]
    assert "rayspec.providers.claude" not in sys.modules
    assert "rayspec.providers.codex" not in sys.modules
    claude = get_registration("claude")
    assert isinstance(claude, ProviderRegistration)
    assert claude.capabilities is CLAUDE_CAPABILITIES
    assert get_registration("codex").capabilities is CODEX_CAPABILITIES
    assert get_registration("stub").capabilities is STUB_CAPABILITIES
    assert claude.display_name and get_registration("codex").display_name
    assert ENTRY_POINT_GROUP == "rayspec.providers"


def test_unknown_id_raises_lookup_error_with_did_you_mean():
    with pytest.raises(UnknownProviderError) as info:
        get_registration("claud")
    err = info.value
    assert isinstance(err, RayspecError) and isinstance(err, LookupError)
    assert "claud" in str(err)
    assert err.hint is not None and "claude" in err.hint
    with pytest.raises(UnknownProviderError) as info2:
        get_registration("zzz")
    assert info2.value.hint is not None
    assert "claude, codex, stub" in info2.value.hint  # lists available ids when no close match


def test_create_provider_for_missing_adapter_raises_not_installed(monkeypatch):
    # a None entry in sys.modules makes `import rayspec.providers.claude` raise ImportError
    monkeypatch.setitem(sys.modules, "rayspec.providers.claude", None)  # type: ignore[arg-type]
    with pytest.raises(ProviderNotInstalledError) as info:
        create_provider("claude", {})
    assert "provider adapter not available" in str(info.value)
    assert info.value.hint and "claude" in info.value.hint
    assert info.value.kind == "not_installed"


def test_create_provider_lazily_imports_adapter_and_passes_settings(monkeypatch):
    created: list[Mapping[str, Any]] = []

    class ClaudeProvider:
        id = "claude"
        capabilities = CLAUDE_CAPABILITIES

        def __init__(self, settings: Mapping[str, Any]):
            created.append(settings)

    fake = types.ModuleType("rayspec.providers.claude")
    fake.ClaudeProvider = ClaudeProvider  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rayspec.providers.claude", fake)
    provider = create_provider("claude", {"setting_sources": ["project"]})
    assert isinstance(provider, ClaudeProvider)
    assert created == [{"setting_sources": ["project"]}]


def test_create_provider_codex_uses_codex_module(monkeypatch):
    class CodexProvider:
        def __init__(self, settings):
            self.settings = settings

    fake = types.ModuleType("rayspec.providers.codex")
    fake.CodexProvider = CodexProvider  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rayspec.providers.codex", fake)
    assert isinstance(create_provider("codex", {}), CodexProvider)


def _caps() -> ProviderCapabilities:
    return ProviderCapabilities(
        structured_output="best_effort",
        session_resume=False,
        session_fork=False,
        instructions_modes=frozenset({"append"}),
        access_levels=frozenset(),
        tool_groups=frozenset(),
        raw_tool_names=False,
        max_turns=False,
        budget_usd=False,
        cost_reporting=False,
        effort_levels=frozenset(),
    )


class _FakeProvider:
    """Minimal object satisfying the Provider protocol."""

    id = "fake"
    capabilities = _caps()

    def __init__(self, settings: Mapping[str, Any]):
        self.settings = settings

    async def open(self, *, run_id, workdir, env, max_parallel) -> None:
        return None

    async def run(self, req, emit):  # pragma: no cover - never called
        raise NotImplementedError

    async def healthcheck(self, *, probe: bool = False):  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _reg(provider_id: str, display_name: str = "x") -> ProviderRegistration:
    return ProviderRegistration(
        id=provider_id, display_name=display_name, capabilities=_caps(), factory=_FakeProvider
    )


def _fake_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_entry_point_discovery(monkeypatch):
    loaded: list[str] = []

    good = _reg("acme", "ACME Agents")
    monkeypatch.setitem(sys.modules, "acme_rayspec", _fake_module("acme_rayspec", REG=good))
    monkeypatch.setitem(sys.modules, "not_a_reg", _fake_module("not_a_reg", REG=object()))
    monkeypatch.setitem(sys.modules, "mismatch", _fake_module("mismatch", REG=good))

    class _EP(EntryPoint):
        def load(self):
            loaded.append(self.name)
            return super().load()

    eps = [
        _EP(name="acme", value="acme_rayspec:REG", group=ENTRY_POINT_GROUP),
        _EP(name="claude", value="acme_rayspec:REG", group=ENTRY_POINT_GROUP),  # builtin wins
        _EP(name="broken", value="no_such_module_xyz:REG", group=ENTRY_POINT_GROUP),
        _EP(name="bogus", value="not_a_reg:REG", group=ENTRY_POINT_GROUP),
        _EP(name="other", value="mismatch:REG", group=ENTRY_POINT_GROUP),  # name != id
    ]
    monkeypatch.setattr(
        registry, "entry_points", lambda group: eps if group == ENTRY_POINT_GROUP else []
    )
    reset_registry()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        regs = list_registrations()

    assert [r.id for r in regs] == ["claude", "codex", "stub", "acme"]
    assert get_registration("acme") is good
    assert "claude" not in loaded  # skipped before load: builtin ids are never overridden
    messages = "\n".join(str(w.message) for w in caught)
    assert "broken" in messages and "bogus" in messages and "other" in messages
    provider = create_provider("acme", {"k": 1})
    assert isinstance(provider, _FakeProvider) and provider.settings == {"k": 1}


def test_register_programmatically_and_replace_semantics():
    reg = _reg("mine", "Mine")
    register(reg)
    assert get_registration("mine") is reg
    with pytest.raises(RayspecError, match="already registered"):
        register(reg)
    reg2 = _reg("mine", "Mine 2")
    register(reg2, replace=True)
    assert get_registration("mine") is reg2
    with pytest.raises(RayspecError, match="builtin"):
        register(_reg("claude"), replace=True)
    reset_registry()
    with pytest.raises(UnknownProviderError):
        get_registration("mine")


def test_list_registrations_is_cached_and_returns_copies():
    a = list_registrations()
    b = list_registrations()
    assert a == b and a is not b


# -- review fixes ---------------------------------------------------------------------------


def test_create_provider_wraps_non_import_errors_from_adapter_import(monkeypatch):
    """An adapter/SDK that blows up at import (RuntimeError/OSError) is a ProviderError + hint."""
    import importlib

    def boom(name: str):
        if name == "rayspec.providers.codex":
            raise RuntimeError("libfoo.so: cannot open shared object")
        return importlib.import_module(name)

    monkeypatch.setattr(registry.importlib, "import_module", boom)
    with pytest.raises(ProviderError) as info:
        create_provider("codex", {})
    err = info.value
    assert not isinstance(err, ProviderNotInstalledError)
    assert err.kind == "provider" and err.hint and "codex" in err.hint and "libfoo" in err.hint
    assert isinstance(err.__cause__, RuntimeError)
    assert "provider adapter failed to import" in str(err)


def test_programmatic_registration_always_wins_over_entry_points(monkeypatch):
    """Precedence: builtins > programmatic > entry points, regardless of call order."""
    plugin = _reg("acme", "Plugin ACME")
    monkeypatch.setitem(sys.modules, "acme_rayspec", _fake_module("acme_rayspec", REG=plugin))
    eps = [EntryPoint(name="acme", value="acme_rayspec:REG", group=ENTRY_POINT_GROUP)]
    monkeypatch.setattr(
        registry, "entry_points", lambda group: eps if group == ENTRY_POINT_GROUP else []
    )
    # entry point loaded first, then programmatic registration with the same id: no replace= needed
    reset_registry()
    assert get_registration("acme") is plugin
    mine = _reg("acme", "Mine")
    register(mine)
    assert get_registration("acme") is mine
    # a second programmatic registration of the same id still needs replace=True
    with pytest.raises(RayspecError, match="already registered"):
        register(_reg("acme", "Again"))
    # programmatic first, entry point discovered later: programmatic still wins
    reset_registry()
    register(mine)
    assert get_registration("acme") is mine
    assert [r.id for r in list_registrations()] == ["claude", "codex", "stub", "acme"]

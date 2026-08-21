"""Fixtures for the extension-seam tests: fake *installed* distributions.

The seams under test are entry points, so the tests need real distribution metadata rather
than a monkeypatched ``entry_points()``: :func:`install_plugin` writes a module plus a
``*.dist-info`` directory into a directory on ``sys.path``, which is exactly what a
``pip install`` leaves behind. That way the distribution name and version the ``plugins``
command reports come from the metadata backend, not from a stub of it.

Nothing here touches the network or the real environment: the site directory is a ``tmp_path``
child, and every cached registry is reset around each test.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

#: What :func:`install_plugin` accepts: distribution name, modules, entry points.
InstallPlugin = Callable[..., None]


@pytest.fixture(autouse=True)
def _reset_registries() -> Iterator[None]:
    """Forget every cached extension table before and after each test."""
    from rayspec import registry
    from rayspec.cli import plugins

    registry.reset_registry()
    plugins.reset_cli_plugins()
    yield
    registry.reset_registry()
    plugins.reset_cli_plugins()


@pytest.fixture
def install_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[InstallPlugin]:
    """Install fake distributions into a temporary site directory on ``sys.path``."""
    site = tmp_path / "site-packages"
    site.mkdir()
    monkeypatch.syspath_prepend(str(site))
    written: list[str] = []

    def install(
        dist_name: str,
        *,
        version: str = "0.1.0",
        modules: Mapping[str, str],
        entry_points: Mapping[str, Mapping[str, str]],
    ) -> None:
        for module_name, source in modules.items():
            (site / f"{module_name}.py").write_text(source, encoding="utf-8")
            written.append(module_name)
        info = site / f"{dist_name.replace('-', '_')}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n", encoding="utf-8"
        )
        lines: list[str] = []
        for group, eps in entry_points.items():
            lines.append(f"[{group}]")
            lines.extend(f"{name} = {value}" for name, value in eps.items())
            lines.append("")
        (info / "entry_points.txt").write_text("\n".join(lines), encoding="utf-8")
        importlib.invalidate_caches()

    yield install

    for module_name in written:
        sys.modules.pop(module_name, None)
    importlib.invalidate_caches()

"""The capability matrix in ``docs/providers.md`` is generated from the provider registry and
must be up to date (``uv run python scripts/gen_capability_matrix.py`` regenerates it)."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_capability_matrix.py"


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_capability_matrix", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rendered_matrix_lists_every_builtin_provider_and_capability(gen: ModuleType) -> None:
    from rayspec.providers.base import ProviderCapabilities
    from rayspec.providers.registry import BUILTIN_REGISTRATIONS

    table = gen.render_matrix()
    header = table.splitlines()[0]
    for reg in BUILTIN_REGISTRATIONS:
        assert f"`{reg.id}`" in header
    for name in ProviderCapabilities.__dataclass_fields__:
        assert f"`{name}`" in table


def test_providers_doc_matrix_is_up_to_date(gen: ModuleType, docs_dir: Path) -> None:
    doc = docs_dir / "providers.md"
    text = doc.read_text(encoding="utf-8")
    assert gen.START_MARKER in text and gen.END_MARKER in text, (
        f"{doc} must contain the {gen.START_MARKER} / {gen.END_MARKER} markers"
    )
    expected = gen.replace_block(text, gen.render_matrix())
    assert text == expected, (
        "docs/providers.md capability matrix is stale; run "
        "`uv run python scripts/gen_capability_matrix.py`"
    )


def test_script_check_mode_passes_on_committed_doc(gen: ModuleType) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_replace_block_rewrites_only_between_markers(gen: ModuleType) -> None:
    text = f"before\n{gen.START_MARKER}\nold\n{gen.END_MARKER}\nafter\n"
    out = gen.replace_block(text, "| new |")
    assert out == f"before\n{gen.START_MARKER}\n| new |\n{gen.END_MARKER}\nafter\n"


def test_replace_block_requires_markers(gen: ModuleType) -> None:
    with pytest.raises(ValueError):
        gen.replace_block("no markers here\n", "| x |")


def test_script_depends_only_on_public_rayspec_names() -> None:
    """The docs script must not import ``_private`` names from other modules."""
    source = SCRIPT.read_text(encoding="utf-8")
    private = re.findall(r"^from rayspec\S* import .*\b_\w+", source, re.MULTILINE)
    assert not private, private


def test_capability_rows_follow_the_frozen_dataclass_order(gen: ModuleType) -> None:
    """Rows come from ``ProviderCapabilities`` (frozen contract), in declaration order, each noted."""
    from rayspec.providers.base import ProviderCapabilities

    assert tuple(gen.capability_rows()) == tuple(ProviderCapabilities.__dataclass_fields__)
    assert set(gen.CAPABILITY_NOTES) == set(ProviderCapabilities.__dataclass_fields__)

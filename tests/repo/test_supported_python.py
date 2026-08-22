# SPDX-License-Identifier: Apache-2.0
"""The package must be importable on every Python it claims to support.

This exists because it once was not. Three ``@dataclass`` fields defaulted to a
``MappingProxyType`` — an immutable mapping, chosen deliberately so a shared default could not be
written to — and **Python 3.11 refuses it**: its mutable-default check rejects any default whose
``__hash__`` is ``None``, which a ``mappingproxy`` is. Python 3.12 relaxed that check, so the whole
suite passed on the interpreter it ran under while ``import rayspec`` raised ``ValueError`` on the
declared floor. Every command tracebacked there, ``version`` and ``doctor`` included.

Nothing caught it. The gate runs on one interpreter; the CI matrix that covers 3.11 has never had a
green run, because standard runners are free only on public repositories. A wheel installed on 3.11
was the first thing to notice, and only because someone ran it.

So the check here is structural rather than a second interpreter: walk every dataclass the package
defines and require each field default to be hashable, which is the rule 3.11 applies. It holds on
any interpreter, including the newest, where the language itself no longer would.

Note how it has to ask. 3.11 reads ``default.__class__.__hash__ is None``, and that attribute is
exactly what changed: on 3.11 ``mappingproxy.__hash__`` IS ``None``, on 3.12 it is a slot wrapper
that raises when called. Inspecting the attribute from 3.12 therefore sees nothing wrong, so the
check calls ``hash()`` and catches ``TypeError`` — which is the same question asked in a way that
does not depend on the interpreter answering it.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import tomllib
from pathlib import Path

import pytest

import rayspec

REPO_ROOT = Path(__file__).resolve().parents[2]


def dataclasses_of_the_package() -> list[tuple[str, type]]:
    """Every dataclass rayspec defines, as ``(module, class)``.

    Keyed on ``__module__`` so a class merely *imported* by a module is visited once, where it is
    defined, rather than once per importer.
    """
    found: dict[str, type] = {}
    for info in pkgutil.walk_packages(rayspec.__path__, f"{rayspec.__name__}."):
        try:
            module = importlib.import_module(info.name)
        except Exception:  # pragma: no cover - an optional adapter whose SDK is absent
            continue
        for name in dir(module):
            obj = getattr(module, name, None)
            if not isinstance(obj, type) or not dataclasses.is_dataclass(obj):
                continue
            if getattr(obj, "__module__", "") == info.name:
                found[f"{info.name}.{obj.__name__}"] = obj
    return sorted(found.items())


def test_every_dataclass_default_is_hashable() -> None:
    """A field default that is not hashable makes the module unimportable on Python 3.11."""
    offenders: list[str] = []
    for qualname, cls in dataclasses_of_the_package():
        for field in dataclasses.fields(cls):
            default = field.default
            if default is dataclasses.MISSING:
                continue
            try:
                hash(default)
            except TypeError:
                offenders.append(
                    f"{qualname}.{field.name}: {type(default).__name__} is not hashable — "
                    f"use `field(default_factory=lambda: <the shared value>)`"
                )
    assert not offenders, (
        "these defaults raise ValueError at import time on Python 3.11:\n" + "\n".join(offenders)
    )


def test_the_scan_actually_sees_the_packages_dataclasses() -> None:
    """A scan that silently found nothing would make the check above vacuous."""
    found = dataclasses_of_the_package()
    assert len(found) > 20, f"only {len(found)} dataclasses found — the walk has stopped working"


def test_the_supported_floor_is_the_one_the_matrix_tests() -> None:
    """`requires-python` and the CI matrix must name the same lowest version.

    They are two statements of the same promise, and the promise is what a user installing on that
    interpreter relies on. If the floor moves, both move.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = pyproject["project"]["requires-python"]
    assert requires.startswith(">="), f"unexpected form: {requires!r}"
    floor = requires.removeprefix(">=").strip()

    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f'"{floor}"' in workflow, (
        f"pyproject requires Python >= {floor}, but the CI matrix in ci.yml does not test it"
    )


@pytest.mark.parametrize("classifier_floor", ["3.11"])
def test_the_classifiers_agree_with_the_floor(classifier_floor: str) -> None:
    """A wheel's classifiers are what an index shows; they must not promise a version that fails."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = pyproject["project"].get("classifiers", [])
    versions = [
        c.rsplit(" :: ", 1)[-1] for c in classifiers if "Programming Language :: Python ::" in c
    ]
    named = [v for v in versions if v[0].isdigit() and "." in v]
    assert classifier_floor in named, (
        f"the classifiers do not name {classifier_floor}, the declared floor: {named}"
    )

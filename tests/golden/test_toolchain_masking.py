# SPDX-License-Identifier: Apache-2.0
"""``RunRecord.toolchain``: masked where it describes the machine, kept where it describes the run.

Boundary: the classification itself, checked against both ends — the producer
(:func:`rayspec.engine.toolchain.capture_toolchain`) and the committed corpus. ``_capture.py``
explains the decision in prose; this module is what makes it hold: a new toolchain field fails
here until somebody decides whether it belongs in ``MASKED_KEYS``, instead of quietly appearing
in every committed record on the next regeneration.

The producer end is read from its source rather than called: a real capture needs a run context,
a provider pool and a workflow, none of which say anything about masking.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

from rayspec.engine.toolchain import capture_toolchain

from ._capture import MASKED_KEYS

GOLDEN_DIR = Path(__file__).resolve().parent

#: Every field of the toolchain block and what must happen to it in a committed record.
#: ``kept`` is the one deliberate exception: which model each agent resolved to is a decision of
#: the RUN (the workflow, its agent files and the tier table), not a property of the machine —
#: it is the whole reason the block is worth having.
CLASSIFICATION: dict[str, str] = {
    "rayspec": "masked",
    "python": "masked",
    "platform": "masked",
    "providers": "per-provider",
    "models": "kept",
}

#: The same for one provider entry. ``error`` records why a probe failed — a sentence about the
#: machine ("codex not found at …"), so a corpus that grew one was captured on a broken checkout.
PROVIDER_CLASSIFICATION: dict[str, str] = {
    "sdk_version": "masked",
    "cli_version": "masked",
    "cli_path": "masked",
    "error": "forbidden",
}

_MODEL_ID = re.compile(r"^[a-z0-9][\w.@:-]*$")


def produced_fields() -> set[str]:
    """The keys ``capture_toolchain`` returns, read from its own ``return {...}``."""
    tree = ast.parse(inspect.cleandoc(inspect.getsource(capture_toolchain)))
    returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]
    dicts = [node.value for node in returns if isinstance(node.value, ast.Dict)]
    assert len(dicts) == 1, "capture_toolchain no longer ends in one dict literal"
    keys = {str(k.value) for k in dicts[0].keys if isinstance(k, ast.Constant)}
    assert len(keys) == len(dicts[0].keys), "a non-literal key in the toolchain block"
    return keys


def committed_toolchains() -> list[tuple[Path, dict[str, Any]]]:
    """``(path, toolchain)`` for every committed record that has one."""
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(GOLDEN_DIR.rglob("run.json")):
        toolchain = json.loads(path.read_text(encoding="utf-8")).get("toolchain")
        if isinstance(toolchain, dict):
            found.append((path.relative_to(GOLDEN_DIR), toolchain))
    assert found, "no committed record carries a toolchain block"
    return found


TOOLCHAINS = committed_toolchains()


def test_every_toolchain_field_is_classified() -> None:
    """Totality: a field the capture gained must be masked or deliberately kept, never neither."""
    assert produced_fields() == set(CLASSIFICATION), (
        "capture_toolchain and the masking decision disagree; classify the new field in "
        "CLASSIFICATION and, unless it is a property of the RUN, add it to _capture.MASKED_KEYS"
    )


def test_the_masking_matches_the_classification() -> None:
    """``masked`` means _capture.py replaces it; ``kept`` means it deliberately does not."""
    for field, verdict in CLASSIFICATION.items():
        assert (field in MASKED_KEYS) == (verdict == "masked"), field
    for field, verdict in PROVIDER_CLASSIFICATION.items():
        assert (field in MASKED_KEYS) == (verdict == "masked"), field


@pytest.mark.parametrize("path, toolchain", TOOLCHAINS, ids=lambda v: str(v)[:40])
def test_committed_toolchain_is_masked_except_for_the_models(
    path: Path, toolchain: dict[str, Any]
) -> None:
    assert set(toolchain) == set(CLASSIFICATION), path
    for field, verdict in CLASSIFICATION.items():
        if verdict == "masked":
            assert toolchain[field] == MASKED_KEYS[field], f"{path}: {field} is not masked"
    for provider, entry in toolchain["providers"].items():
        unclassified = set(entry) - set(PROVIDER_CLASSIFICATION)
        assert not unclassified, f"{path}: {provider} has unclassified fields {unclassified}"
        for field, value in entry.items():
            verdict = PROVIDER_CLASSIFICATION[field]
            assert verdict != "forbidden", f"{path}: {provider}.{field} = {value!r}"
            assert value == MASKED_KEYS[field], f"{path}: {provider}.{field} is not masked"


@pytest.mark.parametrize("path, toolchain", TOOLCHAINS, ids=lambda v: str(v)[:40])
def test_the_models_of_a_run_survive_masking(path: Path, toolchain: dict[str, Any]) -> None:
    """The kept half must actually be there: agent key → model id, and no placeholder among them."""
    models = toolchain["models"]
    assert isinstance(models, dict) and models, f"{path}: no resolved models recorded"
    for key, model in models.items():
        assert key not in MASKED_KEYS, f"{path}: {key} would be masked as a value elsewhere"
        assert model is None or _MODEL_ID.match(str(model)), f"{path}: {key} = {model!r}"

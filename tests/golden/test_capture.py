# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the golden-corpus masking helpers (``tests/golden/_capture.py``).

The corpus is only a contract if two checkouts of the same commit capture byte-identical files.
Everything that is a property of the *machine* — a detached HEAD, the length of the checkout
path — must be masked before it reaches a committed file.
"""

from __future__ import annotations

from ._capture import USAGE_KEYS, mask


def test_a_null_value_of_a_masked_key_is_still_masked() -> None:
    """A detached-HEAD checkout reports ``branch: None``; the corpus says ``<branch>``."""
    assert mask({"workspace": {"branch": None}}, []) == {"workspace": {"branch": "<branch>"}}
    assert mask({"pid": None}, []) == {"pid": None}


def test_usage_counters_are_masked() -> None:
    """The stub derives its token counts from the prompt, which embeds the checkout path."""
    masked = mask({"usage": {"input": 113, "output": 7, "extra": "keep"}}, [])
    assert masked == {"usage": {"input": 0, "output": 0, "extra": "keep"}}
    assert mask({"usage": None}, []) == {"usage": None}
    assert "input" in USAGE_KEYS


def test_usage_counter_names_are_masked_only_inside_usage() -> None:
    """``output`` is a generic key; only the one under ``usage`` is a derived counter."""
    assert mask({"step": {"output": "hello"}}, []) == {"step": {"output": "hello"}}


def test_discovery_carries_a_malformed_case_file_instead_of_raising(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A neighbour's broken case file must not break collection of the whole session."""
    from .test_golden import discover

    tests_dir = tmp_path / ".rayspec" / "tests" / "demo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "broken.yaml").write_text("workflowz: nope\n", encoding="utf-8")
    suites, error = discover(tmp_path)
    assert suites == []
    assert error is not None and "workflowz" in error

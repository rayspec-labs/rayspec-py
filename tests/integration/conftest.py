# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the end-to-end integration tests.

These tests exercise **how the CLI itself resolves the run store**, so their ``home`` deliberately
only creates the directory and hands the path back — it does **not** export ``RAYSPEC_HOME``.
Each invocation passes the location explicitly instead.

That is why this file exists rather than the tests inheriting the root ``home`` in
``tests/conftest.py``, which does set the environment variable: overriding it here makes the rule
mechanical instead of a comment somebody can miss.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A run store the test passes explicitly — no ``RAYSPEC_HOME`` in the environment."""
    path = tmp_path / "home"
    path.mkdir()
    return path

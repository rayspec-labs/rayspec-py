"""Fixtures shared by every test package.

Only genuinely universal fixtures belong here. In particular there are **two** different `home`
shapes in this suite and they are not interchangeable:

* the one below sets ``RAYSPEC_HOME`` in the environment — what CLI, workspace and example tests
  want, because the command under test should discover the store the way a user's shell does;
* ``tests/integration/*`` deliberately defines its own ``home`` that only creates the directory
  and passes the path explicitly per invocation, because those tests exercise how the CLI itself
  resolves the store. Do not consolidate them.

Suites needing extra environment hygiene (``tests/config``, ``tests/examples``) keep their own
``home`` for the same reason: the cleanup is specific to what they exercise, and hoisting it here
would leak one suite's concerns into every other.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh, isolated ``RAYSPEC_HOME`` exported in the environment.

    A test package that needs different behaviour overrides this by defining its own ``home``;
    the nearest definition wins.
    """
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(path))
    return path

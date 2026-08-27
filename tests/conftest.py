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

import os
import re
from pathlib import Path

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


#: Ambient variables that change what a command DOES or how its output is RENDERED, and which
#: therefore decide test outcomes by where the suite happens to run. Each is removed for every
#: test; a test that wants one sets it itself.
AMBIENT_ENV = (
    # `--locked` and other defaults tighten under CI.
    "CI",
    # Rich reports `is_terminal` true when this is set, whatever the stream really is, so Typer's
    # help formatter emits colour and a flag name arrives split across escape sequences
    # (`\x1b[1;36m-\x1b[0m\x1b[1;36m-probe\x1b[0m`). This is what turned every matrix cell red on
    # the first run this repository's CI ever completed, while the suite was green on the machine
    # that wrote it.
    "GITHUB_ACTIONS",
    # The colour-control family, for the same reason from the other direction. `astral-sh/setup-uv`
    # exports FORCE_COLOR, so this is not hypothetical either.
    "FORCE_COLOR",
    "NO_COLOR",
    "CLICOLOR",
    "CLICOLOR_FORCE",
)

#: The RAYSPEC_* a rayspec run exports INTO every step it launches (`engine/context.py`'s step
#: environment). When rayspec is run on rayspec — a `rayspec test` / `pytest` step during a
#: dogfood — these land in the project suite's own process and silently change its outcome: a
#: `RAYSPEC_POLICY` the developer never chose, a phantom `RAYSPEC_INPUT_*`, a stale run id. The
#: dogfood (PRD-09, finding F4) proved the leak. Scrubbed for every test so the suite's outcome
#: never depends on being run from inside another rayspec run. NOT scrubbed: `RAYSPEC_LIVE`,
#: `RAYSPEC_UPDATE_GOLDEN`, `RAYSPEC_PROP_*` — those are read at import to select what a run *is*,
#: and a test that flips them does so on purpose.
NESTED_ENV = (
    "RAYSPEC_HOME",
    "RAYSPEC_RUN_ID",
    "RAYSPEC_WORKDIR",
    "RAYSPEC_ARTIFACTS_DIR",
    "RAYSPEC_STATE_DIR",
    "RAYSPEC_CONTEXT",
    "RAYSPEC_STEP_PATH",
    "RAYSPEC_POLICY",
    "RAYSPEC_DEBUG",
    "RAYSPEC_ACTOR",
    "RAYSPEC_AUDIT_LOG",
    "RAYSPEC_PUSH_BRANCH",
)

#: Compiled matchers for the two RAYSPEC_* families a run exports one-per-value: the inputs
#: (`RAYSPEC_INPUT_<NAME>`) and the redaction-value slots (`RAYSPEC_V<n>`).
_NESTED_ENV_FAMILIES = (re.compile(r"^RAYSPEC_INPUT_"), re.compile(r"^RAYSPEC_V\d+$"))


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable in :data:`AMBIENT_ENV` and :data:`NESTED_ENV` (and the RAYSPEC_*
    input/value families) for every test.

    A suite whose outcome depends on what the surrounding shell happens to export is not a suite.
    This started as `CI` alone, which is the shape of the mistake rather than the fix: naming one
    variable reads as a rule and behaves as an example. `TERM`, `COLUMNS` and `LINES` are
    deliberately NOT removed — terminal width is something several tests assert about on purpose,
    so a test that cares sets it explicitly. `RAYSPEC_HOME` is scrubbed here and re-set by the
    ``home`` fixture (and the project fixtures that need one), so a test that needs a home asks
    for one rather than inheriting the developer's real `~/.rayspec`.
    """
    for name in (*AMBIENT_ENV, *NESTED_ENV):
        monkeypatch.delenv(name, raising=False)
    for key in list(os.environ):
        if any(pattern.match(key) for pattern in _NESTED_ENV_FAMILIES):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def ambient_env() -> tuple[str, ...]:
    """:data:`AMBIENT_ENV`, for the check that the autouse fixture above still removes them."""
    return AMBIENT_ENV


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


#: The skip reason the live gate stamps on a `live`-marked test when RAYSPEC_LIVE is unset — a
#: distinctive string so a meta-test can tell the CENTRAL gate apart from any per-test skip.
LIVE_GATE_REASON = "needs RAYSPEC_LIVE=1 (the `live` marker hits a real provider)"


def live_is_enabled() -> bool:
    """Whether `live`-marked tests run: RAYSPEC_LIVE is set to a truthy value (`1`)."""
    return os.environ.get("RAYSPEC_LIVE") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Enforce the `live` marker from one place: every `live` item is SKIPPED unless
    RAYSPEC_LIVE=1. This is not a `-m` deselection (a bare `pytest` still collects them, which
    `tests/docs/test_community_health.py` pins) — it is a collection-time skip, so a NEW live
    test needs no per-file `skipif` to stay out of the default run, and a hang inside a live test
    is diagnosable (see `faulthandler_timeout` in pyproject) rather than silent."""
    if live_is_enabled():
        return
    skip = pytest.mark.skip(reason=LIVE_GATE_REASON)
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)

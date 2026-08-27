# SPDX-License-Identifier: Apache-2.0
"""The suite's outcome must not depend on what the surrounding shell exports.

Boundary: runs pytest in a child process with one ambient variable exported, and asserts the
targets still pass. Nothing here inspects rayspec itself.

This exists because the first run GitHub Actions ever completed on this repository turned every
matrix cell red while the same commit was green on the machine that wrote it. Rich reports
``is_terminal`` true whenever ``GITHUB_ACTIONS`` is set, so Typer's help formatter emitted colour
and a flag name arrived split across escape sequences -- ``--probe`` rendered as
``ESC[1;36m-ESC[0mESC[1;36m-probeESC[0m``, and a plain ``in`` check stopped matching text a reader
sees perfectly well.

The list below is deliberately NOT imported from ``tests/conftest.py``. Deriving the cases from
the list under test makes the check circular: removing a name from the fixture would remove it
from the coverage too, and the guard would go on passing while protecting less. That is the exact
shape of the mistake this file is here to catch, so it must not be the shape of the file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Variables proven to change the outcome, each with the reason. Independent of the fixture's list.
DANGEROUS = (
    "GITHUB_ACTIONS",  # Rich reports is_terminal true; Typer's help formatter then emits colour
    "FORCE_COLOR",  # the same, from the other direction; astral-sh/setup-uv exports it
    "CLICOLOR_FORCE",  # click/rich honour it identically to FORCE_COLOR
    "CI",  # `--locked` and other defaults tighten under it
)

#: Targets that DID fail under `GITHUB_ACTIONS`, so this cannot pass vacuously.
TARGETS = (
    "tests/cli/test_doctor_cmd.py::test_doctor_help_lists_options",
    "tests/plugins/test_cli_plugins.py::test_plugin_may_not_shadow_a_builtin_command",
)


@pytest.mark.parametrize("name", DANGEROUS)
def test_an_ambient_variable_does_not_change_the_outcome(name: str) -> None:
    """With ``name`` exported, the targets still pass -- i.e. the autouse fixture removed it."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *TARGETS],
        cwd=REPO,
        # The REAL environment plus the variable. A minimal env is not a smaller version of this
        # check but a different one: stripping TERM and the rest removes the conditions the
        # variable interacts with, and the child then passes whatever the fixture did.
        env={**os.environ, name: "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"exporting {name}=1 changed the outcome of {list(TARGETS)}; the autouse fixture in "
        f"tests/conftest.py must remove it.\n{proc.stdout[-2500:]}"
    )


def test_the_fixture_covers_every_variable_known_to_matter(ambient_env: tuple[str, ...]) -> None:
    """The fixture's list may grow past this one; it may not shrink below it."""
    uncovered = [name for name in DANGEROUS if name not in ambient_env]
    assert not uncovered, (
        f"tests/conftest.py's AMBIENT_ENV no longer removes {uncovered}, which the check above "
        "proves change the suite's outcome"
    )


# --------------------------------------------------------------------------------------------------
# The RAYSPEC_* a SURROUNDING rayspec run exports into its steps (PRD-09 dogfood, F4).
# --------------------------------------------------------------------------------------------------

#: Variables a rayspec run exports into every shell/python/prompt step it launches. When rayspec
#: is run ON rayspec (a `rayspec test`/`pytest` step), these land in the project suite's own
#: environment and silently change its outcome — a policy the developer never set, a phantom
#: input. Each is proven below to change a target's outcome, independent of the fixture's list.
NESTED_DANGEROUS = (
    "RAYSPEC_INPUT_ISSUE",  # a phantom input value — `plan`/`run` read it as if the user passed it
    "RAYSPEC_POLICY",  # a policy file the developer never chose, applied to every run under test
)

#: Targets that DID fail under an ambient RAYSPEC_INPUT_* / RAYSPEC_POLICY (they carried their own
#: ad-hoc scrubs until the autouse fixture subsumed them), so this cannot pass vacuously.
NESTED_TARGETS = (
    "tests/cli/test_plan_inputs.py",
    "tests/engine/test_cli_run.py",
    "tests/policy/test_approval_classes_end_to_end.py",
)


@pytest.mark.parametrize("name", NESTED_DANGEROUS)
def test_a_nested_run_variable_does_not_leak_into_the_suite(name: str) -> None:
    """With ``name`` exported the way a surrounding `rayspec run` would export it, the targets
    still pass — i.e. the autouse fixture removed it. ``RAYSPEC_POLICY`` points at a real but
    empty file so a failure is 'the wrong policy applied', not 'no such file'."""
    value = "issue-from-a-surrounding-run"
    if name == "RAYSPEC_POLICY":
        # a syntactically valid but foreign policy: if it leaks, a run under test sees a ceiling
        # it never set; if the scrub works, it is invisible.
        policy = REPO / ".rayspec" / "policy.yaml"
        value = str(policy) if policy.is_file() else "/dev/null"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *NESTED_TARGETS],
        cwd=REPO,
        env={**os.environ, name: value},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"exporting {name}={value!r} (as a surrounding rayspec run would) changed the outcome of "
        f"{list(NESTED_TARGETS)}; the autouse fixture in tests/conftest.py must remove it.\n"
        f"{proc.stdout[-2500:]}"
    )

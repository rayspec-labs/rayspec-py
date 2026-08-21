# SPDX-License-Identifier: Apache-2.0
"""``rayspec.testing`` — the declarative workflow test harness behind ``rayspec test``.

Boundary: everything needed to run a workflow's cases offline — the case format
(:mod:`~rayspec.testing.spec`), the engine-level executor (:mod:`~rayspec.testing.runner`) and
the failure/JUnit/JSON reporting (:mod:`~rayspec.testing.report`). It ships in the wheel so a
project can run its own suites in CI (``rayspec test``) or import the pieces from pytest.

Nothing in here talks to a real provider: every case runs as a dry run against the stub provider,
so a suite needs no credentials and no network.
"""

from __future__ import annotations

from rayspec.testing.report import (
    CaseResult,
    Failure,
    junit_xml,
    results_json,
    summary_line,
)
from rayspec.testing.runner import run_case
from rayspec.testing.spec import (
    Case,
    CaseFileError,
    CaseLocation,
    Expect,
    StepExpect,
    Suite,
    discover_suites,
    load_cases,
    load_checks,
)

__all__ = [
    "Case",
    "CaseFileError",
    "CaseLocation",
    "CaseResult",
    "Expect",
    "Failure",
    "StepExpect",
    "Suite",
    "discover_suites",
    "junit_xml",
    "load_cases",
    "load_checks",
    "results_json",
    "run_case",
    "summary_line",
]

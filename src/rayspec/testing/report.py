# SPDX-License-Identifier: Apache-2.0
"""What a case run produced and how it is reported: failures, the text report, JUnit and JSON.

Module boundary: pure formatting over :class:`CaseResult` values — no IO, no engine. A failure is
rendered in the house four-line shape (the same one
:class:`~rayspec.errors.UnsupportedFeatureError` uses), so a developer can act on it without
opening the run directory::

    expect.status: run status is 'failed', expected 'succeeded'
      reason: step 'build' failed: stub failure
      fix: update expect.status, or fix the stubs so the run succeeds
      at examples/fix_issue/checks.yaml:12
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rayspec.events.model import RunEvent


@dataclass(frozen=True)
class Failure:
    """One unmet expectation.

    ``field`` is the expectation's path in the case file (``expect.steps.review.status``),
    ``summary`` the claim, ``detail`` the observed context and ``fix`` what to do about it.
    """

    field: str
    summary: str
    detail: str = ""
    fix: str = ""
    location: str = ""

    def lines(self) -> list[str]:
        """The four report lines (claim, detail, fix, location)."""
        return [
            f"{self.field}: {self.summary}",
            f"  {self.detail}",
            f"  fix: {self.fix}",
            f"  at {self.location}",
        ]

    def __str__(self) -> str:
        return "\n".join(self.lines())


@dataclass
class CaseResult:
    """The outcome of one case: its failures plus where the run it drove ended up."""

    suite: str
    case: str
    failures: list[Failure] = field(default_factory=list)
    #: final run status, ``"not run"`` for ``run: false`` / a validate-only case
    status: str = "not run"
    run_id: str | None = None
    run_dir: Path | None = None
    duration_s: float = 0.0
    events: list[RunEvent] = field(default_factory=list, repr=False)

    @property
    def ok(self) -> bool:
        """Whether every expectation held."""
        return not self.failures

    @property
    def name(self) -> str:
        """``<suite>:<case>`` — how a case is addressed on the command line."""
        return f"{self.suite}:{self.case}"

    def fail(
        self, field_: str, summary: str, *, detail: str = "", fix: str = "", location: str = ""
    ) -> None:
        """Record one unmet expectation."""
        self.failures.append(Failure(field_, summary, detail, fix, location))

    def report(self) -> str:
        """One headline plus the four-line block of every failure (indented)."""
        head = f"[{self.suite}:{self.case}] " + ("ok" if self.ok else "FAILED")
        lines = [head]
        for failure in self.failures:
            lines.extend("  " + line for line in failure.lines())
        if not self.ok and self.run_id:
            lines.append(f"  run {self.run_id} · {self.run_dir}")
        return "\n".join(lines)


def summary_line(results: list[CaseResult], elapsed_s: float) -> str:
    """``25 passed in 3.4s`` / ``24 passed, 1 failed in 3.4s``."""
    failed = [r for r in results if not r.ok]
    parts = [f"{len(results) - len(failed)} passed"]
    if failed:
        parts.append(f"{len(failed)} failed")
    return f"{', '.join(parts)} in {elapsed_s:.1f}s"


def results_json(results: list[CaseResult], *, elapsed_s: float = 0.0) -> dict[str, Any]:
    """``rayspec test --json``: one object with a ``cases`` array and the totals."""
    return {
        "passed": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "duration_s": round(elapsed_s, 3),
        "cases": [
            {
                "suite": r.suite,
                "case": r.case,
                "ok": r.ok,
                "status": r.status,
                "run_id": r.run_id,
                "run_dir": str(r.run_dir) if r.run_dir else None,
                "duration_s": round(r.duration_s, 3),
                "failures": [
                    {
                        "field": f.field,
                        "summary": f.summary,
                        "detail": f.detail,
                        "fix": f.fix,
                        "location": f.location,
                    }
                    for f in r.failures
                ],
            }
            for r in results
        ],
    }


def junit_xml(results: list[CaseResult], *, name: str = "rayspec", elapsed_s: float = 0.0) -> str:
    """A JUnit ``<testsuites>`` document (one ``<testsuite>`` per rayspec suite).

    Each case is a ``<testcase>`` whose ``classname`` is the suite; a failed case carries one
    ``<failure>`` element holding every four-line block, so CI shows the same text the terminal
    does.
    """
    root = ET.Element(
        "testsuites",
        name=name,
        tests=str(len(results)),
        failures=str(sum(1 for r in results if not r.ok)),
        time=f"{elapsed_s:.3f}",
    )
    for suite in dict.fromkeys(r.suite for r in results):
        cases = [r for r in results if r.suite == suite]
        element = ET.SubElement(
            root,
            "testsuite",
            name=suite,
            tests=str(len(cases)),
            failures=str(sum(1 for r in cases if not r.ok)),
            time=f"{sum(r.duration_s for r in cases):.3f}",
        )
        for result in cases:
            case = ET.SubElement(
                element,
                "testcase",
                name=result.case,
                classname=suite,
                time=f"{result.duration_s:.3f}",
            )
            if not result.ok:
                first = result.failures[0]
                failure = ET.SubElement(
                    case, "failure", message=f"{first.field}: {first.summary}", type="expectation"
                )
                failure.text = "\n\n".join(str(f) for f in result.failures)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def junit_error_xml(message: str, *, detail: str = "", name: str = "rayspec") -> str:
    """A JUnit document for a suite that could not be run at all (a usage exit).

    ``rayspec test --junit FILE`` promises the file exists whether the suite failed or could not
    start, so a CI job with a publish step shows *why* nothing ran instead of "file not found".
    The document holds one erroring ``<testcase>``; ``errors="1"`` (not ``failures``) is how JUnit
    distinguishes "the harness broke" from "an assertion failed".
    """
    root = ET.Element("testsuites", name=name, tests="1", failures="0", errors="1", time="0.000")
    suite = ET.SubElement(
        root, "testsuite", name=name, tests="1", failures="0", errors="1", time="0.000"
    )
    case = ET.SubElement(suite, "testcase", name="rayspec test", classname=name, time="0.000")
    error = ET.SubElement(case, "error", message=message, type="usage")
    error.text = f"{message}\n{detail}".strip()
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def json_line(payload: Any) -> str:
    """Compact JSON for one machine-readable line."""
    return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = [
    "CaseResult",
    "Failure",
    "json_line",
    "junit_error_xml",
    "junit_xml",
    "results_json",
    "summary_line",
]

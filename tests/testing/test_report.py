"""The failure format, the JUnit document and the ``--json`` payload."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from rayspec.testing.report import (
    CaseResult,
    Failure,
    junit_xml,
    results_json,
    summary_line,
)


def failing() -> CaseResult:
    result = CaseResult("fix_issue", "happy", status="failed", run_id="r1", run_dir=Path("/x"))
    result.fail(
        "expect.status",
        "run status is 'failed', expected 'succeeded'",
        detail="reason: step 'build' failed",
        fix="update expect.status",
        location="examples/fix_issue/checks.yaml:12",
    )
    return result


def test_a_failure_is_four_lines_in_the_house_format() -> None:
    (failure,) = failing().failures
    lines = failure.lines()
    assert lines == [
        "expect.status: run status is 'failed', expected 'succeeded'",
        "  reason: step 'build' failed",
        "  fix: update expect.status",
        "  at examples/fix_issue/checks.yaml:12",
    ]
    assert str(failure) == "\n".join(lines)


def test_the_report_names_the_case_and_the_run() -> None:
    report = failing().report()
    assert report.splitlines()[0] == "[fix_issue:happy] FAILED"
    assert "examples/fix_issue/checks.yaml:12" in report
    assert "r1" in report


def test_a_passing_case_reports_one_line() -> None:
    result = CaseResult("s", "c")
    assert result.ok
    assert result.report() == "[s:c] ok"
    assert result.name == "s:c"


def test_summary_line() -> None:
    assert summary_line([CaseResult("s", "a")], 1.24) == "1 passed in 1.2s"
    assert summary_line([CaseResult("s", "a"), failing()], 2.0) == "1 passed, 1 failed in 2.0s"


def test_results_json_shape() -> None:
    payload = results_json([CaseResult("s", "a"), failing()], elapsed_s=1.5)
    assert payload["passed"] == 1 and payload["failed"] == 1
    assert [c["case"] for c in payload["cases"]] == ["a", "happy"]
    failure = payload["cases"][1]["failures"][0]
    assert set(failure) == {"field", "summary", "detail", "fix", "location"}
    json.dumps(payload)  # must be serialisable


def test_junit_xml_parses_and_carries_the_failure_text(tmp_path: Path) -> None:
    xml = junit_xml([CaseResult("s", "a"), failing()], elapsed_s=1.5)
    path = tmp_path / "out.xml"
    path.write_text(xml, encoding="utf-8")
    tree = ET.parse(path)
    root = tree.getroot()
    assert root.tag == "testsuites"
    assert root.get("tests") == "2" and root.get("failures") == "1"
    suites = {el.get("name"): el for el in root}
    assert set(suites) == {"s", "fix_issue"}
    (case,) = list(suites["fix_issue"])
    assert case.get("name") == "happy" and case.get("classname") == "fix_issue"
    (failure,) = list(case)
    assert failure.tag == "failure"
    assert "expect.status" in (failure.get("message") or "")
    assert "checks.yaml:12" in (failure.text or "")


def test_junit_escapes_hostile_text(tmp_path: Path) -> None:
    """Run data (an output, a reason) must never break the document."""
    result = CaseResult("s<", "c&")
    result.fail("expect.status", "<bad> & 'quotes'", detail="]]>", location="f.yaml:1")
    path = tmp_path / "out.xml"
    path.write_text(junit_xml([result]), encoding="utf-8")
    root = ET.parse(path).getroot()
    assert root[0].get("name") == "s<"
    assert "<bad>" in (root[0][0][0].get("message") or "")


def test_failure_defaults_are_printable() -> None:
    assert Failure("f", "s").lines()[3] == "  at "

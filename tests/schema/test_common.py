from __future__ import annotations

import pytest

from rayspec.schema.common import IDENT_RE, RESERVED_ROOTS, RunStatus, StepStatus, parse_duration


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("90s", 90.0),
        ("10m", 600.0),
        ("1h30m", 5400.0),
        ("500ms", 0.5),
        (30, 30.0),
        (2.5, 2.5),
        ("1h", 3600.0),
    ],
)
def test_parse_duration_accepts_seconds_and_suffixed_strings(raw, seconds):
    assert parse_duration(raw) == seconds


@pytest.mark.parametrize("raw", ["abc", "", "10x", True, None, "-5s"])
def test_parse_duration_rejects_garbage(raw):
    with pytest.raises(ValueError, match="duration"):
        parse_duration(raw)


def test_identifier_regex_is_snake_case_only():
    assert IDENT_RE.fullmatch("fix_issue")
    assert IDENT_RE.fullmatch("a1")
    assert not IDENT_RE.fullmatch("Fix")
    assert not IDENT_RE.fullmatch("fix-issue")
    assert not IDENT_RE.fullmatch("1fix")
    assert not IDENT_RE.fullmatch("_x")


def test_reserved_roots_cover_template_context_names():
    assert {"inputs", "steps", "run", "project", "env", "iteration", "each"} <= RESERVED_ROOTS
    assert "item" not in RESERVED_ROOTS  # the default `as:` name must stay writable


def test_status_enums():
    assert StepStatus.SUCCEEDED == "succeeded"
    assert {s.value for s in StepStatus} == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "skipped",
        "interrupted",
        "paused",
        "rejected",
    }
    assert {s.value for s in RunStatus} == {
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "paused",
        "interrupted",
    }
    assert StepStatus.SUCCEEDED.is_terminal and not StepStatus.RUNNING.is_terminal

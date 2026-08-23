from __future__ import annotations

import pytest

from rayspec.schema.common import (
    IDENT_RE,
    MAX_IDENT_LEN,
    RESERVED_ROOTS,
    RunStatus,
    StepStatus,
    parse_duration,
    validate_identifier,
    validate_name,
)


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


def test_identifiers_are_bounded_and_the_message_names_the_limit():
    """An identifier becomes a file name, so it has a length rule like every other one.

    Without the bound the refusal came from the filesystem instead — `[Errno 63] File name too
    long`, a bare errno naming no rule that rayspec could have stated up front.
    """
    assert validate_identifier("a" * MAX_IDENT_LEN) == "a" * MAX_IDENT_LEN
    assert validate_name("a" * MAX_IDENT_LEN) == "a" * MAX_IDENT_LEN
    for check, kind in ((validate_identifier, "identifier"), (validate_name, "name")):
        with pytest.raises(ValueError) as exc:
            check("a" * (MAX_IDENT_LEN + 1))
        message = str(exc.value)
        assert f"invalid {kind}" in message
        assert f"at most {MAX_IDENT_LEN} characters" in message  # the limit, named
        assert f"is {MAX_IDENT_LEN + 1}" in message  # and what was given
        # the mistake must not bury the rule: the whole 129-character value is not echoed
        assert "a" * (MAX_IDENT_LEN + 1) not in message


def test_the_identifier_bound_clears_every_name_rayspec_builds_from_one():
    """The bound is only worth having if a name AT the limit survives every path rayspec makes
    of it. ``NAME_MAX`` is 255 bytes; the longest thing rayspec appends to an identifier is the
    git ref lock of a worktree branch (``<name>-<short run id>.lock``)."""
    longest_suffix = len("-20260823-1234-abcd") + len(".lock")
    assert MAX_IDENT_LEN + longest_suffix <= 255

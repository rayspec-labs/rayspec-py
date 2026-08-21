"""The class rules themselves: what they permit, and how a refusal reads."""

from __future__ import annotations

from dataclasses import dataclass

from rayspec.engine.approval_classes import (
    BY_APPROVE_CLASS,
    BY_DRY_RUN,
    BY_YES,
    ApprovalClasses,
    ClassRules,
    automatic_by,
    out_of_band_refused,
    rules_from_policy,
    waiver_refused,
)

LOCKED = ClassRules(allow_yes=False)
TTY_ONLY = ClassRules(allow_yes=False, require_tty=True)


def classes(**rules: ClassRules) -> ApprovalClasses:
    return ApprovalClasses(rules=dict(rules))


def test_an_unnamed_class_keeps_the_permissive_default() -> None:
    c = classes(release=LOCKED)
    assert c.rules_for(None) == ClassRules()
    assert c.may_approve_automatically(None) is True
    assert c.may_decide_out_of_band(None) is True


def test_a_class_without_rules_keeps_the_permissive_default() -> None:
    assert classes().rules_for("release") == ClassRules()
    assert classes().may_approve_automatically("release") is True


def test_allow_yes_false_forbids_every_automatic_approval() -> None:
    c = classes(release=LOCKED)
    assert c.may_approve_automatically("release") is False
    # a human deciding this one gate by name is not an automatic approval
    assert c.may_decide_out_of_band("release") is True
    assert c.may_prompt("release") is True


def test_require_tty_also_forbids_deciding_out_of_band() -> None:
    c = classes(release=TTY_ONLY)
    assert c.may_decide_out_of_band("release") is False


def test_require_tty_forbids_a_prompt_that_is_not_the_terminal() -> None:
    c = ApprovalClasses(rules={"release": TTY_ONLY}, terminal_prompt=False)
    assert c.may_prompt("release") is False
    assert c.may_prompt("routine") is True


def test_pre_approved_classes_only_match_a_named_class() -> None:
    c = ApprovalClasses(pre_approved=frozenset({"release"}))
    assert automatic_by(c, "release", yes=False, dry_run=False) == BY_APPROVE_CLASS
    assert automatic_by(c, "routine", yes=False, dry_run=False) is None
    assert automatic_by(c, None, yes=False, dry_run=False) is None


def test_yes_outranks_dry_run_and_pre_approval() -> None:
    c = ApprovalClasses(pre_approved=frozenset({"release"}))
    assert automatic_by(c, "release", yes=True, dry_run=True) == BY_YES
    assert automatic_by(c, "release", yes=False, dry_run=True) == BY_DRY_RUN


def test_the_refusal_names_the_rule_and_what_to_do_instead() -> None:
    message = waiver_refused("release", LOCKED, waiver=BY_YES, step_path="ship")
    assert "ship" in message
    assert "--yes" in message
    assert "'release'" in message
    assert "allow_yes: false" in message
    assert "rayspec approve" in message


def test_a_require_tty_refusal_points_at_the_terminal_not_at_approve() -> None:
    message = waiver_refused("release", TTY_ONLY, waiver=BY_YES, step_path="ship")
    assert "require_tty: true" in message
    assert "rayspec resume" in message
    assert "rayspec approve" not in message
    out_of_band = out_of_band_refused("release", step_path="ship")
    assert "rayspec resume" in out_of_band
    assert "require_tty: true" in out_of_band


@dataclass
class _Entry:
    allow_yes: bool = True
    require_tty: bool = False


@dataclass
class _Policy:
    classes: dict[str, object]


def test_policy_classes_are_read_through_the_accessor() -> None:
    loaded = rules_from_policy(
        _Policy(classes={"release": _Entry(allow_yes=False), "chore": {"require_tty": True}})
    )
    assert loaded == {
        "release": ClassRules(allow_yes=False),
        "chore": ClassRules(require_tty=True),
    }


def test_no_policy_means_no_rules() -> None:
    assert rules_from_policy(None) == {}
    assert rules_from_policy(_Policy(classes={})) == {}

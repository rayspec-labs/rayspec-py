"""``approve:`` gains two fields: ``class:`` (which rules govern it) and ``auto_if:``."""

from __future__ import annotations

import pytest

from rayspec.schema import ApproveStep, SchemaError, parse_step


def approve(**spec: object) -> ApproveStep:
    step = parse_step({"id": "gate", "approve": {"message": "ship it?", **spec}})
    assert isinstance(step, ApproveStep)
    return step


def test_class_and_auto_if_parse() -> None:
    step = approve(**{"class": "release", "auto_if": "steps.tests.output.failures == 0"})
    assert step.approve.class_ == "release"
    assert step.approve.auto_if == "steps.tests.output.failures == 0"


def test_class_and_auto_if_default_to_none() -> None:
    step = approve()
    assert step.approve.class_ is None
    assert step.approve.auto_if is None


def test_string_shorthand_still_works() -> None:
    step = parse_step({"id": "gate", "approve": "ship it?"})
    assert isinstance(step, ApproveStep)
    assert step.approve.message == "ship it?"
    assert step.approve.class_ is None


def test_class_must_be_an_identifier() -> None:
    with pytest.raises(SchemaError) as excinfo:
        approve(**{"class": "Release Now"})
    assert "invalid name 'Release Now'" in str(excinfo.value)


def test_auto_if_must_not_be_empty() -> None:
    with pytest.raises(SchemaError):
        approve(auto_if="   ")


def test_unknown_approve_key_is_still_refused() -> None:
    with pytest.raises(SchemaError):
        approve(klass="release")


def test_class_is_serialised_under_its_yaml_name() -> None:
    step = approve(**{"class": "release"})
    assert step.approve.model_dump(by_alias=True, exclude_none=True)["class"] == "release"

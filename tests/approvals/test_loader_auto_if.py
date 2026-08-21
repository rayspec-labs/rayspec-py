"""``approve.auto_if`` is an expression field and is checked at LOAD time like ``when:``."""

from __future__ import annotations

import pytest

from rayspec.schema import SchemaError

from .conftest import Tree

HEAD = """
rayspec: 1
name: wf
inputs:
  token: { type: string, secret: true }
steps:
  - id: tests
    shell: pytest -q
  - id: gate
    needs: [tests]
    approve:
      message: ship it?
"""


def gate(tree: Tree, *, auto_if: str) -> None:
    tree.workflow("wf", HEAD + f"      auto_if: {auto_if}\n")


def test_a_valid_expression_passes(tree: Tree) -> None:
    gate(tree, auto_if="steps.tests.ok")
    assert tree.check("wf").errors == []


def test_braces_are_refused(tree: Tree) -> None:
    gate(tree, auto_if="'{{ steps.tests.ok }}'")
    errors = tree.check("wf").errors
    assert any("bare Jinja expression" in e for e in errors), errors
    assert any("steps.gate.approve.auto_if" in e for e in errors), errors


def test_an_unparseable_expression_is_an_error(tree: Tree) -> None:
    gate(tree, auto_if="'steps.tests.ok =='")
    errors = tree.check("wf").errors
    assert any("steps.gate.approve.auto_if" in e for e in errors), errors


def test_an_unknown_step_reference_is_an_error(tree: Tree) -> None:
    gate(tree, auto_if="steps.nope.ok")
    errors = tree.check("wf").errors
    assert any("unknown step 'nope'" in e for e in errors), errors


def test_a_step_that_is_not_an_ancestor_is_an_error(tree: Tree) -> None:
    tree.workflow(
        "wf2",
        """
        rayspec: 1
        name: wf2
        steps:
          - id: tests
            shell: pytest -q
          - id: gate
            approve:
              message: ship it?
              auto_if: steps.tests.ok
        """,
    )
    errors = tree.check("wf2").errors
    assert any("steps.gate.approve.auto_if" in e for e in errors), errors


def test_a_secret_input_may_not_be_named(tree: Tree) -> None:
    gate(tree, auto_if="inputs.token != ''")
    errors = tree.check("wf").errors
    assert any("declared secret: true" in e for e in errors), errors
    assert any("steps.gate.approve.auto_if" in e for e in errors), errors


def test_an_empty_expression_is_a_schema_error(tree: Tree) -> None:
    gate(tree, auto_if="' '")
    with pytest.raises(SchemaError) as excinfo:
        tree.load("wf")
    assert "auto_if must not be empty" in str(excinfo.value)

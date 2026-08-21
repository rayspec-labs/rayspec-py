# SPDX-License-Identifier: Apache-2.0
"""One problem per schema mistake of a document, each with its own file:line and hint."""

from __future__ import annotations

import pytest

from rayspec.loader.yaml import load_yaml_with_lines
from rayspec.schema import SchemaError, parse_workflow
from rayspec.schema.errors import MAX_PROBLEMS, SchemaProblem, expand_schema_errors, line_of

THREE_MISTAKES = """\
rayspec: 1
name: broken
descriptionn: oops
steps:
  - id: one
    shell: echo hi
    timeoutt: 5m
  - id: Two
    shell: echo bye
"""


def _expand(text: str, *, source: str = "wf.yaml") -> SchemaError:
    data, lines = load_yaml_with_lines(text, source=source)
    with pytest.raises(SchemaError) as excinfo:
        parse_workflow(data, source=source)
    return expand_schema_errors(excinfo.value, data, parse_workflow, lines=lines)


def test_a_root_level_unknown_key_no_longer_masks_the_rest() -> None:
    error = _expand(THREE_MISTAKES)
    messages = [p.message for p in error.problems]
    assert len(error.problems) == 3, messages
    assert any("descriptionn" in m for m in messages)
    assert any("timeoutt" in m for m in messages)
    assert any("invalid identifier 'Two'" in m for m in messages)


def test_every_problem_carries_its_own_file_and_line() -> None:
    error = _expand(THREE_MISTAKES)
    by_line = {p.line: p for p in error.problems}
    assert set(by_line) == {3, 7, 8}, [(p.line, p.message) for p in error.problems]
    assert all(p.source == "wf.yaml" for p in error.problems)
    assert by_line[3].location == "wf.yaml:3"
    assert by_line[3].rendered().startswith("wf.yaml:3: ")


def test_did_you_mean_is_kept_as_a_hint() -> None:
    error = _expand(THREE_MISTAKES)
    problem = next(p for p in error.problems if "descriptionn" in p.message)
    assert problem.hint == "did you mean 'description'?"
    assert problem.field == "descriptionn"


def test_problems_are_bounded_and_never_loop_forever() -> None:
    text = "rayspec: 1\nname: wide\nsteps: []\n" + "".join(f"k{i}: 1\n" for i in range(40))
    error = _expand(text)
    assert 1 < len(error.problems) <= 50
    assert all(p.line is not None for p in error.problems)


def test_a_clean_document_produces_no_problems() -> None:
    data, _lines = load_yaml_with_lines(
        "rayspec: 1\nname: ok\nsteps:\n  - id: a\n    shell: echo hi\n", source="ok.yaml"
    )
    assert parse_workflow(data, source="ok.yaml").name == "ok"


def test_problem_rendering_degrades_gracefully() -> None:
    problem = SchemaProblem(field="steps[0].join", message="bad")
    assert problem.location is None
    assert problem.rendered() == "steps[0].join: bad"


HOSTILE_KEY = """\
rayspec: 1
name: wf
defaults:
  "x'": 1
  max_parallel: 0
steps: []
"""


def test_a_key_whose_name_contains_a_quote_does_not_swallow_its_siblings() -> None:
    """Pruning the whole parent mapping hid the genuine `max_parallel: 0` error."""
    error = _expand(HOSTILE_KEY)
    fields = [(p.field, p.message) for p in error.problems]
    assert any(p.field == "defaults.x'" for p in error.problems), fields
    assert any(p.field == "defaults.max_parallel" for p in error.problems), fields


def test_a_quoted_key_is_still_reported_under_its_own_name_and_line() -> None:
    error = _expand('rayspec: 1\nname: wf\n"x\'": 1\nsteps: []\n')
    problem = next(p for p in error.problems if "x'" in p.message)
    assert problem.field == "x'"
    assert problem.line == 3


def test_problems_are_truncated_at_the_cap_with_a_marker() -> None:
    """MAX_PROBLEMS did not bound the FIRST pass, so 80 keys reported 80 problems."""
    text = "rayspec: 1\nname: wide\nsteps: []\n" + "".join(f"k{i}: 1\n" for i in range(120))
    error = _expand(text)
    assert len(error.problems) == MAX_PROBLEMS + 1
    assert len(error.errors) == MAX_PROBLEMS + 1
    assert f"showing the first {MAX_PROBLEMS}" in error.problems[-1].message


def test_str_prints_every_problem_with_its_file_and_line() -> None:
    """The rendering `rayspec plan`/`run`/`resume` forward when they only have the message."""
    error = _expand(THREE_MISTAKES)
    lines = str(error).splitlines()
    assert lines == [p.rendered() for p in error.problems]
    assert lines[0].startswith("wf.yaml:3: descriptionn: ")
    assert [line.split(":")[1] for line in lines] == ["3", "7", "8"]


def test_str_without_problems_keeps_the_source_prefixed_rendering() -> None:
    error = SchemaError(["a: bad", "b: worse"], source="wf.yaml")
    assert str(error).splitlines() == ["wf.yaml: a: bad", "wf.yaml: b: worse"]


def test_a_problem_without_a_known_line_renders_the_source_only() -> None:
    error = SchemaError(
        ["x: bad"],
        source="wf.yaml",
        problems=[SchemaProblem(field="x", message="bad", source="wf.yaml")],
    )
    assert str(error) == "wf.yaml: x: bad"


def test_an_unlocatable_loc_has_no_line_instead_of_line_1() -> None:
    """Review: falling back to the root's line makes a wrong location look authoritative."""
    lines = {(): 1, ("steps",): 4}
    assert line_of(lines, ()) == 1
    assert line_of(lines, ("steps", 0)) == 4
    assert line_of(lines, ("nope",)) is None
    assert line_of(lines, ("nope", "deeper")) is None


def test_errors_has_one_entry_per_offending_key() -> None:
    """Pinned in CONTRACTS: `.errors` is one entry per KEY, not one joined entry per model."""
    error = _expand("rayspec: 1\nname: wf\nsteps: []\nzz: 1\nyy: 2\n")
    assert error.errors == [
        "zz: unknown field 'zz' for workflow",
        "yy: unknown field 'yy' for workflow",
    ]

"""Strict YAML loader behaviours."""

import pytest

from rayspec.errors import LoaderError
from rayspec.loader import load_yaml
from rayspec.loader.yaml import load_yaml_with_lines


def test_only_true_false_spellings_are_booleans():
    data = load_yaml(
        "a: true\nb: True\nc: TRUE\nd: false\ne: False\nf: FALSE\n"
        "g: yes\nh: no\ni: on\nj: off\nk: Yes\nl: OFF\n",
        source="x.yaml",
    )
    assert [data[k] for k in "abc"] == [True, True, True]
    assert [data[k] for k in "def"] == [False, False, False]
    assert [data[k] for k in "ghijkl"] == ["yes", "no", "on", "off", "Yes", "OFF"]


def test_on_key_stays_a_string():
    data = load_yaml("on: push\nno: 3\n", source="x.yaml")
    assert data == {"on": "push", "no": 3}


def test_no_sexagesimal_numbers():
    data = load_yaml("a: 1:30\nb: 1:30:00\nc: 190:20:30.15\nd: 12\ne: 1.5\nf: 0x1f\n", source="x")
    assert data["a"] == "1:30"
    assert data["b"] == "1:30:00"
    assert data["c"] == "190:20:30.15"
    assert data["d"] == 12
    assert data["e"] == 1.5
    assert data["f"] == 31


def test_duplicate_keys_raise_with_location():
    with pytest.raises(LoaderError) as ei:
        load_yaml("a: 1\nb: 2\na: 3\n", source="wf.yaml")
    msg = str(ei.value)
    assert "duplicate" in msg
    assert "'a'" in msg
    assert "wf.yaml:3" in msg


def test_nested_duplicate_keys_raise():
    with pytest.raises(LoaderError) as ei:
        load_yaml("steps:\n  - id: a\n    shell: x\n    id: b\n", source="wf.yaml")
    assert "duplicate" in str(ei.value)
    assert "wf.yaml:4" in str(ei.value)


def test_unquoted_jinja_value_gets_hint():
    with pytest.raises(LoaderError) as ei:
        load_yaml("prompt: {{ steps.x.output }}\n", source="wf.yaml")
    err = ei.value
    assert err.hint is not None
    assert "quote it or use a block scalar" in err.hint
    assert "wf.yaml:1" in str(err)


def test_syntax_error_carries_source_and_line():
    with pytest.raises(LoaderError) as ei:
        load_yaml("a: 1\nb: [1, 2\nc: 3\n", source="wf.yaml")
    assert "wf.yaml:" in str(ei.value)


def test_empty_document_is_none_and_scalars_pass():
    assert load_yaml("", source="e.yaml") is None
    assert load_yaml("42", source="e.yaml") == 42


def test_load_yaml_with_lines_records_paths():
    text = (
        "name: x\nsteps:\n  - id: a\n    shell: echo\n  - id: b\n    agent:\n      max_turns: 3\n"
    )
    data, lines = load_yaml_with_lines(text, source="wf.yaml")
    assert data["steps"][1]["agent"]["max_turns"] == 3
    assert lines[("name",)] == 1
    assert lines[("steps", 0)] == 3
    assert lines[("steps", 1, "agent", "max_turns")] == 7


def test_merge_keys_still_work():
    data = load_yaml("base: &b {x: 1}\nd:\n  <<: *b\n  y: 2\n", source="m.yaml")
    assert data["d"] == {"x": 1, "y": 2}


def test_unsafe_tags_rejected():
    with pytest.raises(LoaderError):
        load_yaml("a: !!python/object/apply:os.system ['echo hi']\n", source="u.yaml")


def test_no_leading_zero_octal():
    data = load_yaml("a: 0123\nb: 0o17\nc: 0\nd: -0\ne: 007\nf: 0_1\n", source="x")
    assert data["a"] == "0123"
    assert data["b"] == 15
    assert data["c"] == 0
    assert data["d"] == 0
    assert data["e"] == "007"
    assert data["f"] == "0_1"


def test_dates_and_timestamps_stay_strings():
    data = load_yaml(
        "d: 2024-01-01\nt: 2024-01-01T10:00:00Z\ns: 2001-12-14 21:59:43.10 -5\n", source="x"
    )
    assert data == {
        "d": "2024-01-01",
        "t": "2024-01-01T10:00:00Z",
        "s": "2001-12-14 21:59:43.10 -5",
    }

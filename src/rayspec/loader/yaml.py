# SPDX-License-Identifier: Apache-2.0
"""Strict YAML loading. Boundary: text → plain Python data (+ optional line map); no schema here.

Differences from ``yaml.SafeLoader``:

* booleans are only ``true/false/True/False/TRUE/FALSE`` (``yes``, ``no``, ``on``, ``off`` stay
  strings — GitHub-Actions style ``on:`` keys survive);
* no sexagesimal numbers (``1:30`` stays a string), no leading-zero octal (``0123`` stays a
  string; ``0o17`` is octal), no timestamps (``2024-01-01`` stays a string);
* duplicate mapping keys are an error;
* every error is a :class:`rayspec.errors.LoaderError` carrying ``<source>:<line>``; a value that
  starts with ``{{`` gets the hint "quote it or use a block scalar".
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from rayspec.errors import LoaderError

#: (path of keys / indices) → 1-based line number of the value (or the element).
LineMap = dict[tuple[str | int, ...], int]

_BOOL_TAG = "tag:yaml.org,2002:bool"
_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"

_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_INT_RE = re.compile(
    r"^(?:[-+]?0b[0-1_]+"
    r"|[-+]?0o[0-7_]+"
    r"|[-+]?(?:0|[1-9][0-9_]*)"
    r"|[-+]?0x[0-9a-fA-F_]+)$"
)
_FLOAT_RE = re.compile(
    r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?"
    r"|\.[0-9_]+(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)"
    r"|\.(?:nan|NaN|NAN))$"
)


def _filtered_resolvers() -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    dropped = {_BOOL_TAG, _INT_TAG, _FLOAT_TAG, _TIMESTAMP_TAG}
    out: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
    for first, entries in yaml.SafeLoader.yaml_implicit_resolvers.items():
        kept = [(tag, rx) for tag, rx in entries if tag not in dropped]
        if kept:
            out[first] = kept
    return out


class StrictLoader(yaml.SafeLoader):
    """``SafeLoader`` with the strict resolvers and duplicate-key detection described above."""

    yaml_implicit_resolvers = _filtered_resolvers()

    def construct_mapping(self, node: Node, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None,
                None,
                f"expected a mapping node, but found {type(node).__name__}",
                node.start_mark,
            )
        self.flatten_mapping(node)
        seen: dict[Any, Any] = {}
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                hash(key)
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found unhashable key ({exc})",
                    key_node.start_mark,
                ) from None
            if key in seen:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key {key!r} (first defined on line {seen[key] + 1})",
                    key_node.start_mark,
                )
            seen[key] = key_node.start_mark.line
        return super().construct_mapping(node, deep=deep)


StrictLoader.add_implicit_resolver(_BOOL_TAG, _BOOL_RE, list("tTfF"))
StrictLoader.add_implicit_resolver(_INT_TAG, _INT_RE, list("-+0123456789"))
StrictLoader.add_implicit_resolver(_FLOAT_TAG, _FLOAT_RE, list("-+0123456789."))


def _line_of(mark: yaml.Mark | None) -> int | None:
    return None if mark is None else mark.line + 1


def _value_starts_with_jinja(line: str) -> bool:
    """True when the YAML value on ``line`` starts with ``{{`` (an unquoted Jinja template)."""
    stripped = line.strip()
    if stripped.startswith("- "):
        stripped = stripped[2:].lstrip()
    if stripped.startswith("{{"):
        return True
    _key, sep, value = stripped.partition(": ")
    return bool(sep) and value.lstrip().startswith("{{")


def _lines_mention_jinja(text: str, *marks: yaml.Mark | None) -> bool:
    lines = text.splitlines()
    return any(
        mark is not None and mark.line < len(lines) and _value_starts_with_jinja(lines[mark.line])
        for mark in marks
    )


def _wrap(exc: yaml.YAMLError, text: str, source: str) -> LoaderError:
    hint: str | None = None
    line: int | None = None
    if isinstance(exc, yaml.MarkedYAMLError):
        line = _line_of(exc.problem_mark) or _line_of(exc.context_mark)
        problem = exc.problem or "invalid YAML"
        if exc.context:
            problem = f"{exc.context}: {problem}"
        if _lines_mention_jinja(text, exc.problem_mark, exc.context_mark):
            hint = (
                "a value starting with '{{' is flow-mapping syntax in YAML; "
                "quote it or use a block scalar (|)"
            )
    else:
        problem = str(exc) or "invalid YAML"
    location = f"{source}:{line}" if line is not None else source
    return LoaderError(f"{location}: {problem}", hint=hint, location=location)


def _build_line_map(node: Node | None, out: LineMap, path: tuple[str | int, ...]) -> None:
    if node is None:
        return
    if isinstance(node, MappingNode):
        for key_node, value_node in node.value:
            if isinstance(key_node, ScalarNode):
                key: str | int = key_node.value
                child = (*path, key)
                out[child] = key_node.start_mark.line + 1
                _build_line_map(value_node, out, child)
    elif isinstance(node, SequenceNode):
        for index, item in enumerate(node.value):
            child = (*path, index)
            out[child] = item.start_mark.line + 1
            _build_line_map(item, out, child)


def load_yaml_with_lines(text: str, *, source: str) -> tuple[Any, LineMap]:
    """Parse ``text`` strictly and also return a ``path → line`` map for error locations."""
    loader = StrictLoader(text)
    try:
        try:
            node = loader.get_single_node()
            data = loader.construct_document(node) if node is not None else None
        finally:
            loader.dispose()
    except yaml.YAMLError as exc:
        raise _wrap(exc, text, source) from None
    lines: LineMap = {}
    _build_line_map(node, lines, ())
    if node is not None:
        lines[()] = node.start_mark.line + 1
    return data, lines


def load_yaml(text: str, *, source: str) -> Any:
    """Parse ``text`` with the strict loader; raise :class:`LoaderError` with ``source:line``."""
    data, _ = load_yaml_with_lines(text, source=source)
    return data


__all__ = ["LineMap", "StrictLoader", "load_yaml", "load_yaml_with_lines"]

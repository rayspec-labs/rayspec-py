# SPDX-License-Identifier: Apache-2.0
"""The declarative ``rayspec test`` case format: models, loading with ``file:line``, discovery.

Module boundary: text (YAML) → :class:`Case` / :class:`Suite` values. Nothing here loads a
workflow, touches the store or runs anything — :mod:`rayspec.testing.runner` does that.

A case is one validate-and-dry-run scenario of a workflow. Two layouts are read, and they parse
into exactly the same :class:`Case`:

* a **suite file** — a mapping with a ``checks:`` (or ``cases:``) list, as shipped next to every
  example (``examples/<name>/checks.yaml``) and for the repo's own workflows
  (``.rayspec/dryrun/checks.yaml``);
* a **case file** — ``.rayspec/tests/<workflow>/<case>.yaml``, one case per document, where the
  directory names the workflow and the file stem names the case.

::

    checks:
      - id: happy                      # optional (default: <workflow>-<n>, or the file stem)
        workflow: fix_issue            # workflow name or path (relative to the project root)
        inputs: {issue: 42}            # the run inputs
        stubs: stubs.yaml              # stub script, relative to this file; optional
        env: {SLACK_WEBHOOK: null}     # process env overrides for the run (null = unset)
        allow_unsupported: false       # treat capability mismatches as warnings
        exec_shell: false              # run shell:/python: steps too (default: skipped)
        validate: ok                   # ok (default) | error — expected `rayspec validate` outcome
        run: true                      # false = load + validate only
        expect:                        # only checked when run is true
          status: succeeded            # final run status
          exit_code: 0
          outputs: {verdict: fix}      # subset of the rendered workflow outputs
          steps:                       # subset of step path -> status, or per-step expectations
            bail: skipped
            review: {status: succeeded, output_regex: "LGTM"}
          reason_contains: "..."       # substring of the run's reason (stop/cancel)

Unknown keys are refused with a did-you-mean suggestion and the ``file:line`` of the offending
key, like the workflow loader; every problem of a file is reported together.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator

from rayspec.errors import RayspecError
from rayspec.loader.yaml import LineMap, load_yaml_with_lines
from rayspec.schema.base import StrictModel
from rayspec.schema.errors import schema_error_from_validation

#: Keys that make a document a suite file rather than a single case.
SUITE_KEYS: tuple[str, ...] = ("checks", "cases")

#: Top-level keys of a stub script (``rayspec.providers.stub.StubScript``). A document in a tests
#: directory whose keys are exactly a non-empty subset of these is a stub script, not a case.
STUB_SCRIPT_KEYS: frozenset[str] = frozenset({"steps", "match", "defaults"})

#: What to tell someone whose ``expect:`` block can never be reached.
UNREACHABLE_EXPECT_HINT = "drop `run: false`, or drop the `expect:` block — it is never evaluated"


class CaseFileError(RayspecError, ValueError):
    """A case file is malformed (bad YAML, unknown key, duplicate id …).

    ``errors`` holds one ``<file>:<line>: <message>`` entry per problem.
    """

    def __init__(self, errors: Sequence[str], *, hint: str | None = None):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors), hint=hint)


# --------------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------------


class StepExpect(StrictModel):
    """What one step of the run must look like when it finished.

    Written either as a bare status (``review: succeeded``) or as a mapping. ``output_json``
    compares the parsed JSON output for equality; use ``model_fields_set`` to tell "expected
    ``null``" from "not expected at all".
    """

    status: str | None = None
    skip_reason: str | None = None
    output_regex: str | None = None
    output_json: Any = None

    @classmethod
    def _what(cls) -> str:
        return "expect.steps entry"


class Expect(StrictModel):
    """What a case's run must produce (every field is optional; unset = not checked)."""

    status: str | None = None
    exit_code: int | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    steps: dict[str, StepExpect] = Field(default_factory=dict)
    reason_contains: str | None = None

    @classmethod
    def _what(cls) -> str:
        return "expect"

    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_steps(cls, value: Any) -> Any:
        """``{path: "succeeded"}`` is shorthand for ``{path: {status: "succeeded"}}``."""
        if isinstance(value, Mapping):
            return {k: ({"status": v} if isinstance(v, str) else v) for k, v in value.items()}
        return value


class Case(StrictModel):
    """One validate-and-dry-run scenario of a workflow."""

    id: str = ""
    workflow: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: stub script; resolved to an absolute path against the case file when loaded from YAML
    stubs: Path | None = None
    env: dict[str, str | None] = Field(default_factory=dict)
    allow_unsupported: bool = False
    exec_shell: bool = False
    #: expected outcome of loading + validating the workflow (``rayspec validate``)
    validate_: Literal["ok", "error"] = Field(default="ok", alias="validate")
    run: bool = True
    expect: Expect = Field(default_factory=Expect)

    @classmethod
    def _what(cls) -> str:
        return "case"


def case_keys() -> frozenset[str]:
    """The top-level YAML keys a case (or a suite file) may carry.

    Discovery uses this to tell a case file from another YAML document that happens to sit in
    ``.rayspec/tests/`` — a stub script, say, which the docs tell you to keep next to the case.
    """
    names = {field.alias or name for name, field in Case.model_fields.items()}
    return frozenset(names | set(SUITE_KEYS))


def unreachable_expect(case: Case) -> str | None:
    """Why ``case.expect`` can never be evaluated, or ``None`` when it can.

    ``run: false`` stops before the engine and ``validate: error`` stops at the validator, so an
    ``expect:`` block next to either is a dead assertion: the case would report ``ok`` no matter
    what it claims. That is refused, not ignored — a harness whose assertions can be silently
    switched off is not a gate.
    """
    if not case.expect.model_fields_set:
        return None
    if not case.run:
        return "run: false"
    if case.validate_ == "error":
        return "validate: error"
    return None


# --------------------------------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseLocation:
    """Where a case lives, so a failure can name the exact expectation's ``file:line``."""

    label: str
    keys: tuple[str | int, ...] = ()
    lines: LineMap = field(default_factory=dict, repr=False, compare=False)

    def line_of(self, *fields: str | int) -> int | None:
        """Line of ``<case>.<fields...>``, falling back to the closest known ancestor."""
        keys = (*self.keys, *fields)
        while True:
            line = self.lines.get(keys)
            if line is not None:
                return line
            if not keys:
                return None
            keys = keys[:-1]

    def of(self, *fields: str | int) -> str:
        """``<file>:<line>`` for ``<case>.<fields...>`` (just the file when no line is known)."""
        line = self.line_of(*fields)
        return self.label if line is None else f"{self.label}:{line}"


@dataclass(frozen=True)
class Suite:
    """A project root (an example directory or the repo itself) with its cases."""

    name: str
    root: Path
    #: the suite file, or — for a greenfield ``tests/<workflow>`` suite — the case *directory*
    checks_path: Path
    checks: tuple[Case, ...]
    locations: Mapping[str, CaseLocation] = field(default_factory=dict)
    #: :attr:`checks_path` rendered relative to the repo root, for failure locations
    checks_label: str = ""

    def location(self, case_id: str) -> CaseLocation:
        """The location of ``case_id`` — the suite file itself when it is not known."""
        known = self.locations.get(case_id)
        if known is not None:
            return known
        return CaseLocation(self.checks_label or str(self.checks_path))


# --------------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------------


def _label(path: Path, root: Path | None) -> str:
    """``examples/demo/checks.yaml`` when ``path`` is under ``root``, else the full path."""
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return str(path)


def _validation_messages(
    exc: ValidationError, raw: Any, location: CaseLocation, *, prefix: tuple[str | int, ...]
) -> list[str]:
    """``<file>:<line>: <path>: <message>`` for every problem of one case mapping."""
    messages: list[str] = []
    for text, loc in zip(
        schema_error_from_validation(exc, raw).errors,
        [tuple(e.get("loc", ())) for e in exc.errors(include_url=False)],
        strict=False,
    ):
        keys = _first_unknown_key(exc, loc, raw) or loc
        # a whole-model problem (an unknown key) has no field path to print
        message = text.removeprefix("<root>: ")
        messages.append(f"{location.of(*prefix, *keys)}: {message}")
    return messages


def _first_unknown_key(exc: ValidationError, loc: tuple[Any, ...], raw: Any) -> tuple[Any, ...]:
    """The key an ``unknown_field`` error is about (its loc points at the model, not the key)."""
    for err in exc.errors(include_url=False):
        if tuple(err.get("loc", ())) != loc or err.get("type") != "unknown_field":
            continue
        message = str(err.get("msg", ""))
        cur = raw
        for part in loc:
            cur = cur[part] if isinstance(cur, dict | list) and part in _keys_of(cur) else None
        if isinstance(cur, dict):
            for key in cur:
                if isinstance(key, str) and f"{key!r}" in message:
                    return (*loc, key)
    return ()


def _keys_of(node: Any) -> Any:
    return range(len(node)) if isinstance(node, list) else node


def load_cases(
    path: Path,
    *,
    root: Path | None = None,
    default_id: str | None = None,
    default_workflow: str | None = None,
) -> tuple[tuple[Case, ...], dict[str, CaseLocation]]:
    """Parse one suite file or single-case file into cases plus their locations.

    ``default_id`` / ``default_workflow`` fill in what the greenfield layout takes from the path
    (``.rayspec/tests/<workflow>/<case>.yaml``). Raises :class:`CaseFileError` listing every
    problem of the file.
    """
    label = _label(path, root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaseFileError([f"{label}: cannot be read ({exc.strerror or exc})"]) from None
    try:
        data, lines = load_yaml_with_lines(text, source=label)
    except RayspecError as exc:
        raise CaseFileError([str(exc)], hint=exc.hint) from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise CaseFileError(
            [f"{label}:1: expected a mapping with a 'checks' list, or a single case mapping"]
        )
    key = next((k for k in SUITE_KEYS if k in data), None)
    if key is None:
        raw_cases: list[Any] = [data]
        prefixes: list[tuple[str | int, ...]] = [()]
    else:
        if not isinstance(data[key], list):
            raise CaseFileError([f"{label}:{lines.get((key,), 1)}: '{key}' must be a list"])
        raw_cases = list(data[key])
        prefixes = [(key, i) for i in range(len(raw_cases))]
    cases: list[Case] = []
    locations: dict[str, CaseLocation] = {}
    problems: list[str] = []
    for index, (raw, prefix) in enumerate(zip(raw_cases, prefixes, strict=True), start=1):
        location = CaseLocation(label, prefix, lines)
        if not isinstance(raw, dict):
            problems.append(f"{location.of()}: case {index} is not a mapping")
            continue
        payload = dict(raw)
        if default_workflow is not None:
            payload.setdefault("workflow", default_workflow)
        try:
            case = Case.model_validate(payload)
        except ValidationError as exc:
            problems.extend(_validation_messages(exc, raw, location, prefix=()))
            continue
        if not case.id:
            case.id = default_id or f"{case.workflow}-{index}"
        if case.stubs is not None and not case.stubs.is_absolute():
            case.stubs = (path.parent / case.stubs).resolve()
        reason = unreachable_expect(case)
        if reason is not None:
            problems.append(
                f"{location.of('expect')}: `expect:` is never evaluated for a case with {reason}"
            )
            continue
        if case.id in locations:
            problems.append(f"{location.of('id')}: duplicate case id {case.id!r}")
            continue
        cases.append(case)
        locations[case.id] = location
    if problems:
        hint = UNREACHABLE_EXPECT_HINT if any("never evaluated" in p for p in problems) else None
        raise CaseFileError(problems, hint=hint)
    return tuple(cases), locations


def load_checks(path: Path) -> tuple[Case, ...]:
    """The cases of one suite file (``checks.yaml``) — locations dropped."""
    return load_cases(Path(path))[0]


def discover_suites(root: Path) -> list[Suite]:
    """Every case suite of a project tree, in a stable order.

    * ``examples/<name>/checks.yaml`` — one suite per example, rooted at the example directory
      (each example is a self-contained project);
    * ``<root>/checks.yaml`` — the project's **own** suite, named ``checks``: the same file at
      the place it lands when the project *is* the example (``rayspec init --from <name>``, or a
      copy of ``examples/<name>/``), where there is no ``examples/`` directory to sit under;
    * ``.rayspec/dryrun/checks.yaml`` — the repo's own workflows, suite ``dogfood``;
    * ``.rayspec/tests/<workflow>/<case>.yaml`` — one suite ``tests/<workflow>`` per directory,
      one case per file, plus ``tests/<name>`` for a case file sitting directly in
      ``.rayspec/tests/``.

    All of them are rooted at ``root`` except the examples. Malformed files raise
    :class:`CaseFileError`.
    """
    root = Path(root)
    suites: list[Suite] = []
    examples_dir = root / "examples"
    if examples_dir.is_dir():
        for child in sorted(examples_dir.iterdir()):
            checks_path = child / "checks.yaml"
            if child.is_dir() and checks_path.is_file():
                cases, locations = load_cases(checks_path, root=root)
                suites.append(
                    Suite(
                        child.name,
                        child,
                        checks_path,
                        cases,
                        locations,
                        _label(checks_path, root),
                    )
                )
    own = root / "checks.yaml"
    if own.is_file() and is_suite_document(own):
        cases, locations = load_cases(own, root=root)
        suites.append(Suite("checks", root, own, cases, locations, _label(own, root)))
    dogfood = root / ".rayspec" / "dryrun" / "checks.yaml"
    if dogfood.is_file():
        cases, locations = load_cases(dogfood, root=root)
        suites.append(Suite("dogfood", root, dogfood, cases, locations, _label(dogfood, root)))
    suites.extend(_greenfield_suites(root))
    return suites


def is_suite_document(path: Path) -> bool:
    """Whether ``path`` is positively a rayspec **suite** file — a mapping with ``checks:``.

    The recognition is positive, unlike :func:`is_case_document`'s, and that asymmetry is the
    point. ``.rayspec/tests/`` is rayspec's own directory, so anything in it is read as a case and
    its problems are reported. The root of a project is *shared* — a ``checks.yaml`` there may
    belong to another tool entirely — so only a document that says what it is gets read, and
    anything else is passed over rather than turned into an error about somebody else's file.
    """
    try:
        data, _ = load_yaml_with_lines(path.read_text(encoding="utf-8"), source=str(path))
    except (OSError, RayspecError):
        return False
    return isinstance(data, dict) and any(key in data for key in SUITE_KEYS)


def is_case_document(path: Path) -> bool:
    """Whether ``path`` should be read as a case file at all.

    Discovery globs a directory, so it also sees documents that are deliberately kept next to a
    case — a stub script above all, which is where :doc:`testing` tells you to put it. Only a
    document that is *positively recognisable as something else* is skipped: a mapping whose
    top-level keys are a non-empty subset of :data:`STUB_SCRIPT_KEYS` and name no case key.
    Everything else (an empty document, a typo where a case key was meant, YAML that does not
    parse) is read as a case, so its problems are reported with a ``file:line`` instead of the
    file silently disappearing from the suite.
    """
    try:
        data, _ = load_yaml_with_lines(path.read_text(encoding="utf-8"), source=str(path))
    except (OSError, RayspecError):
        return True  # let load_cases produce the real, located error
    if not isinstance(data, dict) or not data:
        return True
    keys = set(data)
    return not (keys <= STUB_SCRIPT_KEYS and not keys & case_keys())


def _greenfield_suites(root: Path) -> list[Suite]:
    """``.rayspec/tests/<workflow>/<case>.yaml`` (and loose files) as one suite per directory."""
    tests_dir = root / ".rayspec" / "tests"
    if not tests_dir.is_dir():
        return []
    suites: list[Suite] = []
    loose: list[Case] = []
    loose_locations: dict[str, CaseLocation] = {}
    problems: list[str] = []
    for child in sorted(tests_dir.iterdir()):
        if child.is_file() and child.suffix in {".yaml", ".yml"}:
            if not is_case_document(child):
                continue
            cases, locations = load_cases(child, root=root, default_id=child.stem)
            problems.extend(_merge(loose_locations, locations))
            loose.extend(c for c in cases if c.id in loose_locations)
            continue
        if not child.is_dir():
            continue
        dir_cases: list[Case] = []
        dir_locations: dict[str, CaseLocation] = {}
        for case_file in sorted([*child.glob("*.yaml"), *child.glob("*.yml")]):
            if not is_case_document(case_file):
                continue
            file_cases, file_locations = load_cases(
                case_file, root=root, default_id=case_file.stem, default_workflow=child.name
            )
            problems.extend(_merge(dir_locations, file_locations))
            dir_cases.extend(c for c in file_cases if c.id in dir_locations)
        if dir_cases:
            suites.append(
                Suite(
                    f"tests/{child.name}",
                    root,
                    child,
                    tuple(dir_cases),
                    dir_locations,
                    _label(child, root),
                )
            )
    if problems:
        raise CaseFileError(problems)
    if loose:
        suites.append(
            Suite("tests", root, tests_dir, tuple(loose), loose_locations, _label(tests_dir, root))
        )
    return suites


def _merge(into: dict[str, CaseLocation], new: Mapping[str, CaseLocation]) -> list[str]:
    """Add ``new`` to ``into``; a case id that two files share is a problem naming both."""
    problems: list[str] = []
    for case_id, location in new.items():
        first = into.get(case_id)
        if first is not None:
            problems.append(
                f"{location.of('id')}: duplicate case id {case_id!r} "
                f"(already defined in {first.label})"
            )
            continue
        into[case_id] = location
    return problems


__all__ = [
    "STUB_SCRIPT_KEYS",
    "SUITE_KEYS",
    "UNREACHABLE_EXPECT_HINT",
    "Case",
    "CaseFileError",
    "CaseLocation",
    "Expect",
    "StepExpect",
    "Suite",
    "case_keys",
    "discover_suites",
    "is_case_document",
    "is_suite_document",
    "load_cases",
    "load_checks",
    "unreachable_expect",
]

#!/usr/bin/env python
"""Validate, plan and dry-run every example and dogfood workflow, and verify the coverage matrix.

Boundary: a standalone developer/CI script (also imported by ``tests/examples``). The cases
themselves, their format and their execution live in :mod:`rayspec.testing` — the same code
``rayspec test`` runs — so this script is two things the packaged command has no business doing:

* the **coverage matrix** of ``examples/README.md`` (``--matrix``): every ``| Capability |
  Examples |`` row must name existing examples, and every backticked token of a row must actually
  occur in the named examples' trees;
* a **CLI contract smoke** over one case per suite: the engine-level runner behind ``rayspec test``
  never touches ``rayspec run``'s own plumbing, so one case of every suite is additionally driven
  through the Typer app with ``--json`` and its stdout checked against the JSONL contract;
* the **docs-as-tests marker convention** (``--docs``): a fenced YAML block of ``README.md`` or
  ``docs/*.md`` carrying ``rayspec:validate`` / ``rayspec:run`` on the line above it is extracted
  and really checked, and a block that carries neither must explain itself in a one-line
  ``<!-- rayspec:skip … -->`` comment (see :func:`find_doc_blocks`).

The case format is documented in ``docs/testing.md``; a ``checks.yaml`` next to each example (and
``.rayspec/dryrun/checks.yaml`` for the repo's own workflows) is discovered automatically.

Usage: ``uv run python scripts/check_examples.py [--verbose] [--only NAME] [--matrix] [--docs]``;
exit 1 on any failed check, 2 on usage errors (unknown ``--only`` suite).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import textwrap
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rayspec.cli.commands.run import SUMMARY_KEYS
from rayspec.testing import CaseResult, run_case
from rayspec.testing.spec import (
    Case,
    CaseFileError,
    Expect,
    StepExpect,
    Suite,
    load_checks,
)
from rayspec.testing.spec import CaseFileError as CheckFileError
from rayspec.testing.spec import discover_suites as _discover_suites

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
DOGFOOD_CHECKS = REPO_ROOT / ".rayspec" / "dryrun" / "checks.yaml"
EXIT_USAGE = 2

#: Names kept for the callers that predate ``rayspec.testing`` (``tests/examples`` imports the
#: script as a module); ``__all__`` records that surface.
Check = Case
CheckResult = CaseResult

__all__ = [
    "DOC_MARKERS",
    "DOC_RUN_STATUSES",
    "Check",
    "CheckFileError",
    "CheckResult",
    "DocBlock",
    "Expect",
    "Invocation",
    "StepExpect",
    "Suite",
    "check_doc_block",
    "cli_contract_check",
    "discover_suites",
    "doc_block_problems",
    "doc_declared_agents",
    "doc_problems",
    "doc_sources",
    "doc_steps_for",
    "doc_workflow",
    "doc_workflow_name",
    "find_doc_blocks",
    "json_stream_problems",
    "load_checks",
    "main",
    "matrix_needles",
    "parse_coverage_matrix",
    "run_check",
    "smoke_case",
    "stray_doc_markers",
    "unbacked_claims",
    "yaml",
]


def discover_suites(repo_root: Path = REPO_ROOT) -> list[Suite]:
    """Every ``examples/<name>/`` with a ``checks.yaml`` plus the repo's dogfood workflows."""
    return _discover_suites(repo_root)


def run_check(suite: Suite, check: Case, *, home: Path) -> CaseResult:
    """Load, validate and dry-run one case; never raises (see :func:`rayspec.testing.run_case`)."""
    return run_case(suite, check, home=home)


# --------------------------------------------------------------------------------------------------
# CLI contract smoke
# --------------------------------------------------------------------------------------------------


@dataclass
class Invocation:
    """One CLI call and its outcome (``output`` = stdout + stderr interleaved, ``stdout`` alone)."""

    args: list[str]
    exit_code: int
    output: str
    stdout: str = ""

    def __str__(self) -> str:
        return f"$ rayspec {' '.join(self.args)}\n  -> exit {self.exit_code}\n{self.output}"


def _invoke(
    args: list[str], *, home: Path, env_overrides: Mapping[str, str | None] | None = None
) -> Invocation:
    from typer.testing import CliRunner

    from rayspec.cli.app import app

    # CliRunner overlays this mapping onto os.environ; None unsets a variable for the call.
    env: dict[str, str | None] = {
        k: (None if k.startswith("RAYSPEC_INPUT_") else v) for k, v in os.environ.items()
    }
    env["RAYSPEC_HOME"] = str(home)
    env.setdefault("NO_COLOR", "1")
    env["CI"] = None  # commands whose defaults change under CI must not depend on the shell
    env.update(env_overrides or {})
    runner = CliRunner(env=env)
    result = runner.invoke(app, args, catch_exceptions=True)
    output = result.output
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        output += f"\n{type(result.exception).__name__}: {result.exception}"
    return Invocation(args, result.exit_code, output, result.stdout)


_SUMMARY_KEYS = SUMMARY_KEYS  # the CLI's own definition; never hand-copied here
_EVENT_KEYS = frozenset({"type", "run_id", "ts", "step_path", "data"})


def _summary_from_json(output: str) -> dict[str, Any] | None:
    for raw in reversed(output.splitlines()):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "run_id" in data and "exit_code" in data:
            return data
    return None


def json_stream_problems(stdout: str) -> list[str]:
    """Contract of ``rayspec run --json`` stdout: JSONL events/stream records, then the summary.

    Every non-blank line is JSON; events carry exactly ``type, run_id, ts, step_path, data`` (stream
    records ``type, step_path, record``); all events share one ``run_id``; the last event is
    ``run.finished`` and the very last line is the summary object.
    """
    problems: list[str] = []
    lines: list[Any] = []
    for number, raw in enumerate(stdout.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            problems.append(f"stdout line {number} is not JSON: {raw[:80]!r}")
    if problems or not lines:
        return problems or ["stdout is empty"]
    summary, body = lines[-1], lines[:-1]
    if not isinstance(summary, dict) or set(summary) != _SUMMARY_KEYS:
        return [f"last stdout line is not the summary object: {str(summary)[:120]}"]
    run_ids: set[str] = set()
    for line in body:
        if not isinstance(line, dict) or "type" not in line:
            problems.append(f"stdout line without 'type' before the summary: {str(line)[:80]}")
            continue
        if line["type"] == "stream":
            if set(line) != {"type", "step_path", "record"}:
                problems.append(f"stream record keys {sorted(line)}")
            continue
        if set(line) != _EVENT_KEYS:
            problems.append(f"event {line['type']} keys {sorted(line)}")
        run_ids.add(str(line.get("run_id")))
    if run_ids and run_ids != {summary["run_id"]}:
        problems.append(f"events carry run ids {sorted(run_ids)}, summary {summary['run_id']!r}")
    if not body or not isinstance(body[-1], dict) or body[-1].get("type") != "run.finished":
        problems.append("the last event is not run.finished")
    else:
        data = body[-1].get("data")
        if not isinstance(data, dict) or data.get("status") != summary["status"]:
            problems.append("run.finished status/data differs from the summary status")
    return problems


def _step_statuses(output: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "step.finished" and data.get("step_path"):
            statuses[str(data["step_path"])] = str((data.get("data") or {}).get("status"))
    return statuses


def cli_args(suite: Suite, case: Case, *, inputs_file: Path) -> list[str]:
    """The ``rayspec run --dry-run --json`` command line equivalent to ``case``."""
    args = ["run", case.workflow, "--root", str(suite.root), "--dry-run", "--json"]
    if case.inputs:
        args += ["--inputs-file", str(inputs_file)]
    if case.allow_unsupported:
        args.append("--allow-unsupported")
    if case.stubs is not None:
        args += ["--stubs", str(case.stubs)]
    return args


def smoke_case(suite: Suite) -> Case | None:
    """The case of ``suite`` used for the CLI contract smoke (the first runnable one)."""
    return next((c for c in suite.checks if c.run and c.validate_ == "ok"), None)


def cli_contract_check(suite: Suite, case: Case, *, home: Path) -> CaseResult:
    """Drive one case through the real CLI and check ``--json`` stdout against the contract."""
    result = CaseResult(suite.name, f"{case.id} (cli --json)")
    with tempfile.TemporaryDirectory(prefix="rayspec-smoke-") as tmp:
        inputs_file = Path(tmp) / "inputs.json"
        inputs_file.write_text(json.dumps(dict(case.inputs), default=str), encoding="utf-8")
        inv = _invoke(
            cli_args(suite, case, inputs_file=inputs_file), home=home, env_overrides=case.env
        )
    for problem in json_stream_problems(inv.stdout):
        result.fail(
            "--json stdout",
            problem,
            detail=str(inv)[:400],
            fix="see docs/cli.md `rayspec run --json` — the summary object is the last line",
            location=suite.location(case.id).of(),
        )
    summary = _summary_from_json(inv.output)
    if summary is None:
        result.fail(
            "--json stdout",
            f"the run printed no JSON summary (exit {inv.exit_code})",
            detail=str(inv)[:400],
            fix="the CLI must always print the summary object; check the invocation above",
            location=suite.location(case.id).of(),
        )
    elif case.expect.status is not None and summary.get("status") != case.expect.status:
        result.fail(
            "--json stdout",
            f"the CLI reports status {summary.get('status')!r}, the harness {case.expect.status!r}",
            detail=str(inv)[:400],
            fix="the CLI and rayspec.testing must agree; one of them is wrong",
            location=suite.location(case.id).of("expect", "status"),
        )
    return result


# --------------------------------------------------------------------------------------------------
# Coverage matrix (examples/README.md)
# --------------------------------------------------------------------------------------------------

_CELL_SPLIT = re.compile(r"(?<!\\)\|")  # a pipe not escaped as \|
_NAME = re.compile(r"`([a-z_]+)`")


def parse_coverage_matrix(readme: Path) -> dict[str, list[str]]:
    """Return ``{capability: [example names]}`` from the ``| Capability | Examples | ...`` tables.

    Every markdown table whose first header cell is ``Capability`` counts; the example column is
    the second cell and lists example names in backticks.
    """
    rows: dict[str, list[str]] = {}
    in_table = False
    for line in readme.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in _CELL_SPLIT.split(line)[1:]]
        if len(cells) < 2:
            in_table = False
            continue
        capability = cells[0].replace("\\|", "|")
        if capability.lower() == "capability":
            in_table = True
            continue
        if not in_table or set(capability) <= {"-", ":", " "}:
            continue
        names = _NAME.findall(cells[1])
        if capability in rows:
            raise ValueError(f"duplicate coverage row {capability!r}")
        rows[capability] = names
    return rows


_TOKEN = re.compile(r"`([^`]+)`")
_WORD = re.compile(r"^[a-z][a-z|-]*$")
_JINJA_TAG = re.compile(r"^\{% (\w+) %\}$")
_NESTED_ROOTS = {"defaults", "loop", "approve", "include", "stop", "tools"}
_SKIP_TOKENS = {"{{ }}", "…"}


def _needle_variants(words: list[str]) -> list[list[str]]:
    """Expand ``a|b`` alternations word-wise (cartesian product)."""
    variants: list[list[str]] = [[]]
    for word in words:
        variants = [[*v, alt] for v in variants for alt in word.split("|")]
    return variants


def matrix_needles(capability: str) -> list[str]:
    """Regexes that must occur in the named examples for a coverage-matrix row to be backed.

    Every backticked token of the row label yields needles: ``rayspec cmd [sub|alt]`` → one needle
    per alternative; ``stubs: key`` / ``config.yaml key`` → ``key:``; ``defaults.x`` (and the other
    nested specs) → ``x:``; ``{% tag %}`` → ``{% tag``; anything else is the literal token with
    ``<placeholder>`` → ``\\S+`` and ``…`` → ``.*``. ``{{ }}`` alone is too generic and skipped.
    """
    needles: list[str] = []
    for raw in _TOKEN.findall(capability):
        token = raw.replace("\\|", "|").strip()
        if token in _SKIP_TOKENS:
            continue
        words = token.split()
        if words[0] == "rayspec":
            tail = [w for w in words[1:] if _WORD.match(w)]
            needles.extend(re.escape(" ".join(["rayspec", *v])) for v in _needle_variants(tail))
            continue
        if len(words) == 2 and words[0] in {"stubs:", "config.yaml"}:
            needles.append(re.escape(words[1]) + ":")
            continue
        if (tag := _JINJA_TAG.match(token)) is not None:
            needles.append(re.escape("{% " + tag.group(1)))
            continue
        if "." in token and " " not in token and token.split(".")[0] in _NESTED_ROOTS:
            needles.append(re.escape(token.split(".")[-1].rstrip(":")) + ":")
            continue
        pattern = re.escape(token)
        pattern = re.sub(r"<[^>]+>", r"\\S+", pattern)
        pattern = pattern.replace(re.escape("…"), ".*").replace(re.escape("*"), r"[\w*]+")
        needles.append(pattern)
    return needles


def _suite_corpus(suite: Suite) -> str:
    """README + top-level YAML + the whole ``.rayspec/`` tree (contents and relative paths), minus
    comment-only YAML lines."""
    if suite.name == "dogfood":
        files = [p for p in (suite.root / ".rayspec").rglob("*") if p.is_file()]
    else:
        files = [
            suite.root / "README.md",
            *suite.root.glob("*.yaml"),
            *(p for p in (suite.root / ".rayspec").rglob("*") if p.is_file()),
        ]
    chunks: list[str] = [str(p.relative_to(suite.root)) for p in files if p.is_file()]
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix in {".yaml", ".yml"}:
            text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        chunks.append(text)
    return "\n".join(chunks)


def unbacked_claims(rows: Mapping[str, list[str]], suites: list[Suite]) -> list[str]:
    """Matrix rows whose tokens are not found in the named examples (one message per problem).

    Two checks per row: every needle occurs in at least one named example, and every named example
    matches at least one needle of the row (otherwise that attribution is unbacked).
    """
    corpus = {suite.name: _suite_corpus(suite) for suite in suites}
    problems: list[str] = []
    for capability, names in rows.items():
        needles = matrix_needles(capability)
        if not needles:
            continue
        known = [n for n in names if n in corpus]
        hits = {
            needle: [n for n in known if re.search(needle, corpus[n], re.MULTILINE)]
            for needle in needles
        }
        for needle, found in hits.items():
            if not found:
                problems.append(f"{capability!r}: /{needle}/ not found in any of {known}")
        for name in known:
            if not any(name in found for found in hits.values()):
                problems.append(f"{capability!r}: nothing backs the attribution to {name!r}")
    return problems


# --------------------------------------------------------------------------------------------------
# Docs-as-tests (the marker convention)
# --------------------------------------------------------------------------------------------------

#: Every fenced ``yaml`` block of ``README.md`` / ``docs/*.md`` carries exactly one marker on the
#: line above it, and the marker says what happens to the block:
#:
#: * ``<!-- rayspec:validate -->`` — loaded and validated the way ``rayspec validate`` does;
#: * ``<!-- rayspec:run [k=v …] -->`` — additionally driven through ``rayspec run --dry-run``
#:   (every agent becomes the scripted stub, shell/python bodies are skipped), with ``k=v`` as its
#:   ``--input`` pairs. This is where a snippet that parses but cannot run shows up — as far as a
#:   dry run goes, which is the graph, the schedule and every ``prompt:`` body: the run builds,
#:   every step reaches a terminal status and a prompt template is really rendered against the
#:   context. A ``shell:``/``python:`` body is skipped and therefore never rendered either, so a
#:   missing reference inside one is NOT caught at this level;
#: * ``<!-- rayspec:skip <why> -->`` — deliberately illustrative, with the one-line reason.
#:
#: The marker is an HTML comment rather than a fence token so that it stays invisible in the
#: rendered page and the fence keeps saying plain ``yaml`` to every other reader of the docs. It
#: must sit on the line IMMEDIATELY above the opening fence: a marker with anything between it and
#: the fence — a blank line included — is reported as stranded rather than bound to the next block.
#:
#: A fenced block is a run of three or more backticks or tildes, and the language word is compared
#: case-insensitively, so ``~~~YAML`` is as much a block as ```` ```yaml ````. A longer fence
#: quotes shorter ones inside it, which is how a page shows this convention itself.
DOC_MARKERS: tuple[str, ...] = ("rayspec:validate", "rayspec:run", "rayspec:skip")

#: Terminal statuses a ``rayspec:run`` block may reach: ``cancelled`` is a ``stop:`` step doing its
#: job, ``failed`` is the drift this convention exists to catch.
DOC_RUN_STATUSES: frozenset[str] = frozenset({"succeeded", "cancelled"})

#: A fenced block opens with at least three backticks or tildes and closes on a run of the same
#: character at least as long, so a longer fence quotes shorter ones inside it (the way a
#: markdown page shows a markdown snippet). Both halves are recognised here, because a fence
#: variant the scanner does not know is a block nobody checks and nobody is told about.
_DOC_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_DOC_CLOSE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*$")
_DOC_MARKER = re.compile(
    r"^\s*<!--\s*rayspec:(?P<kind>validate|run|skip)(?P<rest>\s[^\n]*?)?\s*-->\s*$"
)
_DOC_LANGS = frozenset({"yaml", "yml"})
_DOC_PAIR = re.compile(r"^(?P<key>[a-z][a-z0-9_]*)=(?P<value>.*)$")
_DOC_IS_DOCUMENT = re.compile(r"^rayspec:\s*1\s*(?:#.*)?$", re.MULTILINE)
_DOC_DECLARED_NAME = re.compile(r"^name:\s*(?P<name>[a-z][a-z0-9_]*)\s*(?:#.*)?$", re.MULTILINE)
_DOC_HAS_STEPS = re.compile(r"^steps:", re.MULTILINE)
_DOC_NOOP_STEP = 'steps:\n  - id: noop\n    shell: "true"\n'
_DOC_STEP_ID = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class DocBlock:
    """One fenced YAML block of a documentation page and the marker above it.

    ``marker`` is ``"validate"``, ``"run"`` or ``None``; ``inputs`` are the ``k=v`` pairs of a
    ``rayspec:run`` marker; ``reason`` is the text of a ``rayspec:skip`` marker; ``unknown`` holds
    marker words that are neither (a typo — reported, never ignored). ``text`` is the block body,
    dedented to column 0.
    """

    source: str
    line: int
    marker: str | None
    inputs: tuple[tuple[str, str], ...] = ()
    reason: str | None = None
    text: str = ""
    unknown: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """``docs/schema.md:60`` — the file and the line of the opening fence (a pytest id)."""
        return f"{self.source}:{self.line}"

    def __str__(self) -> str:
        return self.id


def doc_sources(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Every user-facing markdown page whose YAML blocks the convention covers."""
    return [repo_root / "README.md", *sorted((repo_root / "docs").glob("*.md"))]


def _parse_marker(match: re.Match[str]) -> dict[str, Any]:
    """One ``<!-- rayspec:… -->`` comment → the ``DocBlock`` fields it contributes."""
    kind, rest = match.group("kind"), (match.group("rest") or "").strip()
    if kind == "skip":
        return {"marker": None, "reason": rest, "inputs": (), "unknown": ()}
    inputs: list[tuple[str, str]] = []
    unknown: list[str] = []
    for word in rest.split():
        pair = _DOC_PAIR.match(word)
        if pair is None:
            unknown.append(word)
        else:
            inputs.append((pair.group("key"), pair.group("value")))
    return {"marker": kind, "reason": None, "inputs": tuple(inputs), "unknown": tuple(unknown)}


def _closes(line: str, fence: str) -> bool:
    """Does ``line`` close a block opened with ``fence``?

    A closing fence is the same character repeated at least as often as the opening one and
    nothing else on the line — so a ```` ```yaml ```` inside a four-backtick wrapper is quoted
    text, not a snippet of its own.
    """
    match = _DOC_CLOSE.match(line)
    run = "" if match is None else match.group("fence")
    return bool(run) and run[0] == fence[0] and len(run) >= len(fence)


def _scan_page(path: Path, source: str) -> tuple[list[DocBlock], list[str]]:
    """``(blocks, stray markers)`` of one markdown page.

    A stray marker is one that does not sit directly above a fenced YAML block — a typo, a moved
    snippet or a fence whose language changed. It is reported rather than ignored, because a
    marker nobody reads is a check nobody runs.
    """
    blocks: list[DocBlock] = []
    strays: list[str] = []
    pending: dict[str, Any] | None = None
    pending_line = 0
    indent: str | None = None
    fence_run = ""
    start, lang = 0, ""
    body: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if indent is not None:
            if _closes(line, fence_run):
                if lang in _DOC_LANGS:
                    text = textwrap.dedent("\n".join(body)).rstrip()
                    fields = pending or {"marker": None}
                    blocks.append(DocBlock(source, start, text=text + "\n", **fields))
                elif pending is not None:
                    strays.append(
                        f"{source}:{pending_line}: rayspec marker above a {fence_run}{lang} block"
                    )
                indent, pending = None, None
                continue
            body.append(line[len(indent) :] if line.startswith(indent) else line)
            continue
        fence = _DOC_FENCE.match(line)
        if fence is not None:
            words = fence.group("info").split()
            indent, start, body = fence.group("indent"), number, []
            fence_run = fence.group("fence")
            lang = words[0].lower() if words else ""
            if lang in _DOC_LANGS and any("rayspec:" in word for word in words[1:]):
                strays.append(f"{source}:{number}: a rayspec marker belongs ABOVE the fence")
            continue
        marker = _DOC_MARKER.match(line)
        if marker is not None:
            if pending is not None:
                strays.append(f"{source}:{pending_line}: two rayspec markers above one block")
            pending, pending_line = _parse_marker(marker), number
        elif pending is not None:
            strays.append(f"{source}:{pending_line}: rayspec marker is not above a fenced block")
            pending = None
    if pending is not None:
        strays.append(f"{source}:{pending_line}: rayspec marker is not above a fenced block")
    return blocks, strays


def find_doc_blocks(repo_root: Path = REPO_ROOT) -> list[DocBlock]:
    """Every fenced ``yaml``/``yml`` block of :func:`doc_sources`, in file and line order.

    Indented fences (a block inside a list item) are found and dedented; the marker above such a
    block is indented with it.
    """
    blocks: list[DocBlock] = []
    for path in doc_sources(repo_root):
        if path.is_file():
            blocks.extend(_scan_page(path, path.relative_to(repo_root).as_posix())[0])
    return blocks


def stray_doc_markers(repo_root: Path = REPO_ROOT) -> list[str]:
    """Markers that sit above no fenced YAML block (see :func:`_scan_page`)."""
    strays: list[str] = []
    for path in doc_sources(repo_root):
        if path.is_file():
            strays.extend(_scan_page(path, path.relative_to(repo_root).as_posix())[1])
    return strays


def doc_block_problems(blocks: Iterable[DocBlock]) -> list[str]:
    """Totality of the convention: one message per block that is neither checked nor explained.

    An unmarked block must carry a non-empty ``rayspec:skip`` reason; ``k=v`` inputs only mean
    something for a ``rayspec:run`` block; an unrecognised marker word is always a problem.
    """
    problems: list[str] = []
    for block in blocks:
        if block.marker is None and not block.reason:
            problems.append(
                f"{block.id}: fenced yaml block is neither checked nor explained — put one of "
                f"{', '.join(DOC_MARKERS)} on the line above it "
                f"(`<!-- rayspec:skip <why> -->` for a snippet that is only illustrative)"
            )
        if block.inputs and block.marker != "run":
            problems.append(f"{block.id}: `k=v` only applies to a `<!-- rayspec:run … -->` marker")
        for word in block.unknown:
            problems.append(
                f"{block.id}: unknown marker word {word!r} (expected `<input>=<value>`)"
            )
    return problems


def doc_workflow_name(block: DocBlock) -> str:
    """The workflow name a block is checked under: the one it declares, else one from its id."""
    complete = _DOC_IS_DOCUMENT.search(block.text) is not None
    declared = _DOC_DECLARED_NAME.search(block.text) if complete else None
    if declared is not None:
        return declared.group("name")
    stem = re.sub(r"[^a-z0-9]+", "_", Path(block.source).stem.lower()).strip("_")
    return f"doc_{stem}_{block.line}"


def doc_declared_agents(text: str) -> list[str]:
    """The agent names a fragment declares under ``agents:``, in order (``[]`` when it has none)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    agents = data.get("agents") if isinstance(data, Mapping) else None
    return [str(key) for key in agents] if isinstance(agents, Mapping) else []


def doc_steps_for(text: str) -> str:
    """The ``steps:`` a fragment without any is given so that what it declares is exercised.

    One ``prompt:`` step per declared agent, because a shell noop names no agent and therefore
    resolves no provider capability: an ``agents:`` fragment would validate whatever it said. A
    fragment that declares no agent gets the trivial shell step, which is all a workflow needs to
    be a workflow.
    """
    agents = doc_declared_agents(text)
    if not agents:
        return _DOC_NOOP_STEP
    steps = ["steps:"]
    for index, agent in enumerate(agents, start=1):
        stem = agent if _DOC_STEP_ID.fullmatch(agent) else f"agent_{index}"
        steps += [f"  - id: use_{stem}", f"    agent: {agent}", '    prompt: "Say hi"']
    return "\n".join(steps) + "\n"


def doc_workflow(block: DocBlock) -> str:
    """The workflow document a block is checked as — the block itself, or a minimal wrapper.

    A complete document (it says ``rayspec: 1``) is used verbatim, with a ``name:`` added when it
    declares none. A fragment is wrapped: a step list becomes the ``steps:`` of a minimal
    workflow, any other mapping is spliced into one (and gets the steps of :func:`doc_steps_for`
    when it has none), so an ``inputs:``, ``agents:`` or ``defaults:`` fragment is checked exactly
    as written.
    """
    text = block.text
    name = doc_workflow_name(block)
    if _DOC_IS_DOCUMENT.search(text):
        head = "" if _DOC_DECLARED_NAME.search(text) else f"name: {name}\n"
        return head + text
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if lines and lines[0].startswith("- "):
        return f"rayspec: 1\nname: {name}\nsteps:\n" + textwrap.indent(text, "  ")
    body = f"rayspec: 1\nname: {name}\n" + text
    if not _DOC_HAS_STEPS.search(text):
        body += doc_steps_for(text)
    return body


def check_doc_block(block: DocBlock, *, home: Path) -> list[str]:
    """Load, validate and (for ``rayspec:run``) dry-run one marked block; problems as messages.

    Unmarked blocks are not checked at all — they are covered by :func:`doc_block_problems`.
    Everything happens in a throwaway project under a temporary directory: no network, no
    provider, no worktree (a dry run is ``isolation: none``).
    """
    if block.marker is None:
        return []
    problems: list[str] = []
    name = doc_workflow_name(block)
    with tempfile.TemporaryDirectory(prefix="rayspec-docs-") as tmp:
        root = Path(tmp) / "project"
        (root / ".rayspec" / "workflows").mkdir(parents=True)
        (root / ".rayspec" / "workflows" / f"{name}.yaml").write_text(
            doc_workflow(block), encoding="utf-8"
        )
        inv = _invoke(["validate", name, "--root", str(root), "--json"], home=home)
        rows = json.loads(inv.stdout) if inv.stdout.strip().startswith("[") else []
        row = rows[0] if rows else {}
        problems += [f"{block.id}: {m}" for m in [*row.get("errors", []), *row.get("warnings", [])]]
        if inv.exit_code != 0 and not problems:
            problems.append(f"{block.id}: rayspec validate exited {inv.exit_code}\n{inv}")
        if problems or block.marker != "run":
            return problems
        args = ["run", name, "--root", str(root), "--dry-run", "--json"]
        for key, value in block.inputs:
            args += ["--input", f"{key}={value}"]
        inv = _invoke(args, home=home)
        summary = _summary_from_json(inv.output)
        if summary is None:
            problems.append(f"{block.id}: the documented workflow did not run\n{inv}")
        elif summary.get("status") not in DOC_RUN_STATUSES:
            problems.append(
                f"{block.id}: `rayspec run --dry-run` ended {summary.get('status')!r}"
                f" — {summary.get('reason') or 'no reason recorded'}\n{inv}"
            )
    return problems


def doc_problems(repo_root: Path = REPO_ROOT, *, home: Path) -> list[str]:
    """Every docs-as-tests problem: stray/missing markers plus each marked block's own checks."""
    blocks = find_doc_blocks(repo_root)
    problems = [*stray_doc_markers(repo_root), *doc_block_problems(blocks)]
    for block in blocks:
        problems.extend(check_doc_block(block, home=home))
    return problems


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------


def _iter_results(
    suites: list[Suite], *, home: Path, only: str | None, smoke: bool = True
) -> Iterator[CaseResult]:
    """Every case of the selected suites, plus one CLI contract smoke per suite."""
    for suite in suites:
        if only and suite.name != only:
            continue
        for check in suite.checks:
            yield run_check(suite, check, home=home)
        case = smoke_case(suite) if smoke else None
        if case is not None:
            yield cli_contract_check(suite, case, home=home)


def main(argv: list[str] | None = None) -> int:
    """Run every check; one line per check (details for failures); exit 1 on failure, 2 on usage."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--only", help="restrict to one suite (example dir name or 'dogfood')")
    parser.add_argument("--verbose", action="store_true", help="print every check that passed")
    parser.add_argument("--matrix", action="store_true", help="also verify the coverage matrix")
    parser.add_argument(
        "--docs", action="store_true", help="also check the marked yaml blocks of README/docs"
    )
    ns = parser.parse_args(argv)

    try:
        suites = discover_suites(REPO_ROOT)
    except CaseFileError as exc:
        for line in exc.errors:
            print(f"error: {line}", file=sys.stderr)
        return EXIT_USAGE
    if not suites:
        print("no examples found", file=sys.stderr)
        return 1
    if ns.only and ns.only not in {suite.name for suite in suites}:
        known = ", ".join(suite.name for suite in suites)
        print(f"--only {ns.only!r}: no such suite (known: {known})", file=sys.stderr)
        return EXIT_USAGE
    failed = 0
    with tempfile.TemporaryDirectory(prefix="rayspec-home-") as tmp:
        home = Path(tmp)
        for result in _iter_results(suites, home=home, only=ns.only):
            if not result.ok:
                failed += 1
            if ns.verbose or not result.ok:
                print(result.report())
            else:
                print(result.report().splitlines()[0])
    if ns.docs:
        with tempfile.TemporaryDirectory(prefix="rayspec-docs-home-") as tmp:
            problems = doc_problems(REPO_ROOT, home=Path(tmp))
        for problem in problems:
            failed += 1
            print(f"[docs] {problem}")
        print(f"[docs] {len(find_doc_blocks(REPO_ROOT))} yaml blocks")
    if ns.matrix:
        rows = parse_coverage_matrix(EXAMPLES_DIR / "README.md")
        known_names = {suite.name for suite in suites}
        for capability, names in rows.items():
            bad = [n for n in names if n not in known_names]
            if not names or bad:
                failed += 1
                print(f"[matrix] {capability!r}: {'no example' if not names else bad}")
        for problem in unbacked_claims(rows, suites):
            failed += 1
            print(f"[matrix] {problem}")
        print(f"[matrix] {len(rows)} rows")
    print(f"{failed} failed" if failed else "all checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

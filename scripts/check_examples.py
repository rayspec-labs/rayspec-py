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
  through the Typer app with ``--json`` and its stdout checked against the JSONL contract.

The case format is documented in ``docs/testing.md``; a ``checks.yaml`` next to each example (and
``.rayspec/dryrun/checks.yaml`` for the repo's own workflows) is discovered automatically.

Usage: ``uv run python scripts/check_examples.py [--verbose] [--only NAME] [--matrix]``; exit 1 on
any failed check, 2 on usage errors (unknown ``--only`` suite).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator, Mapping
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
    "Check",
    "CheckFileError",
    "CheckResult",
    "Expect",
    "Invocation",
    "StepExpect",
    "Suite",
    "cli_contract_check",
    "discover_suites",
    "json_stream_problems",
    "load_checks",
    "main",
    "matrix_needles",
    "parse_coverage_matrix",
    "run_check",
    "smoke_case",
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

# SPDX-License-Identifier: Apache-2.0
"""Mutation testing for the governance modules: which lines do the tests not really check?

A green suite says the code passes its tests. It does not say the tests would notice if the
code changed. This tool answers that second question for the handful of modules where a silent
change is expensive — the join table, the scheduler's drain/teardown path, the redactor and the
approval-class gate — by making one small, semantics-changing edit at a time and running the
tests that ought to catch it. A mutant the suite still passes is a **survivor**: a line nothing
asserts on.

Nothing is gated on a score. The output is a list of survivors with ``file:line`` and the exact
edit, to be read and triaged by a person.

Running it::

    uv run python tests/properties/mutation/mutate.py --list
    uv run python tests/properties/mutation/mutate.py --target graph --jobs 6
    uv run python tests/properties/mutation/mutate.py --jobs 6 --json report.json

**The working tree is never modified.** Each worker gets a stand-in checkout in a temporary
directory — a real copy of ``src/rayspec`` inside a mirror of the repository — and runs pytest
with its ``src`` first on ``PYTHONPATH``, which shadows the editable install (a ``.pth`` entry,
appended after ``PYTHONPATH``). A worker that is killed mid-run therefore leaves nothing behind
but a temp directory: there is no "restore the file" step that a crash could skip.

Two phases, because confirming a survivor is expensive:

1. **triage** — every mutant runs the target's own tests (``Target.tests``), stopping at the
   first failure. Most mutants die here in a second or two.
2. **confirm** — every survivor of phase 1 is re-run against the whole suite
   (:data:`CONFIRM_TESTS`). A mutant killed only in phase 2 is not a survivor, it is a mutant
   the target's tests missed and another suite caught; reporting it would be a lie about
   coverage.

A mutant whose tests hang is reported as ``timeout`` and counted as killed: the suite noticed.
An ``unparse`` of the unmutated module is checked against the tests first (the "null mutant"),
because every result is worthless if the round-trip itself changes behaviour.

**Reading the report.** A survivor is a question, not a verdict, and there are three answers:

* *a real gap* — the line decides something and nothing asserts on the decision;
* *an equivalent mutant* — the edit cannot change behaviour, so no test could ever kill it.
  ``if not text or not self.literals: return text`` does the same thing with ``and``, because
  the loop it guards is a no-op in exactly the cases the guard catches. Say so out loud rather
  than writing a test to chase it;
* *killed elsewhere* (phase 2 said so) — the module's own suite does not pin the line and the
  coverage comes from somewhere else. Not a gap, but worth knowing before that other suite moves.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: The repository root (this file is ``<repo>/tests/properties/mutation/mutate.py``).
REPO = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Target:
    """One module under mutation, the tests that ought to catch its mutants, and why it is here."""

    name: str
    module: str
    tests: tuple[str, ...]
    why: str

    @property
    def path(self) -> Path:
        """The module's absolute path in this checkout."""
        return REPO / self.module


#: The modules a silent change in would be expensive, and the suites that own them.
TARGETS: dict[str, Target] = {
    "graph": Target(
        name="graph",
        module="src/rayspec/engine/graph.py",
        tests=(
            "tests/engine/test_graph.py",
            "tests/engine/test_join_table.py",
            "tests/engine/test_scheduler.py",
            "tests/properties/test_scheduler_graphs.py",
        ),
        why="the join truth table: one wrong row silently skips a cleanup step",
    ),
    "scheduler": Target(
        name="scheduler",
        module="src/rayspec/engine/scheduler.py",
        tests=("tests/engine", "tests/properties/test_scheduler_graphs.py"),
        why="drain vs fail-fast, the teardown and the wind-down",
    ),
    "redact": Target(
        name="redact",
        module="src/rayspec/redact.py",
        tests=("tests/secrets", "tests/audit", "tests/plugins"),
        why="the one place a secret is stopped from reaching a file, a log or a sink",
    ),
    "policy_enforce": Target(
        name="policy_enforce",
        module="src/rayspec/policy/enforce.py",
        tests=("tests/policy", "tests/loader"),
        why="whether a policy violation stops a run, and whether the denial is real",
    ),
    "policy_controls": Target(
        name="policy_controls",
        module="src/rayspec/policy/controls.py",
        tests=("tests/policy",),
        why="the classification the provider_options allow-list hangs off — it has to be total",
    ),
    "approval_classes": Target(
        name="approval_classes",
        module="src/rayspec/engine/approval_classes.py",
        tests=("tests/approvals",),
        why="which gates an operator holds shut, and what may waive them",
    ),
}

#: What phase 2 runs: everything except the golden corpus. ``tests/golden`` is a byte-exact
#: snapshot of a real run — it is sensitive to the checkout it runs from and to host state a
#: parallel run can hold, so it makes a poor oracle for a stand-in package and would report
#: "killed elsewhere" for mutants nothing actually noticed. Anything it would have caught is
#: caught by the suites that produced the corpus in the first place.
CONFIRM_TESTS: tuple[str, ...] = ("tests", "--ignore=tests/golden")

#: Comparison swaps: each pair changes the verdict of a branch without changing its shape.
COMPARE_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

#: What a mutated string constant becomes — recognisable, and never a value the code expects.
MUTANT_STRING = "rayspec-mutant"

#: Mutation operators, in the order :func:`sites` reports them.
OPERATORS = ("compare", "boolop", "not", "bool", "str", "quantifier")


@dataclass(frozen=True)
class Site:
    """One mutable place in a module: where it is, what it is, and what it would become."""

    index: int
    line: int
    operator: str
    before: str
    after: str

    def describe(self, target: Target) -> str:
        """One report line: ``file:line  operator  before -> after``."""
        return f"{target.module}:{self.line}  {self.operator}  {self.before} -> {self.after}"


@dataclass
class Result:
    """The verdict on one mutant."""

    target: str
    site: Site
    status: str  # killed | survived | timeout | error
    seconds: float = 0.0
    detail: str = ""
    confirmed: bool = False


# --------------------------------------------------------------------------------------------------
# finding the sites
# --------------------------------------------------------------------------------------------------


def _parents(tree: ast.AST) -> Iterator[tuple[ast.AST, ast.AST, str, int | None]]:
    """``(node, parent, field, index)`` for every child node, in ``ast.walk`` order."""
    for parent in ast.walk(tree):
        for name, value in ast.iter_fields(parent):
            if isinstance(value, list):
                for position, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        yield item, parent, name, position
            elif isinstance(value, ast.AST):
                yield value, parent, name, None


def _inert_string_ids(tree: ast.AST) -> set[int]:
    """String constants whose value cannot change behaviour.

    Docstrings, ``__all__``, annotations and the arguments of a ``Literal[...]``. With
    ``from __future__ import annotations`` an annotation is never evaluated, and a ``Literal``
    alias only ever narrows a type, so mutating either produces a mutant nothing could kill —
    noise that would bury the survivors that matter.
    """
    inert: set[int] = set()

    def mark(node: ast.AST | None) -> None:
        if node is None:
            return
        for child in ast.walk(node):
            inert.add(id(child))

    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                inert.add(id(first.value))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            mark(node.returns)
            for arg in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]:
                mark(arg.annotation)
        if isinstance(node, ast.AnnAssign):
            mark(node.annotation)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            mark(node.value)
        if isinstance(node, ast.Subscript) and _name_of(node.value) == "Literal":
            mark(node.slice)
    return inert


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def sites(source: str, operators: frozenset[str] | None = None) -> list[Site]:
    """Every mutable place in ``source``, numbered — the numbering is what a report quotes.

    ``operators`` narrows which kinds of edit count as a site. It is part of the numbering, so
    :func:`mutate` must be given the same set: a run that mutates a subset is self-consistent,
    but its indexes mean nothing to a run that used a different subset.
    """
    tree = ast.parse(source)
    inert = _inert_string_ids(tree)
    found: list[Site] = []
    for node, _parent, _field, _position in _parents(tree):
        entry = _site_for(node, inert, operators)
        if entry is None:
            continue
        operator, before, after = entry
        found.append(
            Site(
                index=len(found),
                line=getattr(node, "lineno", 0),
                operator=operator,
                before=before,
                after=after,
            )
        )
    return found


def _site_for(
    node: ast.AST, inert: set[int], operators: frozenset[str] | None = None
) -> tuple[str, str, str] | None:
    """``(operator, before, after)`` when ``node`` is mutable (and enabled), else ``None``."""
    entry = _classify(node, inert)
    if entry is None or (operators is not None and entry[0] not in operators):
        return None
    return entry


def _classify(node: ast.AST, inert: set[int]) -> tuple[str, str, str] | None:
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        op = type(node.ops[0])
        if op in COMPARE_SWAP:
            return "compare", _op_text(op), _op_text(COMPARE_SWAP[op])
    if isinstance(node, ast.BoolOp):
        return ("boolop", "and", "or") if isinstance(node.op, ast.And) else ("boolop", "or", "and")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return "not", "not X", "X"
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return "bool", repr(node.value), repr(not node.value)
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in inert
        and node.value != MUTANT_STRING
    ):
        return "str", _short(repr(node.value)), repr(MUTANT_STRING)
    if isinstance(node, ast.Name) and node.id in {"any", "all"} and isinstance(node.ctx, ast.Load):
        return "quantifier", node.id, "all" if node.id == "any" else "any"
    return None


def _op_text(op: type[ast.cmpop]) -> str:
    return {
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.Is: "is",
        ast.IsNot: "is not",
        ast.In: "in",
        ast.NotIn: "not in",
    }[op]


def _short(text: str, limit: int = 48) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------------------------------
# applying one
# --------------------------------------------------------------------------------------------------


def mutate(source: str, index: int, operators: frozenset[str] | None = None) -> str:
    """The source of mutant ``index`` — the same numbering :func:`sites` produced.

    ``operators`` must match the set :func:`sites` was called with.
    """
    tree = ast.parse(source)
    inert = _inert_string_ids(tree)
    seen = -1
    for node, parent, field_name, position in _parents(tree):
        if _site_for(node, inert, operators) is None:
            continue
        seen += 1
        if seen != index:
            continue
        _apply(node, parent, field_name, position)
        return ast.unparse(tree)
    raise IndexError(f"no mutation site {index}")


def _apply(node: ast.AST, parent: ast.AST, field_name: str, position: int | None) -> None:
    if isinstance(node, ast.Compare):
        node.ops = [COMPARE_SWAP[type(node.ops[0])]()]
        return
    if isinstance(node, ast.BoolOp):
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return
    if isinstance(node, ast.UnaryOp):
        _replace(parent, field_name, position, node.operand)
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        _replace(parent, field_name, position, ast.Constant(value=not node.value))
        return
    if isinstance(node, ast.Constant):
        _replace(parent, field_name, position, ast.Constant(value=MUTANT_STRING))
        return
    if isinstance(node, ast.Name):
        _replace(
            parent,
            field_name,
            position,
            ast.Name(id="all" if node.id == "any" else "any", ctx=node.ctx),
        )
        return
    raise AssertionError(f"no mutation for {type(node).__name__}")


def _replace(parent: ast.AST, field_name: str, position: int | None, new: ast.AST) -> None:
    ast.fix_missing_locations(new)
    if position is None:
        setattr(parent, field_name, new)
    else:
        getattr(parent, field_name)[position] = new


def null_mutant(source: str) -> str:
    """``ast.unparse(ast.parse(source))`` — the round trip every mutant also goes through.

    Run against the tests before anything else: if the *unmutated* round trip fails, every
    survivor and every kill in the report is an artefact of the round trip, not of a mutation.
    """
    return ast.unparse(ast.parse(source))


# --------------------------------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------------------------------


@dataclass
class Workers:
    """A pool of stand-in packages, one per worker thread.

    A stand-in is ``<tmp>/wN/src/rayspec`` rebuilt as a tree of directories and **symlinks** to
    the real modules, with the one module under mutation replaced by a real file. Symlinks, not
    copies, because a copy is not a faithful stand-in: rayspec finds its example corpus at
    ``Path(__file__).resolve().parents[4] / "examples"``, and the golden corpus masks absolute
    paths by substituting the checkout's — both read the *resolved* path, which a symlink gives
    back correctly and a copy does not. The null-mutant check is what caught both; without it
    the whole report would have read "killed elsewhere" for reasons that had nothing to do with
    any mutation.

    The working tree is never written to. The mutated file is unlinked before it is written —
    writing *through* a symlink would edit the repository itself, which is the one mistake this
    design must not make.
    """

    root: Path
    count: int
    _free: queue.Queue[Path] = field(default_factory=queue.Queue)

    def start(self) -> None:
        """Build one stand-in package per worker."""
        for n in range(self.count):
            snapshot = self.root / f"w{n}"
            _mirror(REPO / "src" / "rayspec", snapshot / "src" / "rayspec")
            self._free.put(snapshot)

    def take(self) -> Path:
        """Claim a stand-in, blocking until one is free."""
        return self._free.get()

    def give(self, snapshot: Path) -> None:
        """Return a stand-in to the pool (the caller has already restored the mutated file)."""
        self._free.put(snapshot)

    def install(self, snapshot: Path, target: Target, source: str | None) -> None:
        """Put ``source`` at ``target``'s place in ``snapshot``; ``None`` restores the symlink."""
        file = snapshot / target.module
        file.unlink(missing_ok=True)  # never write through the link into the repository
        if source is None:
            file.symlink_to(target.path)
        else:
            file.write_text(source, encoding="utf-8")


def _mirror(source: Path, dest: Path) -> None:
    """Recreate ``source`` as directories plus symlinks to its files."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if entry.name == "__pycache__":
            continue
        if entry.is_dir():
            _mirror(entry, dest / entry.name)
        else:
            (dest / entry.name).symlink_to(entry)


def run_tests(snapshot: Path, tests: Sequence[str], timeout: float) -> tuple[str, str]:
    """Run ``tests`` against the package copy in ``snapshot``; ``(status, detail)``.

    ``status`` is ``passed`` (the mutant survived this selection), ``failed`` (killed) or
    ``timeout``. ``-x`` stops at the first failing test, which is all a kill needs.

    Two details are load-bearing rather than stylistic. Output goes to a **temporary file**, not
    a pipe: this suite starts subprocesses of its own, and a grandchild that outlives the test
    run keeps an inherited pipe open, which makes the timeout path block for ever waiting to
    drain it. And the run gets its **own process group**, killed as a group on timeout, so a
    mutant that wedges the engine cannot leave those grandchildren behind competing with every
    later mutant for the machine.
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(snapshot / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
    }
    command = [
        sys.executable,
        "-m",
        "pytest",
        *tests,
        "-x",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "-m",
        "not live",
    ]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            return "timeout", f"no verdict within {timeout:g}s"
        log.seek(0)
        output = log.read()
    if code == 0:
        return "passed", ""
    tail = [line for line in output.splitlines() if line.startswith(("FAILED", "ERROR"))]
    return "failed", tail[0] if tail else f"exit {code}"


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL the run's whole process group, then reap it."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=30)


def evaluate(
    target: Target,
    site: Site,
    source: str,
    workers: Workers,
    tests: Sequence[str],
    timeout: float,
    *,
    operators: frozenset[str] | None,
) -> Result:
    """Write one mutant into a worker's copy, run ``tests``, restore the copy, return the verdict."""
    snapshot = workers.take()
    started = time.monotonic()
    try:
        workers.install(snapshot, target, mutate(source, site.index, operators))
        status, detail = run_tests(snapshot, tests, timeout)
    except SyntaxError as exc:  # a mutation that does not compile is not a useful mutant
        status, detail = "error", f"{type(exc).__name__}: {exc}"
    finally:
        workers.install(snapshot, target, None)
        workers.give(snapshot)
    return Result(
        target=target.name,
        site=site,
        status={"passed": "survived", "failed": "killed"}.get(status, status),
        seconds=time.monotonic() - started,
        detail=detail,
    )


def run_target(
    target: Target,
    workers: Workers,
    *,
    jobs: int,
    limit: int | None,
    timeout: float,
    confirm: bool,
    operators: frozenset[str] | None = None,
    indexes: Sequence[int] | None = None,
) -> list[Result]:
    """Triage every mutant of ``target``, then confirm the survivors against the whole suite.

    ``indexes`` narrows the run to named sites — how a survivor from an earlier report is
    re-examined without paying for the whole target again.
    """
    source = target.path.read_text(encoding="utf-8")
    found = sites(source, operators)
    if indexes is not None:
        wanted = set(indexes)
        found = [site for site in found if site.index in wanted]
    if limit is not None:
        found = found[:limit]
    print(f"\n== {target.name}: {len(found)} mutant(s) in {target.module}", flush=True)
    print(f"   why it is here: {target.why}", flush=True)
    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            pool.submit(
                evaluate,
                target,
                site,
                source,
                workers,
                target.tests,
                timeout,
                operators=operators,
            )
            for site in found
        ]
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            print(
                f"   [{done}/{len(found)}] {result.status:9} {result.site.describe(target)}",
                flush=True,
            )
    survivors = [r for r in results if r.status == "survived"]
    if confirm and survivors:
        print(f"   confirming {len(survivors)} survivor(s) against the whole suite", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(
                    evaluate,
                    target,
                    r.site,
                    source,
                    workers,
                    CONFIRM_TESTS,
                    timeout * 4,
                    operators=operators,
                ): r
                for r in survivors
            }
            for future in concurrent.futures.as_completed(futures):
                original = futures[future]
                verdict = future.result()
                original.confirmed = verdict.status == "survived"
                if not original.confirmed:
                    original.status = "killed-elsewhere"
                    original.detail = verdict.detail
                print(
                    f"   {'SURVIVOR' if original.confirmed else 'killed elsewhere'}: "
                    f"{original.site.describe(target)}",
                    flush=True,
                )
    return results


# --------------------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------------------


def report(results: Sequence[Result]) -> str:
    """The human report: a score per target and every confirmed survivor with ``file:line``."""
    lines = ["", "=" * 96, "MUTATION REPORT", "=" * 96]
    for name, target in TARGETS.items():
        mine = [r for r in results if r.target == name]
        if not mine:
            continue
        killed = sum(1 for r in mine if r.status in {"killed", "timeout", "killed-elsewhere"})
        survivors = [r for r in mine if r.status == "survived"]
        errors = [r for r in mine if r.status == "error"]
        score = 100.0 * killed / max(1, len(mine) - len(errors))
        lines.append("")
        lines.append(
            f"{name} ({target.module}): {len(mine)} mutants, {killed} killed, "
            f"{len(survivors)} survived  ({score:.0f}% killed)"
        )
        for result in sorted(survivors, key=lambda r: r.site.line):
            lines.append(f"  SURVIVOR {result.site.describe(target)}")
    total = [r for r in results if r.status == "survived"]
    lines += ["", f"{len(total)} confirmed survivor(s) across {len(results)} mutant(s)", ""]
    return "\n".join(lines)


def _check_null_mutant(
    target: Target,
    source: str,
    workers: Workers,
    selections: Sequence[tuple[Sequence[str], float]],
) -> tuple[str, str]:
    """Run every selection that will produce a verdict against the UNMUTATED round trip.

    Both phases have to be checked, not just the first: the confirm phase runs a different (and
    much larger) selection, and a test anywhere in it that depends on the module's source text
    rather than its behaviour would mark every survivor "killed elsewhere" — a report that reads
    as full coverage and means nothing.
    """
    snapshot = workers.take()
    try:
        workers.install(snapshot, target, null_mutant(source))
        for tests, timeout in selections:
            status, detail = run_tests(snapshot, tests, timeout)
            if status != "passed":
                return status, f"{' '.join(tests)}: {detail}"
    finally:
        workers.install(snapshot, target, None)
        workers.give(snapshot)
    return "passed", ""


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; see the module docstring."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--target", action="append", choices=sorted(TARGETS), default=None)
    parser.add_argument("--jobs", type=int, default=4, help="mutants evaluated in parallel")
    parser.add_argument("--limit", type=int, default=None, help="stop after N mutants per target")
    parser.add_argument(
        "--index",
        type=int,
        action="append",
        default=None,
        help="run only these site indexes (repeatable); needs a single --target",
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="per-mutant triage timeout")
    parser.add_argument("--json", type=Path, default=None, help="write the raw results here")
    parser.add_argument("--list", action="store_true", help="list the mutants and exit")
    parser.add_argument("--no-confirm", action="store_true", help="skip the whole-suite phase")
    parser.add_argument(
        "--operators",
        default=",".join(OPERATORS),
        help=f"comma-separated subset of {','.join(OPERATORS)} (narrows the run)",
    )
    args = parser.parse_args(argv)
    chosen = [TARGETS[name] for name in (args.target or sorted(TARGETS))]
    operators = frozenset(name.strip() for name in args.operators.split(",") if name.strip())
    unknown = operators - set(OPERATORS)
    if unknown:
        parser.error(f"unknown operator(s): {', '.join(sorted(unknown))}")
    if args.index and len(chosen) != 1:
        parser.error("--index needs exactly one --target (indexes are per module)")

    if args.list:
        for target in chosen:
            found = sites(target.path.read_text(encoding="utf-8"), operators)
            print(f"{target.name}: {len(found)} mutants in {target.module}")
            for site in found:
                print(f"  {site.index:4}  {site.describe(target)}")
        return 0

    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="rayspec-mutation-") as tmp:
        workers = Workers(root=Path(tmp), count=max(args.jobs, 2))
        workers.start()
        for target in chosen:
            source = target.path.read_text(encoding="utf-8")
            selections = [(target.tests, args.timeout * 2)]
            if not args.no_confirm:
                selections.append((CONFIRM_TESTS, args.timeout * 8))
            status, detail = _check_null_mutant(target, source, workers, selections)
            if status != "passed":
                print(f"!! null mutant of {target.name} does not pass ({status}: {detail})")
                print("   every verdict for this target would be an artefact; skipping it")
                continue
            results += run_target(
                target,
                workers,
                jobs=args.jobs,
                limit=args.limit,
                timeout=args.timeout,
                confirm=not args.no_confirm,
                operators=operators,
                indexes=args.index,
            )
    text = report(results)
    print(text)
    if args.json is not None:
        args.json.write_text(
            json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

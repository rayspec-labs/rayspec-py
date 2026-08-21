# SPDX-License-Identifier: Apache-2.0
"""Declared ``artifacts:`` — the files a step promised to write.

Module boundary: the check that runs once a step has SUCCEEDED, between the executor and the
scheduler's :func:`~rayspec.engine.scheduler.finish`. It resolves each declared path against the
step's working directory, fails the step when the promise was not kept, and hands the files to
:meth:`~rayspec.engine.context.RunContext.write_artifacts`, which copies them into the run
directory through the store.

The promise is about a PATH. Nothing here reads an artifact's content into a record, an event, a
template context or an output — the engine only ever learns that a file exists, where it is and
what its stored copy hashes to. A path that resolves outside the working directory (a symlink
planted by the step) is refused: the schema already rejects ``..`` and absolute paths at load
time, this is the run-time half of the same rule.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from rayspec.engine.context import ExecScope, RunContext, StepOutcome
from rayspec.engine.executors._process import resolve_cwd
from rayspec.schema import PythonStep, ShellStep, StepModel, StepStatus
from rayspec.store.model import ErrorInfo

#: ``ErrorInfo.type`` of every broken artifact promise.
ARTIFACT_ERROR_TYPE = "artifact"


def artifact_dir(step: StepModel, scope: ExecScope, ctx: RunContext) -> Path:
    """The step's working directory: its rendered ``cwd:`` for shell/python, else the workdir."""
    if isinstance(step, ShellStep | PythonStep) and step.cwd is not None:
        return resolve_cwd(step, ctx, ctx.template_context(scope))
    return ctx.workdir


async def collect_artifacts(
    step: StepModel, scope: ExecScope, ctx: RunContext, outcome: StepOutcome
) -> StepOutcome:
    """Check the step's declared artifacts and persist them; returns the (possibly failed) outcome.

    A no-op unless the step declared ``artifacts:`` and actually succeeded in this run — a
    replayed record keeps the artifacts it was recorded with, and a dry run checks nothing
    (nothing was really produced). A declared file that is missing, is a directory or resolves
    outside the working directory fails the step with a reason naming the path.
    """
    record = outcome.record
    if not step.artifacts or outcome.reused or record.status is not StepStatus.SUCCEEDED:
        return outcome
    if ctx.options.dry_run:
        return outcome  # a rehearsal produces no files; checking them would fail every dry run
    try:
        cwd = artifact_dir(step, scope, ctx)
    except Exception as exc:  # a cwd: that no longer renders is the step's failure, not a crash
        return _failed(outcome, f"cannot resolve the working directory of the step: {exc}")
    found: list[tuple[str, Path]] = []
    for declared in step.artifacts:
        path = cwd / Path(*PurePosixPath(declared).parts)
        problem = _problem(declared, path, cwd)
        if problem is not None:
            return _failed(outcome, problem)
        found.append((declared, path))
    await ctx.write_artifacts(record, found)
    return outcome


def _problem(declared: str, path: Path, cwd: Path) -> str | None:
    """Why ``declared`` is not a kept promise, or ``None`` when it is."""
    try:
        resolved = path.resolve(strict=False)
        inside = resolved.is_relative_to(cwd.resolve(strict=False))
    except OSError as exc:  # a broken mount, a path that cannot be resolved
        return f"declared artifact {declared!r} cannot be read: {exc}"
    if not inside:
        return (
            f"declared artifact {declared!r} resolves outside the step's working directory "
            f"({cwd}) — an artifact must be a file the step wrote inside its own workspace"
        )
    if not path.exists():
        return (
            f"declared artifact {declared!r} was not written (looked in {cwd}) — write it, or "
            "drop it from the step's artifacts:"
        )
    if path.is_dir():
        return f"declared artifact {declared!r} is a directory, not a file"
    return None


def _failed(outcome: StepOutcome, message: str) -> StepOutcome:
    """Turn the succeeded outcome into a failed one: the promise is the point of the feature.

    The step's own output (a script's stdout, an agent's answer) is kept — it is usually the
    first thing someone reads when asking why the file is missing.
    """
    record = outcome.record
    record.status = StepStatus.FAILED
    record.ok = False
    record.error = ErrorInfo(type=ARTIFACT_ERROR_TYPE, message=message, transient=False)
    return outcome


__all__ = ["ARTIFACT_ERROR_TYPE", "artifact_dir", "collect_artifacts"]

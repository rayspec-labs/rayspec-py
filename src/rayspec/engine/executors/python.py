# SPDX-License-Identifier: Apache-2.0
"""``python:`` executor: render → ``sys.executable -`` (or ``uv run --with <deps> python -``).

Module boundary: mirrors the shell executor with the python templating environment
(``{{ }}`` → Python literals) and the script fed through stdin.
"""

from __future__ import annotations

import sys

from rayspec.engine.context import ExecScope, RunContext, StepOutcome
from rayspec.engine.executors._process import (
    cleanup_spills,
    dry_run_outcome,
    finish_script_outcome,
    process_env,
    render_failure,
    resolve_cwd,
    run_process,
    script_fingerprint,
)
from rayspec.engine.executors.shell import env_subset
from rayspec.engine.paths import StepPath
from rayspec.schema import PythonStep, StepModel
from rayspec.store.model import StepRecord
from rayspec.templating import TemplateRenderError


def python_command(step: PythonStep) -> list[str]:
    """``uv run --with <dep>... python -`` when ``deps`` are set, else ``sys.executable -``."""
    if step.deps:
        cmd = ["uv", "run", "--no-project"]
        for dep in step.deps:
            cmd += ["--with", dep]
        return [*cmd, "python", "-"]
    return [sys.executable, "-"]


def python_fingerprint(step: PythonStep, scope: ExecScope, ctx: RunContext) -> str:
    tctx = ctx.template_context(scope)
    rendered = ctx.engine.render_python(step.python, tctx, spill_dir=ctx.tmp_dir)
    try:
        cwd = resolve_cwd(step, ctx, tctx)
        env = ctx.render_env(step.env, tctx)
        return script_fingerprint("python", rendered, cwd, {**env, "deps": ",".join(step.deps)})
    finally:
        cleanup_spills(rendered)


async def run_python(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, attempt: int
) -> StepOutcome:
    """One attempt of a ``python:`` step."""
    assert isinstance(step, PythonStep)
    path = StepPath.parse(record.path)
    if ctx.options.dry_run and not ctx.options.exec_shell:
        return dry_run_outcome(step, record)
    tctx = ctx.template_context(scope)
    try:
        rendered = ctx.engine.render_python(step.python, tctx, spill_dir=ctx.tmp_dir)
    except TemplateRenderError as exc:
        return render_failure(record, exc)
    try:
        cwd = resolve_cwd(step, ctx, tctx)
        if not cwd.is_dir():
            raise FileNotFoundError(f"cwd does not exist: {cwd}")
        env = process_env(step, ctx, tctx, rendered, path, scope=scope)
        own = env_subset(step, ctx.render_env(step.env, tctx))  # redacted, see shell.py
        record.fingerprint = script_fingerprint(
            "python", rendered, cwd, {**own, "deps": ",".join(step.deps)}
        )
        result = await run_process(
            python_command(step),
            cwd=cwd,
            env=env,
            stdin_text=rendered.script,
            ctx=ctx,
            path=path,
            attempt=attempt,
        )
    except (TemplateRenderError, OSError) as exc:
        return render_failure(record, exc)
    finally:
        cleanup_spills(rendered)
    return finish_script_outcome(step, record, result)


__all__ = ["python_command", "python_fingerprint", "run_python"]

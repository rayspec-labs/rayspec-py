# SPDX-License-Identifier: Apache-2.0
"""``shell:`` executor: render → ``bash -euo pipefail`` / ``sh -eu`` → stdout as output.

Module boundary: templating (``render_shell``: ``{{ }}`` → ``${RAYSPEC_V<n>}`` slots, spills
into ``<run>/tmp``), the shared process runner and the output mapping
(:func:`~rayspec.engine.executors._process.finish_script_outcome`). Non-zero exit ⇒ failed
(``allow_failure`` tolerates it in the scheduler); ``output_schema`` ⇒ stdout parsed + validated.
"""

from __future__ import annotations

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
from rayspec.engine.paths import StepPath
from rayspec.schema import PythonStep, ShellStep, StepModel
from rayspec.store.model import StepRecord
from rayspec.templating import TemplateRenderError

INTERPRETERS: dict[str, tuple[str, ...]] = {
    "bash": ("bash", "-euo", "pipefail", "-c"),
    "sh": ("sh", "-eu", "-c"),
}


def shell_command(step: ShellStep, script: str) -> list[str]:
    """The argv for ``step.interpreter`` running ``script``."""
    return [*INTERPRETERS[step.interpreter], script]


def shell_fingerprint(step: ShellStep, scope: ExecScope, ctx: RunContext) -> str:
    """Fingerprint of the rendered script + env (resume ``--force`` comparison)."""
    tctx = ctx.template_context(scope)
    rendered = ctx.engine.render_shell(step.shell, tctx, spill_dir=ctx.tmp_dir)
    try:
        cwd = resolve_cwd(step, ctx, tctx)
        env = ctx.render_env(step.env, tctx)
        return script_fingerprint("shell", rendered, cwd, env)
    finally:
        cleanup_spills(rendered)


async def run_shell(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, attempt: int
) -> StepOutcome:
    """One attempt of a ``shell:`` step."""
    assert isinstance(step, ShellStep)
    path = StepPath.parse(record.path)
    if ctx.options.dry_run and not ctx.options.exec_shell:
        return dry_run_outcome(step, record)
    tctx = ctx.template_context(scope)
    try:
        rendered = ctx.engine.render_shell(step.shell, tctx, spill_dir=ctx.tmp_dir)
    except TemplateRenderError as exc:
        return render_failure(record, exc)
    try:
        cwd = resolve_cwd(step, ctx, tctx)
        if not cwd.is_dir():
            raise FileNotFoundError(f"cwd does not exist: {cwd}")
        env = process_env(step, ctx, tctx, rendered, path, scope=scope)
        # the fingerprint is persisted: hash the env mapping rendered from the REDACTED context
        # (``<secret>`` placeholders), never the real secret values
        record.fingerprint = script_fingerprint(
            "shell", rendered, cwd, env_subset(step, ctx.render_env(step.env, tctx))
        )
        result = await run_process(
            shell_command(step, rendered.script),
            cwd=cwd,
            env=env,
            stdin_text=None,
            ctx=ctx,
            path=path,
            attempt=attempt,
        )
    except (TemplateRenderError, OSError) as exc:
        return render_failure(record, exc)
    finally:
        cleanup_spills(rendered)
    return finish_script_outcome(step, record, result)


def env_subset(step: ShellStep | PythonStep, env: dict[str, str]) -> dict[str, str]:
    """Only the step's own ``env:`` keys (the process env includes the whole os.environ)."""
    return {k: env[k] for k in step.env if k in env}


__all__ = ["INTERPRETERS", "run_shell", "shell_command", "shell_fingerprint"]

# SPDX-License-Identifier: Apache-2.0
"""Step executors, one module per kind, sharing the signature
``async (step, scope, ctx, record, attempt) -> StepOutcome``.

Module boundary: :func:`default_executors` is the dispatch table the scheduler uses (tests may
override entries through ``RunContext.executors``); :func:`fingerprint_of` computes the leaf
fingerprint used by resume ``--force``.
"""

from __future__ import annotations

from rayspec.engine.context import ExecScope, ExecutorFn, RunContext
from rayspec.engine.executors.approve import run_approve
from rayspec.engine.executors.each import run_each
from rayspec.engine.executors.include import run_include
from rayspec.engine.executors.loop import run_loop
from rayspec.engine.executors.prompt import prompt_fingerprint, run_prompt
from rayspec.engine.executors.python import python_fingerprint, run_python
from rayspec.engine.executors.shell import run_shell, shell_fingerprint
from rayspec.engine.executors.stop import run_stop
from rayspec.schema import PromptStep, PythonStep, ShellStep, StepModel


def default_executors() -> dict[str, ExecutorFn]:
    """kind → executor coroutine function."""
    return {
        "prompt": run_prompt,
        "shell": run_shell,
        "python": run_python,
        "stop": run_stop,
        "approve": run_approve,
        "loop": run_loop,
        "each": run_each,
        "include": run_include,
    }


def fingerprint_of(step: StepModel, scope: ExecScope, ctx: RunContext) -> str | None:
    """sha256 of the rendered prompt/script + resolved agent for leaf steps (else ``None``)."""
    if isinstance(step, PromptStep):
        return prompt_fingerprint(step, scope, ctx)
    if isinstance(step, ShellStep):
        return shell_fingerprint(step, scope, ctx)
    if isinstance(step, PythonStep):
        return python_fingerprint(step, scope, ctx)
    return None


__all__ = [
    "default_executors",
    "fingerprint_of",
    "run_approve",
    "run_each",
    "run_include",
    "run_loop",
    "run_prompt",
    "run_python",
    "run_shell",
    "run_stop",
]

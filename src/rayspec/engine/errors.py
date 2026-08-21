# SPDX-License-Identifier: Apache-2.0
"""Engine control-flow exceptions.

Module boundary: these are the only exceptions the engine lets escape a step on purpose.
``RunStopped`` (a ``stop:`` step or a rejected gate) and ``RunPaused`` (an approval gate that
could not be answered in-process) travel from the executor through the scheduler up to the
runner, which turns them into the final run status. Everything else a step raises becomes a
*failed outcome*, never a crash.
"""

from __future__ import annotations

from rayspec.errors import RayspecError


class EngineError(RayspecError):
    """Base class for engine-level errors raised to the CLI (bad graph, resume refused…)."""


class GraphError(EngineError):
    """A sibling list is not a valid DAG (unknown/non-sibling ``needs``, cycles)."""


class ResumeError(EngineError):
    """``--resume`` cannot proceed (unknown run, hash mismatch without ``--force``…)."""


class RunControl(Exception):
    """Base of the control-flow exceptions (not a :class:`RayspecError`: never an error)."""


class RunStopped(RunControl):
    """A ``stop:`` step (or an ``on_reject: cancel`` gate) ended the run early."""

    def __init__(self, status: str, reason: str | None = None, *, step_path: str = ""):
        super().__init__(reason or f"stopped ({status})")
        self.status = status
        self.reason = reason
        self.step_path = step_path


class RunPaused(RunControl):
    """An approval gate paused the run (``exit 3``): ``token`` = ``<path>#<attempt>``."""

    def __init__(self, token: str, step_path: str, message: str):
        super().__init__(f"paused at {step_path}: {message}")
        self.token = token
        self.step_path = step_path
        self.message = message


__all__ = ["EngineError", "GraphError", "ResumeError", "RunControl", "RunPaused", "RunStopped"]

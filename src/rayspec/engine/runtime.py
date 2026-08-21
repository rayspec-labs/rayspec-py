# SPDX-License-Identifier: Apache-2.0
"""Concurrency substrate of the engine — anyio only.

Module boundary: no knowledge of steps, stores or providers. Provides

* :func:`run_with_signals` — runs a coroutine under a root ``CancelScope`` with SIGINT/SIGTERM
  handling (first SIGINT/SIGTERM → cancel the root scope so every step unwinds through
  anyio-originated cancellation and the SDKs' shielded cleanup paths are honoured; second
  SIGINT → synchronous flush hook + hard exit 130);
* :class:`Runtime` — the shared primitives: leaf ``CapacityLimiter(max_parallel)``, the launch
  gate (an ``anyio.Event`` that approval gates close to quiesce the run), the active-leaf
  counter and the approval lock (simultaneous gates are handled one at a time);
* executor sizing (:func:`default_executor_workers`, :func:`configure_default_executor`);
* :func:`unwrap_exception_group` and the exit-code mapping.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Generic, TypeVar

import anyio
from anyio import to_thread

from rayspec.schema import RunStatus

T = TypeVar("T")

EXIT_SUCCEEDED = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PAUSED = 3
EXIT_CANCELLED = 4
EXIT_INTERRUPTED = 130

#: Run status → process exit code (``running`` should never be final; it maps to failed).
EXIT_CODES: dict[RunStatus, int] = {
    RunStatus.SUCCEEDED: EXIT_SUCCEEDED,
    RunStatus.FAILED: EXIT_FAILED,
    RunStatus.PAUSED: EXIT_PAUSED,
    RunStatus.CANCELLED: EXIT_CANCELLED,
    RunStatus.INTERRUPTED: EXIT_INTERRUPTED,
    RunStatus.RUNNING: EXIT_FAILED,
}


def exit_code_for(status: RunStatus | str) -> int:
    """Map a final run status to the CLI exit code."""
    return EXIT_CODES.get(RunStatus(status), EXIT_FAILED)


def default_executor_workers(max_parallel: int) -> int:
    """Default-executor size: ``max(32, 2 * max_parallel + 8)`` (the Codex SDK pins threads)."""
    return max(32, 2 * max_parallel + 8)


#: The executor rayspec installed on a loop (+ its size), so repeated runs on the same loop
#: (``rayspec approve`` → resume, tests, long-lived hosts) reuse it instead of leaking pools.
_INSTALLED_EXECUTORS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[ThreadPoolExecutor, int]
] = weakref.WeakKeyDictionary()


def installed_default_executor(loop: asyncio.AbstractEventLoop) -> ThreadPoolExecutor | None:
    """The default executor :func:`configure_default_executor` installed on ``loop`` (if any)."""
    entry = _INSTALLED_EXECUTORS.get(loop)
    return entry[0] if entry is not None else None


def configure_default_executor(max_parallel: int) -> int:
    """Size the running loop's default executor and anyio's thread limiter; returns the size.

    Must be called from inside the running event loop. Safe to call more than once: a pool
    rayspec installed earlier on this loop is kept when it is big enough, otherwise it is
    shut down (``wait=False``) and replaced — never leaked.
    """
    workers = default_executor_workers(max_parallel)
    loop = asyncio.get_running_loop()
    previous = _INSTALLED_EXECUTORS.get(loop)
    if previous is None or previous[1] < workers:
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rayspec")
        try:
            loop.set_default_executor(executor)
        except RuntimeError:  # executor already in use by the loop: keep the loop's own
            executor.shutdown(wait=False)
        else:
            if previous is not None:
                previous[0].shutdown(wait=False)
            _INSTALLED_EXECUTORS[loop] = (executor, workers)
    to_thread.current_default_thread_limiter().total_tokens = workers
    return workers


def unwrap_exception_group(exc: BaseException) -> BaseException:
    """Peel single-leaf ``ExceptionGroup`` wrappers (task groups) down to the real exception."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


class Runtime:
    """Shared concurrency primitives of one run.

    * ``leaf_permit()`` — acquired ONLY around prompt/shell/python executors: waits for the
      launch gate, then a slot of ``CapacityLimiter(max_parallel)``, and counts the leaf as
      active for the duration;
    * ``close_gate()`` / ``open_gate()`` — approval quiesce: no new leaf starts anywhere while
      the gate is closed; ``wait_quiesced()`` returns once no leaf is active;
    * ``approval_lock`` — held by an ``approve:`` step from closing the gate until its decision
      (or pause) is recorded, so simultaneous gates quiesce and prompt one at a time.
    """

    def __init__(self, max_parallel: int) -> None:
        self.max_parallel = max(1, int(max_parallel))
        self.leaf_limiter = anyio.CapacityLimiter(self.max_parallel)
        self.approval_lock = anyio.Lock()
        self.active_leaves = 0
        self._gate = anyio.Event()
        self._gate.set()
        self._idle = anyio.Event()
        self._idle.set()

    # -- launch gate ----------------------------------------------------------------------

    @property
    def gate_open(self) -> bool:
        return self._gate.is_set()

    def close_gate(self) -> None:
        """Stop launching new leaves (an ``anyio.Event`` cannot be cleared: a fresh one)."""
        if self._gate.is_set():
            self._gate = anyio.Event()

    def open_gate(self) -> None:
        """Allow leaves to launch again."""
        self._gate.set()

    async def wait_launch(self) -> None:
        """Block while the gate is closed."""
        await self._gate.wait()

    async def wait_quiesced(self) -> None:
        """Return once no leaf executor is active.

        Loops: a leaf that had already passed the launch gate and was waiting for a limiter
        slot may grab it right as the last active leaf finishes, so the idle event alone is
        not proof — re-check ``active_leaves`` (and the current event) until it is really 0.
        """
        while True:
            await self._idle.wait()
            if self.active_leaves == 0 and self._idle.is_set():
                return
            await anyio.sleep(0)

    # -- leaf permits ---------------------------------------------------------------------

    @asynccontextmanager
    async def leaf_permit(self) -> AsyncIterator[None]:
        """Gate + ``max_parallel`` slot + active-leaf accounting around one leaf attempt."""
        await self.wait_launch()
        async with self.leaf_limiter:
            self.active_leaves += 1
            if self._idle.is_set():
                self._idle = anyio.Event()
            try:
                yield
            finally:
                self.active_leaves -= 1
                if self.active_leaves == 0:
                    self._idle.set()


@dataclass(slots=True)
class SignalResult(Generic[T]):
    """Outcome of :func:`run_with_signals`: the body's value (``None`` when interrupted)."""

    value: T | None
    interrupted: bool
    signal: int | None


def _default_hard_exit(code: int) -> None:  # pragma: no cover - process ends
    os._exit(code)


async def run_with_signals(
    body: Callable[[], Awaitable[T]],
    *,
    on_hard_exit: Callable[[], None] | None = None,
    hard_exit: Callable[[int], None] = _default_hard_exit,
    handle_signals: bool = True,
) -> SignalResult[T]:
    """Run ``body`` under a root ``CancelScope`` with SIGINT/SIGTERM handling.

    First SIGINT or SIGTERM → the root scope is cancelled (the result reports ``interrupted``);
    a second SIGINT → ``on_hard_exit()`` (synchronous flush) then ``hard_exit(130)``.
    ``handle_signals=False`` (or a non-main thread / platform without signal support) runs the
    body without handlers.
    """
    root = anyio.CancelScope()
    received: list[int] = []
    value: T | None = None

    async def watch() -> None:
        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for sig in signals:
                if received and sig == signal.SIGINT:
                    if on_hard_exit is not None:
                        with contextlib.suppress(Exception):  # best effort before hard exit
                            on_hard_exit()
                    hard_exit(EXIT_INTERRUPTED)
                    return
                received.append(int(sig))
                root.cancel()

    can_watch = (
        handle_signals
        and sys.platform != "win32"
        and threading.current_thread() is threading.main_thread()
    )
    async with anyio.create_task_group() as tg:
        if can_watch:
            tg.start_soon(watch)
        with root:
            value = await body()
        tg.cancel_scope.cancel()
    interrupted = root.cancelled_caught or bool(received)
    return SignalResult(
        value=None if interrupted else value,
        interrupted=interrupted,
        signal=received[0] if received else None,
    )


__all__ = [
    "EXIT_CANCELLED",
    "EXIT_CODES",
    "EXIT_FAILED",
    "EXIT_INTERRUPTED",
    "EXIT_PAUSED",
    "EXIT_SUCCEEDED",
    "EXIT_USAGE",
    "Runtime",
    "SignalResult",
    "configure_default_executor",
    "default_executor_workers",
    "exit_code_for",
    "installed_default_executor",
    "run_with_signals",
    "unwrap_exception_group",
]

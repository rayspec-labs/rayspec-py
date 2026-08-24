"""engine.runtime: exit codes, exception-group unwrapping, gate/limiter, signal handling."""

from __future__ import annotations

import os
import signal
from typing import Any

import anyio
import pytest

from rayspec.engine.runtime import (
    EXIT_CANCELLED,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_PAUSED,
    EXIT_SUCCEEDED,
    EXIT_USAGE,
    Runtime,
    SignalResult,
    default_executor_workers,
    exit_code_for,
    run_with_signals,
    unwrap_exception_group,
)
from rayspec.schema import RunStatus

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (RunStatus.SUCCEEDED, 0),
        (RunStatus.FAILED, 1),
        (RunStatus.PAUSED, 3),
        (RunStatus.CANCELLED, 4),
        (RunStatus.INTERRUPTED, 130),
        (RunStatus.RUNNING, 1),
    ],
)
async def test_exit_code_mapping(status: RunStatus, code: int) -> None:
    assert exit_code_for(status) == code
    assert (EXIT_SUCCEEDED, EXIT_FAILED, EXIT_USAGE, EXIT_PAUSED, EXIT_CANCELLED) == (0, 1, 2, 3, 4)
    assert EXIT_INTERRUPTED == 130


def test_default_executor_workers() -> None:
    assert default_executor_workers(1) == 32
    assert default_executor_workers(4) == 32
    assert default_executor_workers(20) == 48


def test_unwrap_exception_group() -> None:
    inner = ValueError("x")
    group = ExceptionGroup("g", [ExceptionGroup("h", [inner])])
    assert unwrap_exception_group(group) is inner
    two = ExceptionGroup("g", [ValueError("a"), ValueError("b")])
    assert unwrap_exception_group(two) is two
    assert unwrap_exception_group(inner) is inner


async def test_leaf_permit_limits_concurrency_and_counts_active() -> None:
    rt = Runtime(max_parallel=2)
    running = 0
    peak = 0

    async def leaf() -> None:
        nonlocal running, peak
        async with rt.leaf_permit():
            running += 1
            peak = max(peak, running)
            assert rt.active_leaves == running
            await anyio.sleep(0.01)
            running -= 1

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(leaf)
    assert peak == 2
    assert rt.active_leaves == 0
    await rt.wait_quiesced()  # idle → returns immediately


async def test_launch_gate_blocks_new_leaves_until_reopened() -> None:
    rt = Runtime(max_parallel=4)
    started = anyio.Event()
    rt.close_gate()

    async def leaf() -> None:
        async with rt.leaf_permit():
            started.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(leaf)
        await anyio.sleep(0.02)
        assert not started.is_set()
        rt.open_gate()
        await started.wait()


async def test_wait_quiesced_waits_for_active_leaves() -> None:
    rt = Runtime(max_parallel=4)
    release = anyio.Event()
    quiesced = anyio.Event()

    async def leaf() -> None:
        async with rt.leaf_permit():
            await release.wait()

    async def waiter() -> None:
        await rt.wait_quiesced()
        quiesced.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(leaf)
        await anyio.sleep(0.01)
        tg.start_soon(waiter)
        await anyio.sleep(0.01)
        assert not quiesced.is_set()
        release.set()
        await quiesced.wait()


async def test_run_with_signals_returns_body_result() -> None:
    async def body() -> str:
        return "done"

    result = await run_with_signals(body)
    assert result.value == "done"
    assert result.interrupted is False
    assert result.signal is None


async def test_first_sigint_cancels_root_scope_and_reports_interrupted() -> None:
    cancelled_seen = anyio.Event()

    async def body() -> str:
        try:
            await anyio.sleep(10)
        except anyio.get_cancelled_exc_class():
            cancelled_seen.set()
            raise
        return "never"

    async def fire() -> None:
        await anyio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    result: SignalResult[Any] | None = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(fire)
        result = await run_with_signals(body)
    assert result is not None
    assert result.interrupted is True
    assert result.signal == signal.SIGINT
    assert result.value is None
    assert cancelled_seen.is_set()


async def test_sigterm_also_cancels() -> None:
    async def body() -> None:
        await anyio.sleep(10)

    async def fire() -> None:
        await anyio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    result: SignalResult[Any] | None = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(fire)
        result = await run_with_signals(body)
    assert result is not None
    assert result.interrupted is True
    assert result.signal == signal.SIGTERM


async def test_second_sigint_calls_hard_exit_hook() -> None:
    """Ctrl-C twice: the first cancels, the second flushes and hard-exits.

    Both deliveries are sequenced on observed state. Standard signals are not queued, so a
    second SIGINT sent while the first is still pending is dropped and the runtime only ever
    sees one — sleeping between the two ``os.kill`` calls is a bet against that, and it loses
    (roughly one run in twenty under heavy CPU oversubscription, as ``flushed == []``).
    """
    hard_exits: list[int] = []
    flushed: list[str] = []
    root_cancelled = anyio.Event()
    hard_exited = anyio.Event()
    before = signal.getsignal(signal.SIGINT)

    async def body() -> None:
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            root_cancelled.set()  # the first signal reached the root scope
            # ignore that cancellation on purpose (shielded) so the second signal has a run to
            # interrupt, and stay open until the hard-exit path has been through. The guard is
            # a hang detector: the real wait is the handler's next turn of the loop, so it
            # gives up only when the second signal produced nothing, and the assertions below
            # then say so.
            with anyio.CancelScope(shield=True), anyio.move_on_after(5):
                await hard_exited.wait()
            raise

    def hard_exit(code: int) -> None:
        hard_exits.append(code)
        hard_exited.set()

    async def fire() -> None:
        while signal.getsignal(signal.SIGINT) is before:
            # the receiver installs its own SIGINT handler as it opens; a signal fired before
            # that goes to whatever the test runner had installed instead of to the runtime
            await anyio.sleep(0)
        os.kill(os.getpid(), signal.SIGINT)
        await root_cancelled.wait()  # delivered and acted on — the next one cannot coalesce
        os.kill(os.getpid(), signal.SIGINT)

    result: SignalResult[Any] | None = None
    with anyio.fail_after(30):  # hang detector for the unshielded waits above
        async with anyio.create_task_group() as tg:
            tg.start_soon(fire)
            result = await run_with_signals(
                body, on_hard_exit=lambda: flushed.append("flush"), hard_exit=hard_exit
            )
    assert result is not None
    assert result.interrupted is True
    assert flushed == ["flush"]
    assert hard_exits == [EXIT_INTERRUPTED]


async def test_configure_default_executor_does_not_leak_pools() -> None:
    import asyncio

    from rayspec.engine.runtime import configure_default_executor, installed_default_executor

    loop = asyncio.get_running_loop()
    configure_default_executor(1)
    first = installed_default_executor(loop)
    assert first is not None
    configure_default_executor(1)  # same size: the pool is kept
    assert installed_default_executor(loop) is first
    configure_default_executor(100)  # bigger: a new pool replaces (and shuts down) the old one
    second = installed_default_executor(loop)
    assert second is not None and second is not first
    with pytest.raises(RuntimeError):  # the previous pool was shut down
        first.submit(lambda: None)
    assert second.submit(lambda: 1).result(timeout=5) == 1

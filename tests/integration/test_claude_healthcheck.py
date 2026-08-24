"""Claude healthcheck: the ``claude -v`` probe tolerates one slow/failed attempt.

Drives the real :mod:`rayspec.providers.claude` module with a fake CLI script — no SDK subprocess,
no network.

Two kinds of double are used on purpose. The retry-on-no-version path runs a real fake CLI, with
the per-attempt timeout left at its shipped 5 s so that no fork+exec can reach it: the attempt
count is then decided by the probe, not by how loaded the machine is. The retry-on-*timeout* path
is driven by a recording ``anyio.run_process`` that raises ``TimeoutError`` outright — a spawn
that is slow enough to time out on purpose is a spawn slow enough to time out by accident.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from rayspec.providers import claude as claude_mod
from rayspec.providers.claude import ClaudeProvider, cli_version_of

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI is a /bin/sh script"),
]


def _flaky_cli(tmp_path: Path, *, fail_first: int, version: str = "3.4.5") -> Path:
    """A CLI that reports no version for the first ``fail_first`` calls, then prints ``version``.

    The failure mode is a fast non-zero exit rather than a hang: ``cli_version_of`` retries an
    attempt that reported no version exactly as it retries a timed-out one, and a child that
    always runs to completion writes its counter instead of racing a killer for it.
    """
    counter = tmp_path / "calls"
    counter.write_text("0")
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter})\n"
        f"echo $((n + 1)) > {counter}\n"
        f'if [ "$n" -lt {fail_first} ]; then echo "unrecognised option" >&2; exit 1; fi\n'
        f"echo '{version} (Claude Code)'\n"
    )
    cli.chmod(0o755)
    return cli


def _calls(tmp_path: Path) -> int:
    return int((tmp_path / "calls").read_text().strip() or 0)


#: A fork+exec of the two-line script above costs single-digit ms; the shipped per-attempt
#: timeout is 5 s. Passing it explicitly keeps the probe's deadline unreachable, so every
#: assertion below is about the probe's control flow and none is about this machine's load.
UNREACHABLE_TIMEOUT_S = 5.0


def _recording_run_process(
    monkeypatch: pytest.MonkeyPatch, *outcomes: BaseException | str
) -> list[list[str]]:
    """Replace ``anyio.run_process`` with a double that plays ``outcomes`` in order.

    A ``BaseException`` outcome is raised, a ``str`` is returned as the process' stdout (an
    exhausted script keeps returning empty stdout). Nothing is spawned, so the attempt count is
    decided by ``cli_version_of``'s control flow rather than by whether a real shell got
    scheduled before a deadline.
    """
    calls: list[list[str]] = []
    pending = list(outcomes)

    async def run_process(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        calls.append(list(cmd))
        outcome = pending.pop(0) if pending else ""
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(returncode=0, stdout=outcome.encode(), stderr=b"")

    monkeypatch.setattr(anyio, "run_process", run_process)
    return calls


async def test_cli_version_of_retries_once_after_a_failed_attempt(tmp_path: Path) -> None:
    cli = _flaky_cli(tmp_path, fail_first=1)
    assert await cli_version_of(str(cli), timeout_s=UNREACHABLE_TIMEOUT_S, retries=1) == "3.4.5"
    assert _calls(tmp_path) == 2


async def test_cli_version_of_retries_once_after_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out attempt is retried — with the timeout injected, not waited for."""
    calls = _recording_run_process(monkeypatch, TimeoutError(), "3.4.5 (Claude Code)")
    assert await cli_version_of("/nonexistent/claude", timeout_s=1.0, retries=1) == "3.4.5"
    assert len(calls) == 2


async def test_cli_version_of_gives_up_after_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recording_run_process(monkeypatch, TimeoutError(), TimeoutError(), TimeoutError())
    assert await cli_version_of("/nonexistent/claude", timeout_s=1.0, retries=1) is None
    assert len(calls) == 2  # one attempt + one retry, never more


async def test_cli_version_of_gives_up_when_no_attempt_reports_a_version(tmp_path: Path) -> None:
    cli = _flaky_cli(tmp_path, fail_first=5)
    assert await cli_version_of(str(cli), timeout_s=UNREACHABLE_TIMEOUT_S, retries=1) is None
    assert _calls(tmp_path) == 2  # one attempt + one retry, never more


async def test_cli_version_of_no_retry_on_success(tmp_path: Path) -> None:
    cli = _flaky_cli(tmp_path, fail_first=0)
    assert await cli_version_of(str(cli), timeout_s=UNREACHABLE_TIMEOUT_S, retries=1) == "3.4.5"
    assert _calls(tmp_path) == 1


async def test_cli_version_of_does_not_retry_a_permanent_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOENT/EACCES are not transient: one attempt, ``None``, no second spawn."""
    calls: list[list[str]] = []

    async def run_process(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        calls.append(list(cmd))
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(anyio, "run_process", run_process)
    assert await cli_version_of(str(tmp_path / "missing"), timeout_s=1.0, retries=3) is None
    assert len(calls) == 1


async def test_cli_version_of_missing_binary_is_none(tmp_path: Path) -> None:
    assert await cli_version_of(str(tmp_path / "missing"), timeout_s=1.0, retries=1) is None


def test_probe_defaults_are_tolerant() -> None:
    """5 s per attempt and one retry (the 2 s single shot timed out once under load)."""
    assert claude_mod.CLI_VERSION_TIMEOUT_S == 5.0
    assert claude_mod.CLI_VERSION_RETRIES == 1


async def test_healthcheck_ok_when_only_the_first_probe_fails(tmp_path: Path) -> None:
    cli = _flaky_cli(tmp_path, fail_first=1)
    health = await ClaudeProvider({"cli_path": str(cli)}).healthcheck()
    assert health.ok is True
    assert health.cli_version == "3.4.5"
    assert _calls(tmp_path) == 2


async def test_healthcheck_not_ok_when_both_probes_fail(tmp_path: Path) -> None:
    cli = _flaky_cli(tmp_path, fail_first=5)
    health = await ClaudeProvider({"cli_path": str(cli)}).healthcheck()
    assert health.ok is False
    assert health.cli_version is None
    assert _calls(tmp_path) == 2
    assert any("did not report a version" in d for d in health.details)

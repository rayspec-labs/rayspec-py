"""Claude healthcheck: the ``claude -v`` probe tolerates one slow/failed attempt.

Drives the real :mod:`rayspec.providers.claude` module with a fake CLI script — no SDK subprocess,
no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
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
    """A CLI that hangs for the first ``fail_first`` invocations, then prints ``version``."""
    counter = tmp_path / "calls"
    counter.write_text("0")
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter})\n"
        f"echo $((n + 1)) > {counter}\n"
        # ``exec`` so that killing the probe reaches the sleeper (no orphaned ``sleep``)
        f'if [ "$n" -lt {fail_first} ]; then exec sleep 5; fi\n'
        f"echo '{version} (Claude Code)'\n"
    )
    cli.chmod(0o755)
    return cli


def _calls(tmp_path: Path) -> int:
    return int((tmp_path / "calls").read_text().strip() or 0)


async def test_cli_version_of_retries_once_after_a_timeout(tmp_path: Path) -> None:
    cli = _flaky_cli(tmp_path, fail_first=1)
    assert await cli_version_of(str(cli), timeout_s=0.3, retries=1) == "3.4.5"
    assert _calls(tmp_path) == 2


async def test_cli_version_of_gives_up_after_the_retry(tmp_path: Path) -> None:
    cli = _flaky_cli(tmp_path, fail_first=5)
    assert await cli_version_of(str(cli), timeout_s=0.2, retries=1) is None
    assert _calls(tmp_path) == 2  # one attempt + one retry, never more


async def test_cli_version_of_no_retry_on_success(tmp_path: Path) -> None:
    cli = _flaky_cli(tmp_path, fail_first=0)
    assert await cli_version_of(str(cli), timeout_s=0.5, retries=1) == "3.4.5"
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


async def test_healthcheck_ok_when_only_the_first_probe_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _flaky_cli(tmp_path, fail_first=1)
    monkeypatch.setattr(claude_mod, "CLI_VERSION_TIMEOUT_S", 0.3)
    health = await ClaudeProvider({"cli_path": str(cli)}).healthcheck()
    assert health.ok is True
    assert health.cli_version == "3.4.5"
    assert _calls(tmp_path) == 2


async def test_healthcheck_not_ok_when_both_probes_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _flaky_cli(tmp_path, fail_first=5)
    monkeypatch.setattr(claude_mod, "CLI_VERSION_TIMEOUT_S", 0.2)
    health = await ClaudeProvider({"cli_path": str(cli)}).healthcheck()
    assert health.ok is False
    assert health.cli_version is None
    assert _calls(tmp_path) == 2
    assert any("did not report a version" in d for d in health.details)

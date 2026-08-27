# SPDX-License-Identifier: Apache-2.0
"""The `live` marker is enforced from ONE place (the root conftest gate), not per-file `skipif`.

A `live`-marked test hits a real provider and must not run by default, but it must still be
COLLECTED (a bare `pytest` sees it; `-m live` selects only these) — `tests/docs/test_community_
health.py` pins that `-m` stays out of addopts. So the gate is a collection-time skip, and this
file proves: (A) the real gate skips a live item and only a live item, conditional on
RAYSPEC_LIVE, by importing and calling the actual conftest hook; (B) end-to-end, a real live
file with no per-file skipif is skipped without the flag, with the gate's own reason.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_CONFTEST = REPO / "tests" / "conftest.py"


def _load_conftest():
    """The real root conftest, imported by path — so the gate under test IS the shipped one."""
    spec = importlib.util.spec_from_file_location("_root_conftest_under_test", _CONFTEST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeItem:
    """The two methods the gate calls on an item: is it `live`, and record an added marker."""

    def __init__(self, *, live: bool) -> None:
        self._live = live
        self.added: list[object] = []

    def get_closest_marker(self, name: str) -> object | None:
        return object() if (name == "live" and self._live) else None

    def add_marker(self, marker: object) -> None:
        self.added.append(marker)


def test_the_gate_skips_only_live_items_and_only_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conftest = _load_conftest()
    monkeypatch.delenv("RAYSPEC_LIVE", raising=False)

    live, plain = _FakeItem(live=True), _FakeItem(live=False)
    conftest.pytest_collection_modifyitems(None, [live, plain])
    assert len(live.added) == 1, "a live item must be skipped when RAYSPEC_LIVE is unset"
    assert live.added[0].name == "skip"  # type: ignore[attr-defined]
    assert live.added[0].kwargs["reason"] == conftest.LIVE_GATE_REASON  # type: ignore[attr-defined]
    assert plain.added == [], "a non-live item must never be touched"

    monkeypatch.setenv("RAYSPEC_LIVE", "1")
    live2, plain2 = _FakeItem(live=True), _FakeItem(live=False)
    conftest.pytest_collection_modifyitems(None, [live2, plain2])
    assert live2.added == [] and plain2.added == [], "RAYSPEC_LIVE=1 runs live tests unchanged"


def _pytest(*args: str, live: bool) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "RAYSPEC_LIVE"}
    if live:
        env["RAYSPEC_LIVE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def test_a_real_live_file_is_skipped_without_the_flag_by_the_gate() -> None:
    """No per-file skipif remains on this file, so ONLY the central gate can skip it — a proof it
    is wired in. (If the gate were gone the tests would try to reach a provider and fail.)"""
    proc = _pytest("-rs", "-q", "tests/providers/test_codex_live.py", live=False)
    assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-1000:])
    assert "SKIPPED" in proc.stdout and "passed" not in proc.stdout, proc.stdout[-2000:]
    # the distinctive gate reason — not a per-file one — proves the CENTRAL gate did the skipping
    reason = _load_conftest().LIVE_GATE_REASON
    assert reason.split("(")[0].strip() in proc.stdout, proc.stdout[-2000:]


def test_live_tests_are_collected_not_deselected() -> None:
    """`-m live` selects them, so they are collected — a bare run does not filter them out."""
    proc = _pytest("--collect-only", "-q", "-m", "live", live=False)
    assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-1000:])
    # `--collect-only -q` prints "<path>: <count>" per file; the live files must be listed
    assert "test_prd_to_pr_live.py" in proc.stdout and "test_codex_live.py" in proc.stdout, (
        proc.stdout[-2000:]
    )

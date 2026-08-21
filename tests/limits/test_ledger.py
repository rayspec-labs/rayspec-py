"""The local spend ledger: day/month buckets, idempotent per-run commits, real concurrency."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rayspec.limits import SpendLedger, SpendState, ledger_path

WHEN = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def test_ledger_path_is_under_the_project_store(tmp_path: Path) -> None:
    assert ledger_path(tmp_path) == tmp_path / "limits" / "spend.json"


def test_a_fresh_ledger_reads_as_zero(tmp_path: Path) -> None:
    state = SpendLedger(ledger_path(tmp_path)).read(when=WHEN)
    assert state == SpendState(day_usd=0.0, month_usd=0.0, consecutive_failures=0)


def test_commit_adds_to_the_day_and_month_buckets(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("run-a", 0.25, when=WHEN)
    ledger.commit("run-b", 0.50, when=WHEN)
    state = ledger.read(when=WHEN)
    assert state.day_usd == pytest.approx(0.75)
    assert state.month_usd == pytest.approx(0.75)


def test_commit_is_idempotent_per_run(tmp_path: Path) -> None:
    """A run commits its ABSOLUTE total, so a resume never double-counts what it already spent."""
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("run-a", 0.25, when=WHEN)
    ledger.commit("run-a", 0.25, when=WHEN)
    ledger.commit("run-a", 0.40, when=WHEN)  # the run spent more
    assert ledger.read(when=WHEN).day_usd == pytest.approx(0.40)


def test_a_run_stays_in_the_bucket_of_its_first_commit(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("run-a", 0.10, when=WHEN)
    ledger.commit("run-a", 0.30, when=WHEN + timedelta(days=1))
    assert ledger.read(when=WHEN).day_usd == pytest.approx(0.30)
    assert ledger.read(when=WHEN + timedelta(days=1)).day_usd == 0.0


def test_other_days_and_months_do_not_count(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("old", 5.0, when=WHEN - timedelta(days=1))
    ledger.commit("older", 7.0, when=WHEN - timedelta(days=40))
    state = ledger.read(when=WHEN)
    assert state.day_usd == 0.0
    assert state.month_usd == pytest.approx(5.0)  # same month, different day


def test_failure_counter_counts_up_and_resets(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    assert ledger.record_outcome(failed=True) == 1
    assert ledger.record_outcome(failed=True) == 2
    assert ledger.read(when=WHEN).consecutive_failures == 2
    assert ledger.record_outcome(failed=False) == 0
    assert ledger.read(when=WHEN).consecutive_failures == 0
    ledger.record_outcome(failed=True)
    ledger.reset_failures()
    assert ledger.read(when=WHEN).consecutive_failures == 0


def test_old_entries_are_pruned_so_the_file_stays_bounded(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("ancient", 1.0, when=WHEN - timedelta(days=400))
    ledger.commit("recent", 1.0, when=WHEN)
    data = json.loads(ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert "ancient" not in data["runs"]
    assert "recent" in data["runs"]


def test_a_corrupt_ledger_is_replaced_rather_than_failing_a_run(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    ledger = SpendLedger(path)
    ledger.commit("run-a", 1.0, when=WHEN)
    assert ledger.read(when=WHEN).day_usd == pytest.approx(1.0)


def test_the_ledger_file_is_private(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("run-a", 1.0, when=WHEN)
    assert ledger_path(tmp_path).stat().st_mode & 0o777 == 0o600
    assert ledger_path(tmp_path).parent.stat().st_mode & 0o777 == 0o700


def test_concurrent_commits_from_many_threads_all_land(tmp_path: Path) -> None:
    """Real concurrency, not a mock: 24 threads commit at once and nothing is lost."""
    path = ledger_path(tmp_path)
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda n: SpendLedger(path).commit(f"run-{n}", 0.5, when=WHEN), range(24)))
    assert SpendLedger(path).read(when=WHEN).day_usd == pytest.approx(12.0)


def test_two_processes_finishing_at_the_same_instant_both_land(tmp_path: Path) -> None:
    """Two real OS processes commit simultaneously; the flock serialises them."""
    script = tmp_path / "commit.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys, time
            from datetime import UTC, datetime
            from rayspec.limits import SpendLedger
            ledger = SpendLedger(sys.argv[1])
            when = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
            start = float(sys.argv[3])
            while time.time() < start:   # both processes wake at the same instant
                time.sleep(0.001)
            for i in range(20):
                ledger.commit(f"{sys.argv[2]}-{i}", 0.5, when=when)
            """
        ),
        encoding="utf-8",
    )
    start = f"{__import__('time').time() + 1.0}"
    procs = [
        subprocess.Popen([sys.executable, str(script), str(ledger_path(tmp_path)), tag, start])
        for tag in ("a", "b")
    ]
    for proc in procs:
        assert proc.wait(timeout=90) == 0
    assert SpendLedger(ledger_path(tmp_path)).read(when=WHEN).day_usd == pytest.approx(20.0)

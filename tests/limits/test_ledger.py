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

from .conftest import LEDGER_NOW

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


def test_spend_lands_in_the_day_it_is_actually_committed(tmp_path: Path) -> None:
    """A run resumed tomorrow spends tomorrow's money — otherwise every other run started
    tomorrow gets headroom the operator never granted."""
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("run-a", 0.10, when=WHEN)
    ledger.commit("run-a", 0.30, when=WHEN + timedelta(days=1))
    assert ledger.read(when=WHEN).day_usd == pytest.approx(0.10)
    assert ledger.read(when=WHEN + timedelta(days=1)).day_usd == pytest.approx(0.20)
    assert ledger.read(when=WHEN).month_usd == pytest.approx(0.30)


def test_a_run_resumed_next_month_pays_into_next_month(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("run-a", 1.0, when=WHEN)
    september = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
    ledger.commit("run-a", 4.0, when=september)
    assert ledger.read(when=WHEN).month_usd == pytest.approx(1.0)
    assert ledger.read(when=september).month_usd == pytest.approx(3.0)


def test_a_run_entry_survives_while_it_is_still_being_committed(tmp_path: Path) -> None:
    """Pruning is measured from the LAST commit, so a long-running run is never re-baselined."""
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("long", 1.0, when=WHEN - timedelta(days=100))
    ledger.commit("long", 2.0, when=WHEN)
    data = json.loads(ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert "long" in data["runs"]
    assert ledger.read(when=WHEN).day_usd == pytest.approx(1.0)


def test_read_and_commit_default_to_now(tmp_path: Path) -> None:
    """No ``when=`` means the moment the ledger is asked — and that moment is *now*.

    A commit and a read that merely agree with each other prove nothing: two defaults of the
    same wrong constant would agree just as well. The package's clock is stopped at
    ``LEDGER_NOW`` (see ``frozen_ledger_clock``), so what "now" means here is a value this test
    can name, and the day either side of it has to be empty.
    """
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("run-a", 2.0)
    assert ledger.read().day_usd == pytest.approx(2.0)
    assert ledger.read(when=LEDGER_NOW).day_usd == pytest.approx(2.0)
    assert ledger.read(when=LEDGER_NOW + timedelta(days=1)).day_usd == 0.0
    assert ledger.read(when=LEDGER_NOW - timedelta(days=1)).day_usd == 0.0


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


def test_a_corrupt_ledger_is_replaced_but_never_in_silence(tmp_path: Path) -> None:
    """Resetting the operator's accrued total may be the least bad answer — it must be loud."""
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    ledger = SpendLedger(path)
    ledger.commit("run-a", 1.0, when=WHEN)
    warnings = ledger.take_warnings()
    assert warnings and "spend.json" in warnings[0]
    assert ledger.take_warnings() == []  # drained
    assert ledger.read(when=WHEN).day_usd == pytest.approx(1.0)


def test_a_failed_write_leaves_the_previous_ledger_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is replaced whole: a crash mid-write cannot reset the day, month and streak."""
    path = ledger_path(tmp_path)
    ledger = SpendLedger(path)
    ledger.commit("run-a", 7.0, when=WHEN)
    before = path.read_text(encoding="utf-8")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("rayspec.limits.ledger.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        ledger.commit("run-b", 3.0, when=WHEN)
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == before
    assert SpendLedger(path).read(when=WHEN).day_usd == pytest.approx(7.0)
    assert not list(path.parent.glob("*.tmp"))


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


#: Commits per child in the cross-process test, and what each one books.
BURST, PER_COMMIT = 200, 0.05


def test_two_processes_finishing_at_the_same_instant_both_land(tmp_path: Path) -> None:
    """Two real OS processes commit simultaneously; the flock serialises them.

    "Simultaneously" is a handshake, not a wall-clock instant. A child cannot reach a
    ``time.time()`` deadline computed in the parent before ``import rayspec.limits`` has run,
    and that import is seconds, not milliseconds — so a shared deadline is already in the past
    when either child evaluates it, the two bursts start whenever they start, and the lock is
    never contended. Here each child announces ``ready`` once its interpreter is warm and then
    blocks on stdin; the parent releases both only after it has heard from both.

    Overlapping is not enough on its own either: :data:`BURST` commits are over in tens of
    milliseconds, which a scheduler hiccup can put entirely on one side of the other child's
    burst. Each write is slowed to about a millisecond, so a burst is a fifth of a second wide
    and the two genuinely interleave. Without the flock the read-modify-write cycles overwrite
    each other and the day total comes up short.
    """
    script = tmp_path / "commit.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys, time
            from datetime import UTC, datetime
            from rayspec.limits import SpendLedger

            # a measurable critical section: a burst cannot be out-run by the other process
            _write = SpendLedger._write
            def slow_write(self, fd, data):
                time.sleep(0.001)
                _write(self, fd, data)
            SpendLedger._write = slow_write

            ledger = SpendLedger(sys.argv[1])
            when = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
            print("ready", flush=True)     # the interpreter is warm; wait to be released
            sys.stdin.readline()
            for i in range(int(sys.argv[3])):
                ledger.commit(f"{sys.argv[2]}-{i}", float(sys.argv[4]), when=when)
            """
        ),
        encoding="utf-8",
    )
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(ledger_path(tmp_path)),
                tag,
                str(BURST),
                str(PER_COMMIT),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for tag in ("a", "b")
    ]
    try:
        for proc in procs:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "ready"
        for proc in procs:  # both are past the import: now they start together
            assert proc.stdin is not None
            proc.stdin.write("go\n")
            proc.stdin.flush()
        for proc in procs:
            assert proc.wait(timeout=90) == 0
    finally:
        for proc in procs:
            if proc.poll() is None:  # pragma: no cover - only after a failure above
                proc.kill()
    state = SpendLedger(ledger_path(tmp_path)).read(when=WHEN)
    assert state.day_usd == pytest.approx(2 * BURST * PER_COMMIT)


# -- a ledger is a plain JSON file: anything can be in it ----------------------------------------


def write_ledger(tmp_path: Path, text: str) -> Path:
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def test_a_bucket_that_is_not_a_number_is_reset_instead_of_crashing(tmp_path: Path) -> None:
    """One malformed byte must not brick the project.

    ``_state_of`` runs on the way OUT of a commit that has already been written, so an
    exception there does not merely fail one command: the bad value stays on disk and every
    later run, resume and approve dies on it too.
    """
    path = write_ledger(
        tmp_path,
        """
        {"version": 1,
         "days": {"2026-08-21": "oops"},
         "months": {"2026-08": [1, 2]},
         "consecutive_failures": 0}
        """,
    )
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN) == SpendState()
    warnings = ledger.take_warnings()
    assert warnings and "days.2026-08-21" in warnings[0] and "months.2026-08" in warnings[0]


def test_a_bucket_that_is_not_finite_is_reset_too(tmp_path: Path) -> None:
    """``json`` happily parses ``Infinity`` and ``NaN``; a ceiling compared against either is
    not a ceiling."""
    path = write_ledger(
        tmp_path, '{"version": 1, "days": {"2026-08-21": Infinity}, "months": {"2026-08": NaN}}'
    )
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN) == SpendState()
    assert ledger.take_warnings()


def test_a_number_too_large_to_be_a_float_is_reset_too(tmp_path: Path) -> None:
    """``json`` will hand back an integer of any length; ``float()`` will not take it."""
    path = write_ledger(tmp_path, '{"version": 1, "days": {"2026-08-21": %s}}' % ("9" * 400))
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN) == SpendState()
    assert ledger.take_warnings()


def test_the_repaired_ledger_is_written_back_so_the_next_command_works(tmp_path: Path) -> None:
    path = write_ledger(tmp_path, '{"version": 1, "days": {"2026-08-21": "oops"}}')
    SpendLedger(path).commit("run-a", 1.0, when=WHEN)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["days"]["2026-08-21"] == pytest.approx(1.0)
    fresh = SpendLedger(path)
    assert fresh.read(when=WHEN).day_usd == pytest.approx(1.0)
    assert fresh.take_warnings() == []  # nothing left to complain about


def test_an_unreadable_failure_counter_cannot_crash_the_breaker(tmp_path: Path) -> None:
    path = write_ledger(tmp_path, '{"version": 1, "consecutive_failures": "lots"}')
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN).consecutive_failures == 0
    assert any("consecutive_failures" in w for w in ledger.take_warnings())


def test_a_negative_failure_counter_is_not_a_disabled_breaker(tmp_path: Path) -> None:
    """A count below zero would give the breaker headroom nobody granted."""
    path = write_ledger(tmp_path, '{"version": 1, "consecutive_failures": -5}')
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN).consecutive_failures == 0
    assert any("consecutive_failures" in w for w in ledger.take_warnings())


def test_a_run_entry_with_an_unreadable_total_is_re_baselined(tmp_path: Path) -> None:
    path = write_ledger(tmp_path, '{"version": 1, "runs": {"run-a": {"cost_usd": "x"}}}')
    ledger = SpendLedger(path)
    ledger.commit("run-a", 2.0, when=WHEN)
    assert ledger.read(when=WHEN).day_usd == pytest.approx(2.0)
    assert any("runs.run-a.cost_usd" in w for w in ledger.take_warnings())


def test_a_day_or_month_map_that_is_not_a_map_is_replaced(tmp_path: Path) -> None:
    path = write_ledger(tmp_path, '{"version": 1, "days": 3, "months": "nope", "runs": []}')
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN) == SpendState()
    warnings = ledger.take_warnings()
    assert warnings and "days" in warnings[0] and "months" in warnings[0]


def test_an_empty_ledger_is_a_reset_and_says_so(tmp_path: Path) -> None:
    """A zero-byte file is a truncation, not an empty ledger — rayspec never writes one.

    Every other corruption shape warns on the console, as a ``warning`` event and in
    ``rayspec show``; a reset of the day, month and failure totals must not be the one that
    happens in silence.
    """
    path = write_ledger(tmp_path, "")
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN) == SpendState()
    warnings = ledger.take_warnings()
    assert warnings and "is empty" in warnings[0]
    assert "start again from zero" in warnings[0]


def test_a_missing_ledger_is_still_silent(tmp_path: Path) -> None:
    """The ordinary case: there is nothing to warn about the first time a project spends."""
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("run-a", 1.0, when=WHEN)
    assert ledger.take_warnings() == []


def test_a_newer_ledger_format_is_replaced_rather_than_reinterpreted(tmp_path: Path) -> None:
    """``_write`` stamps a version; reading has to honour it, or a v2 document is silently
    re-read under v1 rules and stamped back down to v1."""
    path = write_ledger(
        tmp_path, '{"version": 2, "days": {"2026-08-21": 99.0}, "consecutive_failures": 4}'
    )
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN) == SpendState()
    warnings = ledger.take_warnings()
    assert warnings and "version 2" in warnings[0]


def test_an_unreadable_format_version_is_replaced(tmp_path: Path) -> None:
    path = write_ledger(tmp_path, '{"version": "one", "days": {"2026-08-21": 9.0}}')
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN) == SpendState()
    assert ledger.take_warnings()


def test_an_older_ledger_format_is_still_read(tmp_path: Path) -> None:
    """Only a NEWER document is unreadable; version 1 is what everything on disk says today."""
    path = write_ledger(tmp_path, '{"version": 1, "days": {"2026-08-21": 9.0}}')
    ledger = SpendLedger(path)
    assert ledger.read(when=WHEN).day_usd == pytest.approx(9.0)
    assert ledger.take_warnings() == []

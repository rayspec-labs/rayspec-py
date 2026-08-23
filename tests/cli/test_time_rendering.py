# SPDX-License-Identifier: Apache-2.0
"""What the CLI says about *when* something happened — pinned on the printed output.

Every defect these tests hold down was invisible to a green suite, because each one lived in the
last inch of the program: a cell a person reads. So each test drives a real run through the CLI
and asserts on the bytes a user would see.

Three rules, and between them they are the whole contract:

* **every printed clock names its zone.** A run record is written on one machine and read on
  another, so an unlabelled ``17:01`` is a guess, not a time;
* **a redirected listing is a function of the store alone.** ``docs/cli.md`` promises that
  ``rayspec runs > yesterday.txt`` and the same command tomorrow "differ where the runs differ and
  nowhere else"; an age that ticks differs where nothing did;
* **an age is always an age.** ``rayspec show`` prints one beside the absolute stamp, so an age
  that fell back to a second (shorter) copy of that stamp says nothing at all — and a run stamped
  in the *future* (clock skew, a restored backup) reads ``in 9d``, never ``0s ago``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rayspec.cli
from rayspec.cli import _runs_common as common
from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord

WF = """
rayspec: 1
name: clocks
isolation: none
steps:
  - id: one
    shell: "printf one"
  - id: two
    needs: [one]
    shell: "exit 1"
  - id: three
    needs: [two]
    shell: "printf three"
"""

#: ``17:01`` / ``17:01:02`` anywhere in a rendering — the shape a reader takes for a time of day.
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
#: The format string of every ``strftime`` call, wherever one hides in the CLI package.
_STRFTIME_RE = re.compile(r"""strftime\(\s*["']([^"']*)["']""")


@pytest.fixture
def ran(cli: CliRunner, home: Path, project: Path) -> tuple[str, Path, RunRecord]:
    """A finished (failed) run of the three-step ``WF``, and the record behind it."""
    (project / ".rayspec" / "workflows" / "clocks.yaml").write_text(WF, encoding="utf-8")
    result = cli.invoke(app, ["run", "clocks", "--root", str(project), "--quiet"])
    assert result.exit_code == 1, result.output  # step `two` fails on purpose
    store = FileRunStore(home / "projects" / project_slug_for(project))
    run_id = store.list_run_ids()[0]
    return run_id, project, store.load(run_id)


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Pin ``datetime.now()`` *as the moment renderers see it* to ``moment``."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return moment

    monkeypatch.setattr(common, "datetime", Frozen)


def _watching(monkeypatch: pytest.MonkeyPatch, *, terminal: bool) -> None:
    """Who is on the other end of stdout — the one question `ages_are_relative` asks."""
    monkeypatch.setattr(common, "stdout_is_tty", lambda: terminal)


def _started_line(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("  started:"))


# --------------------------------------------------------------------------------------------------
# every printed clock names its zone
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "prints_a_clock"),
    # the survey, as a table: every command that renders a run and what it does with time. The
    # second column keeps the guard honest — a test that would pass on a command printing nothing
    # is a test that cannot fail, so the five that DO print a clock have to prove they still do.
    [
        pytest.param(["runs"], True, id="runs"),
        pytest.param(["show", "@run"], True, id="show"),
        pytest.param(["logs", "@run"], True, id="logs"),
        pytest.param(["audit", "@run"], True, id="audit"),
        pytest.param(["costs", "--since", "30d"], True, id="costs"),
        pytest.param(["explain", "@run", "two"], False, id="explain"),
    ],
)
def test_every_clock_a_command_prints_names_its_zone(
    cli: CliRunner, ran: tuple[str, Path, RunRecord], argv: list[str], prints_a_clock: bool
) -> None:
    """Whichever of these commands prints a time of day, it says which clock it came from.

    `explain` reports only durations and delays, so it has no clock to label; the day it grows
    one, this test is already watching it.
    """
    run_id, project, _record = ran
    args = [run_id if a == "@run" else a for a in argv]
    result = cli.invoke(app, [*args, "--root", str(project)])
    assert result.exit_code == 0, result.output
    clocks = list(_CLOCK_RE.finditer(result.output))
    assert bool(clocks) is prints_a_clock, (
        f"`rayspec {' '.join(argv)}` was surveyed as "
        f"{'printing' if prints_a_clock else 'printing no'} time of day; it printed "
        f"{[m.group() for m in clocks]}:\n{result.output}"
    )
    unzoned = [
        result.output[max(0, m.start() - 30) : m.end() + 10]
        for m in clocks
        if result.output[m.end() : m.end() + 4] != " UTC"
    ]
    assert not unzoned, f"`rayspec {' '.join(argv)}` printed a time with no zone: {unzoned}"


def test_the_moment_helpers_are_the_only_renderers_and_all_of_them_say_utc() -> None:
    """The total rule behind the test above, which can only ever check the commands it names.

    Every ``datetime`` the CLI turns into text goes through ``_runs_common``'s two helpers, and
    both formats end in the zone. A new command that reaches for ``strftime`` itself — the way
    ``logs`` and ``audit`` each did, arriving at ``%H:%M:%S`` twice independently — fails here
    before anybody has to notice its output.
    """
    package = Path(rayspec.cli.__file__).resolve().parent
    helpers = package / "_runs_common.py"
    found: list[tuple[Path, str]] = [
        (path, fmt)
        for path in sorted(package.rglob("*.py"))
        for fmt in _STRFTIME_RE.findall(path.read_text(encoding="utf-8"))
    ]
    assert found, "the moment helpers themselves must show up in this scan"
    elsewhere = [f"{p.relative_to(package)}: {fmt!r}" for p, fmt in found if p != helpers]
    assert not elsewhere, (
        f"a datetime is rendered outside {helpers.name}: {elsewhere} — "
        "use common.fmt_stamp / common.fmt_clock so the zone comes with it"
    )
    zoneless = [fmt for _p, fmt in found if not fmt.endswith("UTC")]
    assert not zoneless, f"a moment format drops its zone: {zoneless}"


def test_the_commands_that_share_a_moment_render_it_identically(
    cli: CliRunner, ran: tuple[str, Path, RunRecord]
) -> None:
    """`runs` and `show` print the run's start; `logs` and `audit` print its events. Each pair
    used to disagree — `2026-07-13 17:01` against `2026-07-13 17:01:00 UTC`, and two independent
    `%H:%M:%S` — which is how a reader ends up comparing two times that are not comparable."""
    run_id, project, record = ran
    started = record.started_at
    assert started is not None
    stamp, clock = common.fmt_stamp(started), common.fmt_clock(started)

    for argv in (["runs"], ["show", run_id]):
        out = cli.invoke(app, [*argv, "--root", str(project)]).output
        assert stamp in out, f"`rayspec {argv[0]}` did not render the start as {stamp!r}:\n{out}"
    for argv in (["logs", run_id], ["audit", run_id]):
        out = cli.invoke(app, [*argv, "--root", str(project)]).output
        assert clock in out, f"`rayspec {argv[0]}` did not render an event as {clock!r}:\n{out}"


# --------------------------------------------------------------------------------------------------
# a redirected listing is a function of the store alone
# --------------------------------------------------------------------------------------------------


def test_a_redirected_listing_is_byte_identical_however_long_you_wait(
    cli: CliRunner, ran: tuple[str, Path, RunRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rayspec runs > yesterday.txt` and the same command a year later, over an untouched store.

    Nothing about the run differs, so nothing in the file may. The listing used to carry the age
    of every run, so redirecting it produced a file that diffed against itself on the next tick
    of the clock — every line changed, none of them because a run had.
    """
    run_id, project, record = ran
    started = record.started_at
    assert started is not None
    _watching(monkeypatch, terminal=False)

    _freeze(monkeypatch, started + timedelta(seconds=13))
    first = cli.invoke(app, ["runs", "--root", str(project)])
    assert first.exit_code == 0, first.output
    _freeze(monkeypatch, started + timedelta(days=400))
    later = cli.invoke(app, ["runs", "--root", str(project)])
    assert later.exit_code == 0, later.output

    assert later.output == first.output, "the redirected listing moved with the wall clock"
    assert run_id in first.output and common.fmt_stamp(started) in first.output
    assert " ago" not in first.output, f"an age reached a redirected stream:\n{first.output}"


def test_a_watched_listing_still_answers_in_ages(
    cli: CliRunner, ran: tuple[str, Path, RunRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: on a terminal the question is "what ran recently", and the answer is an
    age — at any distance, because the `run` column already opens with the date."""
    _run_id, project, record = ran
    started = record.started_at
    assert started is not None
    _watching(monkeypatch, terminal=True)

    _freeze(monkeypatch, started + timedelta(minutes=13))
    assert "13m ago" in cli.invoke(app, ["runs", "--root", str(project)]).output
    _freeze(monkeypatch, started + timedelta(days=431))
    out = cli.invoke(app, ["runs", "--root", str(project)]).output
    assert "431d ago" in out, out


# --------------------------------------------------------------------------------------------------
# an age is always an age
# --------------------------------------------------------------------------------------------------


def test_show_prints_an_age_beside_the_stamp_and_never_a_second_copy_of_it(
    cli: CliRunner, ran: tuple[str, Path, RunRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run older than a month rendered as ``2026-07-01 09:00:00 UTC (2026-07-01 09:00)``: the
    slot reserved for "how long ago" spent itself repeating the stamp two inches to its left."""
    run_id, project, record = ran
    started = record.started_at
    assert started is not None
    _watching(monkeypatch, terminal=True)
    _freeze(monkeypatch, started + timedelta(days=53))

    line = _started_line(cli.invoke(app, ["show", run_id, "--root", str(project)]).output)
    assert line == f"  started:    {common.fmt_stamp(started)} (53d ago)", line
    assert line.count(common.fmt_stamp(started)[:10]) == 1, f"the date is printed twice: {line}"


def test_show_reads_a_future_stamp_forwards_instead_of_calling_it_now(
    cli: CliRunner, ran: tuple[str, Path, RunRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two machines sharing a ``RAYSPEC_HOME`` with skewed clocks, or a restored backup, and the
    run is stamped ahead of now. The age used to be clamped to ``0s ago``, which says the run
    started this second — the one reading that is *never* true."""
    run_id, project, record = ran
    started = record.started_at
    assert started is not None
    _watching(monkeypatch, terminal=True)
    _freeze(monkeypatch, started - timedelta(days=9))

    line = _started_line(cli.invoke(app, ["show", run_id, "--root", str(project)]).output)
    assert line == f"  started:    {common.fmt_stamp(started)} (in 9d)", line


def test_show_drops_the_age_when_nobody_is_watching(
    cli: CliRunner, ran: tuple[str, Path, RunRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirected, the header is the stamp and nothing else — same rule as the listing."""
    run_id, project, record = ran
    started = record.started_at
    assert started is not None
    _watching(monkeypatch, terminal=False)
    _freeze(monkeypatch, started + timedelta(days=53))

    out = cli.invoke(app, ["show", run_id, "--root", str(project)]).output
    assert _started_line(out) == f"  started:    {common.fmt_stamp(started)}"
    assert " ago" not in out, f"an age reached a redirected stream:\n{out}"


def test_run_stamped_in_the_future_sorts_and_renders_without_pretending_it_is_now(
    cli: CliRunner, ran: tuple[str, Path, RunRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same skewed stamp in the listing: ``in 8d``, not a run that started ``0s ago``."""
    _run_id, project, record = ran
    started = record.started_at
    assert started is not None
    _watching(monkeypatch, terminal=True)
    _freeze(monkeypatch, started - timedelta(days=8))

    out = cli.invoke(app, ["runs", "--root", str(project)]).output
    assert "in 8d" in out, out
    assert "0s ago" not in out, out


def test_a_naive_stamp_from_an_old_record_is_still_labelled(
    ran: tuple[str, Path, RunRecord],
) -> None:
    """A record written before the store wrote offsets holds a naive datetime. It is UTC — the
    store never wrote anything else — and the rendering has to say so rather than go quiet."""
    _run_id, _project, record = ran
    started = record.started_at
    assert started is not None
    naive = started.replace(tzinfo=None)
    assert common.fmt_stamp(naive) == common.fmt_stamp(started)
    assert common.fmt_clock(naive) == common.fmt_clock(started)
    assert common.fmt_age(naive, now=started + timedelta(hours=2)) == "2h ago"


def test_utc_is_what_is_printed_whatever_the_offset_of_the_moment(
    ran: tuple[str, Path, RunRecord],
) -> None:
    """An aware stamp in another offset renders as the same instant in UTC, not as its own
    wall clock — otherwise two runs of one project read as hours apart when they were not."""
    _run_id, _project, record = ran
    started = record.started_at
    assert started is not None
    elsewhere = started.astimezone(_offset(hours=9))
    assert elsewhere.hour != started.astimezone(UTC).hour or started.hour == 0
    assert common.fmt_stamp(elsewhere) == common.fmt_stamp(started)
    assert common.fmt_clock(elsewhere) == common.fmt_clock(started)


def _offset(*, hours: int):
    from datetime import timezone

    return timezone(timedelta(hours=hours))

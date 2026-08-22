# SPDX-License-Identifier: Apache-2.0
"""`rayspec costs [--since W] [--workflow N] [--json]` — what this project has spent.

The per-run figures already exist (``rayspec runs`` prints one line each); this command adds
them up per workflow so the question "what has this project cost me" does not require reading
every run by hand. It is **read-only and per-project**: it lists the runs of one store
(``$RAYSPEC_HOME/projects/<slug>``) and writes nothing at all — no record is loaded for
modification, no directory is created.

Module boundary: aggregation and presentation only. The per-run arithmetic stays where it is —
:func:`rayspec.store.model.RunRecord.total_cost_usd` / ``total_usage`` for the numbers and
:mod:`rayspec.cli._runs_common` (``fmt_cost``, ``run_cost_source``, ``fmt_tokens``) for the
rendering — so a roll-up can never disagree with the run listing it sums.

The one property everything else is subordinate to: **a run whose cost is unknown is counted and
shown as unknown, never dropped and never treated as zero.** A total that quietly omits runs is
worse than no total, so an incomplete sum is rendered with the ``≥`` marker the rest of the CLI
uses for a lower bound and the run is counted in the ``unknown`` bucket of the breakdown.

Four things can make a figure less than the whole truth, and each one is named below the table
rather than folded away: a run that carries no cost at all, a priced run containing an unpriced
step, a step that was cut off before the provider reported any usage (``StepRecord.usage_unknown``
— the same lower bound ``rayspec run`` prints as ``tokens: ≥N``), and a run that is still running
or paused. A run record the store cannot read is counted too: it is missing from the sum, which is
the one thing a total may never hide.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table
from rich.text import Text

from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    console,
    err_console,
    fail,
    new_table,
    print_json,
    resolve_output,
)
from rayspec.providers.base import Usage
from rayspec.providers.pricing import COST_SOURCES, combine_cost_sources, cost_marker
from rayspec.schema import RunStatus
from rayspec.store.file import RUN_JSON, FileRunStore
from rayspec.store.model import RunRecord
from rayspec.textsafe import safe_text

#: The bucket a run lands in when it carries no cost at all — deliberately not one of
#: :data:`~rayspec.providers.pricing.COST_SOURCES`, because "no price was found" and "this run
#: was never priced" are different answers and only the second one makes a total a lower bound.
UNKNOWN = "unknown"

#: Breakdown buckets in the order they are printed.
BUCKETS: tuple[str, ...] = (*COST_SOURCES, UNKNOWN)

#: Statuses of a run whose figures can still grow — summed like any other run, but said out loud.
IN_FLIGHT: frozenset[RunStatus] = frozenset({RunStatus.RUNNING, RunStatus.PAUSED})

#: What ``--since`` accepts, named in every error about it.
SINCE_HINT = (
    "use a window like 7d, 24h, 90m or an absolute date like 2026-08-01 "
    "(or 2026-08-01T06:30:00+02:00)"
)

_WINDOW_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhdw])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
#: A value that was *meant* as a date — its parse error ("month must be in 1..12") is worth
#: repeating, while the same error about ``yesterday`` would only be noise next to the hint.
_LOOKS_LIKE_DATE = re.compile(r"^\d{4}-")


def parse_since(text: str, *, now: datetime | None = None) -> datetime:
    """Parse a ``--since`` value into an aware UTC cutoff.

    Two forms, both of which a person types without looking anything up: a **window** back from
    now (``45s``, ``90m``, ``24h``, ``7d``, ``2w`` — a decimal like ``1.5h`` works too) or an
    **absolute** ISO-8601 date/timestamp (``2026-08-01``, ``2026-08-01T06:30:00``,
    ``2026-08-01T06:30:00+02:00``, ``…Z``). A date without a time means midnight, a timestamp
    without an offset is read as UTC, and one with an offset is converted to UTC.

    Raises :class:`ValueError` (message + :data:`SINCE_HINT`) for anything else — including a
    negative window, which would mean "the future" and is never what was meant.
    """
    now = now or datetime.now(UTC)
    raw = text.strip()
    window = _WINDOW_RE.match(raw)
    if window is not None:
        amount = float(window.group("value"))
        try:
            return now - timedelta(**{_UNITS[window.group("unit")]: amount})
        except (OverflowError, ValueError) as exc:  # 99999999w lands before year 1
            raise ValueError(f"invalid --since value {text!r}: the window is out of range") from exc
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00") if raw.endswith("Z") else raw)
    except ValueError as exc:
        detail = f": {exc}" if _LOOKS_LIKE_DATE.match(raw) else ""
        raise ValueError(f"invalid --since value {text!r}{detail}") from exc
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


def _aware(moment: datetime) -> datetime:
    """A stored timestamp as UTC (records written by rayspec are aware; be forgiving anyway)."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


#: The stamp a run id starts with (``new_run_id``: ``YYYYMMDD-HHMMSS-<4 chars>``, UTC).
_RUN_ID_STAMP_RE = re.compile(r"^(\d{8}-\d{6})(?:-|$)")


def run_id_created_at(run_id: str) -> datetime | None:
    """The UTC moment encoded in a run id, or ``None`` when the id has another shape.

    The one thing that is still knowable about a run whose record cannot be read: ``rayspec``
    mints time-sortable ids, so a lost run can still be placed in (or out of) a ``--since``
    window. Anything else about it — its workflow above all — is only in the record.
    """
    match = _RUN_ID_STAMP_RE.match(run_id)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    except ValueError:  # 20261301-000000-… is a directory name, not a date
        return None


#: How long a ``run.json.<pid>.<n>.tmp`` beside a missing record still reads as a save in
#: flight. Generous — an fsync on a busy disk is not instant — but finite, because a staging
#: file left behind by a killed process must not silence its run for good.
SAVE_GRACE_S = 30.0


def save_in_flight(run_dir: Path, *, now: float | None = None) -> bool:
    """Whether a ``run.json`` is being written into ``run_dir`` at this moment.

    ``FileRunStore.save()`` creates the run directory (and ``steps/``, ``artifacts/``, ``tmp/``)
    and only then writes the record — a model dump, a redaction pass, a write and an fsync
    later. In that window the directory exists with no ``run.json`` in it and is indistinguishable
    from a run that lost its record, so a ``costs`` in another terminal would name a healthy run
    that started a moment ago and mark the total ``≥`` for it. The store's own staging file is
    the positive evidence that somebody is writing; :data:`SAVE_GRACE_S` bounds how long that
    evidence counts.
    """
    moment = time.time() if now is None else now
    try:
        return any(
            moment - path.stat().st_mtime < SAVE_GRACE_S
            for path in run_dir.glob(f"{RUN_JSON}.*.tmp")
        )
    except OSError:  # the staging file was renamed into place while we looked: a save landed
        return True


def unreadable_run_ids(
    store: FileRunStore, loaded: Sequence[RunRecord], *, since: datetime | None = None
) -> list[str]:
    """Run ids of the store that produced no record, newest first (the runs missing from a sum).

    Two different failures, one consequence. A ``run.json`` that does not parse is *listed* by
    the store and skipped when it is loaded. A ``run.json`` that is **missing** is worse: the
    store's listing only reports directories that have one, so the run is invisible — the totals
    would silently shrink by one run with ``runs_unreadable`` still saying zero. The run
    directories are therefore scanned as well, and a directory whose name is a run id counts.

    ``since`` drops the ids whose own timestamp puts them outside the window; an id of another
    shape carries no timestamp, so it is kept rather than assumed to be out of scope. There is
    no ``workflow`` counterpart on purpose — which workflow a run belonged to is only in the
    record that could not be read, so a ``--workflow`` roll-up keeps counting it.
    """
    have = {run.run_id for run in loaded}
    listed = set(store.list_run_ids())  # a run.json that is there but does not parse
    candidates = set(listed)
    try:
        entries = list(os.scandir(store.runs_root))
    except OSError:  # no store yet, or one this user may not read
        entries = []
    candidates |= {
        entry.name
        for entry in entries
        if entry.is_dir() and run_id_created_at(entry.name) is not None
    }
    # a directory with no record is only lost once nobody is writing one into it: `save()`
    # creates the directory first and the record after (:func:`save_in_flight`)
    lost = [
        run_id
        for run_id in candidates
        if run_id not in have and (run_id in listed or not save_in_flight(store.run_dir(run_id)))
    ]
    if since is not None:
        lost = [
            run_id
            for run_id in lost
            if (created := run_id_created_at(run_id)) is None or created >= since
        ]
    return sorted(lost, reverse=True)


def cost_bucket(run: RunRecord) -> str:
    """Which breakdown bucket ``run`` belongs to: its run-level cost source, or ``unknown``.

    A run with no cost at all is ``unknown`` whatever its steps recorded — that is the fact the
    reader needs, because it is the reason the enclosing total is a lower bound.

    ``none`` next to a real dollar figure is the legacy bucket: a ``run.json`` written before
    ``StepRecord.cost_source`` existed records a cost without saying where it came from. It is
    reported as it was recorded rather than guessed at, and documented in ``docs/cli.md``.
    """
    if run.total_cost_usd() is None:
        return UNKNOWN
    return common.run_cost_source(run)


@dataclass(frozen=True, slots=True)
class CostGroup:
    """One row of the roll-up: every run that shares a label (a workflow name, or ``total``)."""

    label: str
    runs: int
    runs_unknown_cost: int
    runs_partial_cost: int
    runs_usage_unknown: int
    runs_in_flight: int
    usage: Usage
    cost_usd: float | None
    cost_source: str
    buckets: dict[str, int]
    first_run_at: datetime | None
    last_run_at: datetime | None

    @property
    def partial(self) -> bool:
        """The cost shown is a lower bound: at least one run contributed nothing to it."""
        return self.runs_unknown_cost > 0

    @property
    def tokens_partial(self) -> bool:
        """The token count is a lower bound: a step was cut off before reporting any usage."""
        return self.runs_usage_unknown > 0

    def breakdown(self) -> str:
        """``1 provider · 2 table · 1 unknown`` — every run accounted for, in a fixed order."""
        parts = [f"{self.buckets[name]} {name}" for name in BUCKETS if self.buckets.get(name)]
        return " · ".join(parts) if parts else "-"


def aggregate(records: Sequence[RunRecord], *, label: str, incomplete: bool = False) -> CostGroup:
    """Fold runs into one :class:`CostGroup`. Every record is counted, priced or not.

    ``incomplete`` says that records which belong in this fold are missing from ``records``
    (a ``run.json`` the store could not read): the sum is then a lower bound even when every
    record that *was* read is fully priced.
    """
    usage = Usage()
    total: float | None = None
    unknown = 0
    partial_cost = 0
    usage_unknown = 0
    in_flight = 0
    sources: list[str] = []
    buckets: Counter[str] = Counter()
    for run in records:
        usage = usage + run.total_usage()
        cost = run.total_cost_usd()
        if cost is None:
            unknown += 1
        else:
            total = cost if total is None else total + cost
            sources.append(common.run_cost_source(run))
            if common.unpriced_steps(run):
                # a run that IS priced but holds a step with tokens and no price: its own cost
                # is already a lower bound, which is a different sentence from "no cost at all"
                partial_cost += 1
        # a step interrupted before the adapter reported anything: usage.total is 0 there, so
        # `unpriced_steps` cannot see it — the record says so with its own flag instead
        if any(rec.usage_unknown for rec in run.steps.values()):
            usage_unknown += 1
        if run.status in IN_FLIGHT:
            in_flight += 1
        buckets[cost_bucket(run)] += 1
    stamps = [_aware(run.created_at) for run in records]
    return CostGroup(
        label=label,
        runs=len(records),
        runs_unknown_cost=unknown,
        runs_partial_cost=partial_cost,
        runs_usage_unknown=usage_unknown,
        runs_in_flight=in_flight,
        usage=usage,
        cost_usd=total,
        # an unpriced run makes the sum a lower bound exactly the way an unpriced step makes a
        # run's own sum one, so the run-level fold is reused rather than re-invented here; a
        # cut-off step and a record that could not be read are lower bounds for the same reason
        cost_source=combine_cost_sources(
            sources,
            unpriced=incomplete or unknown > 0 or usage_unknown > 0 or "partial" in sources,
        ),
        buckets=dict(buckets),
        first_run_at=min(stamps) if stamps else None,
        last_run_at=max(stamps) if stamps else None,
    )


@dataclass(frozen=True, slots=True)
class CostReport:
    """The whole answer: one group per workflow plus the total over the same runs.

    ``unreadable`` holds the ids of the runs the store could not hand over — a ``run.json`` that
    does not parse, and one that is not there at all. They are in no group (nothing could be read
    out of them), so they are carried here, named rather than merely counted, and the total is
    marked as a lower bound: a sum that silently shrank is the one failure this command exists to
    prevent.
    """

    groups: tuple[CostGroup, ...]
    total: CostGroup
    unreadable: tuple[str, ...] = ()

    @property
    def runs_unreadable(self) -> int:
        """How many run records are missing from these totals."""
        return len(self.unreadable)


def build_report(records: Sequence[RunRecord], *, unreadable: Sequence[str] = ()) -> CostReport:
    """Group ``records`` by workflow name, most expensive first, then by name.

    An unpriced group sorts last (its cost is unknown, not zero) but is never dropped, so the
    run counts of the groups always add up to the total's. ``unreadable`` are the ids of the run
    records the store could not load; they make the total a lower bound.
    """
    by_workflow: dict[str, list[RunRecord]] = {}
    for run in records:
        by_workflow.setdefault(run.workflow_name, []).append(run)
    groups = [aggregate(runs, label=name) for name, runs in by_workflow.items()]
    # a tri-state key: unknown is not zero, so an unpriced group sorts after a $0.00 one
    groups.sort(key=lambda g: (g.cost_usd is None, -(g.cost_usd or 0.0), g.label))
    return CostReport(
        groups=tuple(groups),
        total=aggregate(list(records), label="total", incomplete=bool(unreadable)),
        unreadable=tuple(unreadable),
    )


def select_runs(
    records: Iterable[RunRecord], *, since: datetime | None, workflow: str | None
) -> list[RunRecord]:
    """The runs in scope, newest first.

    ``since`` compares against ``created_at`` (the field ``rayspec runs`` orders by, and the only
    timestamp every record has) and is **inclusive**: a run created exactly at the cutoff is in.
    ``workflow`` matches the recorded name exactly. The roll-up itself regroups and re-sorts, so
    the newest-first order is for callers that reuse this helper to list what was summed.
    """
    chosen = [
        run
        for run in records
        if (workflow is None or run.workflow_name == workflow)
        and (since is None or _aware(run.created_at) >= since)
    ]
    chosen.sort(key=lambda run: (_aware(run.created_at), run.run_id), reverse=True)
    return chosen


def costs_table(report: CostReport) -> Table:
    """The grouped table: one row per workflow (most expensive first), the total last."""
    table = new_table()
    table.add_column("workflow")
    table.add_column("runs", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("cost source")
    for group in report.groups:
        table.add_row(*_row(group, label=Text(safe_text(group.label))))
    table.add_section()
    table.add_row(*_row(report.total, label=Text("total")))
    return table


def _row(group: CostGroup, *, label: Text) -> list[Any]:
    return [
        label,
        str(group.runs),
        _tokens_cell(group),
        common.fmt_cost(group.cost_usd, group.cost_source, group.usage),
        group.breakdown(),
    ]


def _tokens_cell(group: CostGroup) -> str:
    """``12.3k tok`` · ``≥12.3k tok`` (a step was cut off) · ``unknown`` · ``-`` (no tokens).

    ``usage.total`` is itself a lower bound once a step reported nothing, so the column carries
    the same ``≥`` the cost column does — and when *nothing* was reported it says ``unknown``
    rather than ``-``, which would read as "this run used no tokens" (the wording
    ``rayspec run`` uses in its footer for the same record).
    """
    if not group.usage.total:
        return "unknown" if group.tokens_partial else "-"
    return f"{'≥' if group.tokens_partial else ''}{common.fmt_tokens(group.usage.total)}"


def group_payload(group: CostGroup) -> dict[str, Any]:
    """The ``--json`` object of one workflow group."""
    return {
        "workflow": group.label,
        "runs": group.runs,
        "runs_unknown_cost": group.runs_unknown_cost,
        "runs_partial_cost": group.runs_partial_cost,
        "runs_usage_unknown": group.runs_usage_unknown,
        "runs_in_flight": group.runs_in_flight,
        "tokens": group.usage.total,
        "usage": common.usage_dict(group.usage),
        "cost_usd": group.cost_usd,
        "cost_source": group.cost_source,
        "cost_sources": {name: group.buckets[name] for name in BUCKETS if group.buckets.get(name)},
        "first_run_at": None if group.first_run_at is None else group.first_run_at.isoformat(),
        "last_run_at": None if group.last_run_at is None else group.last_run_at.isoformat(),
    }


def costs_payload(
    report: CostReport, *, project: str | None, since: datetime | None, workflow: str | None
) -> dict[str, Any]:
    """The whole ``--json`` object: the totals, the filters that produced them, the groups.

    ``project`` is ``None`` outside a rayspec project — no slug is claimed for a directory that
    is not one, on the filesystem or in the output.
    """
    total = group_payload(report.total)
    del total["workflow"]  # the totals are not a workflow; the filters below name the scope
    return {
        "project": project,
        "since": None if since is None else since.isoformat(),
        "workflow": workflow,
        **total,
        "runs_unreadable": report.runs_unreadable,
        "runs_unreadable_ids": list(report.unreadable),
        "workflows": [group_payload(group) for group in report.groups],
    }


def empty_notice(*, project: str, since: datetime | None, workflow: str | None, runs: Path) -> str:
    """The line a human gets instead of a table when nothing is in scope."""
    scope = [f"project {project}"]
    if workflow is not None:
        scope.append(f"workflow {workflow!r}")
    if since is not None:
        scope.append(f"since {common.fmt_stamp(since)}")
    tail = f" (run dir {runs})" if workflow is None and since is None else " — hint: rayspec runs"
    return f"no runs for {' · '.join(scope)}{tail}"


def scope_line(*, project: str, since: datetime | None, workflow: str | None) -> str:
    """The header above the table: which runs were summed."""
    parts = [f"project {project}"]
    if workflow is not None:
        parts.append(f"workflow {workflow}")
    parts.append(f"since {common.fmt_stamp(since)}" if since else "all runs")
    return " · ".join(parts)


def _runs(count: int) -> str:
    return "1 run" if count == 1 else f"{count} runs"


#: How many lost run ids the notice spells out before it only counts the rest.
NAMED_UNREADABLE = 3


def unreadable_notice(ids: Sequence[str], *, runs: Path) -> str | None:
    """The line for run records the store could not read, or ``None`` when there are none.

    The ids are NAMED, not just counted: a record that cannot be read is missing from
    ``rayspec runs`` as well, so "run `rayspec runs` to see which" would send the reader after
    rows that are not there either. The id is the only handle the run still has.

    For the same reason the line ends at the run DIRECTORY under ``runs`` and not at a command:
    ``rayspec show <id>`` cannot show that run either — it exits 2 and points at the listing
    this notice just refused to point at. The directory is what is left of the run, and what
    somebody looking into it has to open.
    """
    if not ids:
        return None
    count = len(ids)
    noun, verb = ("record", "is") if count == 1 else ("records", "are")
    subject = "its run.json is" if count == 1 else "their run.json files are"
    named = ", ".join(ids[:NAMED_UNREADABLE])
    if count > NAMED_UNREADABLE:
        named += f", … ({count - NAMED_UNREADABLE} more)"
    where = runs / ids[0] if count == 1 else runs
    return (
        f"{count} run {noun} could not be read and {verb} not in these totals: {named} "
        f"— {subject} missing or unparseable ({where})"
    )


def partial_notices(report: CostReport, *, runs: Path) -> list[str]:
    """The lines below the table that keep an incomplete sum honest — empty when nothing is
    missing.

    One line per reason the figures are less than the whole truth (runs with no cost at all,
    priced runs holding an unpriced step, runs whose usage was cut off, runs still in flight,
    records that could not be read) — the first two are counted apart because "this run has no
    cost" and "this run's cost is already a lower bound" are different answers. Then one line
    explaining the marker on screen — and only
    a marker that *is* on screen: when nothing in scope is priced, the cost column is empty
    everywhere and pointing at ``≥`` would send the reader looking for something that is not
    there.
    """
    total = report.total
    lines: list[str] = []
    if total.partial:
        lines.append(
            f"{total.runs_unknown_cost} of {total.runs} runs have no recorded cost "
            f"(dry runs, an unpriced provider, no pricing entry)"
        )
    if total.runs_partial_cost:
        verb = "holds" if total.runs_partial_cost == 1 else "hold"
        lines.append(
            f"{total.runs_partial_cost} of {total.runs} priced runs {verb} steps with tokens "
            f"but no price"
        )
    if total.runs_usage_unknown:
        lines.append(
            f"{_runs(total.runs_usage_unknown)} had a step cut off before it reported usage "
            f"— tokens and cost are lower bounds"
        )
    if total.runs_in_flight:
        verb = "is" if total.runs_in_flight == 1 else "are"
        lines.append(
            f"{_runs(total.runs_in_flight)} {verb} still running or paused — those figures "
            f"are not final"
        )
    unreadable = unreadable_notice(report.unreadable, runs=runs)
    if unreadable is not None:
        lines.append(unreadable)
    if not lines:
        return []
    if total.cost_usd is None:
        lines.append("no cost is known for any run in scope")
    elif cost_marker(total.cost_source) == "≥":
        lines.append("totals marked ≥ are a lower bound")
    return lines


def register(app: typer.Typer) -> None:
    @app.command()
    def costs(
        since: Annotated[
            str | None,
            typer.Option(
                "--since",
                help="Only runs created at or after this point: a window (7d, 24h, 90m) or a "
                "date (2026-08-01).",
                show_default=False,
            ),
        ] = None,
        workflow: Annotated[
            str | None,
            typer.Option(
                "--workflow", help="Only runs of this workflow (exact name).", show_default=False
            ),
        ] = None,
        json_: JsonOption = False,
        output: OutputOption = None,
        root: RootOption = None,
    ) -> None:
        """Sum what this project's runs cost, grouped by workflow."""
        json_ = resolve_output(output, json_)
        from rayspec.cli.commands.runs import is_project_dir

        cutoff: datetime | None = None
        if since is not None:
            try:
                cutoff = parse_since(since)
            except ValueError as exc:
                fail(str(exc), hint=SINCE_HINT)
        ctx = common.make_runs_context(root)
        out = console()
        if not is_project_dir(ctx.project_root):
            # the same rule as `rayspec runs`: an arbitrary directory is not a project, and
            # answering "$0.00" for one would be a lie rather than an empty result
            err_console().print(
                f"not inside a rayspec project (no .rayspec/ or git repo at or above "
                f"{ctx.project_root}) — hint: rayspec costs --root <project dir>",
                markup=False,
                highlight=False,
            )
            if json_:
                empty = build_report([])
                # no slug is minted for this directory, so none is reported either
                print_json(costs_payload(empty, project=None, since=cutoff, workflow=workflow))
            return
        # list_runs() drops a run.json it cannot parse (a log warning the CLI suppresses) and
        # never sees one that is missing; both leave a run out of the sum, which is the one thing
        # a total may not hide, so they are collected by id and reported below the table
        loaded = ctx.store.list_runs(limit=None)
        records = select_runs(loaded, since=cutoff, workflow=workflow)
        report = build_report(
            records, unreadable=unreadable_run_ids(ctx.store, loaded, since=cutoff)
        )
        if json_:
            print_json(costs_payload(report, project=ctx.slug, since=cutoff, workflow=workflow))
            return
        if not records:
            out.print(
                empty_notice(
                    project=ctx.slug,
                    since=cutoff,
                    workflow=workflow,
                    runs=ctx.store.root / "runs",
                ),
                markup=False,
                highlight=False,
            )
            unreadable = unreadable_notice(report.unreadable, runs=ctx.store.runs_root)
            if unreadable is not None:
                out.print(unreadable, style="dim", markup=False, highlight=False)
            return
        out.print(
            scope_line(project=ctx.slug, since=cutoff, workflow=workflow),
            style="dim",
            markup=False,
            highlight=False,
        )
        out.print(costs_table(report))
        for note in partial_notices(report, runs=ctx.store.runs_root):
            out.print(note, style="dim", markup=False, highlight=False)


__all__ = [
    "BUCKETS",
    "IN_FLIGHT",
    "NAMED_UNREADABLE",
    "SINCE_HINT",
    "UNKNOWN",
    "CostGroup",
    "CostReport",
    "aggregate",
    "build_report",
    "cost_bucket",
    "costs_payload",
    "costs_table",
    "empty_notice",
    "group_payload",
    "parse_since",
    "partial_notices",
    "register",
    "run_id_created_at",
    "save_in_flight",
    "scope_line",
    "select_runs",
    "unreadable_notice",
    "unreadable_run_ids",
]

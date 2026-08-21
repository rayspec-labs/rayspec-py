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
"""

from __future__ import annotations

import json
import re
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
from rayspec.cli.commands._loader_common import JsonOption, RootOption, console, err_console, fail
from rayspec.providers.base import Usage
from rayspec.providers.pricing import COST_SOURCES, combine_cost_sources
from rayspec.store.model import RunRecord
from rayspec.textsafe import safe_text

#: The bucket a run lands in when it carries no cost at all — deliberately not one of
#: :data:`~rayspec.providers.pricing.COST_SOURCES`, because "no price was found" and "this run
#: was never priced" are different answers and only the second one makes a total a lower bound.
UNKNOWN = "unknown"

#: Breakdown buckets in the order they are printed.
BUCKETS: tuple[str, ...] = (*COST_SOURCES, UNKNOWN)

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


def cost_bucket(run: RunRecord) -> str:
    """Which breakdown bucket ``run`` belongs to: its run-level cost source, or ``unknown``.

    A run with no cost at all is ``unknown`` whatever its steps recorded — that is the fact the
    reader needs, because it is the reason the enclosing total is a lower bound.
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

    def breakdown(self) -> str:
        """``1 provider · 2 table · 1 unknown`` — every run accounted for, in a fixed order."""
        parts = [f"{self.buckets[name]} {name}" for name in BUCKETS if self.buckets.get(name)]
        return " · ".join(parts) if parts else "-"


def aggregate(records: Sequence[RunRecord], *, label: str) -> CostGroup:
    """Fold runs into one :class:`CostGroup`. Every record is counted, priced or not."""
    usage = Usage()
    total: float | None = None
    unknown = 0
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
        buckets[cost_bucket(run)] += 1
    stamps = [_aware(run.created_at) for run in records]
    return CostGroup(
        label=label,
        runs=len(records),
        runs_unknown_cost=unknown,
        usage=usage,
        cost_usd=total,
        # an unpriced run makes the sum a lower bound exactly the way an unpriced step makes a
        # run's own sum one, so the run-level fold is reused rather than re-invented here
        cost_source=combine_cost_sources(sources, unpriced=unknown > 0 or "partial" in sources),
        buckets=dict(buckets),
        first_run_at=min(stamps) if stamps else None,
        last_run_at=max(stamps) if stamps else None,
    )


@dataclass(frozen=True, slots=True)
class CostReport:
    """The whole answer: one group per workflow plus the total over the same runs."""

    groups: tuple[CostGroup, ...]
    total: CostGroup


def build_report(records: Sequence[RunRecord]) -> CostReport:
    """Group ``records`` by workflow name, most expensive first, then by name.

    An unpriced group sorts last (its cost is unknown, not zero) but is never dropped, so the
    run counts of the groups always add up to the total's.
    """
    by_workflow: dict[str, list[RunRecord]] = {}
    for run in records:
        by_workflow.setdefault(run.workflow_name, []).append(run)
    groups = [aggregate(runs, label=name) for name, runs in by_workflow.items()]
    groups.sort(key=lambda g: (-(g.cost_usd or 0.0), g.label))
    return CostReport(groups=tuple(groups), total=aggregate(list(records), label="total"))


def select_runs(
    records: Iterable[RunRecord], *, since: datetime | None, workflow: str | None
) -> list[RunRecord]:
    """The runs in scope, newest first.

    ``since`` compares against ``created_at`` (the field ``rayspec runs`` orders by, and the only
    timestamp every record has) and is **inclusive**: a run created exactly at the cutoff is in.
    ``workflow`` matches the recorded name exactly.
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
    table = Table(show_edge=False, pad_edge=False, box=None, header_style="bold")
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
        common.fmt_tokens(group.usage.total) if group.usage.total else "-",
        common.fmt_cost(group.cost_usd, group.cost_source, group.usage),
        group.breakdown(),
    ]


def group_payload(group: CostGroup) -> dict[str, Any]:
    """The ``--json`` object of one workflow group."""
    return {
        "workflow": group.label,
        "runs": group.runs,
        "runs_unknown_cost": group.runs_unknown_cost,
        "tokens": group.usage.total,
        "usage": common.usage_dict(group.usage),
        "cost_usd": group.cost_usd,
        "cost_source": group.cost_source,
        "cost_sources": {name: group.buckets[name] for name in BUCKETS if group.buckets.get(name)},
        "first_run_at": None if group.first_run_at is None else group.first_run_at.isoformat(),
        "last_run_at": None if group.last_run_at is None else group.last_run_at.isoformat(),
    }


def costs_payload(
    report: CostReport, *, project: str, since: datetime | None, workflow: str | None
) -> dict[str, Any]:
    """The whole ``--json`` object: the totals, the filters that produced them, the groups."""
    total = group_payload(report.total)
    del total["workflow"]  # the totals are not a workflow; the filters below name the scope
    return {
        "project": project,
        "since": None if since is None else since.isoformat(),
        "workflow": workflow,
        **total,
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


def partial_notice(total: CostGroup) -> str | None:
    """The line that keeps an incomplete sum honest, or ``None`` when nothing is missing.

    Two ways a total can be a lower bound, and the reader is owed the difference: whole runs
    that carry no cost at all, or priced runs that contain an unpriced step.
    """
    if total.partial:
        return (
            f"{total.runs_unknown_cost} of {total.runs} runs have no recorded cost "
            f"(dry runs, an unpriced provider, no pricing entry) — totals marked ≥ are a "
            f"lower bound"
        )
    if total.cost_source == "partial":
        return "some runs have steps with tokens but no price — totals marked ≥ are a lower bound"
    return None


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
        root: RootOption = None,
    ) -> None:
        """Sum what this project's runs cost, grouped by workflow."""
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
                out.print(
                    json.dumps(
                        costs_payload(empty, project=ctx.slug, since=cutoff, workflow=workflow),
                        ensure_ascii=False,
                    ),
                    markup=False,
                    highlight=False,
                )
            return
        records = select_runs(ctx.store.list_runs(limit=None), since=cutoff, workflow=workflow)
        report = build_report(records)
        if json_:
            out.print(
                json.dumps(
                    costs_payload(report, project=ctx.slug, since=cutoff, workflow=workflow),
                    ensure_ascii=False,
                ),
                markup=False,
                highlight=False,
            )
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
            return
        out.print(
            scope_line(project=ctx.slug, since=cutoff, workflow=workflow),
            style="dim",
            markup=False,
            highlight=False,
        )
        out.print(costs_table(report))
        note = partial_notice(report.total)
        if note is not None:
            out.print(note, style="dim", markup=False, highlight=False)


__all__ = [
    "BUCKETS",
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
    "partial_notice",
    "register",
    "scope_line",
    "select_runs",
]

# SPDX-License-Identifier: Apache-2.0
"""Helpers shared by the run-management commands (``runs``, ``show``, ``logs``, ``resume``,
``approve``, ``reject``, ``cancel``).

Module boundary: store discovery (``$RAYSPEC_HOME/projects/<slug>``), run lookup with friendly
errors (unique prefixes, fallback to every project), formatting (durations, tokens, cost via
``providers.pricing.format_cost``, relative timestamps), output previews, and the glue that turns
a stored :class:`RunRecord` back into a resumable :class:`~rayspec.engine.runner.Runner`. No
business logic beyond that lives here — the engine and the store own the semantics.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rayspec.cli.commands import _loader_common as loader_common
from rayspec.cli.commands._loader_common import Context, fail, make_context
from rayspec.config import Config
from rayspec.engine.runtime import EXIT_USAGE
from rayspec.errors import RayspecError
from rayspec.loader import ResolvedWorkflow
from rayspec.providers.base import Usage
from rayspec.providers.pricing import combine_cost_sources, format_cost, format_tokens
from rayspec.schema import RunStatus, StepStatus
from rayspec.store.file import (
    AmbiguousRunIdError,
    FileRunStore,
    StoreError,
    UnknownRunIdError,
)
from rayspec.store.model import RunRecord, StepRecord
from rayspec.textsafe import safe_text

if TYPE_CHECKING:
    from rayspec.providers.stub import StubScript
    from rayspec.secrets import SecretProvider

_log = logging.getLogger(__name__)

#: Characters kept of an output preview (``rayspec show``).
PREVIEW_LIMIT = 60

#: Statuses whose listings count the *planned* steps of the workflow in the total: the
#: run may still continue (live, paused, or resumable with ``rayspec resume``), so the recorded
#: steps under-report its size. Only a succeeded run is final.
_RESUMABLE_STATUSES = frozenset(
    {
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.INTERRUPTED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)


# --------------------------------------------------------------------------------------------------
# context + stores
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RunsContext:
    """Resolved project/home/config plus the project's run store."""

    project_root: Path
    home: Path
    config: Config
    slug: str
    store: FileRunStore

    @property
    def loader_context(self) -> Context:
        """The read-only loader context (``load_workflow`` needs it)."""
        return Context(project_root=self.project_root, home=self.home, config=self.config)


def project_store(home: Path, slug: str) -> FileRunStore:
    """The :class:`FileRunStore` of one project (``<home>/projects/<slug>``)."""
    return FileRunStore(home / "projects" / slug)


def make_runs_context(root: Path | None) -> RunsContext:
    """Resolve the project root (``--root`` / walk up), ``RAYSPEC_HOME``, config and store."""
    from rayspec.cli.commands.run import project_slug_for

    base = make_context(root)
    slug = project_slug_for(base.project_root)
    return RunsContext(
        project_root=base.project_root,
        home=base.home,
        config=base.config,
        slug=slug,
        store=project_store(base.home, slug),
    )


#: Directory names under a project dir that hold checkouts, never run stores.
_NON_STORE_DIRS = frozenset({"worktrees", "source.git", "locks"})


def iter_project_stores(home: Path) -> Iterator[tuple[str, FileRunStore]]:
    """Every ``(slug, store)`` under ``<home>/projects`` that has a ``runs/`` directory.

    ``projects/`` is walked recursively (slugs are ``local/<name>``, ``host/owner/repo`` or
    deeper for subgroup remotes such as ``gitlab.com/group/sub/repo``); a directory with a
    ``runs/`` child is a store and is not descended into, and ``worktrees/``, ``source.git/``
    and ``locks/`` are never entered — a ``runs`` directory inside a checkout is not a store.
    """
    projects = home / "projects"
    if not projects.is_dir():
        return
    found: list[tuple[str, FileRunStore]] = []
    for dirpath, dirnames, _filenames in os.walk(projects):
        current = Path(dirpath)
        if current != projects and "runs" in dirnames and (current / "runs").is_dir():
            parts = current.relative_to(projects).parts
            found.append(("/".join(parts), FileRunStore(current)))
            dirnames[:] = []  # a store never contains another store
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in _NON_STORE_DIRS)
    found.sort(key=lambda item: item[0])
    yield from found


def find_run(ctx: RunsContext, ref: str) -> tuple[FileRunStore, RunRecord]:
    """Resolve ``ref`` (full id or unique prefix) to ``(store, record)``.

    The current project's store is consulted first; when nothing matches there, every project
    under the home is searched (a prefix matching runs in several projects is ambiguous).
    Raises :class:`UnknownRunIdError` / :class:`AmbiguousRunIdError`.
    """
    try:
        run_id = ctx.store.resolve_run_id(ref)
    except UnknownRunIdError:
        pass
    else:
        return ctx.store, ctx.store.load(run_id)
    matches: list[tuple[FileRunStore, str]] = []
    for slug, store in iter_project_stores(ctx.home):
        if slug == ctx.slug:
            continue
        try:
            matches.append((store, store.resolve_run_id(ref)))
        except AmbiguousRunIdError as exc:
            matches.extend((store, rid) for rid in exc.candidates)
        except UnknownRunIdError:
            continue
    if not matches:
        raise UnknownRunIdError(
            f"no run matches {ref!r}", hint="run `rayspec runs --all` to list known runs"
        )
    if len(matches) > 1:
        raise AmbiguousRunIdError(ref, sorted((rid for _, rid in matches), reverse=True))
    store, run_id = matches[0]
    return store, store.load(run_id)


def lookup_run(ctx: RunsContext, ref: str) -> tuple[FileRunStore, RunRecord]:
    """:func:`find_run` that prints a CLI error (exit 2) instead of raising."""
    try:
        return find_run(ctx, ref)
    except StoreError as exc:
        fail(str(exc), hint=exc.hint)
        raise AssertionError("unreachable") from None  # pragma: no cover


# --------------------------------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------------------------------


def fmt_duration(ms: float | None) -> str:
    """``850ms`` · ``12.3s`` · ``1m35s`` · ``1h02m``; ``-`` when unknown."""
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{int(ms)}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def fmt_tokens(total: int) -> str:
    """``850 tok`` / ``12.3k tok`` / ``1.5M tok`` (see ``providers.pricing.format_tokens``)."""
    return format_tokens(total)


def fmt_cost(cost_usd: float | None, cost_source: str, usage: Usage) -> str:
    """``$0.12`` / ``~$0.12`` (price table) / ``≥$0.12`` (partial: some steps have tokens but
    no price); ``-`` when no cost is known.

    Tokens are never shown in a cost slot (stub/dry runs and providers without cost reporting
    have tokens but no price) — callers print :func:`fmt_tokens` in a ``tokens`` column.
    """
    if cost_usd is None:
        return "-"
    return format_cost(cost_usd, cost_source, usage)


def fmt_when(moment: datetime | None, *, now: datetime | None = None) -> str:
    """Relative (``30s ago``, ``5m ago``, ``3h ago``, ``2d ago``) within a month, else a date."""
    if moment is None:
        return "-"
    now = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delta = now - moment
    seconds = int(delta.total_seconds())
    seconds = max(seconds, 0)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    if seconds < 30 * 86_400:
        return f"{seconds // 86_400}d ago"
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def fmt_stamp(moment: datetime | None) -> str:
    """``2026-08-20 10:00:00 UTC`` or ``-``."""
    if moment is None:
        return "-"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def run_duration_ms(run: RunRecord, *, now: datetime | None = None) -> int | None:
    """Wall-clock duration (``ended_at - started_at``; still ticking for a running run)."""
    if run.started_at is None:
        return None
    end = run.ended_at
    if end is None:
        if run.status is not RunStatus.RUNNING:
            return None
        end = now or datetime.now(UTC)
    start = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=UTC)
    end = end if end.tzinfo else end.replace(tzinfo=UTC)
    return max(0, int((end - start).total_seconds() * 1000))


def _is_done(rec: StepRecord) -> bool:
    """A record the engine resolved: succeeded, tolerated failure or skipped."""
    return (
        rec.status is StepStatus.SUCCEEDED
        or rec.status is StepStatus.SKIPPED
        or (rec.status is StepStatus.FAILED and rec.tolerated)
    )


def steps_progress(run: RunRecord, *, planned: Iterable[str] | None = None) -> tuple[int, int]:
    """``(done, total)`` for the ``steps`` column.

    ``done`` = succeeded, tolerated or skipped records (a succeeded run reads ``n/n``);
    ``total`` = the recorded step paths plus ``planned`` (the workflow's static step paths —
    see :func:`planned_step_paths`) so an unfinished or resumable run shows how much is left.
    """
    done = sum(1 for rec in run.steps.values() if _is_done(rec))
    paths = set(run.steps)
    if planned is not None:
        paths.update(planned)
    return done, len(paths)


def steps_detail(run: RunRecord) -> str:
    """``3 ok · 2 skipped`` — the breakdown shown by ``rayspec show`` (tolerated failures are
    ``ok``; the ``skipped`` part is omitted when no step was skipped)."""
    skipped = sum(1 for rec in run.steps.values() if rec.status is StepStatus.SKIPPED)
    ok = sum(1 for rec in run.steps.values() if _is_done(rec)) - skipped
    detail = f"{ok} ok"
    if skipped:
        detail += f" · {skipped} skipped"
    return detail


def planned_step_paths(
    ctx: RunsContext, run: RunRecord, *, cache: dict[tuple[str, str], set[str] | None] | None = None
) -> set[str] | None:
    """The workflow's statically known step paths (root steps and ``include:`` bodies, not the
    iterations of ``loop``/``each`` bodies) for a run that may still continue or be resumed
    (running/paused/interrupted/failed/cancelled) — ``None`` for a succeeded run or when the
    workflow cannot be loaded any more (old record, file gone, *any* loader failure: a listing
    must never fail because a workflow somewhere is broken).

    ``cache`` (one dict per listing) memoises the result per ``(project root, workflow)`` so
    ``rayspec runs --all`` loads every workflow file once, not once per run.
    """
    if run.status not in _RESUMABLE_STATUSES:
        return None
    key = (run.project_root or str(ctx.project_root), run.workflow_path or run.workflow_name)
    if cache is not None and key in cache:
        return cache[key]
    static: set[str] | None = set()
    try:
        resolved = load_resolved_for(ctx, run)
    except Exception as exc:  # best effort, see docstring
        _log.debug("planned steps of %s unavailable: %s", run.run_id, exc)
        static = None
    else:
        assert static is not None
        for graph in resolved.graphs():
            if graph.kind == "root" or (
                graph.kind == "include"
                and graph.parent_path is not None
                and graph.parent_path in static
            ):
                static.update(graph.path_of(step) for step in graph.steps)
    if cache is not None:
        cache[key] = static
    return static


def unpriced_steps(run: RunRecord) -> int:
    """Steps that reported tokens but no cost (unpriced provider, no pricing entry)."""
    return sum(1 for rec in run.steps.values() if rec.usage.total and rec.cost_usd is None)


def run_cost_source(run: RunRecord) -> str:
    """Run-level cost source: ``provider`` · ``table`` (any estimate) · ``partial`` (some
    steps have tokens but no cost) · ``none`` — see ``providers.pricing.combine_cost_sources``."""
    sources = [rec.cost_source for rec in run.steps.values() if rec.cost_usd is not None]
    return combine_cost_sources(sources, unpriced=unpriced_steps(run) > 0)


def status_style(status: str) -> str:
    """Rich style for a run/step status."""
    return {
        "succeeded": "green",
        "running": "cyan",
        "paused": "yellow",
        "cancelled": "yellow",
        "skipped": "dim",
        "pending": "dim",
    }.get(status, "red")


def usage_dict(usage: Usage) -> dict[str, int]:
    """The ``--json`` shape of a :class:`Usage`."""
    return {
        "input": usage.input,
        "cached_input": usage.cached_input,
        "cache_write": usage.cache_write,
        "output": usage.output,
        "reasoning": usage.reasoning,
    }


def run_row(
    run: RunRecord, *, now: datetime | None = None, planned: Iterable[str] | None = None
) -> dict[str, Any]:
    """One ``rayspec runs --json`` row (``planned`` = :func:`planned_step_paths`)."""
    done, total = steps_progress(run, planned=planned)
    skipped = sum(1 for rec in run.steps.values() if rec.status is StepStatus.SKIPPED)
    usage = run.total_usage()
    return {
        "run_id": run.run_id,
        "workflow": run.workflow_name,
        "status": run.status.value,
        "reason": run.reason,
        "project_slug": run.project_slug,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "ended_at": _iso(run.ended_at),
        "duration_ms": run_duration_ms(run, now=now),
        "steps_done": done,
        "steps_total": total,
        "steps_ok": done - skipped,
        "steps_skipped": skipped,
        "tokens": usage.total,
        "usage": usage_dict(usage),
        "cost_usd": run.total_cost_usd(),
        "cost_source": run_cost_source(run),
        "resume_count": run.resume_count,
        "dry_run": run.dry_run,
        "pid": run.pid,
        "host": run.host,
        "workspace": run.workspace.model_dump(mode="json"),
        "pause": run.pause.model_dump(mode="json") if run.pause else None,
    }


def step_row(store: FileRunStore, run: RunRecord, rec: StepRecord) -> dict[str, Any]:
    """One step entry of ``rayspec show --json`` (record + output preview)."""
    data = rec.model_dump(mode="json")
    data["usage"] = usage_dict(rec.usage)
    data["tokens"] = rec.usage.total
    data["output_preview"] = output_preview(store, run, rec)
    return data


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


# --------------------------------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------------------------------


def output_preview(
    store: FileRunStore, run: RunRecord, rec: StepRecord, *, limit: int = PREVIEW_LIMIT
) -> str:
    """First line of the step output (JSON outputs compacted to one line; ``…`` appended when
    cut); ``''`` when unavailable."""
    if rec.output_ref is None:
        return ""
    try:
        text = store.read_output(run.run_id, rec.output_ref)
    except (OSError, ValueError):
        return ""
    if rec.output_kind == "json":
        with contextlib.suppress(ValueError):
            text = json.dumps(json.loads(text), ensure_ascii=False, separators=(", ", ": "))
    lines = safe_text(text).splitlines()
    if not lines:
        return ""
    first = lines[0].rstrip()
    cut = len(lines) > 1 or len(first) > limit
    if len(first) > limit:
        first = first[: max(0, limit - 1)].rstrip()
    return f"{first} …" if cut and first else first


def read_output_text(store: FileRunStore, run: RunRecord, rec: StepRecord) -> str | None:
    """Full output text of a step (``None`` when absent/unreadable)."""
    if rec.output_ref is None:
        return None
    try:
        return store.read_output(run.run_id, rec.output_ref)
    except (OSError, ValueError):
        return None


def value_text(value: Any) -> str:
    """Render a workflow output value for a table cell (plain text, escapes removed)."""
    if isinstance(value, str):
        return safe_text(value)
    return safe_text(json.dumps(value, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------------------------------
# record & replay
# --------------------------------------------------------------------------------------------------


def _path_is_ordered(run: RunRecord, path: str) -> bool:
    """Whether every composite this step sits inside iterated in a guaranteed order.

    A ``loop:`` body does (iteration 2 starts after iteration 1); an ``each:`` body does not
    (items run in parallel), so its answers must not be recorded as an order-dependent
    ``sequence:``. An ancestor whose record is missing is assumed ordered.
    """
    from rayspec.engine.paths import StepPath

    try:
        step = StepPath.parse(path)
    except ValueError:
        return True
    for i, (name, index) in enumerate(step.segments[:-1]):
        if index is None:
            continue
        ancestor = StepPath((*step.segments[:i], (name, None)))
        rec = run.steps.get(str(ancestor))
        if rec is not None and rec.kind == "each":
            return False
    return True


def _recorded_failure(rec: StepRecord) -> Any:
    """The step's error as a :class:`~rayspec.providers.stub.StubFailure`, or ``None``."""
    from typing import cast, get_args

    from rayspec.providers.base import ErrorKind
    from rayspec.providers.stub import StubFailure

    if rec.status is not StepStatus.FAILED or rec.error is None:
        return None
    known: frozenset[str] = frozenset(get_args(ErrorKind))
    kind = cast("ErrorKind", rec.error.type if rec.error.type in known else "api")
    return StubFailure(kind=kind, message=rec.error.message, transient=rec.error.transient)


#: prompt-step statuses that never produced an answer: they are left out of a recording entirely
#: rather than written as an entry with no answer (which would replay as the stub provider's
#: built-in default and be indistinguishable from a faithful recording).
_UNANSWERED_STATUSES = frozenset(
    {
        StepStatus.SKIPPED,
        StepStatus.PENDING,
        StepStatus.RUNNING,
        StepStatus.PAUSED,
        StepStatus.INTERRUPTED,
        StepStatus.REJECTED,
    }
)


def recorded_calls(store: FileRunStore, run: RunRecord) -> list[Any]:
    """Every prompt step of ``run`` as a :class:`~rayspec.providers.stub.RecordedCall`.

    Only ``prompt:`` steps are agent calls; shell/python/approve steps and composites are not
    scripted by a stub file. Steps that never got an answer (skipped, never started, still
    running, paused, interrupted, rejected) are left out — the script must not claim an answer
    the run never got.

    A step that *does* claim an answer rayspec cannot read (its ``output_ref`` file was pruned,
    or a succeeded step has no output at all) is a **usage error**, exit 2 naming the step and the
    missing ref: an entry without an answer replays as the stub provider's default, which looks
    exactly like a faithful replay.
    """
    from rayspec.providers.stub import RecordedCall

    calls: list[RecordedCall] = []
    for path in sorted(run.steps):
        rec = run.steps[path]
        if rec.kind != "prompt" or rec.status in _UNANSWERED_STATUSES:
            continue
        text: str | None = None
        output: Any = None
        has_output = False
        raw = read_output_text(store, run, rec)
        if raw is not None:
            if rec.output_kind == "json":
                try:
                    output, has_output = json.loads(raw), True
                except ValueError:
                    text = raw
            else:
                text = raw
        failure = _recorded_failure(rec)
        if raw is None and failure is None:
            if rec.output_ref is not None or rec.status is StepStatus.SUCCEEDED:
                missing = rec.output_ref or "no output file"
                fail(
                    f"run {run.run_id}: step {path} ({rec.status.value}) has no readable output "
                    f"({missing}) — a stub script cannot claim an answer rayspec cannot read",
                    hint="record another run of the workflow, or restore the run directory "
                    f"({store.run_dir(run.run_id)})",
                )
            continue  # never answered and nothing claimed: leave the step out of the script
        calls.append(
            RecordedCall(
                step_path=path,
                text=text,
                output=output,
                has_output=has_output,
                usage=rec.usage if rec.usage.total else None,
                failure=failure,
                sequential=_path_is_ordered(run, path),
            )
        )
    return calls


def recording_notes(run: RunRecord) -> list[str]:
    """One line per lossy substitution a recording of ``run`` makes (empty when it is exact).

    Today that is only the error kind: the stub script's ``fail.kind`` is a provider
    :data:`~rayspec.providers.base.ErrorKind`, so an engine-level failure type (``rejected``,
    ``exit``, …) is recorded as ``api``. The author can correct the entry by hand — but only if
    the substitution is said out loud.
    """
    from typing import get_args

    from rayspec.providers.base import ErrorKind

    known: frozenset[str] = frozenset(get_args(ErrorKind))
    notes: list[str] = []
    for path in sorted(run.steps):
        rec = run.steps[path]
        if rec.kind != "prompt" or rec.status is not StepStatus.FAILED or rec.error is None:
            continue
        if rec.error.type not in known:
            notes.append(
                f"step {path} failed with error type {rec.error.type!r}, which a stub script "
                f"cannot express — recorded as `fail: {{kind: api}}`"
            )
    return notes


def stub_script_data(store: FileRunStore, run: RunRecord) -> dict[str, Any]:
    """The recorded run as a stub-script mapping (YAML-dumpable, ``StubScript.from_dict``-able)."""
    from rayspec.providers.stub import record_script

    return record_script(recorded_calls(store, run))


#: Refusal shown for a run whose inputs were declared ``secret: true``: its prompts
#: may quote the secret, and a stub script is a plain file meant to be committed.
def secret_refusal(run: RunRecord) -> str | None:
    """The refusal message for a run with secret inputs, or ``None`` when recording is safe."""
    if not run.secret_inputs:
        return None
    names = ", ".join(run.secret_inputs)
    return (
        f"run {run.run_id} was launched with secret input(s) {names}: its recorded prompts and "
        f"outputs may quote them, so rayspec refuses to write them into a stub script"
    )


def secret_output_notice(run: RunRecord) -> str | None:
    """Warning for a reader that prints stored step OUTPUT of a run with secret inputs.

    Unlike a stub script (a file meant to be committed — :func:`secret_refusal` refuses that
    outright), showing an output is what the reader was asked to do; ``rayspec show`` and
    ``rayspec logs`` print the same bytes that were stored.

    Those bytes went through :class:`rayspec.redact.Redactor` on the way in, so an
    exact occurrence of a secret is already ``[REDACTED:<name>]``. The notice stays because
    redaction is **exact-match and best-effort**: a value the step transformed, truncated or
    re-encoded (``${TOKEN:0:4}``, base64, a hash) is not an exact occurrence and survives.
    """
    if not run.secret_inputs:
        return None
    names = ", ".join(run.secret_inputs)
    return (
        f"note: run {run.run_id} was launched with secret input(s) {names} — exact occurrences "
        f"were redacted when stored, but a value a step transformed or truncated can survive"
    )


def replay_source(ref: str, *, root: Path | None) -> tuple[StubScript, str]:
    """``(script, full run id)`` for ``--stubs-from <run>``.

    ``ref`` is a run id or a unique prefix, resolved in the current project first and then in
    every project under the home. Unknown/ambiguous ids and runs with secret inputs are usage
    errors (exit 2) — nothing is executed before the script exists. The resolved id is what the
    launching run records (``run.json``'s ``stubs_path`` as ``run:<id>``), so a resume entry can
    rebuild the very same script instead of answering with the stub provider's default.
    """
    from rayspec.providers.stub import StubScript

    ctx = make_runs_context(root)
    store, record = lookup_run(ctx, ref)
    refusal = secret_refusal(record)
    if refusal is not None:
        fail(refusal, hint="re-run the workflow without the secret inputs to record it")
    for note in recording_notes(record):
        loader_common.err_console().print(note, markup=False, highlight=False)
    script = StubScript.from_dict(stub_script_data(store, record), source=f"run {record.run_id}")
    return script, record.run_id


def replay_script(ref: str, *, root: Path | None) -> StubScript:
    """The recorded answers of ``ref`` as a live :class:`StubScript` (see :func:`replay_source`)."""
    return replay_source(ref, root=root)[0]


# --------------------------------------------------------------------------------------------------
# resume glue
# --------------------------------------------------------------------------------------------------


def load_resolved_for(ctx: RunsContext, run: RunRecord) -> ResolvedWorkflow:
    """Re-load the run's workflow: by its recorded path (relative to the project root or the home),
    then by name. Raises :class:`RayspecError` when neither resolves."""
    from rayspec.loader import load_workflow

    project_root = Path(run.project_root) if run.project_root else ctx.project_root
    if not project_root.is_dir():
        project_root = ctx.project_root
    candidates: list[str | Path] = []
    label = run.workflow_path
    if label:
        if label.startswith("~/.rayspec/"):
            candidates.append(ctx.home / label[len("~/.rayspec/") :])
        elif Path(label).is_absolute():
            candidates.append(Path(label))
        else:
            candidates.append(project_root / label)
    candidates.append(run.workflow_name)
    last: RayspecError | None = None
    for candidate in candidates:
        if isinstance(candidate, Path) and not candidate.is_file():
            continue
        try:
            return load_workflow(
                candidate, project_root=project_root, home=ctx.home, config=ctx.config
            )
        except RayspecError as exc:
            last = exc
    assert last is not None
    raise last


def check_workflow_unchanged(run: RunRecord, resolved: ResolvedWorkflow, *, force: bool) -> None:
    """Mirror the engine's resume rule up front: a changed workflow is refused unless ``force``.

    Lets callers that persist something before resuming (``approve``/``reject`` write the
    decision) refuse *before* touching ``run.json``. Raises :class:`~rayspec.engine.errors.
    ResumeError` with the engine's wording and hint.
    """
    from rayspec.engine.errors import ResumeError

    if force or run.workflow_hash == resolved.hash:
        return
    raise ResumeError(
        f"workflow {resolved.workflow.name!r} changed since run {run.run_id} "
        f"(hash {run.workflow_hash[:12]} → {resolved.hash[:12]})",
        hint="pass --force to resume anyway (changed steps are re-run)",
    )


def resume_run(
    ctx: RunsContext,
    store: FileRunStore,
    run: RunRecord,
    *,
    force: bool = False,
    yes: bool = False,
    interactive: bool = False,
    json_mode: bool = False,
    quiet: bool = False,
    verbose: bool = False,
    resolved: ResolvedWorkflow | None = None,
    inputs: Mapping[str, Any] | None = None,
    stub_script: StubScript | None = None,
    stubs_path: str | None = None,
    secret_provider: SecretProvider | None = None,
) -> int:
    """Resume ``run`` in-process through the engine runner and print the summary.

    ``resolved`` is the run's workflow when the caller already loaded it (otherwise it is loaded
    with :func:`load_resolved_for`). ``inputs`` are the re-supplied **secret** inputs (the
    other inputs come from ``run.json``); ``stub_script`` / ``stubs_path`` script the stub
    provider (``stubs_path`` replaces the recorded file when given). A ``--dry-run`` record
    resumes as a dry run. Returns the process exit code (0 succeeded · 1 failed · 2 usage ·
    3 paused · 4 cancelled · 130 interrupted). Errors (workflow gone, hash mismatch, live pid)
    are printed with exit 2.

    ``secret_provider`` is the caller's own :class:`~rayspec.secrets.SecretProvider` — the
    same instance :func:`~rayspec.cli.commands.resume.resume_secret_inputs` already used, so a
    ``cmd:`` helper runs at most once per command; one is built here only when the caller has
    none.
    """
    import anyio

    from rayspec.cli.commands.run import (
        _sinks,
        approval_prompt_for,
        configured_approval,
        print_summary,
        warn_unredactable_secrets,
        workspace_from_record,
    )
    from rayspec.engine.context import RunOptions
    from rayspec.engine.errors import EngineError
    from rayspec.engine.runner import Runner
    from rayspec.providers.pricing import PriceTable
    from rayspec.secrets import SecretError, build_redactor, provider_for, used_config_secrets

    out = loader_common.err_console() if json_mode else loader_common.console()
    if resolved is None:
        try:
            resolved = load_resolved_for(ctx, run)
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return EXIT_USAGE
    project_root = Path(run.project_root) if Path(run.project_root).is_dir() else ctx.project_root
    workspace = workspace_from_record(run, project_root)
    # the run's secret sources — they feed the shell/python step env and, together with
    # the re-supplied secret inputs, the one redactor every writer goes through. Only the
    # entries this workflow reads are resolved, and the caller's provider is reused so a `cmd:`
    # helper is not run (or prompted for) twice in one command.
    if secret_provider is None:
        secret_provider = provider_for(ctx.config, base_dir=project_root)
    try:
        config_secrets = used_config_secrets(
            secret_provider, [s for _, s in resolved.all_steps()], ctx.config.secrets
        )
    except SecretError as exc:
        fail(str(exc), hint=exc.hint)
        return EXIT_USAGE
    redactor = build_redactor(ctx.config, {**config_secrets, **dict(inputs or {})})
    store.redactor = redactor
    warn_unredactable_secrets(out, redactor)  # a value too short to redact is named
    # `config.extensions` applies to the second half of a run exactly as it did to the first:
    # an audit sink that observed the steps before the gate observes the ones after it, and
    # `run.finished` reaches it however the run was resumed. An id that names nothing is a
    # usage error here too, not a crash mid-resume.
    try:
        sinks = _sinks(
            json_mode,
            out,
            verbose=verbose and not quiet,
            quiet=quiet,
            redactor=redactor,
            extensions=ctx.config.extensions,
        )
        # console tree paused while asking
        prompt = approval_prompt_for(
            sinks,
            interactive=interactive,
            prompt=configured_approval(ctx.config.extensions, interactive=interactive, console=out),
        )
    except RayspecError as exc:
        fail(str(exc), hint=exc.hint)
        return EXIT_USAGE
    options = RunOptions(
        dry_run=run.dry_run,
        yes=yes,
        interactive=interactive,
        force=force,
        resume=True,
        stub_script=stub_script,
        stubs_path=stubs_path,
        provider_settings=ctx.config.providers,
        config_secrets=config_secrets,  # shell/python step env only
    )
    try:
        price_table = PriceTable.from_config(ctx.config.pricing)
    except RayspecError:
        price_table = None
    runner = Runner(
        resolved,
        inputs=dict(inputs or {}),
        store=store,
        project_root=project_root,
        project_slug=run.project_slug or ctx.slug,
        run_id=run.run_id,
        sinks=sinks,
        workspace=workspace,
        options=options,
        approval_prompt=prompt,
        resume_run_id=run.run_id,
        price_table=price_table,
        home=ctx.home,
    )
    try:
        result = runner.run_sync()
    except EngineError as exc:
        fail(str(exc), hint=exc.hint)
        return EXIT_USAGE
    finally:
        anyio.run(sinks.aclose, backend="asyncio")
    # --json: the summary object joins the JSONL events on stdout (Rich progress stays on stderr)
    print_summary(loader_common.console() if json_mode else out, result, json_mode=json_mode)
    return result.exit_code


def stdin_is_tty() -> bool:
    """Whether an approval gate could be answered interactively."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


# --------------------------------------------------------------------------------------------------
# processes + locks
# --------------------------------------------------------------------------------------------------


def pid_alive(run: RunRecord) -> bool:
    """Whether ``run.pid`` is a live process on this host (POSIX; other hosts ⇒ False).

    Delegates to the engine's liveness rule (the one ``resume`` applies) so the two never drift.
    """
    from rayspec.engine.runner import _pid_alive

    return _pid_alive(run)


def on_other_host(run: RunRecord) -> bool:
    """Whether ``run.host`` names a machine other than this one (a shared ``RAYSPEC_HOME``)."""
    return bool(run.host) and run.host != socket.gethostname()


def pid_start_time(pid: int) -> str | None:
    """The live start time of ``pid`` — the engine's probe (``ps -o lstart=`` / ``/proc``),
    so the string compares equal to what the engine recorded as ``pid_started_at``."""
    from rayspec.engine.runner import process_start_time

    return process_start_time(pid)


def interrupt_pid(pid: int) -> None:
    """Send SIGINT to ``pid`` (the engine treats the first SIGINT as a graceful cancel).

    Callers verify the pid first (:func:`pid_is_rayspec_run`) — this helper only signals.
    """
    os.kill(pid, signal.SIGINT)


def pid_command_line(pid: int, *, timeout_s: float = 5.0) -> str | None:
    """The command line of process ``pid`` via ``ps -o command= -p <pid>`` (POSIX, no psutil).

    ``None`` when the process does not exist, ``ps`` is unavailable or the call fails — the
    caller treats "unknown" as "not verified".
    """
    if pid <= 0:
        return None
    try:
        proc = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip()
    return line or None


#: ``rayspec`` (as a whole token: ``rayspec``, ``/…/bin/rayspec``, ``-m rayspec``, ``rayspec.exe``)
#: followed by one of the commands that run an engine process.
_RAYSPEC_COMMAND_RE = re.compile(
    r"(?:^|[\s/])rayspec(?:\.exe)?\s+(?:run|resume|approve|reject)(?=\s|$)"
)


def _names_token(cmdline: str, needle: str) -> bool:
    """Whether ``needle`` occurs in ``cmdline`` as a whole token — preceded by the start,
    whitespace, ``/`` or a quote and followed by the end, whitespace, ``/``, ``.`` (a file
    suffix) or a quote; ``gate`` therefore does not match ``gate2``, ``olive`` or
    ``--input a=gate``."""
    if not needle:
        return False
    pattern = r"(?:^|[\s/\"'])" + re.escape(needle) + r"(?=$|[\s/.\"'])"
    return re.search(pattern, cmdline) is not None


def pid_is_rayspec_run(run: RunRecord) -> bool:
    """Whether ``run.pid`` is *this run's* rayspec process.

    The command line must contain a rayspec execution command (``rayspec run|resume|approve|
    reject`` as whole tokens — ``python -c '…' rayspec gate`` does not count) AND name the run
    as a whole token: its id (``rayspec resume <id>``, ``rayspec run --resume <id>``), its
    workflow name or its workflow file / file stem (``rayspec run <workflow>``). Substrings do
    not match, so short workflow names (``a``, ``ci``) are not near-universal needles. An
    unrelated process that reused the pid after a crash, or an edited record, fails the check;
    anything that cannot be read is "not ours".

    Exact check first: when the record carries ``pid_started_at`` (the launching process's
    start time, recorded by the engine at launch and on every resume) the live process's start
    time (:func:`pid_start_time`) must be exactly that string — two live ``rayspec run <same
    workflow>`` processes, or a pid reused by another run of the same workflow after a crash,
    differ there even though their command lines match. An unknown live start time is a
    mismatch. Older records without the field use the command-line heuristic alone; ``cancel
    --mark`` is the escape hatch either way.
    """
    if run.pid is None:
        return False
    if run.pid_started_at is not None:
        live = pid_start_time(run.pid)
        if live is None or live != run.pid_started_at:
            return False
    cmdline = pid_command_line(run.pid)
    if cmdline is None or _RAYSPEC_COMMAND_RE.search(cmdline) is None:
        return False
    needles = {run.run_id, run.workflow_name}
    if run.workflow_path:
        needles.add(run.workflow_path)
        stem = Path(run.workflow_path).stem
        if stem:
            needles.add(stem)
    return any(_names_token(cmdline, needle) for needle in needles)


def release_workdir_lock(ctx: RunsContext, run: RunRecord) -> bool:
    """Best effort: clear the workdir lock of a run that is no longer alive.

    Uses ``rayspec.workspace.PathLock`` when importable: taking and releasing the lock truncates
    a stale holder record; a lock held by a live process is left alone. Returns ``True`` when a
    lock file was cleared.
    """
    from rayspec.loader.loader import import_optional

    workdir = run.workspace.workdir
    if not workdir:
        return False
    module = import_optional("rayspec.workspace")
    lock_cls = getattr(module, "PathLock", None) if module is not None else None
    if lock_cls is None:
        return False
    try:
        lock = lock_cls(ctx.home, run.project_slug or ctx.slug, Path(workdir), run_id=run.run_id)
        if not Path(lock.path).exists():
            return False
        lock.acquire()
        lock.release()
    except Exception:
        return False
    return True


__all__ = [
    "PREVIEW_LIMIT",
    "RunsContext",
    "check_workflow_unchanged",
    "find_run",
    "fmt_cost",
    "fmt_duration",
    "fmt_stamp",
    "fmt_tokens",
    "fmt_when",
    "interrupt_pid",
    "iter_project_stores",
    "load_resolved_for",
    "lookup_run",
    "make_runs_context",
    "on_other_host",
    "output_preview",
    "pid_alive",
    "pid_command_line",
    "pid_is_rayspec_run",
    "pid_start_time",
    "planned_step_paths",
    "project_store",
    "read_output_text",
    "recorded_calls",
    "release_workdir_lock",
    "replay_script",
    "resume_run",
    "run_cost_source",
    "run_duration_ms",
    "run_row",
    "secret_refusal",
    "status_style",
    "stdin_is_tty",
    "step_row",
    "steps_detail",
    "steps_progress",
    "stub_script_data",
    "unpriced_steps",
    "usage_dict",
    "value_text",
]

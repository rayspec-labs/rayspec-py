# SPDX-License-Identifier: Apache-2.0
"""Console sinks: ``QuietConsoleSink`` (one line per event) and ``ConsoleSink`` (Rich Live tree).

Module boundary: this module observes :class:`~rayspec.events.model.RunEvent` /
:class:`~rayspec.events.model.StreamRecord` and draws on a ``rich.console.Console``. It never
persists anything and never raises into the engine.

* :class:`QuietConsoleSink` — one plain line per step finish / run event (non-TTY / ``--quiet``).
  Subclass and override :meth:`QuietConsoleSink.format_event` or a single ``format_<event>()``
  hook to suppress/replace lines.
* :class:`ConsoleSink` — the Live tree: an in-memory :class:`RunView` (updated by events and
  stream records under a lock) and a timer-driven ``rich.live.Live`` that re-renders the whole
  tree from the model at ``refresh_per_second`` (never per event). The frame is budgeted
  against the console height (children cap, collapsed finished composites, shrinking tails,
  footer always visible). Degrades to the quiet lines
  on a non-terminal console or with ``quiet=True``; :meth:`ConsoleSink.pause` /
  :meth:`ConsoleSink.resume` / :meth:`ConsoleSink.suspended` stop the display around a prompt.
  The pure renderer (:meth:`ConsoleSink.render`, :meth:`ConsoleSink.render_summary`) is
  deterministic given the model + an injectable clock, so tests need no timers.

The formatting helpers (``fmt_duration``/``fmt_tokens``/``fmt_cost``) are the shared ones:
:mod:`rayspec.fmt` renders the duration, :mod:`rayspec.providers.pricing` the tokens and the
cost marker. This module names them, it does not decide how they look.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from rayspec.engine.paths import StepPath
from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.events.sinks._log import log
from rayspec.fmt import format_duration
from rayspec.providers.pricing import combine_cost_sources, cost_marker, format_tokens
from rayspec.textsafe import safe_text

_STATUS_STYLE: dict[str, tuple[str, str]] = {
    # status -> (symbol, rich style)
    "succeeded": ("✓", "green"),
    "failed": ("✗", "red"),
    "skipped": ("○", "dim"),
    "cancelled": ("·", "yellow"),
    "interrupted": ("·", "yellow"),
    "rejected": ("·", "yellow"),
    "paused": ("‖", "yellow"),
    "running": ("→", "cyan"),
    "pending": ("·", "dim"),
}
_DEFAULT_STYLE = ("·", "")


#: ``850ms`` · ``1.2s`` · ``2m05s`` · ``1h02m`` (:mod:`rayspec.fmt`).
fmt_duration = format_duration

#: ``999 tok`` · ``1.2k tok`` · ``1.3M tok`` (``providers.pricing.format_tokens``).
fmt_tokens = format_tokens


def fmt_cost(usd: float, *, approx: bool = False, source: str | None = None) -> str:
    """``$0.12`` — two decimals; ``~$0.12`` when ``approx`` or ``source == "table"`` (a price-table
    estimate); ``≥$0.12`` when ``source == "partial"`` (some steps have tokens but no price). The
    marker is :func:`rayspec.providers.pricing.cost_marker` (the one formatting rule);
    ``approx`` is this sink's way of saying "table" for a total it estimated itself. The
    argument is a cost, never a usage — tokens have their own slot."""
    estimated = approx and source != "partial"
    return f"{cost_marker('table' if estimated else source)}${usd:.2f}"


def usage_total(usage: Any) -> int | None:
    """Total tokens from a ``Usage`` dataclass or its dict form (``input`` + ``output``)."""
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        try:
            return int(usage.get("input", 0)) + int(usage.get("output", 0))
        except (TypeError, ValueError):
            return None
    total = getattr(usage, "total", None)
    return total if isinstance(total, int) else None


def error_text(error: Any) -> str:
    """``Type: message`` from an ``ErrorInfo``-like dict, or the plain string.

    The type is dropped when the message already starts with it (``exit: exit code 3`` →
    ``exit code 3``) or equals it (``interrupted: interrupted`` → ``interrupted``).
    """
    if error is None:
        return ""
    if isinstance(error, Mapping):
        etype, msg = error.get("type"), error.get("message", "")
        msg = str(msg) if msg is not None else ""
        if not etype or msg.lower().startswith(str(etype).lower()):
            return msg or str(etype or "")
        return f"{etype}: {msg}"
    return str(error)


def _int(value: Any, default: int) -> int:
    """``int(value)`` or ``default`` when the event data is missing/malformed (never raises)."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    """``float(value)`` or ``None`` when the event data is missing/malformed (never raises)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_line(text: Any) -> str:
    """One display line of untrusted text: escape sequences and control characters removed
    (:func:`rayspec.textsafe.safe_text`), newlines/tabs → space."""
    return safe_text(text, keep_newlines=False)


class QuietConsoleSink:
    """Print one line per ``step.finished``/``step.retry``/``run.*``/``workspace.created``/
    ``warning`` event (``step.started`` only with ``show_started=True``).

    Stream records are ignored (quiet). ``loop.iteration``/``each.item`` are not printed: the
    nested steps report themselves. Output goes through ``console.print`` with markup disabled
    so step paths and messages are shown verbatim; errors from the console are logged once.
    """

    def __init__(self, console: Console, *, show_started: bool = False) -> None:
        self.console = console
        self.show_started = show_started
        self._failed = False
        #: per-step cost sources seen so far + whether a step had tokens without a price —
        #: the run line derives its cost marker from these when ``run.finished`` carries no
        #: ``cost_source``
        self._cost_sources: set[str] = set()
        self._unpriced = False

    async def emit(self, event: RunEvent) -> None:
        """Render ``event`` (if :meth:`format_event` returns a line) to the console."""
        try:
            line = self.format_event(event)
            if line is None:
                return
            self.console.print(line, markup=False, highlight=False, soft_wrap=True)
        except Exception as exc:
            self._warn_once("console sink: cannot print: %s", exc)

    def _warn_once(self, message: str, *args: Any) -> None:
        if not self._failed:
            self._failed = True
            log.warning(message, *args)

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        """Quiet mode shows no stream output — except provider warnings (``kind == "warning"``:
        rate limits, tool-translation notes …), printed as ``⚠ <step>: <warning>``."""
        if record.kind != "warning":
            return
        try:
            self.console.print(
                format_stream_warning(step_path, record.text),
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
        except Exception as exc:
            self._warn_once("console sink: cannot print: %s", exc)

    async def aclose(self) -> None:
        """Nothing to release (the console is owned by the caller)."""

    # -- formatting (override points) -----------------------------------------------------------

    def format_event(self, event: RunEvent) -> Text | None:
        """Return the line for ``event`` or ``None`` to print nothing.

        Dispatches by method *name* (``format_<event>``) through the instance, so subclasses
        overriding a single hook are honoured.
        """
        name = _HANDLERS.get(event.type)
        if name is None:
            return None
        handler: Callable[[RunEvent], Text | None] = getattr(self, name)
        return handler(event)

    def format_run_started(self, event: RunEvent) -> Text | None:
        verb = "resumed" if event.type is EventType.RUN_RESUMED else "started"
        line = Text.assemble(("▶ ", "bold"), f"run {event.run_id} {verb}")
        workflow = event.data.get("workflow") or event.data.get("workflow_name")
        if workflow:
            line.append(f" ({_clean_line(workflow)})", style="dim")
        return line

    def format_run_paused(self, event: RunEvent) -> Text | None:
        step = _clean_line(event.data.get("step") or event.step_path or "?")
        line = Text.assemble(("‖ ", "yellow"), f"run {event.run_id} paused at {step}")
        if event.data.get("message"):
            line.append(f" — {_clean_line(event.data['message'])}")
        return line

    def format_run_decision(self, event: RunEvent) -> Text | None:
        approved = bool(event.data.get("approved"))
        word, style = ("approved", "green") if approved else ("rejected", "red")
        line = Text.assemble(("● ", style), "decision: ", (word, style))
        if event.data.get("comment"):
            line.append(f" — {_clean_line(event.data['comment'])}")
        return line

    def format_run_finished(self, event: RunEvent) -> Text | None:
        status = str(event.data.get("status") or "finished")
        _, style = _STATUS_STYLE.get(status, _DEFAULT_STYLE)
        line = Text.assemble(("■ ", style), f"run {event.run_id} ", (status, style))
        data: dict[str, Any] = dict(event.data)
        if not data.get("cost_source"):
            data["cost_source"] = self.derived_cost_source()
        line.append(_stats(data))
        reason = event.data.get("reason")
        if reason and str(reason) != status:  # "interrupted — interrupted" says nothing
            line.append(f" — {_clean_line(reason)}")
        return line

    def derived_cost_source(self) -> str:
        """Run-level cost source folded from the ``step.finished`` events seen so far."""
        return combine_cost_sources(self._cost_sources, unpriced=self._unpriced)

    def _note_step_cost(self, data: Mapping[str, Any]) -> None:
        cost = _float(data.get("cost_usd"))
        if cost is not None:
            self._cost_sources.add(str(data.get("cost_source") or "provider"))
        elif usage_total(data.get("usage")):
            self._unpriced = True

    def format_step_started(self, event: RunEvent) -> Text | None:
        if not self.show_started:
            return None
        line = Text.assemble(("→ ", "cyan"), _clean_line(event.step_path or "?"))
        if event.data.get("kind"):
            line.append(f" ({_clean_line(event.data['kind'])})", style="dim")
        attempt = _int(event.data.get("attempt"), 1)
        if attempt > 1:
            line.append(f" attempt {attempt}", style="yellow")
        return line

    def format_step_retry(self, event: RunEvent) -> Text | None:
        delay = event.data.get("delay_s")
        delay_txt = f" in {_fmt_seconds(delay)}" if delay is not None else ""
        line = Text.assemble(
            ("↻ ", "yellow"), f"{_clean_line(event.step_path or '?')} retry{delay_txt}"
        )
        if event.data.get("attempt") is not None:
            line.append(f" (attempt {_clean_line(event.data['attempt'])})", style="dim")
        err = error_text(event.data.get("error"))
        if err:
            line.append(f": {_clean_line(err)}")
        return line

    def format_step_finished(self, event: RunEvent) -> Text | None:
        data = event.data
        self._note_step_cost(data)
        status = str(data.get("status") or "finished")
        symbol, style = _STATUS_STYLE.get(status, _DEFAULT_STYLE)
        path = _clean_line(event.step_path or "?")
        if data.get("reused"):
            # a resume replay: the step did not run in this attempt
            line = Text.assemble(("↺ ", "dim"), f"{path} ", ("reused", "dim"))
            duration = _float(data.get("duration_ms"))
            if duration is not None:
                line.append(f" ({fmt_duration(duration)})", style="dim")
            return line
        line = Text.assemble((f"{symbol} ", style), f"{path} ", (status, style))
        if data.get("tolerated"):
            line.append(" (tolerated)", style="dim")
        duration = _float(data.get("duration_ms"))
        if duration is not None:
            line.append(f" {fmt_duration(duration)}")
        line.append(_stats(data))
        detail = error_text(data.get("error")) or data.get("skip_reason") or ""
        if detail and str(detail) != status:
            line.append(f" — {_clean_line(detail)}")
        return line

    def format_workspace_created(self, event: RunEvent) -> Text | None:
        workdir = _clean_line(event.data.get("workdir", "?"))
        line = Text.assemble(("● ", "cyan"), f"workspace {workdir}")
        if event.data.get("branch"):
            line.append(f" ({_clean_line(event.data['branch'])})", style="dim")
        return line

    def format_warning(self, event: RunEvent) -> Text | None:
        message = event.data.get("message") or event.data.get("warning") or ""
        return Text.assemble(("! ", "yellow"), "warning: ", _clean_line(message))


def format_stream_warning(step_path: str, text: str) -> Text:
    """``⚠ <step>: <warning>`` — a provider warning streamed by a running step."""
    return Text.assemble(
        ("⚠ ", "yellow"), (f"{_clean_line(step_path)}: ", "bold"), _clean_line(text.strip())
    )


def _stats(data: Mapping[str, Any]) -> str:
    parts: list[str] = []
    tokens = usage_total(data.get("usage"))
    if tokens:
        parts.append(fmt_tokens(tokens))
    cost = _float(data.get("cost_usd"))
    if cost is not None:
        # ``cost_source`` is optional (not a pinned key): "table" marks a price-table estimate,
        # "partial" a run where some steps have tokens but no price
        parts.append(fmt_cost(cost, source=str(data.get("cost_source") or "")))
    return "".join(f" · {p}" for p in parts)


def _fmt_seconds(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{seconds:g}s"


# event type -> name of the ``format_<event>`` method (looked up on the instance at dispatch time)
_HANDLERS: dict[EventType, str] = {
    EventType.RUN_STARTED: "format_run_started",
    EventType.RUN_RESUMED: "format_run_started",
    EventType.RUN_PAUSED: "format_run_paused",
    EventType.RUN_DECISION: "format_run_decision",
    EventType.RUN_FINISHED: "format_run_finished",
    EventType.STEP_STARTED: "format_step_started",
    EventType.STEP_RETRY: "format_step_retry",
    EventType.STEP_FINISHED: "format_step_finished",
    EventType.WORKSPACE_CREATED: "format_workspace_created",
    EventType.WARNING: "format_warning",
}


# ==================================================================================================
# Live tree: model
# ==================================================================================================

_TERMINAL = frozenset(
    {"succeeded", "failed", "skipped", "cancelled", "interrupted", "rejected", "paused"}
)
#: worst-first ranking used to derive an iteration/item status from its children
_SEVERITY = ("failed", "interrupted", "cancelled", "rejected", "paused", "skipped", "succeeded")
_TAIL_KEYS = ("command", "file_path", "path", "pattern", "query", "url", "prompt", "description")
_MAX_WARNINGS = 5
DEFAULT_TAIL_LINES = 6
VERBOSE_TAIL_LINES = 20
#: finished, clean children shown per node before the rest collapses into ``… +N more``
DEFAULT_MAX_CHILDREN = 8
#: statuses (own or of a descendant) that keep a finished node expanded
_PROBLEMS = frozenset({"failed", "interrupted", "cancelled", "rejected", "paused"})


@dataclass(slots=True, eq=False)
class StepView:
    """One node of the run tree: a step, a loop iteration (``build[2]``) or an each item.

    ``iteration``/``item`` mark the synthetic container nodes the engine addresses only through
    ``loop.iteration``/``each.item`` events and the paths of their body steps; their status is
    derived from the children (see :meth:`refresh_derived`). Nodes compare by identity.
    """

    path: str
    name: str
    kind: str | None = None
    status: str = "running"
    attempt: int = 1
    started_at: float = 0.0
    ended_at: float | None = None
    duration_ms: float | None = None
    usage: Any = None
    cost_usd: float | None = None
    cost_source: str | None = None
    error: Any = None
    skip_reason: str | None = None
    tolerated: bool = False
    iteration: tuple[int, int | None] | None = None
    item: tuple[int, int | None] | None = None
    children: list[StepView] = field(default_factory=list)
    parent: StepView | None = field(default=None, repr=False)
    awaiting_approval: str | None = None
    _tail: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=DEFAULT_TAIL_LINES))
    _partial: str = ""
    _streamed: bool = False

    # -- classification -------------------------------------------------------------------------

    @property
    def is_container(self) -> bool:
        """``True`` for iteration/item nodes (no ``step.*`` events of their own)."""
        return self.iteration is not None or self.item is not None

    @property
    def is_done(self) -> bool:
        return self.status in _TERMINAL

    @property
    def has_problem(self) -> bool:
        """``True`` when this node or any descendant failed/was tolerated/interrupted/… — such
        subtrees stay expanded after they finish."""
        if self.tolerated or self.status in _PROBLEMS:
            return True
        return any(child.has_problem for child in self.children)

    @property
    def label(self) -> str:
        """``implement`` · ``iteration 2/3`` · ``item 0/4`` · ``build[2]`` (unknown container)."""
        if self.iteration is not None:
            n, mx = self.iteration
            return f"iteration {n}/{mx}" if mx is not None else f"iteration {n}"
        if self.item is not None:
            i, total = self.item
            return f"item {i}/{total}" if total is not None else f"item {i}"
        return self.name

    # -- tails ----------------------------------------------------------------------------------

    def set_tail_size(self, lines: int) -> None:
        self._tail = deque(self._tail, maxlen=lines)

    def push_line(self, text: str, style: str = "") -> None:
        """Append one finished tail line (flushing a pending partial line first); ANSI escapes
        are stripped and embedded newlines joined so every entry is exactly one line."""
        self.flush_partial()
        self._tail.append((_clean_line(text), style))

    def push_delta(self, text: str) -> None:
        """Append streamed text; complete lines move into the tail, the rest stays pending."""
        self._streamed = True
        self._partial += text
        *done, self._partial = self._partial.split("\n")
        for line in done:
            self._tail.append((_clean_line(line), ""))

    def flush_partial(self) -> None:
        if self._partial:
            self._tail.append((_clean_line(self._partial), ""))
            self._partial = ""

    def tail_entries(self) -> list[tuple[str, str]]:
        """``(text, style)`` of the visible tail (pending partial line included), oldest first."""
        entries = list(self._tail)
        if self._partial:
            entries.append((_clean_line(self._partial), ""))
        maxlen = self._tail.maxlen
        if maxlen is None:
            return entries
        return entries[-maxlen:] if maxlen else []

    def tail_lines(self) -> list[str]:
        """The visible tail as plain strings (oldest first)."""
        return [text for text, _ in self.tail_entries()]

    def clear_tail(self) -> None:
        self._tail.clear()
        self._partial = ""
        self._streamed = False

    # -- derived status (containers) ------------------------------------------------------------

    def refresh_derived(self, now: float, *, force_done: bool = False) -> None:
        """Recompute a container's status from its children (running while any child runs or,
        unless ``force_done``, while no child has started yet)."""
        if not self.is_container:
            return
        kids = self.children
        running = not kids or any(not c.is_done for c in kids)
        if running and not force_done:
            if self.is_done:  # a new body step started after we collapsed: re-open
                self.status = "running"
                self.ended_at = None
            return
        statuses = {c.status for c in kids if not (c.status == "failed" and c.tolerated)}
        self.tolerated = any(c.tolerated for c in kids)
        self.status = next((s for s in _SEVERITY if s in statuses), "succeeded")
        if self.ended_at is None:
            self.ended_at = now


@dataclass(slots=True, eq=False)
class RunView:
    """In-memory picture of one run, updated from events/stream records, read by the renderer.

    ``apply``/``apply_stream`` coerce malformed event data (non-numeric ``attempt``, ``n``,
    ``index``, ``duration_ms``, ``cost_usd``…) to defaults instead of raising.
    """

    clock: Callable[[], float] = time.monotonic
    tail_lines: int = DEFAULT_TAIL_LINES
    run_id: str = ""
    workflow: str | None = None
    status: str = "running"
    reason: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    finished: bool = False
    roots: list[StepView] = field(default_factory=list)
    nodes: dict[str, StepView] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    pause: tuple[str, str] | None = None  # (step, message)
    decision: tuple[bool, str] | None = None  # (approved, comment)
    workdir: str | None = None
    branch: str | None = None
    usage: Any = None  # run.finished usage (dict) — totals below accumulate while running
    cost_usd: float | None = None
    cost_source: str | None = None
    outputs: Any = None
    tokens_total: int = 0
    cost_total: float | None = None
    steps_finished: int = 0
    #: per-step cost sources (of steps with a cost) + any step with tokens but no price: the
    #: run-level source is folded from these until ``run.finished`` reports one
    step_cost_sources: set[str] = field(default_factory=set)
    unpriced_steps: int = 0

    @property
    def derived_cost_source(self) -> str:
        """``provider`` · ``table`` · ``partial`` · ``none`` folded from the finished steps."""
        return combine_cost_sources(self.step_cost_sources, unpriced=self.unpriced_steps > 0)

    # -- lookup -------------------------------------------------------------------------------

    def get(self, path: str) -> StepView | None:
        """The node for a step path (``build[1]/implement``), or ``None``."""
        return self.nodes.get(path)

    def now(self) -> float:
        return self.clock()

    # -- event application ----------------------------------------------------------------------

    def apply(self, event: RunEvent) -> None:
        """Fold one lifecycle event into the model (unknown types are ignored)."""
        if not self.run_id:
            self.run_id = event.run_id
        now = self.now()
        if self.started_at is None:
            self.started_at = now
        handler = _APPLY.get(event.type)
        if handler is not None:
            handler(self, event, now)

    def _apply_run_started(self, event: RunEvent, now: float) -> None:
        self.workflow = event.data.get("workflow") or event.data.get("workflow_name")
        if event.data.get("workdir"):
            self.workdir = str(event.data["workdir"])
        self.status = "running"

    def _apply_run_paused(self, event: RunEvent, now: float) -> None:
        step = str(event.data.get("step") or event.step_path or "?")
        message = _clean_line(str(event.data.get("message") or ""))
        self.pause = (step, message)
        self.decision = None
        node = self.nodes.get(step)
        if node is not None:
            node.status = "paused"
            node.awaiting_approval = message

    def _apply_run_decision(self, event: RunEvent, now: float) -> None:
        comment = _clean_line(str(event.data.get("comment") or ""))
        self.decision = (bool(event.data.get("approved")), comment)
        if self.pause is not None:
            node = self.nodes.get(self.pause[0])
            if node is not None and node.status == "paused":
                node.status = "running"
                node.awaiting_approval = None
        self.pause = None

    def _apply_run_finished(self, event: RunEvent, now: float) -> None:
        data = event.data
        self.status = str(data.get("status") or "finished")
        self.reason = data.get("reason") or None
        self.usage = data.get("usage")
        self.cost_usd = _float(data.get("cost_usd"))
        self.cost_source = str(data.get("cost_source") or self.derived_cost_source)
        self.outputs = data.get("outputs")
        self.ended_at = now
        self.finished = True
        for node in self.nodes.values():
            if node.is_container:
                node.refresh_derived(now, force_done=True)

    def _apply_step_started(self, event: RunEvent, now: float) -> None:
        path = event.step_path or ""
        if not path:
            return
        node = self._ensure(path, now)
        node.kind = event.data.get("kind") or node.kind
        node.attempt = _int(event.data.get("attempt"), 1)
        node.status = "running"
        node.started_at = now
        node.ended_at = None
        node.awaiting_approval = None
        node.clear_tail()
        self._refresh_ancestors(node, now)

    def _apply_step_retry(self, event: RunEvent, now: float) -> None:
        path = event.step_path or ""
        node = self.nodes.get(path)
        if node is None:
            return
        err = error_text(event.data.get("error"))
        delay = event.data.get("delay_s")
        text = "↻ retry" + (f" in {_fmt_seconds(delay)}" if delay is not None else "")
        node.push_line(f"{text}: {err}" if err else text, "yellow")

    def _apply_step_finished(self, event: RunEvent, now: float) -> None:
        path = event.step_path or ""
        if not path:
            return
        data = event.data
        node = self._ensure(path, now)
        node.status = str(data.get("status") or "finished")
        node.duration_ms = _float(data.get("duration_ms"))
        node.usage = data.get("usage")
        node.cost_usd = _float(data.get("cost_usd"))
        node.cost_source = data.get("cost_source")
        node.error = data.get("error")
        node.skip_reason = data.get("skip_reason")
        node.tolerated = bool(data.get("tolerated"))
        node.ended_at = now
        node.awaiting_approval = None
        node.clear_tail()
        self.steps_finished += 1
        tokens = usage_total(node.usage)
        if tokens:
            self.tokens_total += tokens
        if node.cost_usd is not None:
            self.cost_total = (self.cost_total or 0.0) + node.cost_usd
            self.step_cost_sources.add(str(node.cost_source or "provider"))
        elif tokens:
            self.unpriced_steps += 1
        self.cost_source = self.derived_cost_source
        # a finished composite closes its iterations/items; a finished body step may close its
        # container
        for child in node.children:
            child.refresh_derived(now, force_done=True)
        self._refresh_ancestors(node, now)

    def _apply_loop_iteration(self, event: RunEvent, now: float) -> None:
        base = event.step_path or ""
        n = _int(event.data.get("n"), -1)
        if not base or n < 0:
            return
        parent = self._ensure(base, now)
        if parent.kind is None:
            parent.kind = "loop"
        for previous in parent.children:
            previous.refresh_derived(now, force_done=True)
        node = self._ensure(f"{base}[{n}]", now)
        mx = _int(event.data.get("max"), -1)
        node.iteration = (n, mx if mx >= 0 else None)
        node.item = None
        node.started_at = now

    def _apply_each_item(self, event: RunEvent, now: float) -> None:
        base = event.step_path or ""
        index = _int(event.data.get("index"), -1)
        if not base or index < 0:
            return
        parent = self._ensure(base, now)
        if parent.kind is None:
            parent.kind = "each"
        node = self._ensure(f"{base}[{index}]", now)
        total = _int(event.data.get("total"), -1)
        node.item = (index, total if total >= 0 else None)
        node.iteration = None
        node.started_at = now

    def _apply_workspace_created(self, event: RunEvent, now: float) -> None:
        self.workdir = str(event.data.get("workdir") or self.workdir or "")
        self.branch = event.data.get("branch")

    def _apply_warning(self, event: RunEvent, now: float) -> None:
        message = _clean_line(str(event.data.get("message") or event.data.get("warning") or ""))
        if event.step_path:
            message = f"{event.step_path}: {message}"
        self.warnings.append(message)

    # -- stream records -------------------------------------------------------------------------

    def apply_stream(self, step_path: str, record: StreamRecord, *, verbose: bool = False) -> None:
        """Fold one stream record into the step's tail (records for unknown steps are dropped)."""
        node = self.nodes.get(step_path)
        if node is None or node.is_done:
            return
        kind = record.kind
        if kind == "text_delta":
            node.push_delta(record.text)
            return
        if kind == "text":
            if node._streamed:  # deltas already showed this block
                node.flush_partial()
                node._streamed = False
                return
            for line in record.text.splitlines():
                node.push_line(line)
            return
        if kind in {"stdout", "stderr", "command_output"}:
            style = "red" if kind == "stderr" else ""
            for line in record.text.rstrip("\n").split("\n") if record.text else []:
                node.push_line(line, style)
            return
        if kind == "tool_call":
            node.push_line(f"⚙ {record.name or 'tool'} {_tool_summary(record.data)}".rstrip())
        elif kind == "tool_result":
            first = record.text.strip().splitlines()
            node.push_line(f"  ↳ {first[0] if first else '(no output)'}", "dim")
        elif kind == "command_start":
            command = record.data.get("command") or record.text
            node.push_line(f"$ {command}", "bold")
        elif kind == "command_end":
            code = record.data.get("exit_code")
            if code not in (None, 0):
                node.push_line(f"  ↳ exit {code}", "red")
        elif kind == "file_change":
            first = record.text.splitlines()
            node.push_line(f"✎ {record.name or (first[0] if first else '')}", "cyan")
        elif kind in {"warning", "error"}:
            node.push_line(f"! {record.text.strip()}", "yellow" if kind == "warning" else "red")
            if kind == "warning":  # provider warnings stay visible in the footer
                warning = _clean_line(record.text.strip())
                self.warnings.append(f"{_clean_line(step_path)}: {warning}")
        elif kind == "reasoning" and verbose:
            first = record.text.strip().splitlines()
            if first:
                node.push_line(f"· thinking: {first[0]}", "dim")
        elif kind == "plan" and verbose:
            for line in record.text.strip().splitlines():
                node.push_line(f"· plan: {line}", "dim")
        # session / usage / raw / exit: nothing to show

    # -- tree maintenance -----------------------------------------------------------------------

    def _ensure(self, path: str, now: float) -> StepView:
        """Return the node for ``path``, creating it and every missing ancestor."""
        node = self.nodes.get(path)
        if node is not None:
            return node
        parent: StepView | None = None
        for ancestor_path, name, index in _chain(path):
            existing = self.nodes.get(ancestor_path)
            if existing is None:
                existing = StepView(path=ancestor_path, name=name, started_at=now, parent=parent)
                existing.set_tail_size(self.tail_lines)
                if index is not None:
                    if parent is not None and parent.kind == "loop":
                        existing.iteration = (index, None)
                    else:
                        existing.item = (index, None)
                if parent is None:
                    self.roots.append(existing)
                else:
                    parent.children.append(existing)
                self.nodes[ancestor_path] = existing
            parent = existing
        assert parent is not None
        return parent

    def _refresh_ancestors(self, node: StepView, now: float) -> None:
        """Re-derive every container above ``node`` (a body step started or finished)."""
        parent = node.parent
        while parent is not None:
            if parent.is_container:
                parent.refresh_derived(now)
            parent = parent.parent

    # -- elapsed --------------------------------------------------------------------------------

    def elapsed_ms(self, node: StepView) -> float:
        """Wall-clock duration of a node (``duration_ms`` when reported, else clock based)."""
        if not node.is_container and node.duration_ms is not None:
            return float(node.duration_ms)
        end = node.ended_at if node.ended_at is not None else self.now()
        return max(0.0, (end - node.started_at) * 1000)

    def run_elapsed_ms(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else self.now()
        return max(0.0, (end - self.started_at) * 1000)


_APPLY: dict[EventType, Callable[[RunView, RunEvent, float], None]] = {
    EventType.RUN_STARTED: RunView._apply_run_started,
    EventType.RUN_RESUMED: RunView._apply_run_started,
    EventType.RUN_PAUSED: RunView._apply_run_paused,
    EventType.RUN_DECISION: RunView._apply_run_decision,
    EventType.RUN_FINISHED: RunView._apply_run_finished,
    EventType.STEP_STARTED: RunView._apply_step_started,
    EventType.STEP_RETRY: RunView._apply_step_retry,
    EventType.STEP_FINISHED: RunView._apply_step_finished,
    EventType.LOOP_ITERATION: RunView._apply_loop_iteration,
    EventType.EACH_ITEM: RunView._apply_each_item,
    EventType.WORKSPACE_CREATED: RunView._apply_workspace_created,
    EventType.WARNING: RunView._apply_warning,
}


def _chain(path: str) -> list[tuple[str, str, int | None]]:
    """``build[2]/fix_all[0]/patch`` → ``[(build, build, None), (build[2], build, 2),
    (build[2]/fix_all, fix_all, None), (build[2]/fix_all[0], fix_all, 0),
    (build[2]/fix_all[0]/patch, patch, None)]``."""
    try:
        segments = StepPath.parse(path).segments
    except ValueError:
        return [(path, path, None)]
    chain: list[tuple[str, str, int | None]] = []
    prefix = ""
    for name, index in segments:
        base = f"{prefix}/{name}" if prefix else name
        chain.append((base, name, None))
        if index is not None:
            base = f"{base}[{index}]"
            chain.append((base, name, index))
        prefix = base
    return chain


def _tool_summary(data: Mapping[str, Any]) -> str:
    """A short argument summary for a tool call (first well-known key, else compact JSON)."""
    for key in _TAIL_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value.splitlines()[0]
    if not data:
        return ""
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(data)
    return text if len(text) <= 60 else text[:59] + "…"


# ==================================================================================================
# Live tree: renderer + sink
# ==================================================================================================


def _line(*parts: str | tuple[str, str]) -> Text:
    """One never-wrapping tree line (overflow → ellipsis at the console width)."""
    return Text.assemble(*parts, overflow="ellipsis", no_wrap=True)


def render_step_line(view: RunView, node: StepView) -> Text:
    """``✓ implement (prompt) 4.0s · 1.5k tok · $0.01`` — the one-line form of a node."""
    symbol, style = _STATUS_STYLE.get(node.status, _DEFAULT_STYLE)
    line = _line(
        (f"{symbol} ", style), (_clean_line(node.label), "bold" if not node.is_done else "")
    )
    if node.kind and not node.is_container:
        line.append(f" ({_clean_line(node.kind)})", style="dim")
    line.append(f" {fmt_duration(view.elapsed_ms(node))}")
    if node.attempt > 1:
        line.append(f" attempt {node.attempt}", style="yellow")
    line.append(
        _stats({"usage": node.usage, "cost_usd": node.cost_usd, "cost_source": node.cost_source})
    )
    if node.tolerated:
        line.append(" (tolerated)", style="dim")
    detail = error_text(node.error) or node.skip_reason or ""
    if detail:
        line.append(f" — {_clean_line(str(detail))}", style=style)
    return line


def render_run_line(view: RunView) -> Text:
    """``▶ wf · <run-id> · running 12.3s`` / ``■ wf · <run-id> · succeeded 7.0s · 1.5k tok``."""
    if view.finished:
        _, style = _STATUS_STYLE.get(view.status, _DEFAULT_STYLE)
        head: tuple[str, str] = ("■ ", style)
        status = view.status
        stats = _stats(
            {"usage": view.usage, "cost_usd": view.cost_usd, "cost_source": view.cost_source}
        )
    else:
        head, style, status = ("▶ ", "bold"), "cyan", "running"
        stats = ""
        if view.tokens_total:
            stats += f" · {fmt_tokens(view.tokens_total)}"
        if view.cost_total is not None:
            stats += f" · {fmt_cost(view.cost_total, source=view.cost_source)}"
    line = _line(head)
    if view.workflow:
        line.append(_clean_line(view.workflow), style="bold")
        line.append(" · ", style="dim")
    line.append(view.run_id or "run", style="dim")
    line.append(" · ", style="dim")
    line.append(status, style=style)
    line.append(f" {fmt_duration(view.run_elapsed_ms())}")
    line.append(stats)
    return line


def render_tree(
    view: RunView,
    *,
    max_children: int = DEFAULT_MAX_CHILDREN,
    tail_limit: int | None = None,
) -> Tree:
    """The whole run as a ``rich.tree.Tree`` (running leaves show their tails).

    Every node renders as exactly one line. Finished, clean subtrees (iterations, items and
    composite steps without a failed/tolerated/interrupted descendant) collapse to their one
    line; per node only running/problem children plus the last ``max_children`` finished ones
    are listed, the rest become a dim ``… +N more`` line. ``tail_limit`` caps the tail lines
    shown per running step (``None`` = the model's tail size).
    """
    return _build_tree(view, max_children=max_children, tail_limit=tail_limit)[0]


def _build_tree(view: RunView, *, max_children: int, tail_limit: int | None) -> tuple[Tree, int]:
    """``(tree, number of lines)`` — see :func:`render_tree`."""
    tree = Tree(render_run_line(view), guide_style="dim")
    count = 1 + _add_children(view, tree, view.roots, max_children, tail_limit)
    return tree, count


def _add_children(
    view: RunView, branch: Tree, children: list[StepView], max_children: int, tail_limit: int | None
) -> int:
    """Add ``children`` to ``branch`` honouring the cap; returns the number of lines added."""
    visible = _visible_children(children, max_children)
    lines = 0
    hidden = 0
    for child in children:
        if id(child) in visible:
            if hidden:
                branch.add(_line((f"… +{hidden} more", "dim")))
                lines += 1
                hidden = 0
            lines += _add_node(view, branch, child, max_children, tail_limit)
        else:
            hidden += 1
    if hidden:
        branch.add(_line((f"… +{hidden} more", "dim")))
        lines += 1
    return lines


def _visible_children(children: list[StepView], max_children: int) -> set[int]:
    """Ids of the children to list: unfinished/problem ones plus the last ``max_children``
    finished clean ones."""
    if len(children) <= max_children:
        return {id(c) for c in children}
    keep = {id(c) for c in children if not c.is_done or c.has_problem}
    if max_children > 0:
        rest = [c for c in children if id(c) not in keep]
        keep.update(id(c) for c in rest[-max_children:])
    return keep


def _add_node(
    view: RunView, branch: Tree, node: StepView, max_children: int, tail_limit: int | None
) -> int:
    sub = branch.add(render_step_line(view, node))
    lines = 1
    if node.is_done and not node.has_problem:
        return lines  # a clean finished subtree collapses to its one line (problems stay visible)
    lines += _add_children(view, sub, node.children, max_children, tail_limit)
    if node.awaiting_approval is not None:
        sub.add(_line(("‖ ", "yellow"), ("approval required: ", "yellow"), node.awaiting_approval))
        lines += 1
    if node.is_done:
        return lines
    tail = node.tail_entries()
    if tail_limit is not None:
        tail = tail[-tail_limit:] if tail_limit else []
    for text, style in tail:
        sub.add(_line((text, style or "dim")))
        lines += 1
    return lines


def render_footer(view: RunView) -> list[Text]:
    """Warnings (last few) and the pause/decision line shown under the tree."""
    lines: list[Text] = []
    warnings = view.warnings
    if len(warnings) > _MAX_WARNINGS:
        more = len(warnings) - _MAX_WARNINGS
        lines.append(_line(("! ", "yellow"), (f"… {more} more warnings", "dim")))
    for message in warnings[-_MAX_WARNINGS:]:
        lines.append(_line(("! ", "yellow"), ("warning: ", "yellow"), message))
    if view.pause is not None:
        step, message = view.pause
        line = _line(("‖ ", "yellow"), ("paused at ", "yellow"), (step, "bold"))
        if message:
            line.append(f" — {message}")
        if view.run_id:
            line.append(
                f" (rayspec approve {view.run_id} / rayspec reject {view.run_id})", style="dim"
            )
        lines.append(line)
    elif view.decision is not None:
        approved, comment = view.decision
        word, style = ("approved", "green") if approved else ("rejected", "red")
        line = _line(("● ", style), "decision: ", (word, style))
        if comment:
            line.append(f" — {comment}")
        lines.append(line)
    return lines


#: (tail_limit, max_children) tried in order until the frame fits the height budget
_FIT_STAGES: tuple[tuple[int | None, int | None], ...] = (
    (None, None),
    (2, None),
    (0, None),
    (0, 2),
    (0, 0),
)


def render_view(
    view: RunView, *, height: int | None = None, max_children: int = DEFAULT_MAX_CHILDREN
) -> RenderableType:
    """Tree + footer — what the Live display shows (pure function of the model).

    With ``height`` (terminal rows) the frame is budgeted: the footer (warnings, pause/decision)
    is always kept and the tree gets the rest — tails shrink (full → 2 → 0 lines), then the
    children cap drops (→ 2 → 0) and as a last resort the tree is cropped from the top (run line,
    a ``… N lines hidden`` marker, then the most recent lines). ``None`` = unbounded.
    """
    footer = render_footer(view)
    if height is None or height <= 0:
        tree = render_tree(view, max_children=max_children)
        return Group(tree, *footer) if footer else tree
    budget = max(height - len(footer), 2)
    tree, count = _build_tree(view, max_children=max_children, tail_limit=None)
    for tail_limit, cap in _FIT_STAGES[1:]:
        if count <= budget:
            break
        tree, count = _build_tree(
            view, max_children=max_children if cap is None else cap, tail_limit=tail_limit
        )
    if count <= budget:
        return Group(tree, *footer)
    return _CroppedTree(tree, footer, budget)


class _CroppedTree:
    """A tree cropped from the top to ``budget`` lines, followed by the footer (last resort)."""

    def __init__(self, tree: Tree, footer: list[Text], budget: int) -> None:
        self.tree = tree
        self.footer = footer
        self.budget = budget

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = console.render_lines(self.tree, options.update(height=None), pad=False)
        if len(lines) > self.budget:
            keep = max(self.budget - 2, 0)
            hidden = len(lines) - keep - 1
            marker = _line((f"… {hidden} lines hidden", "dim"))
            lines = (
                lines[:1]
                + console.render_lines(marker, options.update(height=None), pad=False)
                + (lines[-keep:] if keep else [])
            )
        new_line = Segment.line()
        for line in lines:
            yield from line
            yield new_line
        yield from self.footer


def render_summary(view: RunView) -> Panel:
    """Final panel: status, reason, outputs table, workspace, totals."""
    _, style = _STATUS_STYLE.get(view.status, _DEFAULT_STYLE)
    body: list[RenderableType] = []
    if view.reason:
        body.append(Text(safe_text(view.reason), style=style))
    outputs = view.outputs if isinstance(view.outputs, Mapping) else None
    if outputs:
        if body:
            body.append(Text(""))
        body.append(Text("outputs", style="bold"))
        table = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
        table.add_column("name", style="bold", no_wrap=True)
        table.add_column("value", overflow="fold")
        for name, value in outputs.items():
            if isinstance(value, str):
                text = value
            else:
                try:
                    text = json.dumps(value, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    text = str(value)
            # Text cells: output values are untrusted free text — never console markup, no
            # escape sequences
            table.add_row(Text(safe_text(name)), Text(safe_text(text), overflow="fold"))
        body.append(table)
    info = Table(show_header=False, show_edge=False, pad_edge=False, box=None)
    info.add_column("key", style="dim", no_wrap=True)
    info.add_column("value", overflow="fold")
    if view.branch or view.workdir:
        ws = view.workdir or ""
        if view.branch:
            ws += f" (branch {view.branch})"
        info.add_row(Text("workspace"), Text(safe_text(ws.strip()), overflow="fold"))
    totals = [f"{view.steps_finished} step{'s' if view.steps_finished != 1 else ''}"]
    totals.append(fmt_duration(view.run_elapsed_ms()))
    stats = _stats(
        {"usage": view.usage, "cost_usd": view.cost_usd, "cost_source": view.cost_source}
    )
    if not stats and (view.tokens_total or view.cost_total is not None):
        stats = ""
        if view.tokens_total:
            stats += f" · {fmt_tokens(view.tokens_total)}"
        if view.cost_total is not None:
            stats += f" · {fmt_cost(view.cost_total, source=view.cost_source)}"
    info.add_row(Text("totals"), Text(" · ".join(totals) + stats, overflow="fold"))
    if body:
        body.append(Text(""))
    body.append(info)
    title = Text.assemble("run ", (view.run_id or "?", "bold"), " ", (view.status, style))
    return Panel(Group(*body), title=title, title_align="left", border_style=style or "none")


class ConsoleSink(QuietConsoleSink):
    """Rich Live step tree (TTY) that degrades to :class:`QuietConsoleSink` lines otherwise.

    * The model (:attr:`view`) is updated under an ``anyio.Lock`` (engine tasks) and a
      ``threading.Lock`` (the Live refresh thread reads it); the display is re-drawn from the
      model on Rich's timer (``refresh_per_second``, default 8), never per event.
    * Live starts lazily on the first event and stops on ``run.finished`` (the final frame stays
      on screen, then the summary panel is printed) or on :meth:`aclose`.
    * :meth:`pause` / :meth:`resume` (or ``async with sink.suspended():``) stop the display
      around an interactive prompt and restart it afterwards; nesting is counted.
    * ``live=None`` (auto) enables the tree only when ``console.is_terminal`` and not
      ``quiet``; ``display=False`` keeps the tree model (for :meth:`render` / tests / embedding)
      without starting a Live display. If the Live display cannot be started the sink degrades
      to quiet lines for the rest of the run.
    * Each frame is budgeted against ``console.size.height`` (see :func:`render_view`):
      ``tail_lines`` (6, 20 with ``verbose``, ``0`` = no tail) and ``max_children`` (finished
      children listed per node before ``… +N more``) are the starting point.
    * Model updates never raise: malformed event data is coerced, unexpected errors are logged
      once to ``rayspec.events``.
    """

    def __init__(
        self,
        console: Console,
        *,
        quiet: bool = False,
        verbose: bool = False,
        tail_lines: int | None = None,
        live: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        refresh_per_second: float = 8,
        summary: bool = True,
        display: bool = True,
        show_started: bool | None = None,
        max_children: int = DEFAULT_MAX_CHILDREN,
    ) -> None:
        super().__init__(console, show_started=verbose if show_started is None else show_started)
        self.quiet = quiet
        self.verbose = verbose
        if tail_lines is None:
            tail_lines = VERBOSE_TAIL_LINES if verbose else DEFAULT_TAIL_LINES
        #: tail lines kept per running step (``0`` = no tail)
        self.tail_lines = max(0, tail_lines)
        self.max_children = max(0, max_children)
        if live is None:
            live = bool(console.is_terminal) and not console.is_dumb_terminal
        #: the tree model is maintained (else every event is a quiet line)
        self.tree_enabled = bool(live) and not quiet
        #: a Live display is driven from the model (``display=False`` → headless model)
        self.live_enabled = self.tree_enabled and display
        self.refresh_per_second = refresh_per_second
        self.summary = summary
        self.view = RunView(clock=clock, tail_lines=self.tail_lines)
        self._lock = anyio.Lock()
        self._model_lock = threading.Lock()
        self._live: Live | None = None
        self._suspend_depth = 0
        self._closed = False
        self._render_failed = False
        self._apply_failed = False
        self._last_render: RenderableType = Text("")

    # -- properties -------------------------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        """``True`` while a Live display is on screen."""
        return self._live is not None

    # -- EventSink ------------------------------------------------------------------------------

    async def emit(self, event: RunEvent) -> None:
        """Fold ``event`` into the model and drive the display (quiet lines when not live)."""
        if not self.tree_enabled:
            await super().emit(event)
            return
        async with self._lock:
            was_finished = self.view.finished
            with self._model_lock:
                self._apply(event)
            if not self.live_enabled:
                return
            if was_finished or self._closed:
                # after the final frame every late event degrades to a quiet line
                await super().emit(event)
                return
            if self.view.finished:
                self._stop_live()
                if self.summary:
                    self._print(self.render_summary())
                return
            if self._suspend_depth == 0:
                self._start_live()
                if not self.tree_enabled:  # the display failed to start: quiet from now on
                    await super().emit(event)

    def _apply(self, event: RunEvent) -> None:
        try:
            self.view.apply(event)
        except Exception as exc:
            if not self._apply_failed:
                self._apply_failed = True
                log.warning("console sink: cannot apply %s: %s", event.type.value, exc)

    def _apply_stream(self, step_path: str, record: StreamRecord) -> None:
        try:
            self.view.apply_stream(step_path, record, verbose=self.verbose)
        except Exception as exc:
            if not self._apply_failed:
                self._apply_failed = True
                log.warning("console sink: cannot apply stream %s: %s", record.kind, exc)

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        """Feed a step's tail; in quiet / non-tree mode only provider warnings are printed
        (``⚠ <step>: …``, see :meth:`QuietConsoleSink.emit_stream`)."""
        if not self.tree_enabled:
            await super().emit_stream(step_path, record)
            return
        async with self._lock:
            with self._model_lock:
                self._apply_stream(step_path, record)

    async def aclose(self) -> None:
        """Stop the Live display (its last frame stays on screen); idempotent."""
        async with self._lock:
            self._closed = True
            self._stop_live()

    # -- pause / resume -------------------------------------------------------------------------

    async def pause(self) -> None:
        """Stop the Live display (printing the current tree once) until :meth:`resume`.

        Calls nest: the display restarts when every ``pause()`` has been matched by a
        ``resume()``. Safe when nothing is live (a later event will not start the display while
        suspended).
        """
        async with self._lock:
            self._suspend_depth += 1
            if self._live is not None:
                self._stop_live()

    async def resume(self) -> None:
        """Undo one :meth:`pause`; when the outermost pause ends the display restarts if the
        model has received any event (also events that arrived while suspended) and the run is
        neither finished nor closed."""
        async with self._lock:
            if self._suspend_depth == 0:
                return
            self._suspend_depth -= 1
            if (
                self._suspend_depth == 0
                and self.live_enabled
                and self.view.started_at is not None
                and not self.view.finished
                and not self._closed
            ):
                self._start_live()

    @asynccontextmanager
    async def suspended(self) -> AsyncIterator[None]:
        """``async with sink.suspended(): await prompt(...)`` — pause around a prompt."""
        await self.pause()
        try:
            yield
        finally:
            await self.resume()

    # -- rendering (pure, deterministic) --------------------------------------------------------

    def render(self, *, height: int | None = None) -> RenderableType:
        """The current tree + footer; ``height`` (rows) budgets the frame like the Live display
        does (``None`` = unbounded)."""
        with self._model_lock:
            return render_view(self.view, height=height, max_children=self.max_children)

    def render_summary(self) -> Panel:
        """The final summary panel for the current model."""
        with self._model_lock:
            return render_summary(self.view)

    def _get_renderable(self) -> RenderableType:
        try:
            self._last_render = self.render(height=self.console.size.height)
        except Exception as exc:  # pragma: no cover - defensive: keep the refresh thread alive
            if not self._render_failed:
                self._render_failed = True
                log.warning("console sink: render failed: %s", exc)
        return self._last_render

    # -- Live plumbing --------------------------------------------------------------------------

    def _start_live(self) -> None:
        if self._live is not None or self._closed:
            return
        try:
            live = Live(
                get_renderable=self._get_renderable,
                console=self.console,
                refresh_per_second=self.refresh_per_second,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
                vertical_overflow="ellipsis",
            )
            live.start()
        except Exception as exc:
            # degrade to quiet lines for the rest of the run (CONTRACTS: never raise, never go
            # silent)
            self.live_enabled = False
            self.tree_enabled = False
            log.warning("console sink: cannot start the live display: %s", exc)
            return
        self._live = live

    def _stop_live(self) -> None:
        live, self._live = self._live, None
        if live is None:
            return
        try:
            live.stop()
        except Exception as exc:
            if not self._failed:
                self._failed = True
                log.warning("console sink: cannot stop the live display: %s", exc)

    def _print(self, renderable: RenderableType) -> None:
        try:
            self.console.print(renderable)
        except Exception as exc:
            if not self._failed:
                self._failed = True
                log.warning("console sink: cannot print: %s", exc)


__all__ = [
    "DEFAULT_MAX_CHILDREN",
    "DEFAULT_TAIL_LINES",
    "VERBOSE_TAIL_LINES",
    "ConsoleSink",
    "QuietConsoleSink",
    "RunView",
    "StepView",
    "error_text",
    "fmt_cost",
    "fmt_duration",
    "fmt_tokens",
    "format_stream_warning",
    "render_run_line",
    "render_step_line",
    "render_summary",
    "render_tree",
    "render_view",
    "usage_total",
]

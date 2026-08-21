# SPDX-License-Identifier: Apache-2.0
"""`rayspec logs <run> [--step <path>] [--follow] [--stream] [--verbose] [--raw] [--json]` —
render a run's logs.

Default: the lifecycle events (``events.jsonl``), one timestamped line each. ``--step <path>``
renders that step's ``stream.jsonl`` (agent text/tool calls, shell stdout/stderr/exit; reasoning
deltas are joined per block and printed as whole ``thinking:`` lines, internal ``raw`` SDK
records are hidden unless ``--verbose``); ``--stream`` interleaves every step's stream
into the event log (ordered by timestamp); ``--json`` prints the raw JSONL records (events as
stored, stream records wrapped as ``{"type": "stream", "step_path": ..., "record": {...}}``);
``--follow`` tails the files until ``run.json`` leaves the ``running`` status. Every rendered
string is untrusted text and goes through :mod:`rayspec.textsafe`; ``--raw`` prints it
unescaped for debugging. Reading only — the store owns the files.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import anyio
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.text import Text

from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import JsonOption, RootOption, console, fail
from rayspec.engine.paths import StepPath
from rayspec.engine.runtime import EXIT_INTERRUPTED
from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.schema import RunStatus
from rayspec.store.file import EVENTS_JSONL, STREAM_JSONL, FileRunStore, StoreError
from rayspec.textsafe import safe_text

EVENTS_SOURCE = "events"
LogItem = RunEvent | StreamRecord
EmitFn = Callable[[str, LogItem], None]


# --------------------------------------------------------------------------------------------------
# reading + tailing
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class LogTailer:
    """Incremental reader over ``events.jsonl`` and the selected ``stream.jsonl`` files.

    ``poll()`` returns the records appended since the previous call (complete lines only; a
    torn trailing line waits for the next poll). Sources are rediscovered on every poll so a
    step that starts streaming later is picked up.
    """

    store: FileRunStore
    run_id: str
    step: str | None = None
    stream: bool = False
    _offsets: dict[Path, int] = field(default_factory=dict)

    @property
    def run_dir(self) -> Path:
        return self.store.run_dir(self.run_id)

    def sources(self) -> list[tuple[str, Path]]:
        """``(source, file)`` pairs: ``events`` and/or step paths."""
        out: list[tuple[str, Path]] = []
        if self.step is None:
            out.append((EVENTS_SOURCE, self.run_dir / EVENTS_JSONL))
            if self.stream:
                steps_dir = self.run_dir / "steps"
                if steps_dir.is_dir():
                    for file in sorted(steps_dir.glob(f"**/{STREAM_JSONL}")):
                        out.append((file.parent.relative_to(steps_dir).as_posix(), file))
        else:
            out.append((self.step, self.run_dir / "steps" / self.step / STREAM_JSONL))
        return out

    def poll(self) -> list[tuple[str, LogItem]]:
        """New records since the last poll, file by file (events first)."""
        items: list[tuple[str, LogItem]] = []
        for source, file in self.sources():
            parse: Callable[[str], LogItem] = (
                RunEvent.from_json if source == EVENTS_SOURCE else StreamRecord.from_json
            )
            for line in self._new_lines(file):
                try:
                    items.append((source, parse(line)))
                except (ValidationError, ValueError):
                    continue  # an unreadable middle line is skipped (store semantics)
        return items

    def _new_lines(self, file: Path) -> Iterator[str]:
        offset = self._offsets.get(file, 0)
        try:
            with file.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
        except FileNotFoundError:
            return
        if not chunk:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return  # only a torn line so far
        self._offsets[file] = offset + end + 1
        for raw in chunk[:end].split(b"\n"):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                yield line


def _sort_key(pair: tuple[str, LogItem]) -> datetime:
    ts = pair[1].ts
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def read_history(
    store: FileRunStore, run_id: str, *, step: str | None, stream: bool
) -> list[tuple[str, LogItem]]:
    """Everything recorded so far; interleaved sources are ordered by timestamp."""
    tailer = LogTailer(store, run_id, step=step, stream=stream)
    items = tailer.poll()
    if step is None and stream:
        items.sort(key=_sort_key)  # stable: ties keep file order (events first)
    return items


def _run_is_live(store: FileRunStore, run_id: str) -> bool:
    try:
        return store.load(run_id).status is RunStatus.RUNNING
    except StoreError:
        return False
    except OSError:
        return False


async def follow(
    store: FileRunStore,
    run_id: str,
    *,
    step: str | None,
    stream: bool,
    emit: EmitFn,
    poll_s: float = 0.25,
) -> None:
    """Emit stored records, then tail until ``run.json`` is no longer ``running`` (one final
    drain after the status flips so the closing events are not lost)."""
    tailer = LogTailer(store, run_id, step=step, stream=stream)
    first = True
    while True:
        items = tailer.poll()
        if first and step is None and stream:
            items.sort(key=_sort_key)
        first = False
        for source, item in items:
            emit(source, item)
        if not _run_is_live(store, run_id):
            for source, item in tailer.poll():  # final drain
                emit(source, item)
            return
        await anyio.sleep(poll_s)


# --------------------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------------------


def _stamp(ts: datetime) -> str:
    moment = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%H:%M:%S")


def format_event(event: RunEvent, formatter: Any, *, raw: bool = False) -> Text:
    """One line for a lifecycle event: the quiet console sink's rendering (already safe text),
    or a generic ``<type> <step> <data>`` line for the events it does not print (loop/each
    progress) — escape sequences removed unless ``raw``."""
    line = formatter.format_event(event)
    if line is None:

        def clean(value: Any) -> str:
            return str(value) if raw else safe_text(value, keep_newlines=False)

        line = Text(event.type.value)
        if event.step_path:
            line.append(f" {clean(event.step_path)}")
        if event.type is EventType.LOOP_ITERATION:
            n, mx = event.data.get("n"), event.data.get("max")
            line.append(f" iteration {n}" + (f"/{mx}" if mx else ""), style="dim")
        elif event.type is EventType.EACH_ITEM:
            idx, total = event.data.get("index"), event.data.get("total")
            line.append(f" item {idx}" + (f"/{total}" if total is not None else ""), style="dim")
        elif event.data:
            data = json.dumps(event.data, ensure_ascii=False, default=str)
            line.append(f" {clean(data)}", style="dim")
    return Text.assemble((f"{_stamp(event.ts)}  ", "dim"), line)


#: Stream kinds that are internal SDK plumbing: rendered only with ``--verbose``.
_VERBOSE_ONLY_KINDS = frozenset({"raw"})
#: Stream kinds the renderer knows how to print (anything else is hidden unless ``--verbose``).
_KNOWN_KINDS = frozenset(
    {
        "text_delta",
        "text",
        "reasoning",
        "tool_call",
        "tool_result",
        "command_start",
        "command_output",
        "command_end",
        "stdout",
        "stderr",
        "exit",
        "usage",
        "warning",
        "error",
        "session",
        "plan",
        "file_change",
    }
)


class StreamRenderer:
    """Turn stream records into lines.

    ``text_delta`` fragments are buffered per step and flushed as one block when the completed
    ``text`` (or any other record) arrives; ``reasoning`` deltas are buffered per block too and
    printed as whole ``thinking:`` lines (complete lines as they appear, the rest when the block
    ends). ``raw``/unknown kinds are hidden unless ``verbose``. Every printed string is
    passed through :func:`rayspec.textsafe.safe_text` unless ``raw``.
    """

    def __init__(
        self, out: Console, *, prefix_steps: bool, verbose: bool = False, raw: bool = False
    ) -> None:
        self.out = out
        self.prefix_steps = prefix_steps
        self.verbose = verbose
        self.raw = raw
        self._buffers: dict[str, tuple[datetime, str]] = {}
        self._thinking: dict[str, tuple[datetime, str]] = {}

    def _clean(self, text: Any) -> str:
        return str(text) if self.raw else safe_text(text, keep_newlines=False)

    def _print(self, step_path: str, ts: datetime, body: Text | str, *, style: str = "") -> None:
        line = Text.assemble((f"{_stamp(ts)}  ", "dim"))
        if self.prefix_steps:
            line.append(f"[{self._clean(step_path)}] ", style="cyan")
        if isinstance(body, Text):
            line.append_text(body)
        else:
            line.append(self._clean(body), style=style)
        self.out.print(line, markup=False, highlight=False, soft_wrap=True)

    def flush(self, step_path: str | None = None) -> None:
        """Print buffered text deltas and thinking (of one step or all)."""
        keys = [step_path] if step_path is not None else list(self._buffers)
        for key in keys:
            buffered = self._buffers.pop(key, None)
            if buffered and buffered[1].strip():
                ts, text = buffered
                for line in text.splitlines():
                    self._print(key, ts, line)
        self.flush_thinking(step_path)

    def flush_thinking(self, step_path: str | None = None) -> None:
        """Print the pending (partial) thinking line(s) of one step or all — a block ended."""
        keys = [step_path] if step_path is not None else list(self._thinking)
        for key in keys:
            buffered = self._thinking.pop(key, None)
            if buffered is None:
                continue
            ts, text = buffered
            for line in text.splitlines():
                if line.strip():
                    self._print(key, ts, f"thinking: {line.strip()}", style="dim")

    def _think(self, step_path: str, rec: StreamRecord) -> None:
        ts, text = self._thinking.get(step_path, (rec.ts, ""))
        text += rec.text
        *done, rest = text.split("\n")
        for line in done:
            if line.strip():
                self._print(step_path, ts, f"thinking: {line.strip()}", style="dim")
        if done:
            ts = rec.ts  # a fresh line starts at this record's time
        self._thinking[step_path] = (ts, rest)

    def render(self, step_path: str, rec: StreamRecord) -> None:
        """Print ``rec`` (see the class docstring for the per-kind shapes)."""
        kind = rec.kind
        if kind == "text_delta":
            self.flush_thinking(step_path)
            ts, text = self._buffers.get(step_path, (rec.ts, ""))
            self._buffers[step_path] = (ts, text + rec.text)
            return
        if kind == "reasoning":
            self._think(step_path, rec)
            return
        if kind in _VERBOSE_ONLY_KINDS or kind not in _KNOWN_KINDS:
            if not self.verbose:
                return  # internal plumbing ('raw status', 'raw thinking_tokens' …)
            self.flush(step_path)
            text = rec.text.strip().splitlines()
            body = f"{kind} {rec.name}" if rec.name else kind
            if text:
                body += f": {text[0]}"
            if rec.attempt > 1:
                body += f" (attempt {rec.attempt})"
            self._print(step_path, rec.ts, body, style="dim")
            return
        if kind == "text":
            self.flush_thinking(step_path)
            self._buffers.pop(step_path, None)
            for line in rec.text.splitlines() or [""]:
                self._print(step_path, rec.ts, line)
            return
        self.flush(step_path)
        attempt = f" (attempt {rec.attempt})" if rec.attempt > 1 else ""
        if kind == "tool_call":
            args = rec.data.get("input")
            shown = f"({json.dumps(args, ensure_ascii=False, default=str)})" if args else ""
            self._print(
                step_path, rec.ts, f"⚙ {rec.name or 'tool'}{shown}{attempt}", style="magenta"
            )
        elif kind == "tool_result":
            text = rec.text.strip()
            first = text.splitlines()[0] if text else ""
            more = " …" if len(text.splitlines()) > 1 else ""
            self._print(step_path, rec.ts, f"  → {first}{more}", style="dim")
        elif kind == "command_start":
            cmd = rec.data.get("command") or rec.text
            self._print(step_path, rec.ts, f"$ {cmd}", style="bold")
        elif kind in {"command_output", "stdout"}:
            for line in rec.text.splitlines():
                self._print(step_path, rec.ts, line)
        elif kind == "stderr":
            for line in rec.text.splitlines():
                self._print(step_path, rec.ts, line, style="red")
        elif kind == "command_end":
            code = rec.data.get("exit_code")
            self._print(
                step_path, rec.ts, f"exit {code}" if code is not None else "done", style="dim"
            )
        elif kind == "exit":
            code = rec.data.get("exit_code", rec.text)
            self._print(step_path, rec.ts, f"exit {code}", style="dim")
        elif kind == "usage":
            usage = rec.data.get("usage") or {}
            self._print(
                step_path, rec.ts, f"usage {json.dumps(usage, ensure_ascii=False)}", style="dim"
            )
        elif kind in {"warning", "error"}:
            style = "yellow" if kind == "warning" else "red"
            self._print(step_path, rec.ts, f"{kind}: {rec.text.strip()}", style=style)
        elif kind == "session":
            self._print(step_path, rec.ts, f"session {rec.text}{attempt}", style="dim")
        elif kind == "file_change":
            first = rec.text.strip().splitlines()
            self._print(
                step_path, rec.ts, f"✎ {rec.name or (first[0] if first else '')}", style="cyan"
            )
        else:  # plan
            for line in rec.text.strip().splitlines():
                self._print(step_path, rec.ts, f"plan: {line}", style="dim")


def _json_line(source: str, item: LogItem) -> str:
    if isinstance(item, RunEvent):
        return item.to_json()
    return json.dumps(
        {"type": "stream", "step_path": source, "record": json.loads(item.to_json())},
        ensure_ascii=False,
    )


def make_emitter(
    out: Console,
    *,
    json_mode: bool,
    prefix_steps: bool,
    verbose: bool = False,
    raw: bool = False,
) -> tuple[EmitFn, Callable[[], None]]:
    """``(emit, finish)``: ``emit`` prints one record, ``finish`` flushes buffered deltas."""
    if json_mode:

        def emit_json(source: str, item: LogItem) -> None:
            out.print(_json_line(source, item), markup=False, highlight=False, soft_wrap=True)

        return emit_json, lambda: None
    from rayspec.events.sinks import QuietConsoleSink

    formatter = QuietConsoleSink(out, show_started=True)
    renderer = StreamRenderer(out, prefix_steps=prefix_steps, verbose=verbose, raw=raw)

    def emit(source: str, item: LogItem) -> None:
        if isinstance(item, RunEvent):
            renderer.flush()
            line = format_event(item, formatter, raw=raw)
            out.print(line, markup=False, highlight=False, soft_wrap=True)
        else:
            renderer.render(source, item)

    return emit, renderer.flush


# --------------------------------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    @app.command()
    def logs(  # noqa: PLR0917 - Typer options are positional by construction
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        step: Annotated[
            str | None,
            typer.Option("--step", help="Show this step's stream (e.g. build[1]/implement)."),
        ] = None,
        follow: Annotated[
            bool, typer.Option("--follow", "-f", help="Keep tailing while the run is live.")
        ] = False,
        stream: Annotated[
            bool, typer.Option("--stream", help="Interleave every step's stream into the log.")
        ] = False,
        verbose: Annotated[
            bool,
            typer.Option("--verbose", help="Also show internal/raw SDK records of a step stream."),
        ] = False,
        raw: Annotated[
            bool,
            typer.Option(
                "--raw",
                help="Print stored text unescaped (control characters and escape sequences "
                "included) — for debugging only.",
            ),
        ] = False,
        json_: JsonOption = False,
        root: RootOption = None,
    ) -> None:
        """Show a run's event log (or one step's stream); --follow tails a live run."""
        ctx = common.make_runs_context(root)
        store, record = common.lookup_run(ctx, run)
        if step is not None:
            if not step:  # StepPath.parse('') is the root path, not an error
                fail("--step needs a step path (e.g. --step build[1]/implement)")
                return
            problem = step_path_problem(step)
            if problem is not None:
                fail(problem)
                return
            known = step in record.steps or (store.run_dir(record.run_id) / "steps" / step).is_dir()
            if not known:
                fail(
                    f"no step {step!r} in run {record.run_id}",
                    hint=f"run `rayspec show {record.run_id}` to list its steps",
                )
                return
        out = console()
        emit, finish = make_emitter(
            out, json_mode=json_, prefix_steps=step is None and stream, verbose=verbose, raw=raw
        )
        if follow:
            try:
                anyio.run(
                    lambda: _follow(store, record.run_id, step=step, stream=stream, emit=emit),
                    backend="asyncio",
                )
            except KeyboardInterrupt:
                finish()
                raise typer.Exit(code=EXIT_INTERRUPTED) from None
        else:
            for source, item in read_history(store, record.run_id, step=step, stream=stream):
                emit(source, item)
        finish()


async def _follow(
    store: FileRunStore, run_id: str, *, step: str | None, stream: bool, emit: EmitFn
) -> None:
    await follow(store, run_id, step=step, stream=stream, emit=emit)


def step_path_problem(step: str) -> str | None:
    """The one-line error for an invalid ``--step`` value, ``None`` when it parses.

    ``StepPath.parse`` already prefixes ``invalid step path '<p>':`` — it is not repeated; an
    absolute path says so instead of the cryptic ``bad segment ''``.
    """
    prefix = f"invalid step path {step!r}: "
    if step.startswith("/"):
        return prefix + "absolute paths are not step paths"
    try:
        StepPath.parse(step)
    except ValueError as exc:
        message = str(exc)
        return message if message.startswith(prefix) else prefix + message
    return None


__all__ = [
    "EVENTS_SOURCE",
    "LogTailer",
    "StreamRenderer",
    "follow",
    "format_event",
    "make_emitter",
    "read_history",
    "register",
    "step_path_problem",
]

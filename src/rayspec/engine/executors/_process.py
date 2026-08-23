# SPDX-License-Identifier: Apache-2.0
"""Shared subprocess runner for ``shell:`` and ``python:`` steps.

Module boundary: spawns the interpreter with ``anyio.open_process(start_new_session=True)``,
streams stdout/stderr to ``steps/<path>/stdout.log|stderr.log`` (attempt 1 starts the file,
later attempts — retries and resumes — append under a ``--- attempt N ---`` header so no
attempt's diagnostics are lost) and to the stream sinks as ``StreamRecord`` kinds
``stdout``/``stderr``/``exit``, and on cancellation/timeout kills the whole process group
(``SIGTERM`` → bounded wait → ``SIGKILL``).

The log files are the one persisted surface that does not go through the run store, so the pump
applies the store's :class:`~rayspec.redact.Redactor` itself — through a
:class:`~rayspec.redact.StreamRedactor`, so a secret split across two reads of the pipe is
caught as well. Everything downstream (the captured chunks that become the step output, the
stream records) therefore sees the redacted text.
"""

from __future__ import annotations

import codecs
import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import jsonschema
from anyio.abc import Process

from rayspec.engine.context import ExecScope, RunContext, StepOutcome, error_info, sha256_json
from rayspec.engine.paths import StepPath
from rayspec.events.model import StreamRecord
from rayspec.redact import StreamRedactor
from rayspec.schema import PythonStep, ShellStep, StepStatus
from rayspec.store.file import open_private
from rayspec.store.model import ErrorInfo, StepRecord
from rayspec.templating import RenderedScript, export_env, write_context_file

#: seconds between SIGTERM and SIGKILL when a process must die.
KILL_GRACE_S = 2.0
_STDERR_TAIL_LINES = 20
#: the marker :data:`rayspec.redact.REDACTION` leaves behind, used to explain a JSON
#: document that redaction — not the script — made invalid.
_REDACTED_RE = re.compile(r"\[REDACTED:([^\]]+)\]")

#: Variables that describe the environment rayspec itself was launched from (``uv run``, an
#: activated venv) and must not leak into shell/python steps — a step that sets them explicitly
#: in its ``env:`` keeps its own value.
LAUNCHER_ENV_VARS: frozenset[str] = frozenset(
    {"VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "UV_PROJECT_ENVIRONMENT", "PYTHONHOME"}
)


def launcher_venvs(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The virtualenv prefixes rayspec runs from: ``sys.prefix`` (when it is a venv) and
    ``$VIRTUAL_ENV`` of the launching environment — values pointing into them are scrubbed."""
    found: list[str] = []
    if sys.prefix != sys.base_prefix:
        found.append(sys.prefix)
    venv = (env or {}).get("VIRTUAL_ENV")
    if venv and venv not in found:
        found.append(venv)
    return tuple(found)


def _points_into(value: str, venvs: Iterable[str]) -> bool:
    path = os.path.normcase(value)
    for venv in venvs:
        root = os.path.normcase(venv.rstrip("/\\"))
        if not root:
            continue
        if path == root or path.startswith(root + "/") or path.startswith(root + "\\"):
            return True
    return False


def scrub_launcher_env(
    env: Mapping[str, str], *, venvs: Iterable[str] | None = None
) -> dict[str, str]:
    """A copy of ``env`` without the launcher-only variables.

    Drops :data:`LAUNCHER_ENV_VARS` and every other variable whose value is a path inside one
    of ``venvs`` (default: :func:`launcher_venvs`). A list value (``os.pathsep``-separated, e.g.
    ``PYTHONPATH``) is filtered entry by entry: only the entries inside a venv are removed, the
    variable itself goes only when nothing is left. ``PATH`` is left untouched: tools on it must
    keep resolving inside steps.
    """
    roots = tuple(launcher_venvs(env) if venvs is None else venvs)
    out: dict[str, str] = {}
    for key, raw in env.items():
        if key in LAUNCHER_ENV_VARS:
            continue
        value = raw
        if key != "PATH" and roots:
            if os.pathsep in raw:
                kept = [e for e in raw.split(os.pathsep) if not _points_into(e, roots)]
                if not kept:
                    continue
                value = os.pathsep.join(kept)
            elif _points_into(raw, roots):
                continue
        out[key] = value
    return out


@dataclass(slots=True)
class ProcessResult:
    """Exit code plus the captured (decoded) stdout/stderr of one attempt."""

    exit_code: int
    stdout: str
    stderr: str


def _kill_group(process: Process, sig: signal.Signals) -> None:
    # start_new_session=True ⇒ the process leads its own group (pgid == pid), so the group can
    # be signalled even after the leader itself has exited (its children may still be alive).
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            process.send_signal(sig)


async def terminate_process(process: Process, *, grace_s: float = KILL_GRACE_S) -> None:
    """SIGTERM the process group, wait ``grace_s`` for the leader, then SIGKILL the group."""
    with anyio.CancelScope(shield=True):
        _kill_group(process, signal.SIGTERM)
        if process.returncode is None:
            with anyio.move_on_after(grace_s):
                await process.wait()
        _kill_group(process, signal.SIGKILL)
        if process.returncode is None:
            with anyio.move_on_after(grace_s):
                await process.wait()


async def _close_streams(process: Process) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.aclose()


async def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin_text: str | None,
    ctx: RunContext,
    path: StepPath,
    attempt: int,
) -> ProcessResult:
    """Run ``command`` streaming output to logs/sinks; kills the group on cancellation."""
    step_dir = ctx.step_dir(path)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    process = await anyio.open_process(
        list(command),
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with anyio.create_task_group() as tg:
            if stdin_text is not None and process.stdin is not None:
                tg.start_soon(_feed_stdin, process, stdin_text)
            tg.start_soon(
                _pump,
                process.stdout,
                "stdout",
                step_dir / "stdout.log",
                stdout_chunks,
                ctx,
                str(path),
                attempt,
            )
            tg.start_soon(
                _pump,
                process.stderr,
                "stderr",
                step_dir / "stderr.log",
                stderr_chunks,
                ctx,
                str(path),
                attempt,
            )
        await process.wait()
    except BaseException:
        await terminate_process(process)
        raise
    finally:
        with anyio.CancelScope(shield=True):
            await _close_streams(process)
    exit_code = process.returncode if process.returncode is not None else -1
    with anyio.CancelScope(shield=True):
        await ctx.emit_stream(
            str(path),
            StreamRecord(kind="exit", attempt=attempt, data={"exit_code": exit_code}),
        )
    return ProcessResult(
        exit_code=exit_code, stdout="".join(stdout_chunks), stderr="".join(stderr_chunks)
    )


async def _feed_stdin(process: Process, text: str) -> None:
    assert process.stdin is not None
    try:
        await process.stdin.send(text.encode("utf-8"))
    finally:
        await process.stdin.aclose()


async def _pump(  # noqa: PLR0917 - task-group entry point, positional by design
    stream: Any,
    kind: str,
    log_path: Path,
    chunks: list[str],
    ctx: RunContext,
    path: str,
    attempt: int,
) -> None:
    if stream is None:
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    # redact before the text reaches the log file, the captured output or a sink; the
    # stream buffer holds back the boundary so a secret split across two reads is still caught
    scrub = StreamRedactor(ctx.store.redactor)
    pending = ""
    first = attempt <= 1 or not log_path.exists()
    with open_private(log_path, "w" if first else "a") as log:  # 0600: script output
        if not first:
            log.write(f"\n--- attempt {attempt} ---\n")
        async for raw in stream:
            text = scrub.feed(decoder.decode(raw))
            if not text:
                continue
            chunks.append(text)
            log.write(text)
            log.flush()
            pending += text
            *lines, pending = pending.split("\n")
            for line in lines:
                await ctx.emit_stream(
                    path, StreamRecord(kind=kind, attempt=attempt, text=line + "\n")
                )
        tail = scrub.feed(decoder.decode(b"", final=True)) + scrub.flush()
        if tail:
            chunks.append(tail)
            log.write(tail)
            pending += tail
        if pending:
            await ctx.emit_stream(path, StreamRecord(kind=kind, attempt=attempt, text=pending))


# --------------------------------------------------------------------------------------------------
# shared step logic
# --------------------------------------------------------------------------------------------------


def resolve_cwd(step: ShellStep | PythonStep, ctx: RunContext, tctx: Mapping[str, Any]) -> Path:
    """``cwd:`` rendered relative to the run's workdir (default: the workdir itself)."""
    if step.cwd is None:
        return ctx.workdir
    rendered = ctx.engine.render_str(step.cwd, tctx)
    path = Path(rendered).expanduser()
    if not path.is_absolute():
        path = ctx.workdir / path
    return path


def process_env(
    step: ShellStep | PythonStep,
    ctx: RunContext,
    tctx: Mapping[str, Any],
    rendered: RenderedScript,
    path: StepPath,
    *,
    scope: ExecScope | None = None,
) -> dict[str, str]:
    """``os.environ`` (minus launcher-only vars) + exported ``RAYSPEC_*`` + ``RAYSPEC_CONTEXT`` +
    step ``env:`` + slots. See :func:`scrub_launcher_env`.

    ``tctx`` is the (redacted) template context: it is what ``context.json`` records and what
    the script body was rendered from. With ``scope`` given, the ``secret: true`` inputs
    are added as ``RAYSPEC_INPUT_<NAME>`` and the step's ``env:`` mapping is rendered with the
    real values (:meth:`RunContext.secret_context`) — the only two ways a secret input reaches a
    step. The resolved ``config.secrets`` entries are added under their own names, below
    the step's ``env:`` so a step can still override one; like every secret they are absent from
    ``context.json``, from ``export_env`` and from the fingerprint.
    """
    env: dict[str, str] = scrub_launcher_env(ctx.env)
    env.update(export_env(tctx))
    context_path = write_context_file(tctx, ctx.step_dir(path) / "context.json")
    env["RAYSPEC_CONTEXT"] = str(context_path)
    env["RAYSPEC_STEP_PATH"] = str(path)
    env.update(ctx.options.config_secrets)  # config secrets, shell/python steps only
    if scope is not None and ctx.secret_inputs:
        env.update(ctx.secret_env(scope))
        env.update(ctx.render_env(step.env, ctx.secret_context(scope)))
    else:
        env.update(ctx.render_env(step.env, tctx))
    env.update(rendered.env)
    return env


def finish_script_outcome(
    step: ShellStep | PythonStep,
    record: StepRecord,
    result: ProcessResult,
    *,
    dry_run: bool = False,
) -> StepOutcome:
    """Map a process result onto the record: output, exit code, ``output_schema``, failure."""
    record.exit_code = result.exit_code
    stdout = result.stdout.rstrip("\r\n")
    outcome = StepOutcome(record=record, output=stdout, output_kind="text", stderr=result.stderr)
    if dry_run:
        outcome.event_data["dry_run"] = True
    if result.exit_code != 0:
        record.status = StepStatus.FAILED
        record.ok = False
        tail = "\n".join(result.stderr.strip().splitlines()[-_STDERR_TAIL_LINES:])
        message = f"exit code {result.exit_code}"
        if tail:
            message += f": {tail}"
        record.error = ErrorInfo(type="exit", message=message, transient=False)
        return outcome
    if step.output_schema is not None:
        try:
            value = json.loads(stdout)
        except ValueError as exc:
            record.status = StepStatus.FAILED
            record.ok = False
            record.error = ErrorInfo(
                type="output_schema",
                message=(
                    f"stdout is not valid JSON ({exc}); output_schema requires a JSON document"
                    + _redaction_note(stdout)
                ),
                transient=False,
            )
            return outcome
        try:
            jsonschema.validate(value, step.output_schema)
        except jsonschema.ValidationError as exc:
            record.status = StepStatus.FAILED
            record.ok = False
            record.error = ErrorInfo(
                type="output_schema",
                message=f"stdout does not match output_schema: {exc.message}",
                transient=False,
            )
            return outcome
        outcome.output = value
        outcome.output_kind = "json"
    record.status = StepStatus.SUCCEEDED
    record.ok = True
    return outcome


def _redaction_note(stdout: str) -> str:
    """The explanation to append when redaction is what broke the JSON document.

    A secret that appears as a BARE JSON token (a number, ``true``, ``null``) is replaced by
    ``[REDACTED:<name>]`` — which is not a JSON value — so the document the user printed is not
    the document rayspec parsed, and the log shows one that looks broken for no reason.
    """
    names = sorted(set(_REDACTED_RE.findall(stdout)))
    if not names:
        return ""
    listed = ", ".join(names)
    return (
        f" — the redacted secret(s) {listed} appear as [REDACTED:{names[0]}] in it; a secret "
        "used as a bare JSON token (a number, true, null) must be quoted in the document"
    )


def dry_run_outcome(step: ShellStep | PythonStep, record: StepRecord) -> StepOutcome:
    """``--dry-run`` without ``--exec-shell``: succeed with an empty (or minimal) output."""
    from rayspec.providers.stub import minimal_instance

    record.status = StepStatus.SUCCEEDED
    record.ok = True
    record.exit_code = 0
    if step.output_schema is not None:
        value = minimal_instance(step.output_schema)
        outcome = StepOutcome(record=record, output=value, output_kind="json", stderr="")
    else:
        outcome = StepOutcome(record=record, output="", output_kind="text", stderr="")
    outcome.event_data["dry_run"] = True
    return outcome


def script_fingerprint(
    kind: str, rendered: RenderedScript, cwd: Path, env: Mapping[str, str]
) -> str:
    """sha256 over the rendered script, its slot values, cwd and the step env.

    Spilled values (> 64 KiB) name a random tmp path — in the shell preamble that assigns the
    slot, or in the python ``Path(...)`` call; the path is replaced by a digest of the spill's
    content so the fingerprint stays stable.
    """
    script = rendered.script
    for spill in rendered.spills:
        try:
            digest = hashlib.sha256(Path(spill).read_bytes()).hexdigest()
        except OSError:
            digest = "missing"
        script = script.replace(str(spill), f"<spill:{digest}>")
    return sha256_json(
        {"kind": kind, "script": script, "slots": rendered.env, "cwd": str(cwd), "env": env}
    )


def render_failure(record: StepRecord, exc: Exception) -> StepOutcome:
    """A rendering/preparation error is a failed (non-transient) attempt."""
    record.status = StepStatus.FAILED
    record.ok = False
    record.error = error_info(exc, type_="render")
    return StepOutcome(record=record)


def cleanup_spills(rendered: RenderedScript) -> None:
    for spill in rendered.spills:
        with contextlib.suppress(OSError):
            Path(spill).unlink()


__all__ = [
    "KILL_GRACE_S",
    "LAUNCHER_ENV_VARS",
    "ProcessResult",
    "cleanup_spills",
    "dry_run_outcome",
    "finish_script_outcome",
    "launcher_venvs",
    "process_env",
    "render_failure",
    "resolve_cwd",
    "run_process",
    "script_fingerprint",
    "scrub_launcher_env",
    "terminate_process",
]

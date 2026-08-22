# SPDX-License-Identifier: Apache-2.0
"""Capture and mask the observable output of one dry run — the golden corpus machinery.

Boundary: pure capture + masking helpers for ``tests/golden/test_golden.py``. Nothing here is
shipped; the corpus exists because accidental changes to the ``--json`` event stream, the summary
object or ``run.json`` are invisible to unit tests and break scripts, sinks and resume.

Each case of every example (and of the repo's own workflows) is driven through the real Typer app
as ``rayspec run <wf> --dry-run --json --stubs …`` and three files are written:

``events.jsonl``   every stdout line but the last (events + stream records), masked, one per line
``summary.json``   the last stdout line (the ``--json`` summary object), masked, pretty-printed
``run.json``       the record the store wrote, masked, pretty-printed

Masking replaces everything that cannot be reproduced on another machine or in another second —
run ids, timestamps, durations, absolute paths, the host, pids, content hashes and the stub's
derived token counters — with a fixed placeholder, keeping the key and the JSON type. A masked key
is masked *unconditionally*, ``None`` included, because a value that is null on one checkout and a
string on another (``workspace.branch`` on a detached HEAD) is exactly the drift the corpus must
not have. What is left is exactly the shape and the run's own decisions (statuses, skip reasons,
outputs), which is what the corpus is for.
"""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from rayspec.testing.spec import Case, Suite

#: Keys whose value is replaced wholesale (the placeholder keeps the JSON type).
MASKED_KEYS: dict[str, Any] = {
    "ts": "<ts>",
    "started_at": "<ts>",
    "ended_at": "<ts>",
    "created_at": "<ts>",
    "pid_started_at": "<ts>",
    "duration_ms": 0,
    "duration_s": 0,
    "pid": None,
    "host": "<host>",
    "run_id": "<run-id>",
    "run_dir": "<run-dir>",
    "workdir": "<workdir>",
    "project_root": "<root>",
    "project_slug": "<slug>",
    "workflow_hash": "<hash>",
    # the checkout's git branch is not a property of the run under test
    "branch": "<branch>",
    "base_branch": "<branch>",
    "fingerprint": "<sha>",
    "output_sha256": "<sha>",
    "item_sha256": "<sha>",
    "base_sha": "<sha>",
    "head_sha": "<sha>",
    "rayspec_version": "<version>",
    # RunRecord.actor / the run.decision event's actor: who launched the run or answered the
    # gate. That is a property of the machine's user (or of the checkout's git config), not of
    # the run under test — on another laptop it is a different string. Masked whole, so the
    # placeholder keeps the JSON type (a mapping) without pinning anybody's email into the repo.
    "actor": {"id": "<actor>", "source": "<source>", "ci": None, "provider_accounts": {}},
    # RunRecord.toolchain records what was in effect for the run:
    # rayspec/python/platform versions and each provider's SDK + CLI. Every one of those is a
    # property of the MACHINE, not of the run under test — unmasked they make the corpus fail on
    # any other OS, interpreter or rayspec version. `toolchain.models` is deliberately NOT masked:
    # which model each agent resolved to IS a property of the run, and is worth pinning.
    "rayspec": "<version>",
    "python": "<python>",
    "platform": "<platform>",
    "sdk_version": "<version>",
    "cli_version": "<version>",
    "cli_path": "<path>",
}

#: Counters of a ``usage`` object, masked to ``0`` wherever a ``usage:`` mapping appears.
#:
#: The stub provider derives its default token counts from the prompt
#: (``Usage(input=len(req.prompt) // 4)``) and a prompt may embed ``{{ run.workdir }}``, so an
#: unmasked count is a function of the *length of the checkout path* — captured on two differently
#: named directories the same case yields different numbers. A stub token count is not a property
#: of the run under test. They are masked only inside ``usage`` because ``input``/``output`` are
#: generic key names elsewhere.
USAGE_KEYS: frozenset[str] = frozenset(
    {"input", "output", "cached_input", "cache_write", "reasoning"}
)

_RUN_ID = re.compile(r"\b\d{8}-\d{6}-[0-9a-z]{4}\b")
_ISO_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


def text_substitutions(*, home: Path, root: Path) -> list[tuple[re.Pattern[str], str]]:
    """Regexes applied to every string of the captured JSON (paths leak into reasons/outputs)."""
    paths = [
        (Path(home).resolve(), "<home>"),
        (Path(root).resolve(), "<root>"),
        (Path.cwd().resolve(), "<cwd>"),
    ]
    subs = [
        (re.compile(re.escape(str(path))), token)
        for path, token in sorted(paths, key=lambda p: -len(str(p[0])))
    ]
    subs.append((_RUN_ID, "<run-id>"))
    subs.append((_ISO_TS, "<ts>"))
    subs.append((re.compile(re.escape(socket.gethostname())), "<host>"))
    return subs


def mask(value: Any, subs: list[tuple[re.Pattern[str], str]]) -> Any:
    """Recursively mask ``value``: keyed placeholders first, then text substitutions."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in MASKED_KEYS:
                # unconditionally, `None` included: a detached-HEAD checkout has no branch, and a
                # `branch: null` that slipped through would drift against the committed "<branch>"
                out[key] = MASKED_KEYS[key]
            elif key == "usage" and isinstance(item, Mapping):
                out[key] = {k: (0 if k in USAGE_KEYS else mask(v, subs)) for k, v in item.items()}
            else:
                out[key] = mask(item, subs)
        return out
    if isinstance(value, list):
        return [mask(item, subs) for item in value]
    if isinstance(value, str):
        for pattern, token in subs:
            value = pattern.sub(token, value)
        return value
    return value


#: Sort keys for run-level events at the head and tail of the stream; no step path can equal
#: them (a path segment is ``[a-z][a-z0-9_]*`` with optional ``[n]``).
FIRST: Final = ""
LAST: Final = "\uffff"


def canonical_order(events: list[Any]) -> list[Any]:
    """Group each step's events together, so concurrent steps cannot reorder the corpus.

    The stream is captured from a real run, and steps that run in parallel interleave: whichever
    of two concurrent steps the event loop wakes first emits first. The corpus asserted a *total*
    order over that, which is not a property of the run — it is a property of which coroutine got
    resumed, and it made the suite fail about two runs in five once the engine grew a little more
    work on the step-start path.

    Steps are grouped in the order their paths sort, not the order they appeared: an ``each:``
    fans its items out concurrently, so even which of them emits FIRST is a race, and ordering by
    first appearance would only have moved the flake earlier in the stream. Sorting is total and
    costs the reader nothing the record does not already say — ``run.json`` holds each step's
    place in the graph, and the engine's own tests pin execution order.

    What is still compared, which is what the corpus is for: every event of one step, in the order
    that step emitted it; every step that ran; and the run-level events (``run.started``,
    ``run.finished``, the warnings between them), which keep their position relative to the steps
    around them. What is discarded is only the interleaving *between* steps that were running at
    the same time — which no committed file could pin down without being flaky.
    """

    def step_of(event: Any) -> str | None:
        path = event.get("step_path")
        return path if isinstance(path, str) else None

    last_step = max((i for i, e in enumerate(events) if step_of(e)), default=-1)
    keys: list[tuple[str, int]] = []
    current = FIRST  # sorts before every step path, so run.started stays first
    for index, event in enumerate(events):
        path = step_of(event)
        if path is not None:
            current = path
        elif index > last_step:
            # a run-level event after the last step belongs at the end, whichever step happened
            # to finish last — that is exactly the race being canonicalised away
            current = LAST
        keys.append((current, index))
    return [event for _, event in sorted(zip(keys, events, strict=True), key=lambda pair: pair[0])]


def cli_args(suite: Suite, case: Case, *, inputs_file: Path) -> list[str]:
    """``rayspec run`` command line for ``case`` — the same one ``rayspec test`` simulates."""
    args = ["run", case.workflow, "--root", str(suite.root), "--dry-run", "--json"]
    if case.inputs:
        args += ["--inputs-file", str(inputs_file)]
    if case.allow_unsupported:
        args.append("--allow-unsupported")
    if case.stubs is not None:
        args += ["--stubs", str(case.stubs)]
    return args


def invoke(args: list[str], *, home: Path, env: Mapping[str, str | None]) -> tuple[int, str]:
    """Drive the Typer app in process; returns ``(exit_code, stdout)``."""
    from typer.testing import CliRunner

    from rayspec.cli.app import app

    environ: dict[str, str | None] = {
        k: (None if k.startswith("RAYSPEC_INPUT_") else v) for k, v in os.environ.items()
    }
    environ["RAYSPEC_HOME"] = str(home)
    environ["NO_COLOR"] = "1"
    environ.update(env)
    result = CliRunner(env=environ).invoke(app, args, catch_exceptions=False)
    return result.exit_code, result.stdout


def capture(suite: Suite, case: Case, *, home: Path, tmp_path: Path) -> dict[str, str]:
    """Run one case through the CLI and return the three masked golden files by name."""
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(json.dumps(dict(case.inputs), default=str), encoding="utf-8")
    exit_code, stdout = invoke(
        cli_args(suite, case, inputs_file=inputs_file), home=home, env=case.env
    )
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"{suite.name}:{case.id}: --json printed nothing (exit {exit_code})"
    summary = json.loads(lines[-1])
    record_path = Path(summary["run_dir"]) / "run.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    subs = text_substitutions(home=home, root=suite.root)
    events = canonical_order([mask(json.loads(line), subs) for line in lines[:-1]])
    return {
        "events.jsonl": "".join(json.dumps(e, sort_keys=True) + "\n" for e in events),
        "summary.json": json.dumps(mask(summary, subs), indent=2, sort_keys=True) + "\n",
        "run.json": json.dumps(mask(record, subs), indent=2, sort_keys=True) + "\n",
    }


__all__ = [
    "MASKED_KEYS",
    "USAGE_KEYS",
    "canonical_order",
    "capture",
    "cli_args",
    "invoke",
    "mask",
    "text_substitutions",
]

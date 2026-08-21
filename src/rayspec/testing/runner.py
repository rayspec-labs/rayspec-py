# SPDX-License-Identifier: Apache-2.0
"""Execute one :class:`~rayspec.testing.spec.Case` and check its expectations.

Module boundary: the engine-level executor behind ``rayspec test``. It loads and validates the
workflow, then drives :class:`~rayspec.engine.runner.Runner` **in process** as a dry run with the
stub provider and a :class:`~rayspec.events.sinks.CollectingSink` — no subprocess, no
credentials, no network, no worktree. Nothing here prints; :mod:`rayspec.testing.report` owns the
output shape.

``exec_shell`` is the caller's authorisation, never the data's: a case file's ``exec_shell: true``
is a *declaration* that this case wants real ``shell:``/``python:`` execution and is checked by
``rayspec test`` against ``--exec-shell``; :func:`run_case` itself executes shell steps only when
its own ``exec_shell=`` argument says so. A checked-in YAML file must not be able to widen what
the command does.

Two rules keep cases hermetic and are the reason the environment is patched around a run:

* ``RAYSPEC_INPUT_*`` variables of the developer's shell are removed — a case must resolve the
  same inputs on every machine;
* a case's ``env:`` mapping is applied to the process environment (``null`` unsets), because
  ``env.<VAR>`` in a template and a ``shell:`` step's environment must agree.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
import traceback
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from rayspec.config import load_config
from rayspec.engine.context import RunOptions
from rayspec.engine.errors import EngineError
from rayspec.engine.runner import Runner, RunResult, Workspace, fallback_project_slug
from rayspec.errors import InputError, RayspecError
from rayspec.events.sinks import CollectingSink
from rayspec.loader import load_workflow, resolve_inputs, validate_workflow
from rayspec.providers.base import ProviderCapabilities
from rayspec.store.file import FileRunStore, UnknownRunIdError, open_private
from rayspec.store.model import StepRecord
from rayspec.templating import TemplateEngine
from rayspec.testing.report import CaseResult
from rayspec.testing.spec import (
    UNREACHABLE_EXPECT_HINT,
    Case,
    CaseLocation,
    StepExpect,
    Suite,
    unreachable_expect,
)

#: Exit code the CLI (and therefore a ``validate: error`` case) uses for a refused workflow.
EXIT_USAGE = 2


@contextlib.contextmanager
def case_environment(env: Mapping[str, str | None], *, home: Path) -> Iterator[None]:
    """Run the body under the case's environment; restore ``os.environ`` afterwards.

    ``os.environ`` is process-wide, so this — and therefore :func:`run_case` — is **not
    thread-safe**: two cases running concurrently in one process would corrupt each other's
    environment. Drive cases sequentially (what ``rayspec test`` does) or in separate processes
    (``pytest -p xdist`` forks, which is fine).
    """
    saved = dict(os.environ)
    try:
        for name in [n for n in os.environ if n.startswith("RAYSPEC_INPUT_")]:
            del os.environ[name]
        os.environ["RAYSPEC_HOME"] = str(home)
        os.environ.setdefault("NO_COLOR", "1")
        for name, value in env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def capability_lookup() -> tuple[Any, list[str]]:
    """``(capabilities_for, provider_ids)`` from the provider registry (never raises)."""
    from rayspec.providers.registry import get_registration, list_registrations

    table: dict[str, ProviderCapabilities] = {
        reg.id: reg.capabilities for reg in list_registrations()
    }

    def lookup(provider_id: str) -> ProviderCapabilities | None:
        if provider_id not in table:
            try:
                table[provider_id] = get_registration(provider_id).capabilities
            except (LookupError, RayspecError):
                return None
        return table[provider_id]

    return lookup, sorted(table)


def run_case(
    suite: Suite,
    case: Case,
    *,
    home: Path,
    exec_shell: bool = False,
    keep_run_dir: bool = True,
) -> CaseResult:
    """Load, validate and dry-run ``case`` against ``suite.root``; never raises.

    ``home`` is a rayspec home (the run store lives under ``<home>/projects/<slug>``, so
    ``rayspec logs <run_id>`` explains a failure); ``exec_shell`` makes ``shell:``/``python:``
    steps execute instead of being simulated. The case's own ``exec_shell:`` key is deliberately
    *not* consulted here — see the module docstring.

    Every expectation is checked, not just the first — a case reports all of its problems at once.
    With ``keep_run_dir=False`` a *passing* case deletes the run it created through the store
    (what ``rayspec test`` does so a suite does not bury the project's real runs); a failing one
    always keeps it.

    Never raises: a workflow that fails to load, a broken stub file and a bug anywhere in the
    harness all become a :class:`~rayspec.testing.report.Failure` on the result.
    """
    result = CaseResult(suite.name, case.id)
    location = suite.location(case.id)
    started = time.monotonic()
    try:
        with case_environment(case.env, home=home):
            _execute(suite, case, result, location, home=home, exec_shell=exec_shell)
        if not keep_run_dir and result.ok:
            _delete_run(suite, result, home=home)
    except Exception as exc:
        result.fail(
            "internal",
            f"the case crashed: {exc!r}",
            detail=traceback.format_exc().strip().replace("\n", "\n    "),
            fix="this is a rayspec bug — please report it with the traceback above",
            location=location.of(),
        )
    finally:
        result.duration_s = time.monotonic() - started
    return result


def _delete_run(suite: Suite, result: CaseResult, *, home: Path) -> None:
    """Drop a passing case's run through the store — it owns the on-disk layout of a run."""
    if result.run_id is None:
        return
    store = FileRunStore(home / "projects" / fallback_project_slug(suite.root))
    try:
        store.delete_run(result.run_id)
    except (UnknownRunIdError, OSError):
        return  # a run we cannot delete is kept, not hidden
    result.run_dir = None


def _execute(
    suite: Suite,
    case: Case,
    result: CaseResult,
    location: CaseLocation,
    *,
    home: Path,
    exec_shell: bool,
) -> None:
    """The body of :func:`run_case`, inside the patched environment."""
    reason = unreachable_expect(case)
    if reason is not None:
        # `load_cases` refuses this at load time; a Case built in Python reaches us anyway, and a
        # case whose every assertion is unreachable must never report ok
        result.fail(
            "expect",
            f"`expect:` is never evaluated for a case with {reason}",
            detail="the case would report ok whatever it claims",
            fix=UNREACHABLE_EXPECT_HINT,
            location=location.of("expect"),
        )
        return
    config = load_config(suite.root, home=home)
    capabilities_for, provider_ids = capability_lookup()
    try:
        rw = load_workflow(case.workflow, project_root=suite.root, home=home, config=config)
    except RayspecError as exc:
        if case.validate_ == "error":
            return  # a workflow that must not load is a satisfied `validate: error`
        result.fail(
            "workflow",
            f"workflow {case.workflow!r} failed to load",
            detail=str(exc).replace("\n", "\n    "),
            fix=exc.hint or "fix the workflow, or set `validate: error` if this is the point",
            location=location.of("workflow"),
        )
        return
    report = validate_workflow(
        rw,
        capabilities_for=capabilities_for,
        template_checker=TemplateEngine(),
        on_unsupported="warn" if case.allow_unsupported else "error",
        provider_ids=provider_ids,
    )
    if case.validate_ == "error":
        if not report.errors:
            result.fail(
                "validate",
                f"workflow {case.workflow!r} validates cleanly, expected errors",
                detail=f"`rayspec validate {case.workflow}` would exit 0, not {EXIT_USAGE}",
                fix="drop `validate: error`, or make the workflow use the unsupported feature",
                location=location.of("validate"),
            )
        return
    if report.errors:
        result.fail(
            "validate",
            f"workflow {case.workflow!r} does not validate",
            detail="; ".join(report.errors[:3]).replace("\n", "\n    "),
            fix="fix the workflow, add `allow_unsupported: true`, or set `validate: error`",
            location=location.of("workflow"),
        )
        return
    if not case.run:
        return
    values = _inputs(case, rw, result, location)
    if values is None:
        return
    stub_script = _stub_script(case, result, location)
    if stub_script is False:
        return
    run_result = _run(
        suite,
        case,
        result,
        location,
        rw=rw,
        home=home,
        config=config,
        values=values,
        stub_script=stub_script,
        exec_shell=exec_shell,
    )
    if run_result is None:
        return
    store = FileRunStore(home / "projects" / fallback_project_slug(suite.root))
    _check(case, result, location, run_result, store=store)


def _inputs(
    case: Case, rw: Any, result: CaseResult, location: CaseLocation
) -> dict[str, Any] | None:
    """Resolve the case inputs the way ``rayspec run --inputs-file`` does."""
    if not case.inputs:
        try:
            return resolve_inputs(rw.workflow, cli_pairs=[])
        except InputError as exc:
            result.fail(
                "inputs",
                "the workflow inputs cannot be resolved",
                detail="; ".join(exc.errors),
                fix="add the missing values under `inputs:` in the case",
                location=location.of("inputs"),
            )
            return None
    with tempfile.TemporaryDirectory(prefix="rayspec-case-") as tmp:
        path = Path(tmp) / "inputs.json"
        # a case's `inputs:` may carry a value for a `secret: true` input (never do that in a
        # committed file, but the harness must not be the one that widens the mode)
        with open_private(path) as handle:
            handle.write(json.dumps(dict(case.inputs), default=str))
        try:
            return resolve_inputs(rw.workflow, inputs_file=path)
        except InputError as exc:
            result.fail(
                "inputs",
                "the case inputs are rejected by the workflow",
                detail="; ".join(exc.errors),
                fix="fix the values under `inputs:` (or the workflow's `inputs:` declarations)",
                location=location.of("inputs"),
            )
            return None


def _stub_script(case: Case, result: CaseResult, location: CaseLocation) -> Any:
    """The parsed ``StubScript``, ``None`` when the case has none, ``False`` when it is broken."""
    if case.stubs is None:
        return None
    from rayspec.providers.stub import StubScript

    try:
        return StubScript.from_file(case.stubs)
    except OSError as exc:
        result.fail(
            "stubs",
            f"stubs file not readable: {case.stubs.name} ({exc.strerror or exc})",
            detail=f"looked for {case.stubs}",
            fix="point `stubs:` at a file next to the case file, or drop the key",
            location=location.of("stubs"),
        )
        return False
    except RayspecError as exc:
        result.fail(
            "stubs",
            f"stub script {case.stubs.name} is malformed",
            detail=str(exc),
            fix=exc.hint or "see `rayspec run --stubs-init` for a scaffold",
            location=location.of("stubs"),
        )
        return False


def _run(
    suite: Suite,
    case: Case,
    result: CaseResult,
    location: CaseLocation,
    *,
    rw: Any,
    home: Path,
    config: Any,
    values: Mapping[str, Any],
    stub_script: Any,
    exec_shell: bool,
) -> RunResult | None:
    """Drive the engine once; an engine error becomes a failure, never an exception."""
    store = FileRunStore(home / "projects" / fallback_project_slug(suite.root))
    sink = CollectingSink()
    runner = Runner(
        rw,
        inputs=values,
        store=store,
        project_root=suite.root,
        project_slug=fallback_project_slug(suite.root),
        sinks=sink,
        workspace=Workspace.in_place(suite.root),
        options=RunOptions(
            dry_run=True,
            exec_shell=exec_shell,
            interactive=False,
            stub_script=stub_script or None,
            provider_settings=config.providers,
        ),
        handle_signals=False,
    )
    try:
        run_result = runner.run_sync()
    except (EngineError, RayspecError) as exc:
        result.fail(
            "run",
            f"the run could not start: {exc}",
            detail=f"workflow {case.workflow!r} in {suite.root}",
            fix=getattr(exc, "hint", None) or "fix the workflow or the case",
            location=location.of(),
        )
        return None
    result.events = list(sink.events)
    result.run_id = run_result.run_id
    result.run_dir = run_result.run_dir
    result.status = run_result.status.value
    return run_result


# --------------------------------------------------------------------------------------------------
# Expectations
# --------------------------------------------------------------------------------------------------


def _check(
    case: Case,
    result: CaseResult,
    location: CaseLocation,
    run: RunResult,
    *,
    store: FileRunStore,
) -> None:
    """Compare the run against ``case.expect`` — every expectation, not just the first."""
    expect = case.expect
    logs = f"rayspec logs {run.run_id}"
    if expect.status is not None and run.status.value != expect.status:
        result.fail(
            "expect.status",
            f"run status is {run.status.value!r}, expected {expect.status!r}",
            detail=f"reason: {run.reason or '(none)'}",
            fix=f"update expect.status, or fix the workflow/stubs ({logs})",
            location=location.of("expect", "status"),
        )
    if expect.exit_code is not None and run.exit_code != expect.exit_code:
        result.fail(
            "expect.exit_code",
            f"run exited {run.exit_code}, expected {expect.exit_code}",
            detail=f"status {run.status.value!r} maps to exit {run.exit_code}",
            fix="update expect.exit_code (see the exit-code table in docs/cli.md)",
            location=location.of("expect", "exit_code"),
        )
    outputs = run.outputs or {}
    for key, value in expect.outputs.items():
        if key not in outputs:
            result.fail(
                f"expect.outputs.{key}",
                f"the run has no output {key!r}",
                detail=f"rendered outputs: {', '.join(sorted(outputs)) or '(none)'}",
                fix=f"rename the expectation, or add {key!r} to the workflow's `outputs:`",
                location=location.of("expect", "outputs", key),
            )
        elif outputs[key] != value:
            result.fail(
                f"expect.outputs.{key}",
                f"output {key!r} is {_short(outputs[key])}, expected {_short(value)}",
                detail=f"reason: {run.reason or '(none)'}",
                fix=f"update the expectation, or fix the template/stubs ({logs})",
                location=location.of("expect", "outputs", key),
            )
    if expect.reason_contains is not None and expect.reason_contains not in (run.reason or ""):
        result.fail(
            "expect.reason_contains",
            f"the run reason does not contain {expect.reason_contains!r}",
            detail=f"reason: {run.reason or '(none)'}",
            fix="update expect.reason_contains, or fix the stop/cancel path",
            location=location.of("expect", "reason_contains"),
        )
    for path, step_expect in expect.steps.items():
        _check_step(path, step_expect, result, location, run, store=store, logs=logs)


def _check_step(
    path: str,
    expect: StepExpect,
    result: CaseResult,
    location: CaseLocation,
    run: RunResult,
    *,
    store: FileRunStore,
    logs: str,
) -> None:
    """One entry of ``expect.steps``."""
    where = ("expect", "steps", path)
    record = run.steps.get(path)
    if record is None:
        result.fail(
            f"expect.steps.{path}",
            f"step {path!r} never finished",
            detail=f"recorded steps: {', '.join(sorted(run.steps)) or '(none)'}",
            fix="use the RECORD path (loop/each bodies are indexed: `build[1]/review`)",
            location=location.of(*where),
        )
        return
    if expect.status is not None and record.status.value != expect.status:
        result.fail(
            f"expect.steps.{path}.status",
            f"step {path!r} is {record.status.value!r}, expected {expect.status!r}",
            detail=_step_detail(record),
            fix=f"update the expectation, or fix the workflow/stubs ({logs} --step {path})",
            location=location.of(*where, "status"),
        )
    if expect.skip_reason is not None and (record.skip_reason or "") != expect.skip_reason:
        result.fail(
            f"expect.steps.{path}.skip_reason",
            f"step {path!r} skip_reason is {record.skip_reason!r}, expected {expect.skip_reason!r}",
            detail=_step_detail(record),
            fix="update the expectation (the engine words the reason, e.g. `when: false`)",
            location=location.of(*where, "skip_reason"),
        )
    if expect.output_regex is None and "output_json" not in expect.model_fields_set:
        return
    text = _output_text(store, run.run_id, record)
    if text is None:
        result.fail(
            f"expect.steps.{path}.output",
            f"step {path!r} wrote no output file",
            detail=_step_detail(record),
            fix="only a step that finished has an output; check its status first",
            location=location.of(*where),
        )
        return
    if expect.output_regex is not None and re.search(expect.output_regex, text) is None:
        result.fail(
            f"expect.steps.{path}.output_regex",
            f"the output of {path!r} does not match /{expect.output_regex}/",
            detail=f"output: {_short(text)}",
            fix=f"update the pattern, or fix the stubbed answer ({logs} --step {path})",
            location=location.of(*where, "output_regex"),
        )
    if "output_json" in expect.model_fields_set:
        try:
            actual = json.loads(text)
        except json.JSONDecodeError as exc:
            result.fail(
                f"expect.steps.{path}.output_json",
                f"the output of {path!r} is not JSON ({exc.msg})",
                detail=f"output: {_short(text)}",
                fix="use output_regex for text output, or give the step an `output_schema:`",
                location=location.of(*where, "output_json"),
            )
            return
        if actual != expect.output_json:
            result.fail(
                f"expect.steps.{path}.output_json",
                f"the output of {path!r} is {_short(actual)}, expected "
                f"{_short(expect.output_json)}",
                detail=f"output: {_short(text)}",
                fix=f"update the expectation, or fix the stubbed answer ({logs} --step {path})",
                location=location.of(*where, "output_json"),
            )


def _output_text(store: FileRunStore, run_id: str, record: StepRecord) -> str | None:
    """The step's persisted output, or ``None`` when it has none / cannot be read."""
    if record.output_ref is None:
        return None
    try:
        return store.read_output(run_id, record.output_ref)
    except (OSError, RayspecError):
        return None


def _step_detail(record: StepRecord) -> str:
    """``status … · attempts 2 · error: …`` — the context a step failure needs."""
    parts = [f"status {record.status.value}", f"attempts {record.attempts}"]
    if record.skip_reason:
        parts.append(f"skip_reason: {record.skip_reason}")
    if record.error is not None:
        parts.append(f"error: {record.error.message}")
    if record.tolerated:
        parts.append("tolerated")
    return " · ".join(parts)


def _short(value: Any, limit: int = 120) -> str:
    """A one-line, quoted rendering of a value for a failure line."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return repr(text) if isinstance(value, str) else text


__all__ = ["EXIT_USAGE", "capability_lookup", "case_environment", "run_case"]

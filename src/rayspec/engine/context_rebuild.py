# SPDX-License-Identifier: Apache-2.0
"""Rebuild the template context of a step — after the run, or before it.

Module boundary: read-only reconstruction of the lexical
:class:`~rayspec.templating.Scope` chain the engine builds while running. Given a
:class:`~rayspec.loader.ResolvedWorkflow` and a *source of step views*, it walks the definition
tree down to one step path and returns the mapping ``TemplateEngine.render_*``/``eval_*`` would
have been handed there (``inputs``, ``steps``, ``run``, ``project``, ``env`` plus ``iteration`` /
``each`` / the ``as:`` item of every enclosing body).

Two sources ship here:

* :func:`from_run` — the views come from a stored :class:`~rayspec.store.model.RunRecord` and its
  output files (``rayspec explain``, ``rayspec eval``);
* :func:`from_plan` — the views come from a ``--stubs`` script, with a visible placeholder for
  every step the script does not mention (``rayspec plan --render``).

Nothing here executes a step, creates a provider or writes a file: it is the read-only half of
the engine. ``each`` items and an ``include:``'s ``with:`` bindings are *re-evaluated* in the
parent scope (the engine does not persist them); when that fails the rebuild degrades to a
placeholder and records a warning instead of raising.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from rayspec.engine.context import body_ids, sha256_json
from rayspec.engine.paths import StepPath
from rayspec.errors import RayspecError
from rayspec.loader import IncludedBody, ResolvedWorkflow
from rayspec.loader.inputs import SECRET_PLACEHOLDER
from rayspec.schema import EachStep, IncludeStep, LoopStep, StepModel, StepStatus
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord, StepRecord
from rayspec.templating import (
    ReferenceKind,
    RenderedScript,
    Scope,
    StepView,
    TemplateEngine,
    build_context,
    stringify_text,
)

#: Where a step's view comes from: ``(record path, step) -> view | None`` (``None`` = the step is
#: not visible in the rebuilt scope, e.g. it has no record).
ViewSource = Callable[[str, StepModel], "StepView | None"]
#: The record of a step path, when there is one (``rayspec explain``'s "why did this happen").
RecordSource = Callable[[str], "StepRecord | None"]

#: Kinds whose ``stderr.log`` is worth reading back into ``steps.<id>.stderr``.
_STDERR_KINDS = frozenset({"shell", "python"})


class ContextRebuildError(RayspecError):
    """The requested step path does not exist in this workflow (or is not addressable)."""


@dataclass(frozen=True, slots=True)
class RebuiltContext:
    """One step's rebuilt template context (``step``/``record`` are ``None`` at the run root)."""

    record_path: StepPath
    def_path: str
    step: StepModel | None
    record: StepRecord | None
    scope: Scope
    context: dict[str, Any]
    inputs: Mapping[str, Any]
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class _Frame:
    """One level of the scope chain: the sibling list visible there and its inputs."""

    prefix: StepPath
    def_prefix: str
    steps: tuple[StepModel, ...]
    scope: Scope
    inputs: Mapping[str, Any]


class ContextRebuilder:
    """Rebuilds the template context of any step of one workflow (see the module docstring)."""

    def __init__(
        self,
        resolved: ResolvedWorkflow,
        *,
        view_for: ViewSource,
        inputs: Mapping[str, Any],
        run: Mapping[str, Any],
        project: Mapping[str, Any],
        record_for: RecordSource | None = None,
        env: Mapping[str, str] | None = None,
        engine: TemplateEngine | None = None,
    ) -> None:
        self.resolved = resolved
        self.view_for = view_for
        self.record_for = record_for or (lambda _path: None)
        self.inputs = dict(inputs)
        self.run = dict(run)
        self.project = dict(project)
        self.env = dict(os.environ if env is None else env)
        self.engine = engine or TemplateEngine()

    # -- public ---------------------------------------------------------------------------

    def at(self, path: str | None = None) -> RebuiltContext:
        """The context of ``path`` (a record path such as ``build[2]/implement``); the run's
        root scope when ``path`` is empty/``None``.

        ``path`` may also be a *definition* path (``build/implement``): the missing indices read
        as iteration 1 / item 0 and :attr:`RebuiltContext.record_path` reports the record path
        that was actually bound (``build[1]/implement``), so a caller can hand it to
        ``rayspec explain`` and look up the right record.

        Raises :class:`ContextRebuildError` for a path this workflow does not define, one that
        descends into a step without a body, or an ``[index]`` on a step that has no iterations.
        """
        target = self._parse(path)
        warnings: list[str] = []
        frame = self._root_frame()
        step: StepModel | None = None
        def_path = ""
        record_path = StepPath.root()
        for i, (name, index) in enumerate(target.segments):
            def_path = f"{frame.def_prefix}{name}"
            step = self._step_in(frame, name, def_path)
            record_path = frame.prefix.child(name)
            if index is not None:
                self._check_indexable(step, def_path)
                record_path = record_path.indexed(index)
            if i == len(target.segments) - 1:
                break
            frame = self._descend(frame, step, index, def_path, warnings)
            record_path = frame.prefix
        return RebuiltContext(
            record_path=record_path,
            def_path=def_path,
            step=step,
            record=self.record_for(str(record_path)) if not record_path.is_root else None,
            scope=frame.scope,
            context=self._context_of(frame),
            inputs=frame.inputs,
            warnings=tuple(warnings),
        )

    # -- internals ------------------------------------------------------------------------

    def _parse(self, path: str | None) -> StepPath:
        try:
            return StepPath.parse(path or "")
        except ValueError as exc:
            raise ContextRebuildError(
                str(exc), hint="use a step path such as build[2]/implement"
            ) from exc

    def _context_of(self, frame: _Frame) -> dict[str, Any]:
        return build_context(
            frame.scope,
            inputs=frame.inputs,
            run=self.run,
            project=self.project,
            env=self.env,
        )

    def _root_frame(self) -> _Frame:
        steps = tuple(self.resolved.workflow.steps)
        prefix = StepPath.root()
        return _Frame(
            prefix=prefix,
            def_prefix="",
            steps=steps,
            scope=Scope(None, self._views(prefix, steps)),
            inputs=self.inputs,
        )

    def _views(self, prefix: StepPath, steps: Sequence[StepModel]) -> dict[str, StepView]:
        """The visible views of one sibling list (composites carry their ``body_ids`` so the
        "step 'x' is inside loop 'y'" hint still works)."""
        views: dict[str, StepView] = {}
        for step in steps:
            view = self.view_for(str(prefix.child(step.id)), step)
            if view is None:
                continue
            ids = body_ids(step)
            views[step.id] = replace(view, body_ids=ids) if ids else view
        return views

    def _check_indexable(self, step: StepModel, def_path: str) -> None:
        """An ``[index]`` is only meaningful on a step that has iterations or items.

        Discarding it silently would answer confidently about a *different* step than the one
        the user asked for (``a[999]`` on a plain ``shell:`` step).
        """
        if isinstance(step, LoopStep | EachStep):
            return
        raise ContextRebuildError(
            f"step {def_path!r} is a {type(step).kind} step and has no [index]",
            hint="only loop: iterations and each: items are indexed",
        )

    def _step_in(self, frame: _Frame, name: str, def_path: str) -> StepModel:
        for step in frame.steps:
            if step.id == name:
                return step
        known = ", ".join(step.id for step in frame.steps) or "(none)"
        raise ContextRebuildError(
            f"no step {def_path!r} in workflow {self.resolved.workflow.name!r}",
            hint=f"steps at this level: {known}",
        )

    def _descend(
        self,
        frame: _Frame,
        step: StepModel,
        index: int | None,
        def_path: str,
        warnings: list[str],
    ) -> _Frame:
        """The child frame of a composite step (loop iteration / each item / include body)."""
        if isinstance(step, LoopStep):
            n = 1 if index is None else index
            prefix = frame.prefix.child(step.id).indexed(n)
            body = tuple(step.loop.steps)
            iteration = {
                "n": n,
                "max": step.loop.max_iterations,
                "first": n == 1,
                "prev": self._prev_views(frame, step, n),
            }
            return self._child(frame, prefix, def_path, body, {"iteration": iteration})
        if isinstance(step, EachStep):
            i = 0 if index is None else index
            prefix = frame.prefix.child(step.id).indexed(i)
            items = self._each_items(frame, step, def_path, warnings)
            item = self._item_at(
                items, i, def_path, prefix=prefix, body=tuple(step.steps), warnings=warnings
            )
            total = len(items) if items is not None else self._recorded_total(frame, step)
            variables: dict[str, Any] = {"each": {"index": i, "total": total}, step.as_: item}
            return self._child(frame, prefix, def_path, tuple(step.steps), variables)
        if isinstance(step, IncludeStep):
            body_def = self.resolved.includes.get(def_path)
            if body_def is None:
                raise ContextRebuildError(f"include body of {def_path!r} is not loaded")
            prefix = frame.prefix.child(step.id)
            inputs = self._include_inputs(frame, step, body_def, def_path, warnings)
            return self._child(
                frame, prefix, def_path, tuple(body_def.steps), {}, inputs=inputs, lexical=True
            )
        raise ContextRebuildError(
            f"step {def_path!r} is a {type(step).kind} step and has no body",
            hint="only loop:, each: and include: steps contain steps",
        )

    def _child(
        self,
        frame: _Frame,
        prefix: StepPath,
        def_path: str,
        steps: tuple[StepModel, ...],
        variables: Mapping[str, Any],
        *,
        inputs: Mapping[str, Any] | None = None,
        lexical: bool = False,
    ) -> _Frame:
        views = self._views(prefix, steps)
        scope = Scope(None, views, variables) if lexical else frame.scope.child(views, variables)
        return _Frame(
            prefix=prefix,
            def_prefix=f"{def_path}/",
            steps=steps,
            scope=scope,
            inputs=frame.inputs if inputs is None else inputs,
        )

    def _prev_views(self, frame: _Frame, step: LoopStep, n: int) -> dict[str, StepView] | None:
        """``iteration.prev`` — the previous iteration's body views (``None`` on iteration 1)."""
        if n <= 1:
            return None
        return self._views(frame.prefix.child(step.id).indexed(n - 1), tuple(step.loop.steps))

    def _each_items(
        self, frame: _Frame, step: EachStep, def_path: str, warnings: list[str]
    ) -> list[Any] | None:
        """Re-evaluate the ``each:`` expression in the parent scope (it is never persisted)."""
        try:
            raw = self.engine.eval_expr(step.each, self._context_of(frame))
        except RayspecError as exc:
            warnings.append(f"{def_path}: each: {step.each!r} could not be evaluated: {exc}")
            return None
        if isinstance(raw, list | tuple):
            return list(raw)
        warnings.append(f"{def_path}: each: {step.each!r} did not evaluate to a list")
        return None

    def _item_at(
        self,
        items: list[Any] | None,
        index: int,
        def_path: str,
        *,
        prefix: StepPath,
        body: tuple[StepModel, ...],
        warnings: list[str],
    ) -> Any:
        """The ``as:`` value of item ``index``, checked against what the run actually ran."""
        if items is None or index >= len(items):
            return f"<item {index} of each {def_path}>"
        item = items[index]
        recorded = self._recorded_item_sha(prefix, body)
        if recorded is not None and recorded != sha256_json(item):
            warnings.append(
                f"{def_path}: item {index} differs from the one the run used "
                "(the each: expression depends on something that changed since)"
            )
        return item

    def _recorded_item_sha(self, prefix: StepPath, body: tuple[StepModel, ...]) -> str | None:
        """``item_sha256`` stamped on any recorded step of this item's graph."""
        for step in body:
            record = self.record_for(str(prefix.child(step.id)))
            if record is not None and record.item_sha256:
                return record.item_sha256
        return None

    def _recorded_total(self, frame: _Frame, step: EachStep) -> int:
        record = self.record_for(str(frame.prefix.child(step.id)))
        return record.each.total if record is not None and record.each is not None else 0

    def _include_inputs(
        self,
        frame: _Frame,
        step: IncludeStep,
        body: IncludedBody,
        def_path: str,
        warnings: list[str],
    ) -> dict[str, Any]:
        """The included workflow's own ``inputs`` — its ``with:`` bindings, re-rendered."""
        from rayspec.engine.executors.include import resolve_include_inputs

        try:
            values = self.engine.render_value(dict(step.with_), self._context_of(frame))
            return resolve_include_inputs(body.inputs, dict(values))
        except Exception as exc:  # a broken binding must not sink the whole rebuild
            warnings.append(f"{def_path}: with: could not be re-rendered ({exc}); using defaults")
            return {name: spec.default for name, spec in body.inputs.items() if spec.has_default}


# --------------------------------------------------------------------------------------------------
# rendering a step's body in a rebuilt context
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderedBody:
    """A leaf step's body rendered in a rebuilt context.

    ``kind`` is ``prompt`` (text environment), ``shell`` or ``python`` (the code environments,
    where ``env`` holds the ``RAYSPEC_V<n>`` slots the ``${...}`` references stand for).
    ``text`` is ``None`` when rendering failed — ``error`` says why.
    """

    kind: str
    text: str | None
    env: dict[str, str]
    error: str | None = None


def oversize_placeholder(size: int) -> str:
    """What a value too large to inline (>64 KiB) reads as in a preview.

    A *run* spills such a value to a file under the run's ``tmp/`` and reads it back at
    execution time; a preview has no run dir and would throw the whole script away over one
    slot. The placeholder keeps the script readable and names where the value can be read.
    """
    return (
        f"<{size} bytes — too large to inline here; "
        "read it in the producing step's output file under the run dir>"
    )


def render_script(
    engine: TemplateEngine, body: str, context: Mapping[str, Any], *, kind: str
) -> RenderedScript:
    """Render a ``shell:``/``python:`` body for *display* (raises like ``render_shell``).

    Values over the 64 KiB spill threshold are rendered into a throwaway scratch directory and
    then replaced by :func:`oversize_placeholder`, so the script itself is always shown and no
    preview leaves a temporary file behind (the run's own spill files are untouched).
    """
    render = engine.render_shell if kind == "shell" else engine.render_python
    with tempfile.TemporaryDirectory(prefix="rayspec-preview-") as tmp:
        rendered = render(body, context, spill_dir=Path(tmp))
        script = rendered.script
        for path in rendered.spills:
            script = script.replace(str(path), oversize_placeholder(path.stat().st_size))
    return RenderedScript(script=script, env=dict(rendered.env), spills=[])


def render_body(
    engine: TemplateEngine, body: str, context: Mapping[str, Any], *, kind: str
) -> RenderedBody:
    """Render one step body in ``context`` — the preview `explain` and `plan --render` show.

    Never raises: a template that cannot be rendered here (a missing value, a body that no
    longer compiles) becomes a :class:`RenderedBody` with ``error`` set, because a preview of
    ten steps must not die on the one that is broken.
    """
    try:
        if kind in {"shell", "python"}:
            script = render_script(engine, body, context, kind=kind)
            return RenderedBody(kind, script.script, dict(script.env))
        return RenderedBody(kind, engine.render_str(body, context), {})
    except RayspecError as exc:
        return RenderedBody(kind, None, {}, error=str(exc))


#: Warning for a rebuild that re-evaluates a template reading the ``env.*`` root.
ENV_IS_LOCAL_WARNING = (
    "env.* comes from this shell, not from the run — nothing records the environment a run had"
)


def env_reference_warning(
    engine: TemplateEngine, texts: Iterable[tuple[str, ReferenceKind]]
) -> str | None:
    """:data:`ENV_IS_LOCAL_WARNING` when any ``(text, kind)`` pair reads the ``env`` root.

    The stale-workflow warning exists because a re-evaluated template can differ from what ran;
    the process environment is the other such input, and it is never recorded — so say so, but
    only when the step actually reads it.
    """
    for text, kind in texts:
        try:
            refs = engine.references(text, kind=kind)
        except RayspecError:  # a body that no longer compiles is reported elsewhere
            continue
        if any(ref.root == "env" for ref in refs):
            return ENV_IS_LOCAL_WARNING
    return None


def render_step_env(
    engine: TemplateEngine, step: StepModel, context: Mapping[str, Any]
) -> dict[str, str]:
    """A leaf step's own ``env:`` mapping, deep-rendered and str-coerced for display.

    Mirrors ``RunContext.render_env`` — minus the secrets: a rebuilt context holds the
    ``"<secret>"`` placeholder, never the real value. A value that cannot be rendered
    becomes ``<error: …>`` rather than sinking the whole preview.
    """
    env = getattr(step, "env", None)
    if not env:
        return {}
    out: dict[str, str] = {}
    for key, raw in env.items():
        try:
            out[str(key)] = stringify_text(engine.render_value(raw, context))
        except RayspecError as exc:
            out[str(key)] = f"<error: {exc}>"
    return out


# --------------------------------------------------------------------------------------------------
# source: a stored run
# --------------------------------------------------------------------------------------------------


def run_vars(run: RunRecord, run_dir: Path) -> dict[str, Any]:
    """The ``run.*`` context root of a stored run (mirrors ``RunContext.run_vars``)."""
    ws = run.workspace
    return {
        "id": run.run_id,
        "workflow": run.workflow_name,
        "workdir": ws.workdir or run.project_root,
        "artifacts_dir": str(run_dir / "artifacts"),
        "state_dir": str(run_dir),
        "branch": ws.branch,
        "base_branch": ws.base_branch,
        "started_at": run.started_at.isoformat() if run.started_at else None,
    }


def read_ref(store: FileRunStore, run_id: str, ref: str | None) -> str | None:
    """Read a run-dir-relative file of a run (``None`` when absent/unreadable)."""
    if not ref:
        return None
    try:
        return store.read_output(run_id, ref)
    except (OSError, ValueError):
        return None


def view_of_record(
    record: StepRecord, step: StepModel | None, *, store: FileRunStore, run_id: str
) -> StepView:
    """The :class:`StepView` templates saw for a finished step, rebuilt from its record.

    ``output`` is read back from the output file (parsed for a ``json`` output), ``stderr`` from
    ``stderr.log`` of a shell/python step. ``items`` (an ``each:`` step's per-item detail) is not
    persisted and stays ``None`` — it resolves to an undefined-with-hint, never to a wrong value.
    """
    text = read_ref(store, run_id, record.output_ref)
    output: Any = text
    if text is not None and record.output_kind == "json":
        try:
            output = json.loads(text)
        except ValueError:
            output = text
    stderr = None
    if record.kind in _STDERR_KINDS:
        stderr = read_ref(
            store, run_id, f"steps/{StepPath.parse(record.path).fs_path()}/stderr.log"
        )
    return StepView(
        id=record.id,
        kind=record.kind,
        status=record.status,
        output=output,
        ok=record.ok,
        exit_code=record.exit_code,
        stderr=stderr,
        duration_s=(record.duration_ms / 1000.0) if record.duration_ms is not None else None,
        cost_usd=record.cost_usd,
        usage=record.usage if (record.usage.total or record.kind == "prompt") else None,
        session=record.session_ref.id if record.session_ref is not None else None,
        model=record.model,
        approved=record.approved,
        iterations=record.loop.iterations if record.loop is not None else None,
        converged=record.loop.converged if record.loop is not None else None,
        items=None,
        skip_reason=record.skip_reason,
        error=record.error.message if record.error is not None else None,
        tolerated=record.tolerated,
        body_ids=body_ids(step) if step is not None else frozenset(),
    )


def stale_workflow_warning(run: RunRecord, resolved: ResolvedWorkflow) -> str | None:
    """A warning when the workflow file changed since ``run`` finished.

    Everything a rebuild *re-evaluates* — a ``when:``, an ``each:`` item, a body that was not
    persisted — is read from the file as it is **now**, so a changed file can explain the run
    with a template it never saw. The stored records themselves are unaffected.
    """
    if run.workflow_hash == resolved.hash:
        return None
    return (
        f"workflow {resolved.workflow.name!r} changed since this run "
        f"(hash {run.workflow_hash[:12]} → {resolved.hash[:12]}); anything re-evaluated below "
        "comes from the file as it is now, not from what ran"
    )


def from_run(
    run: RunRecord,
    resolved: ResolvedWorkflow,
    *,
    store: FileRunStore,
    engine: TemplateEngine | None = None,
    env: Mapping[str, str] | None = None,
) -> ContextRebuilder:
    """A rebuilder over a stored run: views come from its records and output files.

    ``inputs`` are the recorded ones (a ``secret: true`` input is stored — and therefore shown —
    as ``"<secret>"``; real secret values are never persisted and never rebuilt). ``env`` is this
    process's environment, not the run's: nothing records it.
    """

    def view_for(record_path: str, step: StepModel) -> StepView | None:
        record = run.steps.get(record_path)
        if record is None:
            return None
        return view_of_record(record, step, store=store, run_id=run.run_id)

    return ContextRebuilder(
        resolved,
        view_for=view_for,
        record_for=run.steps.get,
        inputs=run.inputs,
        run=run_vars(run, Path(store.run_dir(run.run_id))),
        project={
            "root": run.project_root,
            "name": Path(run.project_root).name,
            "slug": run.project_slug,
        },
        env=env,
        engine=engine,
    )


# --------------------------------------------------------------------------------------------------
# source: a plan (stub script + placeholders)
# --------------------------------------------------------------------------------------------------


class StubOutcomeLike(Protocol):
    """The part of a stub outcome a preview reads (``providers.stub.StubOutcome``)."""

    @property
    def has_output(self) -> bool: ...
    @property
    def output(self) -> Any: ...
    @property
    def text(self) -> str | None: ...


class StubEntryLike(Protocol):
    """The part of a stub entry a preview reads (``providers.stub.StubEntry``)."""

    def outcome_for(self, n: int) -> StubOutcomeLike: ...


class StubScriptLike(Protocol):
    """The part of ``providers.stub.StubScript`` this module consumes.

    A structural type rather than an import: ``engine`` never imports a concrete provider (see
    CONTRACTS "Dependency rules"), and stating the surface here means pyright fails the build if
    the script's owner reshapes ``resolve``/``match`` under us instead of the preview silently
    losing its values.
    """

    @property
    def match(self) -> tuple[Any, ...]: ...
    def resolve(self, step_path: str, prompt: str) -> StubEntryLike | None: ...


def placeholder_output(record_path: str) -> str:
    """What an upstream step's output reads as when no stub scripts it."""
    return f"<{record_path} output>"


def stub_output(script: StubScriptLike | None, record_path: str) -> Any | None:
    """The value a ``--stubs`` script gives ``steps.<path>.output`` (``None`` when it has none).

    Precedence is **not** re-derived here: :meth:`StubScript.resolve` decides, exactly as the
    runner does, so a preview and the run it previews cannot drift apart (a glob declared before
    an exact key must not win). Only the ``steps:`` entries apply — a ``match:`` entry keys on
    the *prompt*, which does not exist before the upstream values are known, so an entry that
    came from ``match:`` is dropped.
    """
    if script is None:
        return None
    entry = script.resolve(record_path, "")
    if entry is None or any(entry is candidate for candidate in script.match):
        return None
    outcome = entry.outcome_for(1)
    if outcome.has_output:
        return outcome.output
    return outcome.text


def from_plan(
    resolved: ResolvedWorkflow,
    *,
    inputs: Mapping[str, Any],
    project_root: Path,
    script: StubScriptLike | None = None,
    engine: TemplateEngine | None = None,
    env: Mapping[str, str] | None = None,
) -> ContextRebuilder:
    """A rebuilder for a workflow that has not run: every upstream step succeeded, its output
    coming from ``script`` (a :class:`~rayspec.providers.stub.StubScript`) or from a visible
    ``<path output>`` placeholder.

    ``secret: true`` inputs are replaced by ``"<secret>"`` before anything is rendered — a
    preview must never print a credential.
    """

    def view_for(record_path: str, step: StepModel) -> StepView | None:
        value = stub_output(script, record_path)
        return StepView(
            id=step.id,
            kind=type(step).kind,
            status=StepStatus.SUCCEEDED,
            output=placeholder_output(record_path) if value is None else value,
            ok=True,
            exit_code=0 if type(step).kind in _STDERR_KINDS else None,
            duration_s=0.0,
        )

    return ContextRebuilder(
        resolved,
        view_for=view_for,
        inputs=redact_secret_inputs(resolved, inputs),
        run={
            "id": "<run-id>",
            "workflow": resolved.workflow.name,
            "workdir": str(project_root),
            "artifacts_dir": "<run-dir>/artifacts",
            "state_dir": "<run-dir>",
            "branch": None,
            "base_branch": None,
            "started_at": None,
        },
        project={
            "root": str(project_root),
            "name": Path(project_root).name,
            "slug": "<project-slug>",
        },
        env=env,
        engine=engine,
    )


def redact_secret_inputs(resolved: ResolvedWorkflow, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """``inputs`` with every ``secret: true`` value replaced by ``"<secret>"``."""
    specs = resolved.workflow.inputs
    return {
        name: SECRET_PLACEHOLDER if (name in specs and specs[name].secret) else value
        for name, value in inputs.items()
    }


__all__ = [
    "ENV_IS_LOCAL_WARNING",
    "ContextRebuildError",
    "ContextRebuilder",
    "RebuiltContext",
    "RecordSource",
    "RenderedBody",
    "StubEntryLike",
    "StubOutcomeLike",
    "StubScriptLike",
    "ViewSource",
    "env_reference_warning",
    "from_plan",
    "from_run",
    "oversize_placeholder",
    "placeholder_output",
    "read_ref",
    "redact_secret_inputs",
    "render_body",
    "render_script",
    "render_step_env",
    "run_vars",
    "stale_workflow_warning",
    "stub_output",
    "view_of_record",
]

# SPDX-License-Identifier: Apache-2.0
"""Lexical scopes, step views and the template context.

- :class:`Scope` — the chain of visible steps (``steps.<id>`` resolves innermost-first, then
  outward) plus scope-level variables (``iteration``, ``each``, the ``as:`` item ...).
- :class:`StepView` — what ``steps.<id>`` exposes; :meth:`StepView.resolve` is the hint-bearing
  attribute lookup used by the engine's sandboxed ``getattr``.
- :func:`build_context` — builds the mapping handed to ``TemplateEngine.render_*``/``eval_*``.
- :func:`export_env` / :func:`write_context_file` — the ``RAYSPEC_*`` variables and the
  ``RAYSPEC_CONTEXT`` JSON dump handed to shell/python steps.

Module boundary: depends on :mod:`rayspec.schema` (``StepStatus``) and
:mod:`rayspec.templating.undefined` only. No Jinja here — the environment lives in
:mod:`rayspec.templating.engine`.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path, PurePath
from typing import Any

from jinja2 import Undefined

from rayspec.schema import StepStatus
from rayspec.store.file import open_private, secure_mkdir
from rayspec.templating.undefined import RayspecUndefined

#: Attributes every ``StepView`` resolves (``steps.<id>.<attr>``), in documentation order.
STEP_ATTRIBUTES: tuple[str, ...] = (
    "output",
    "status",
    "ok",
    "exit_code",
    "stderr",
    "duration_s",
    "cost_usd",
    "usage",
    "session",
    "model",
    "approved",
    "iterations",
    "converged",
    "items",
    "id",
    "kind",
    "skip_reason",
    "error",
    "tolerated",
)


class _TextOutput(str):
    """A step's text output; carries its context path so ``.field`` on it yields a good hint."""

    __slots__ = ("_rayspec_path",)

    _rayspec_path: str

    def __new__(cls, value: str, path: str) -> _TextOutput:
        obj = super().__new__(cls, value)
        obj._rayspec_path = path
        return obj


class _DictOutput(dict[str, Any]):
    """A step's structured output; carries its context path for undefined-attribute messages."""

    __slots__ = ("_rayspec_path",)

    def __init__(self, value: Mapping[str, Any], path: str) -> None:
        super().__init__(value)
        self._rayspec_path = path


def plain(value: Any) -> Any:
    """Strip the message-carrying wrappers (``_TextOutput``/``_DictOutput``) from a value."""
    if isinstance(value, _TextOutput):
        return str(value)
    if isinstance(value, _DictOutput):
        return dict(value)
    return value


@dataclass(frozen=True)
class StepView:
    """What ``steps.<id>`` exposes to templates. Attributes not applicable to the step kind are
    ``None`` and resolve to a chainable undefined carrying a hint (``model`` is the provider's
    resolved model id of a prompt step; ``session``/``cost_usd``/``usage`` likewise).

    ``output`` is status-aware: a step that did not succeed (skipped, failed, pending ...) and has
    no output resolves to an undefined whose hint says how to guard the reference; ``ok`` is
    status-aware the same way on a **skipped** step — it never silently reads ``False``
    for a step that never answered. Text outputs
    resolve to a ``str`` that remembers its path so ``.field`` on it says "no output_schema (try
    | fromjson)". ``body_ids`` lists the ids of the steps inside a ``loop:``/``each:`` body so an
    outer scope can explain why ``steps.<inner>`` is not addressable.
    """

    id: str
    kind: str
    status: StepStatus = StepStatus.PENDING
    output: Any = None
    ok: bool | None = None
    exit_code: int | None = None
    stderr: str | None = None
    duration_s: float | None = None
    cost_usd: float | None = None
    usage: Any = None
    session: Any = None
    model: str | None = None
    approved: bool | None = None
    iterations: int | None = None
    converged: bool | None = None
    items: list[Any] | None = None
    skip_reason: str | None = None
    error: str | None = None
    tolerated: bool = False
    body_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def _rayspec_path(self) -> str:
        return f"steps.{self.id}"

    @property
    def status_name(self) -> str:
        """``status`` as a plain string (``StepStatus`` values or free strings)."""
        return str(self.status.value) if isinstance(self.status, Enum) else str(self.status)

    def resolve(self, name: str) -> Any:
        """Hint-bearing attribute lookup (``steps.<id>.<name>``); never raises."""
        if name == "output":
            return self._resolve_output()
        if name == "status":
            return self.status_name
        if name == "ok":
            return self._resolve_ok()
        if name == "usage":
            if self.usage is None:
                return self._undefined(name, f"no usage recorded for {self.kind} step {self.id!r}")
            if dataclasses.is_dataclass(self.usage) and not isinstance(self.usage, type):
                return dataclasses.asdict(self.usage)
            return self.usage
        if name in ("id", "kind", "tolerated"):
            return getattr(self, name)
        if name in STEP_ATTRIBUTES:
            value = getattr(self, name)
            if value is None:
                return self._undefined(
                    name,
                    f"steps.{self.id}.{name} is not set for this {self.kind} step; "
                    "use | default(...) or guard with `is defined`",
                )
            return value
        return self._undefined(
            name,
            f"did you mean steps.{self.id}.output.{name}? "
            f"(step attributes: {', '.join(STEP_ATTRIBUTES[:14])})",
        )

    def _resolve_ok(self) -> Any:
        """``steps.<id>.ok``: a bool, except on a **skipped** step.

        A skipped step never answered the question, so ``ok`` is an undefined carrying the same
        hint ``.output`` gives — ``when: steps.x.ok`` fails loudly instead of silently reading
        ``False``. ``| default(false)`` and ``is defined`` still work.
        """
        if self.status_name == StepStatus.SKIPPED.value:
            return self._undefined("ok", self._skipped_hint())
        if self.ok is not None:
            return self.ok
        return self.status_name == StepStatus.SUCCEEDED.value

    def _skipped_hint(self) -> str:
        """The shared "this step was skipped" hint of ``.output`` and ``.ok``."""
        why = f" ({self.skip_reason})" if self.skip_reason else ""
        return (
            f"step {self.id!r} was skipped{why} — guard with steps.{self.id}.status == 'succeeded'"
        )

    def _resolve_output(self) -> Any:
        status = self.status_name
        if self.output is None:
            if status == StepStatus.SKIPPED.value:
                return self._undefined("output", self._skipped_hint())
            if status == StepStatus.FAILED.value:
                return self._undefined(
                    "output",
                    f"step {self.id!r} failed — guard with steps.{self.id}.status == 'succeeded'",
                )
            if status == StepStatus.SUCCEEDED.value:
                return self._undefined(
                    "output",
                    f"step {self.id!r} produced no output; use | default(...)",
                )
            return self._undefined(
                "output",
                f"step {self.id!r} has not finished (status {status}) — guard with "
                f"steps.{self.id}.status == 'succeeded'",
            )
        path = f"steps.{self.id}.output"
        if isinstance(self.output, str):
            return _TextOutput(self.output, path)
        if isinstance(self.output, Mapping):
            return _DictOutput(self.output, path)
        return self.output

    def _undefined(self, name: str, hint: str) -> RayspecUndefined:
        return RayspecUndefined(obj=self, name=name, rayspec_hint=hint)

    def to_json(self) -> dict[str, Any]:
        """JSON-safe dict of the view (for ``RAYSPEC_CONTEXT``)."""
        data: dict[str, Any] = {}
        for name in STEP_ATTRIBUTES:
            if name == "status":
                data[name] = self.status_name
            elif name == "ok":
                # a skipped step's ``ok`` is undefined → ``null`` in RAYSPEC_CONTEXT
                data[name] = to_jsonable(self.resolve("ok"))
            else:
                data[name] = to_jsonable(getattr(self, name))
        return data


class Scope:
    """A lexical scope: the steps visible at this level plus scope variables.

    ``steps.<id>`` resolves innermost-first then outward; a body step is never addressable from
    an enclosing scope (the outer scope only sees the composite step). ``variables`` hold values
    such as ``iteration``/``each``/``<as>`` that bodies introduce; inner values shadow outer ones.
    """

    def __init__(
        self,
        parent: Scope | None,
        steps: Mapping[str, StepView],
        variables: Mapping[str, Any] | None = None,
    ) -> None:
        self.parent = parent
        self.steps = steps
        self.variables: Mapping[str, Any] = variables if variables is not None else {}

    def child(
        self, steps: Mapping[str, StepView], variables: Mapping[str, Any] | None = None
    ) -> Scope:
        """A new scope nested inside this one."""
        return Scope(self, steps, variables)

    def chain(self) -> Iterator[Scope]:
        """Scopes from innermost (self) to outermost."""
        scope: Scope | None = self
        while scope is not None:
            yield scope
            scope = scope.parent

    def lookup_step(self, name: str) -> StepView | None:
        """The nearest visible step called ``name`` (or ``None``)."""
        for scope in self.chain():
            view = scope.steps.get(name)
            if view is not None:
                return view
        return None

    def visible_steps(self) -> dict[str, StepView]:
        """All visible steps, innermost definitions winning, in innermost-first order."""
        seen: dict[str, StepView] = {}
        for scope in self.chain():
            for name, view in scope.steps.items():
                seen.setdefault(name, view)
        return seen

    def lookup_var(self, name: str, default: Any = None) -> Any:
        """The nearest scope variable called ``name``."""
        for scope in self.chain():
            if name in scope.variables:
                return scope.variables[name]
        return default

    def merged_vars(self) -> dict[str, Any]:
        """All scope variables, inner shadowing outer."""
        merged: dict[str, Any] = {}
        for scope in reversed(list(self.chain())):
            merged.update(scope.variables)
        return merged

    def missing_step_hint(self, name: str) -> str:
        """Why ``steps.<name>`` is not visible here, phrased as a fix."""
        for view in self.visible_steps().values():
            if name in view.body_ids:
                if view.kind == "each":
                    return (
                        f"step {name!r} is inside each {view.id!r}; use "
                        f"steps.{view.id}.output[<index>].{name} or steps.{view.id}.items"
                    )
                return (
                    f"step {name!r} is inside {view.kind} {view.id!r}; use "
                    f"steps.{view.id}.output.{name}"
                )
        return (
            f"unknown step {name!r}; steps can only reference upstream steps in the same or an "
            "enclosing scope (a step inside a loop/each body is not addressable from outside)"
        )


class Namespace(Mapping[str, Any]):
    """Read-only mapping root of the context (``inputs``, ``run``, ``iteration`` ...).

    Carries its path and a ``missing_hint`` callback so the engine's ``getattr`` can produce
    ``'inputs' has no attribute 'x'; <hint>`` instead of a bare undefined.
    """

    def __init__(
        self,
        path: str,
        data: Mapping[str, Any],
        missing_hint: Callable[[str], str | None] | None = None,
    ) -> None:
        self._rayspec_path = path
        self._data = dict(data)
        self._missing_hint = missing_hint

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"Namespace({self._rayspec_path}, {self._data!r})"

    def _rayspec_missing_hint(self, name: str) -> str | None:
        return self._missing_hint(name) if self._missing_hint else None


class StepsNamespace(Mapping[str, StepView]):
    """The ``steps`` root: a read-only view over a :class:`Scope` chain."""

    _rayspec_path = "steps"

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def __getitem__(self, key: str) -> StepView:
        view = self.scope.lookup_step(key)
        if view is None:
            raise KeyError(key)
        return view

    def __iter__(self) -> Iterator[str]:
        return iter(self.scope.visible_steps())

    def __len__(self) -> int:
        return len(self.scope.visible_steps())

    def __repr__(self) -> str:
        return f"StepsNamespace({list(self)!r})"

    def _rayspec_missing_hint(self, name: str) -> str:
        return self.scope.missing_step_hint(name)


def _inputs_hint(name: str) -> str:
    return (
        f"input {name!r} is not declared or was not provided; declare it under inputs: "
        "(with a default) or use | default(...)"
    )


def _env_hint(name: str) -> str:
    return f"environment variable {name!r} is not set; use | default(...)"


def _iteration_hint(name: str) -> str | None:
    if name == "prev":
        return "iteration.prev is undefined on the first iteration; use | default(...)"
    return "iteration exposes n, max, first and prev.<body step id>"


def _each_hint(name: str) -> str:
    return "each exposes index and total; the current item is available under its `as:` name"


def _fixed_hint(root: str, keys: tuple[str, ...]) -> Callable[[str], str]:
    def hint(name: str) -> str:
        return f"{root} exposes {', '.join(keys)}"

    return hint


RUN_KEYS = (
    "id",
    "workflow",
    "workdir",
    "artifacts_dir",
    "state_dir",
    "branch",
    "base_branch",
    "started_at",
)
PROJECT_KEYS = ("root", "name", "slug")


def build_context(
    scope: Scope,
    *,
    inputs: Mapping[str, Any],
    run: Mapping[str, Any],
    project: Mapping[str, Any],
    iteration: Mapping[str, Any] | None = None,
    each: Mapping[str, Any] | None = None,
    item_var: str | None = None,
    item: Any = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the mapping handed to the engine's ``render_*``/``eval_*`` methods.

    Roots: ``inputs``, ``steps`` (the scope chain), ``run``, ``project``, ``env``, plus
    ``iteration`` / ``each`` / the ``as:`` item when given (explicit arguments override values
    of the same name found in ``scope.variables``). ``iteration.prev`` that is ``None``
    (iteration 1) is dropped so ``iteration.prev.x.output | default('')`` works through a
    chainable undefined.
    When ``each`` is given without ``item_var`` the item is exposed as ``item``.
    """
    ctx: dict[str, Any] = {}
    for name, value in scope.merged_vars().items():
        ctx[name] = _wrap_var(name, value)
    ctx["inputs"] = Namespace("inputs", inputs, _inputs_hint)
    ctx["steps"] = StepsNamespace(scope)
    ctx["run"] = Namespace("run", run, _fixed_hint("run", RUN_KEYS))
    ctx["project"] = Namespace("project", project, _fixed_hint("project", PROJECT_KEYS))
    ctx["env"] = Namespace("env", env or {}, _env_hint)
    if iteration is not None:
        ctx["iteration"] = _wrap_var("iteration", iteration)
    if each is not None:
        ctx["each"] = _wrap_var("each", each)
        if item_var is None:
            item_var = "item"
    if item_var is not None:
        ctx[item_var] = item
    return ctx


def _wrap_var(name: str, value: Any) -> Any:
    if name == "iteration" and isinstance(value, Mapping):
        data = {k: v for k, v in value.items() if not (k == "prev" and v is None)}
        return Namespace("iteration", data, _iteration_hint)
    if name == "each" and isinstance(value, Mapping):
        return Namespace("each", value, _each_hint)
    return value


def stringify_scalar(value: Any) -> str:
    """Env-export text form: str as-is, bool → true/false, numbers → str, composites → JSON."""
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(to_jsonable(value), ensure_ascii=False)


def export_env(ctx: Mapping[str, Any]) -> dict[str, str]:
    """``RAYSPEC_INPUT_<NAME>`` (scalar text / JSON for composites), ``RAYSPEC_RUN_ID``,
    ``RAYSPEC_WORKDIR``, ``RAYSPEC_ARTIFACTS_DIR``, ``RAYSPEC_STATE_DIR`` for a step's process."""
    out: dict[str, str] = {}
    inputs = ctx.get("inputs") or {}
    for name, value in inputs.items():
        if value is None or isinstance(value, Undefined):
            continue
        out[f"RAYSPEC_INPUT_{str(name).upper()}"] = stringify_scalar(value)
    run = ctx.get("run") or {}
    for key, var in (
        ("id", "RAYSPEC_RUN_ID"),
        ("workdir", "RAYSPEC_WORKDIR"),
        ("artifacts_dir", "RAYSPEC_ARTIFACTS_DIR"),
        ("state_dir", "RAYSPEC_STATE_DIR"),
    ):
        value = run.get(key)
        if value is not None:
            out[var] = str(value)
    return out


def to_jsonable(value: Any) -> Any:
    """Deep-convert a context value to JSON-safe data (views → dicts, undefined → null)."""
    if isinstance(value, _TextOutput):
        return str(value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Undefined):
        return None
    if isinstance(value, StepView):
        return value.to_json()
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def write_context_file(ctx: Mapping[str, Any], path: Path) -> Path:
    """Write the ``RAYSPEC_CONTEXT`` JSON dump of a step's template context and return ``path``.

    The ``env`` root is **not** written: it mirrors the whole process environment (API keys,
    tokens, everything ``.env`` merged in) and the script already has the real environment —
    persisting it under the run directory would leak secrets in plaintext. The file is written
    to ``<path>.tmp`` and moved into place with :func:`os.replace`, so a crash mid-write never
    leaves a truncated context file behind; the file is created ``0600`` (directories ``0700``)
    because the context holds the run's inputs.
    """
    path = Path(path)
    secure_mkdir(path.parent)
    data = {
        str(k): to_jsonable(v)
        for k, v in ctx.items()
        if not str(k).startswith("__") and str(k) != "env"
    }
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open_private(tmp, "w") as fh:  # 0600: the context holds the inputs
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


__all__ = [
    "STEP_ATTRIBUTES",
    "Namespace",
    "Scope",
    "StepView",
    "StepsNamespace",
    "build_context",
    "export_env",
    "plain",
    "stringify_scalar",
    "to_jsonable",
    "write_context_file",
]

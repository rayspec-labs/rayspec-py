# SPDX-License-Identifier: Apache-2.0
"""``TemplateEngine`` — the three sandboxed Jinja environments and the expression API.

Environments (all ``ImmutableSandboxedEnvironment``, ``trim_blocks``/``lstrip_blocks``/
``keep_trailing_newline``, undefined = :class:`RayspecUndefined`, every Jinja builtin filter/test
plus ``fromjson``/``regex_search``/``has_signal``):

- **text** (prompts, instructions, approve message, stop.reason, outputs, with, env, cwd):
  ``{{ x }}`` renders str as-is, bool → ``true``/``false``, numbers → ``str``, list/dict →
  ``json.dumps(indent=2)``, ``None`` → error ("use | default"), undefined → error, callables /
  arbitrary objects → error (no ``<bound method ...>`` reprs in prompts). ``render_text``
  returns the raw value for a template that is exactly one ``{{ expr }}``; text *fields* use
  ``render_str`` which applies the same rule to that value.
- **shell**: every ``{{ expr }}`` renders to the variable reference ``${RAYSPEC_V<n>}`` and the
  stringified value is collected into the returned env (never spliced into the script, so bash's
  own quoting applies and ``$(rm -rf /)`` in a value stays inert). Values over 64 KiB spill to
  ``<spill_dir>/v<n>-<random>`` (``mkstemp``; ``spill_dir`` is made absolute and may be shared
  by parallel steps); the body still reads ``${RAYSPEC_V<n>}`` and a preamble line prepended to
  the script assigns the slot from that file (:func:`_read_back`), so the reference behaves the
  same either side of the threshold and no scratch path reaches the body. A spilled slot is a
  *shell* variable and is not exported — the one difference that remains, and a deliberate one
  (:func:`_read_back` says why). Comment delimiters are ``{{# … #}}`` so bash ``${#VAR}``
  survives. Because the substitution is a plain ``${VAR}`` reference, bash's own rules decide
  whether it expands: inside single quotes or a quoted heredoc (``<<'EOF'``) it stays the
  literal text ``${RAYSPEC_V<n>}`` — use double quotes / an unquoted heredoc.
- **python**: ``{{ expr }}`` renders ``repr()`` of JSON-like values (str/int/float/bool/None/
  list/dict with str keys → a valid Python literal); anything else is an error. Oversized
  literals spill to a JSON file and render as ``json.loads(Path(...).read_text())``. Same comment
  delimiters. The literal is a *Python expression*: ``x = {{ v }}`` is right; placing
  ``{{ v }}`` inside a (triple-)quoted string puts the repr, quotes included, into that string.

Literal braces in code bodies (Go templates for ``docker --format``, ``gh``, ``kubectl``,
``helm``; ``printf '{{'``) must be wrapped in ``{% raw %} … {% endraw %}``. GitHub-Actions
``${{`` is a lint error (:func:`rayspec.templating.lints.has_gha_syntax`). ``{% macro %}``,
``{% call %}``, ``{% filter %}`` blocks and ``{% set x %}…{% endset %}`` are compile errors in
shell/python bodies: they would re-substitute already substituted text (see
:func:`_reject_refinalizing_constructs`); use ``{% set x = expr %}`` and inline filters.

Attribute lookup differs from stock Jinja in two deliberate ways: on mappings ``.name`` is an
item lookup first (``inputs.items`` is the input called ``items``) and falls back only to the
mapping methods ``items``/``keys``/``values``/``get`` (``.items()`` still works unless shadowed);
on a :class:`StepView` / text output it goes through the hint-bearing ``StepView.resolve`` so
every miss names the fix.

Module boundary: depends on :mod:`rayspec.templating.{errors,filters,scope,undefined}`;
nothing else in rayspec.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Literal

from jinja2 import TemplateError, TemplateSyntaxError, Undefined, UndefinedError, nodes
from jinja2.environment import Template, TemplateExpression
from jinja2.exceptions import SecurityError
from jinja2.parser import Parser
from jinja2.sandbox import ImmutableSandboxedEnvironment

from rayspec.templating.errors import TemplateCompileError, TemplateRenderError
from rayspec.templating.filters import FILTERS, TESTS
from rayspec.templating.scope import StepView, _TextOutput, plain, to_jsonable
from rayspec.templating.undefined import RayspecUndefined

TemplateKind = Literal["text", "shell", "python"]
ReferenceKind = Literal["text", "shell", "python", "expr"]

#: Values whose rendered size exceeds this many bytes are spilled to a file in code bodies.
SPILL_THRESHOLD = 64 * 1024

#: Context roots recognised by :meth:`TemplateEngine.references`.
REFERENCE_ROOTS: frozenset[str] = frozenset(
    {"steps", "inputs", "iteration", "each", "run", "project", "env"}
)

_NULL_MESSAGE = "value is null; use | default(...)"

#: Mapping methods reachable as ``mapping.<name>`` when no item of that name exists.
_MAPPING_METHODS: frozenset[str] = frozenset({"items", "keys", "values", "get"})


@dataclass(frozen=True)
class Ref:
    """A reference found in a template/expression: ``steps.a.output.b`` →
    ``Ref("steps", "a", ("output", "b"))``. ``name`` is ``None`` when the first segment is
    dynamic (``steps[inputs.x]``) or the root is used bare (``{{ inputs }}``)."""

    root: str
    name: str | None
    attr_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderedScript:
    """A rendered shell/python body: the script text, the ``RAYSPEC_V<n>`` env slots and the
    spill files written (the engine must add the exported ``RAYSPEC_*`` env itself).

    ``script`` carries the preamble for any spilled slot; it is part of the script rather than a
    separate field so every consumer — the executor, the fingerprint, ``explain`` — handles the
    two sides of the spill threshold without knowing there are two."""

    script: str
    env: dict[str, str] = field(default_factory=dict)
    spills: list[Path] = field(default_factory=list)


def stringify_text(value: Any) -> str:
    """The text-environment finalizer rule: str as-is, bool → ``true``/``false``, numbers →
    ``str``, composites/dataclasses → JSON (indent 2), paths/dates/enums → their text form.

    ``None`` and undefined raise :class:`TemplateRenderError` naming the fix, and so does
    anything else (a callable such as an un-called method, an arbitrary object): a repr like
    ``<bound method ...>`` must never leak into a prompt or an env value.
    """
    if isinstance(value, Undefined):
        value._fail_with_undefined_error()
    if value is None:
        raise TemplateRenderError(_NULL_MESSAGE, hint="use | default(...)")
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, Mapping | list | tuple | set | frozenset) or _is_dataclass_instance(value):
        return json.dumps(to_jsonable(value), indent=2, ensure_ascii=False)
    if isinstance(value, Enum):
        return stringify_text(value.value)
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if callable(value):
        raise TemplateRenderError(
            f"cannot render {_describe_callable(value)}; did you mean to call it "
            "(add parentheses), or use | tojson on the value?",
            hint="did you mean to call it (add parentheses)?",
        )
    raise TemplateRenderError(
        f"cannot render {type(value).__name__} value {value!r}; only text, numbers, booleans, "
        "lists and mappings can be rendered (use | tojson / | string for anything else)",
        hint="use | tojson or | string",
    )


def jsonable_for_tojson(value: Any) -> Any:
    """``value`` as plain JSON data for ``| tojson`` — strict about undefined.

    The context roots are not plain containers (``Namespace``, ``StepsNamespace``,
    :class:`~rayspec.templating.scope.StepView`), so the stock serialiser refused the documented
    ``{{ inputs | tojson }}`` with a raw ``TypeError: Object of type Namespace is not JSON
    serializable`` — at run time, after ``rayspec validate`` had passed. Everything is converted
    the way ``RAYSPEC_CONTEXT`` converts it (:func:`~rayspec.templating.scope.to_jsonable`), so
    a script reads the same shape whichever of the two it was handed.

    An undefined reached while walking is an ERROR, not ``null``: ``to_jsonable`` maps one to
    ``null`` because a context FILE has to be writable whatever the run did, but a template that
    names something that is not there must fail the way every other rendering path fails. (The
    one place a null still appears is inside a step view, where ``StepView.to_json`` decides it
    — ``ok`` on a skipped step — and that is the view's own answer, not a typo.)
    """
    if isinstance(value, Undefined):
        value._fail_with_undefined_error()
    if isinstance(value, Mapping):
        return {str(k): jsonable_for_tojson(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable_for_tojson(v) for v in value]
    return to_jsonable(value)


def _tojson_dumps(value: Any, **kwargs: Any) -> str:
    """The environments' ``json.dumps_function`` policy — what ``| tojson`` serialises through."""
    return json.dumps(jsonable_for_tojson(value), **kwargs)


def _describe_callable(value: Any) -> str:
    name = getattr(value, "__name__", None)
    kind = "method" if hasattr(value, "__self__") else "function"
    return f"{kind} {name!r}" if isinstance(name, str) else f"callable {type(value).__name__}"


def _is_dataclass_instance(value: Any) -> bool:
    import dataclasses

    return dataclasses.is_dataclass(value) and not isinstance(value, type)


def _python_literal(value: Any) -> str:
    """``repr`` of a JSON-like value as a valid Python literal; anything else is an error.

    Mapping keys must be strings (JSON-like); a non-string key is an error rather than a silent
    coercion to ``'1'``.
    """
    if isinstance(value, Undefined):
        value._fail_with_undefined_error()
    value = plain(value)
    _check_str_keys(value, "value")
    try:
        dumped = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TemplateRenderError(
            "python: {{ }} values must be JSON-like (str/int/float/bool/None/list/dict), "
            f"got {type(value).__name__} ({exc})"
        ) from exc
    return repr(json.loads(dumped))


def _check_str_keys(value: Any, where: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TemplateRenderError(
                    f"python: {{{{ }}}} values must be JSON-like; {where} has a non-string key "
                    f"{key!r} ({type(key).__name__})"
                )
            _check_str_keys(item, f"{where}[{key!r}]")
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            _check_str_keys(item, f"{where}[{i}]")


def _shell_quote(text: str) -> str:
    """*text* as one single-quoted POSIX word — the only quoting that is literal throughout."""
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _read_back(name: str, path: Path) -> str:
    """The preamble assignment that puts a spilled file's exact bytes into ``$<name>``.

    Every part of it earns its place:

    - ``unset`` first. rayspec can be run *from* a shell step, so ``RAYSPEC_V<n>`` may already
      be in the environment; assigning to an exported name keeps the export attribute, which
      would push a value larger than the threshold straight back into the environment block
      that spilling exists to keep it out of. After ``unset`` the assignment makes a plain
      shell variable, which is **not** exported — a child process started by the body reads a
      small slot from its own environment but not a spilled one. That difference across the
      threshold is deliberate and is the one that remains.
    - ``&& printf x`` rather than ``; printf x``, and ``|| exit $?``: with ``;`` the sentinel is
      printed even when ``cat`` failed, the slot becomes the empty string and the body runs on
      with it. This form fails the script where the read failed, with ``cat``'s own message,
      also when the caller did not set ``-e``.
    - ``${name%x}`` removes the sentinel, and the sentinel is the point: ``$( )`` strips every
      trailing newline, so a non-newline last byte is what makes the read verbatim.

    POSIX only — ``unset``, ``$( )`` and ``${v%x}`` are in the standard, ``$(<file)`` and
    ``read -r -d ''`` are not: the same rendered script also runs under ``interpreter: sh``.
    """
    return (
        f"unset {name}; {name}=$(cat {_shell_quote(str(path))} && printf x) || exit $?; "
        f"{name}=${{{name}%x}}"
    )


class _SlotCollector:
    """Per-render collector for shell ``${RAYSPEC_V<n>}`` slots, spill files and the preamble.

    Spill files are created with :func:`tempfile.mkstemp` (``<spill_dir>/v<n>-<random>``) so
    concurrent renders — parallel steps sharing the run's ``tmp/`` directory — never overwrite
    each other; ``spill_dir`` is made absolute so the embedded path survives a step ``cwd:``.

    Slot indices restart at 1 per render, but a preamble line and the slot it assigns are in
    the same script and each script is its own process, so two concurrently rendered scripts
    cannot see each other's variables however much their numbering overlaps.
    """

    def __init__(self, spill_dir: Path | None, threshold: int) -> None:
        self.spill_dir = spill_dir.absolute() if spill_dir is not None else None
        self.threshold = threshold
        self.env: dict[str, str] = {}
        self.spills: list[Path] = []
        self.preamble: list[str] = []
        self._n = 0

    def next_index(self) -> int:
        self._n += 1
        return self._n

    def spill(self, n: int, text: str) -> Path:
        if self.spill_dir is None:
            raise TemplateRenderError(
                f"value #{n} is larger than {self.threshold} bytes and no spill directory is "
                "configured (pass spill_dir=)"
            )
        self.spill_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"v{n}-", dir=self.spill_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        path = Path(name)
        self.spills.append(path)
        return path

    def shell_ref(self, value: Any) -> str:
        text = stringify_text(value)
        n = self.next_index()
        name = f"RAYSPEC_V{n}"
        if len(text.encode("utf-8")) > self.threshold:
            self.preamble.append(_read_back(name, self.spill(n, text)))
        else:
            self.env[name] = text
        return f"${{{name}}}"

    def python_ref(self, value: Any) -> str:
        literal = _python_literal(value)
        n = self.next_index()
        if len(literal.encode("utf-8")) > self.threshold:
            path = self.spill(n, json.dumps(plain(value), ensure_ascii=False))
            return (
                "__import__('json').loads(__import__('pathlib').Path("
                f"{str(path)!r}).read_text(encoding='utf-8'))"
            )
        return literal


#: The collector of the render in progress (set by ``TemplateEngine._render_code``); a context
#: variable so it is never visible to templates and is safe across threads/tasks.
_current_collector: ContextVar[_SlotCollector | None] = ContextVar(
    "rayspec_slot_collector", default=None
)


def _collector() -> _SlotCollector:
    collector = _current_collector.get()
    if collector is None:  # pragma: no cover - only reachable by rendering outside the engine
        raise TemplateRenderError("shell/python templates must be rendered through TemplateEngine")
    return collector


def _shell_finalize(value: Any) -> str:
    return _collector().shell_ref(value)


def _python_finalize(value: Any) -> str:
    return _collector().python_ref(value)


class _RayspecEnvironment(ImmutableSandboxedEnvironment):
    """Sandboxed environment with rayspec's attribute-lookup rules (see module docstring)."""

    def getattr(self, obj: Any, attribute: str) -> Any:
        if isinstance(obj, StepView):
            return obj.resolve(attribute)
        if isinstance(obj, _TextOutput):
            if not attribute.startswith("_") and hasattr(str, attribute):
                return super().getattr(obj, attribute)
            return RayspecUndefined(
                obj=obj,
                name=attribute,
                rayspec_hint="this step has no output_schema (try | fromjson)",
            )
        if isinstance(obj, Mapping):
            try:
                return obj[attribute]
            except (KeyError, TypeError):
                pass
            if attribute in _MAPPING_METHODS and hasattr(type(obj), attribute):
                value = getattr(obj, attribute)
                if self.is_safe_attribute(obj, attribute, value):
                    return value
            return RayspecUndefined(
                obj=obj, name=attribute, rayspec_hint=_missing_hint(obj, attribute)
            )
        return super().getattr(obj, attribute)

    def getitem(self, obj: Any, argument: Any) -> Any:
        if isinstance(obj, StepView) and isinstance(argument, str):
            return obj.resolve(argument)
        if isinstance(obj, _TextOutput) and isinstance(argument, str):
            return self.getattr(obj, argument)
        if isinstance(obj, Mapping):
            try:
                return obj[argument]
            except (KeyError, TypeError):
                hint = _missing_hint(obj, argument) if isinstance(argument, str) else None
                return RayspecUndefined(obj=obj, name=argument, rayspec_hint=hint)
        return super().getitem(obj, argument)


def _missing_hint(obj: Any, name: str) -> str | None:
    hinter = getattr(obj, "_rayspec_missing_hint", None)
    if callable(hinter):
        try:
            hint = hinter(name)
        except Exception:  # pragma: no cover - a hint must never break rendering
            return None
        return hint if isinstance(hint, str) else None
    return None


def _make_env(kind: TemplateKind) -> _RayspecEnvironment:
    kwargs: dict[str, Any] = {
        "undefined": RayspecUndefined,
        "autoescape": False,
        "trim_blocks": True,
        "lstrip_blocks": True,
        "keep_trailing_newline": True,
    }
    if kind == "text":
        kwargs["finalize"] = stringify_text
    else:
        kwargs["comment_start_string"] = "{{#"
        kwargs["comment_end_string"] = "#}}"
        kwargs["finalize"] = _shell_finalize if kind == "shell" else _python_finalize
    env = _RayspecEnvironment(**kwargs)
    env.filters.update(FILTERS)
    env.tests.update(TESTS)
    # ``| tojson`` goes through the same converter ``RAYSPEC_CONTEXT`` uses, so the context
    # roots (``inputs``, ``steps``, ``run`` …) serialise instead of raising a bare TypeError.
    env.policies["json.dumps_function"] = _tojson_dumps
    return env


class TemplateEngine:
    """Facade over the three environments; compiled templates/expressions are cached per text.

    ``ctx`` arguments are the mapping produced by :func:`rayspec.templating.build_context`
    (any mapping with the context roots works).
    """

    def __init__(self, *, spill_threshold: int = SPILL_THRESHOLD) -> None:
        self.spill_threshold = spill_threshold
        self._envs: dict[str, _RayspecEnvironment] = {
            "text": _make_env("text"),
            "shell": _make_env("shell"),
            "python": _make_env("python"),
        }
        self._templates: dict[tuple[str, str], Template] = {}
        self._single: dict[str, TemplateExpression | None] = {}
        self._exprs: dict[str, TemplateExpression] = {}

    # -- environments ------------------------------------------------------------------
    def environment(self, kind: TemplateKind) -> _RayspecEnvironment:
        """The underlying Jinja environment (read-only use; e.g. for ``parse``)."""
        return self._envs[kind]

    # -- load-time -----------------------------------------------------------------------
    def compile_template(self, text: str, *, where: str, kind: TemplateKind = "text") -> Template:
        """Compile ``text`` (load time); raises :class:`TemplateCompileError` naming ``where``."""
        try:
            return self._template(text, kind)
        except TemplateSyntaxError as exc:
            raise TemplateCompileError(where, exc.message or str(exc), exc.lineno) from exc
        except TemplateError as exc:
            raise TemplateCompileError(where, str(exc)) from exc

    def compile_expr(self, text: str, *, where: str) -> TemplateExpression:
        """Compile a bare expression (``when``/``until``/``each``); raises TemplateCompileError."""
        try:
            return self._expression(text)
        except TemplateSyntaxError as exc:
            raise TemplateCompileError(where, exc.message or str(exc), exc.lineno) from exc
        except TemplateError as exc:
            raise TemplateCompileError(where, str(exc)) from exc

    def references(self, text: str, *, kind: ReferenceKind = "text") -> frozenset[Ref]:
        """All ``<root>.<name>.<attr...>`` references in a template (``kind="expr"``: expression).

        Walks the AST for ``Getattr``/``Getitem`` chains rooted at a context root
        (:data:`REFERENCE_ROOTS`); ``steps["x"]`` counts like ``steps.x``. Raises
        :class:`TemplateCompileError` (``where=<kind>``) when the text does not parse.
        """
        try:
            if kind == "expr":
                node: nodes.Node = self._parse_expression(self._envs["text"], text)
            else:
                node = self._envs[kind].parse(text)
        except TemplateSyntaxError as exc:
            raise TemplateCompileError(kind, exc.message or str(exc), exc.lineno) from exc
        found: set[Ref] = set()
        self._collect_refs(node, found)
        return frozenset(found)

    # -- run-time ------------------------------------------------------------------------
    def render_text(self, template: str, ctx: Mapping[str, Any]) -> Any:
        """Render a text-environment template. A template that is exactly one ``{{ expr }}`` is
        evaluated as an expression and keeps its Python type; everything else returns ``str``."""
        with _render_guard():
            single = self._single_expression(template)
            if single is not None:
                return self._evaluate(single, ctx)
            return self._template(template, "text").render(ctx)

    def render_str(self, template: str, ctx: Mapping[str, Any]) -> str:
        """Render a text field (prompt, approve message, ``stop.reason``, ``cwd`` ...) to ``str``.

        ``stringify_text(render_text(...))``: a single ``{{ expr }}`` is str-coerced with the
        text-environment rule, so ``None``/undefined/callables fail loudly instead of leaking
        ``None`` or a repr into a prompt.
        """
        value = self.render_text(template, ctx)
        with _render_guard():
            return stringify_text(value)

    def render_value(self, value: Any, ctx: Mapping[str, Any]) -> Any:
        """Deep-render ``outputs:``/``with:``/``env:`` values: ``str`` is a template, dict/list
        are recursed, other scalars pass through (single ``{{ expr }}`` keeps its type)."""
        if isinstance(value, str):
            return self.render_text(value, ctx)
        if isinstance(value, Mapping):
            return {k: self.render_value(v, ctx) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.render_value(v, ctx) for v in value]
        return value

    def render_shell(
        self, body: str, ctx: Mapping[str, Any], *, spill_dir: Path | None = None
    ) -> RenderedScript:
        """Render a ``shell:`` body: ``{{ expr }}`` → ``${RAYSPEC_V<n>}`` (+ env), big → spill."""
        return self._render_code(body, ctx, "shell", spill_dir)

    def render_python(
        self, body: str, ctx: Mapping[str, Any], *, spill_dir: Path | None = None
    ) -> RenderedScript:
        """Render a ``python:`` body: ``{{ expr }}`` → Python literal (``repr`` of JSON-like)."""
        return self._render_code(body, ctx, "python", spill_dir)

    def eval_expr(self, expr: str, ctx: Mapping[str, Any]) -> Any:
        """Evaluate a bare expression and return its value (an undefined result is an error)."""
        with _render_guard():
            return self._evaluate(self._expression(expr), ctx)

    def eval_bool(self, expr: str, ctx: Mapping[str, Any]) -> bool:
        """Evaluate a ``when:``/``until:`` expression; the result must be exactly a bool."""
        value = self.eval_expr(expr, ctx)
        if isinstance(value, bool):
            return value
        raise TemplateRenderError(
            f"expression {expr!r} must evaluate to true/false, got "
            f"{type(value).__name__} {value!r}; compare explicitly or test emptiness "
            "(e.g. `x == 'yes'`, `x | length > 0`)",
            hint="compare explicitly or test emptiness",
        )

    # -- internals -----------------------------------------------------------------------
    def _template(self, text: str, kind: TemplateKind) -> Template:
        key = (kind, text)
        tpl = self._templates.get(key)
        if tpl is None:
            env = self._envs[kind]
            if kind != "text":
                _reject_refinalizing_constructs(env.parse(text), kind)
            tpl = env.from_string(text)
            self._templates[key] = tpl
        return tpl

    @staticmethod
    def _parse_expression(env: _RayspecEnvironment, text: str) -> nodes.Expr:
        parser = Parser(env, text, state="variable")
        expr = parser.parse_expression()
        if not parser.stream.eos:
            raise TemplateSyntaxError(
                "chunk after expression", parser.stream.current.lineno, None, None
            )
        expr.set_environment(env)
        return expr

    def _compile_expression_node(self, expr: nodes.Expr) -> TemplateExpression:
        env = self._envs["text"]
        body = [nodes.Assign(nodes.Name("result", "store"), expr, lineno=1)]
        template = env.from_string(nodes.Template(body, lineno=1))
        return TemplateExpression(template, False)

    def _expression(self, text: str) -> TemplateExpression:
        compiled = self._exprs.get(text)
        if compiled is None:
            expr = self._parse_expression(self._envs["text"], text)
            compiled = self._compile_expression_node(expr)
            self._exprs[text] = compiled
        return compiled

    def _single_expression(self, template: str) -> TemplateExpression | None:
        """The compiled expression when ``template`` is exactly one ``{{ expr }}``, else None."""
        if template in self._single:
            return self._single[template]
        result: TemplateExpression | None = None
        if "{{" in template:
            env = self._envs["text"]
            tree = env.parse(template)
            body = tree.body
            if (
                len(body) == 1
                and isinstance(body[0], nodes.Output)
                and len(body[0].nodes) == 1
                and not isinstance(body[0].nodes[0], nodes.TemplateData)
            ):
                expr = body[0].nodes[0]
                expr.set_environment(env)
                result = self._compile_expression_node(expr)
        self._single[template] = result
        return result

    @staticmethod
    def _evaluate(compiled: TemplateExpression, ctx: Mapping[str, Any]) -> Any:
        value = compiled(dict(ctx))
        if isinstance(value, Undefined):
            value._fail_with_undefined_error()
        return plain(value)

    def _render_code(
        self, body: str, ctx: Mapping[str, Any], kind: TemplateKind, spill_dir: Path | None
    ) -> RenderedScript:
        collector = _SlotCollector(
            Path(spill_dir) if spill_dir is not None else None, self.spill_threshold
        )
        token = _current_collector.set(collector)
        try:
            with _render_guard():
                tpl = self._template(body, kind)
                script = tpl.render(dict(ctx))
        finally:
            _current_collector.reset(token)
        if collector.preamble:
            script = "\n".join([*collector.preamble, script])
        return RenderedScript(script=script, env=collector.env, spills=collector.spills)

    def _collect_refs(self, node: nodes.Node, found: set[Ref]) -> None:
        if isinstance(node, nodes.Getattr | nodes.Getitem):
            ref, dynamic_children = _unwind(node)
            if ref is not None:
                found.add(ref)
                for child in dynamic_children:
                    self._collect_refs(child, found)
                return
        elif isinstance(node, nodes.Call) and isinstance(node.node, nodes.Getattr | nodes.Getitem):
            # a method call: `steps.a.output.strip()` refers to steps.a.output, not `.strip`
            ref, dynamic_children = _unwind(node.node.node)
            if ref is not None:
                found.add(ref)
                for child in dynamic_children:
                    self._collect_refs(child, found)
                for child in node.iter_child_nodes(exclude=("node",)):
                    self._collect_refs(child, found)
                return
        elif isinstance(node, nodes.Name) and node.ctx == "load" and node.name in REFERENCE_ROOTS:
            found.add(Ref(node.name, None, ()))
            return
        for child in node.iter_child_nodes():
            self._collect_refs(child, found)


#: Block constructs whose captured output would be finalized a second time (or filtered as
#: text) in the shell/python environments — rejected at compile time for those kinds.
_REFINALIZING_NODES: tuple[tuple[type[nodes.Node], str, str], ...] = (
    (nodes.Macro, "macro", "{% macro %}"),
    (nodes.CallBlock, "call block", "{% call %}"),
    (nodes.FilterBlock, "filter block", "{% filter %}"),
    (nodes.AssignBlock, "set block", "{% set x %}...{% endset %}"),
)


def _reject_refinalizing_constructs(tree: nodes.Template, kind: TemplateKind) -> None:
    """Raise ``TemplateSyntaxError`` for constructs that re-finalize rendered text in code bodies.

    In the shell/python environments every ``{{ expr }}`` is replaced by a placeholder
    (``${RAYSPEC_V<n>}`` / a Python literal). A macro, ``{% call %}``, ``{% filter %}`` block or
    ``{% set x %}…{% endset %}`` captures that already-substituted text and either feeds it
    through the finalizer again (``echo ${RAYSPEC_V2}`` with ``V2='${RAYSPEC_V1}'`` — bash prints
    the literal string) or mangles the placeholder with a filter (``${rayspec_v1}``). Both are
    silent wrong output, so they are rejected with a message naming the fix.
    """
    worst: tuple[int, str, str] | None = None
    for node_type, label, tag in _REFINALIZING_NODES:
        for node in tree.find_all(node_type):
            if worst is None or node.lineno < worst[0]:
                worst = (node.lineno, label, tag)
    if worst is None:
        return
    lineno, label, tag = worst
    raise TemplateSyntaxError(
        f"{label} ({tag}) is not supported in {kind} bodies: its output would be substituted "
        "twice (the {{ }} placeholder rule is applied to already substituted text); use "
        "`{% set x = expr %}` and inline filters (`{{ x | lower }}`) instead",
        lineno,
    )


def _unwind(node: nodes.Node) -> tuple[Ref | None, list[nodes.Node]]:
    """Turn a ``Getattr``/``Getitem`` chain into a :class:`Ref` (or None if not rooted)."""
    segments: list[str | None] = []
    dynamic: list[nodes.Node] = []
    current: nodes.Node = node
    while True:
        if isinstance(current, nodes.Getattr):
            segments.append(current.attr)
            current = current.node
        elif isinstance(current, nodes.Getitem):
            arg = current.arg
            if isinstance(arg, nodes.Const) and isinstance(arg.value, str):
                segments.append(arg.value)
            else:
                segments.append(None)
                dynamic.append(arg)
            current = current.node
        else:
            break
    if not (isinstance(current, nodes.Name) and current.ctx == "load"):
        return None, []
    if current.name not in REFERENCE_ROOTS:
        return None, []
    segments.reverse()
    name = segments[0] if segments else None
    if name is None:
        return Ref(current.name, None, ()), dynamic
    attrs: list[str] = []
    for seg in segments[1:]:
        if seg is None:
            break
        attrs.append(seg)
    return Ref(current.name, name, tuple(attrs)), dynamic


@contextmanager
def _render_guard() -> Iterator[None]:
    """Convert everything raised while rendering into :class:`TemplateRenderError`."""
    try:
        yield
    except (TemplateRenderError, TemplateCompileError):
        raise
    except UndefinedError as exc:
        raise TemplateRenderError(
            str(exc), hint="use | default(...) or guard with `is defined`"
        ) from exc
    except TemplateSyntaxError as exc:
        raise TemplateRenderError(f"syntax error: {exc.message} (line {exc.lineno})") from exc
    except SecurityError as exc:
        raise TemplateRenderError(f"sandbox: {exc}") from exc
    except TemplateError as exc:
        raise TemplateRenderError(str(exc)) from exc
    except Exception as exc:
        raise TemplateRenderError(f"{type(exc).__name__}: {exc}") from exc


__all__ = [
    "REFERENCE_ROOTS",
    "SPILL_THRESHOLD",
    "Ref",
    "ReferenceKind",
    "RenderedScript",
    "TemplateEngine",
    "TemplateKind",
    "jsonable_for_tojson",
    "stringify_text",
]

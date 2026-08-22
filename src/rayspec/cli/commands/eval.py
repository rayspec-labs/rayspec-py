# SPDX-License-Identifier: Apache-2.0
"""`rayspec eval <run> '<expr>' [--step PATH] [--shell] [--json]` — expressions over a run.

A read-only Jinja prompt over a *stored* run: the expression is evaluated in exactly the lexical
scope the chosen step had (``iteration.prev`` resolves inside a loop body, an ``each`` item is
bound to its ``as:`` name, an include body sees only its own inputs), with the same
:class:`~rayspec.templating.RayspecUndefined` hints the engine raises. Writing a ``when:`` or
``until:`` stops being trial and error.

The command never starts a step, never opens a provider and never writes to the store — it reads
``run.json`` and the step outputs through :mod:`rayspec.engine.context_rebuild`.

Module boundary: presentation only. The value formatting helpers (:func:`format_value`,
:func:`value_type`, :func:`echo_block`, :func:`print_warning`) are shared with ``explain.py``
and ``plan.py``; every one of them prints untrusted text as :class:`rich.text.Text`, never as
Rich markup.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    console,
    fail,
    print_json,
    resolve_output,
)
from rayspec.engine import context_rebuild
from rayspec.errors import RayspecError
from rayspec.templating import RenderedScript, TemplateEngine, to_jsonable
from rayspec.textsafe import safe_text

#: How the ``--json`` envelope names a value's shape.
_TYPES: tuple[tuple[type | tuple[type, ...], str], ...] = (
    (bool, "boolean"),
    (str, "string"),
    ((int, float), "number"),
    (dict, "mapping"),
    ((list, tuple), "list"),
)


def value_type(value: Any) -> str:
    """``string`` · ``number`` · ``boolean`` · ``mapping`` · ``list`` · ``null`` · ``other``."""
    if value is None:
        return "null"
    for types, name in _TYPES:
        if isinstance(value, types):
            return name
    return "other"


def format_value(value: Any) -> str:
    """A value as a human would want it on stdout: text as-is, everything else as JSON.

    ``true``/``false``/``null`` for the scalars that have no text form, numbers as their literal,
    mappings and lists pretty-printed (indent 2) so ``rayspec eval`` output can be piped.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(to_jsonable(value), ensure_ascii=False, indent=2, default=str)


def echo_block(text: str) -> None:
    """Print untrusted multi-line text verbatim — no Rich markup, no escape sequences."""
    typer.echo(safe_text(text))


def print_warning(out: Console, message: str) -> None:
    """``warning: <message>`` — the message is untrusted text, never Rich markup.

    Shared with ``explain.py``/``plan.py``: a warning quotes step output, an ``each:``
    expression or an exception message, any of which may contain ``[/bold]``-looking text that
    Rich would refuse to parse.
    """
    out.print(Text.assemble(("warning:", "yellow"), " ", safe_text(message, keep_newlines=False)))


def register(app: typer.Typer) -> None:
    @app.command(name="eval")
    def eval_(  # noqa: PLR0917 - Typer options are positional by construction
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        expression: Annotated[
            str, typer.Argument(metavar="EXPR", help="Jinja expression, e.g. 'steps.a.ok'.")
        ],
        step: Annotated[
            str | None,
            typer.Option(
                "--step",
                help="Evaluate in this step's scope (record path, e.g. build[2]/implement).",
                show_default=False,
            ),
        ] = None,
        shell: Annotated[
            bool,
            typer.Option(
                "--shell", help="Render as a shell: body — show the ${RAYSPEC_V<n>} slot."
            ),
        ] = False,
        root: RootOption = None,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """Evaluate a Jinja expression in a stored run's context (read-only)."""
        json_ = resolve_output(output, json_)
        ctx = common.make_runs_context(root)
        store, record = common.lookup_run(ctx, run)
        try:
            resolved = common.load_resolved_for(ctx, record)
        except RayspecError as exc:
            fail(f"cannot load the workflow of run {record.run_id}: {exc}", hint=exc.hint)
            return
        engine = TemplateEngine()
        rebuilder = context_rebuild.from_run(record, resolved, store=store, engine=engine)
        try:
            rebuilt = rebuilder.at(step)
        except context_rebuild.ContextRebuildError as exc:
            fail(str(exc), hint=exc.hint)
            return
        stale = context_rebuild.stale_workflow_warning(record, resolved)
        # the argument is an expression in both modes (``--shell`` wraps it in ``{{ }}``)
        local_env = context_rebuild.env_reference_warning(engine, [(expression, "expr")])
        warnings = [
            *([stale] if stale is not None else []),
            *rebuilt.warnings,
            *([local_env] if local_env is not None else []),
        ]
        out = console()
        rendered: RenderedScript | None = None
        value: Any = None
        try:
            if shell:
                rendered = context_rebuild.render_script(
                    engine, f"{{{{ {expression} }}}}", rebuilt.context, kind="shell"
                )
            else:
                value = engine.eval_expr(expression, rebuilt.context)
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return
        if json_:
            payload: dict[str, Any] = {
                "run_id": record.run_id,
                "step": str(rebuilt.record_path) if step else None,
                "expr": expression,
                "warnings": warnings,
            }
            if rendered is not None:
                payload |= {"shell": rendered.script, "env": dict(rendered.env)}
            else:
                payload |= {"value": to_jsonable(value), "type": value_type(value)}
            print_json(payload)
            return
        if rendered is not None:
            echo_block(rendered.script)
            for name, text in rendered.env.items():
                out.print(
                    Text.assemble(
                        ("  ", ""),
                        (safe_text(name, keep_newlines=False), "dim"),
                        " = ",
                        safe_text(text, keep_newlines=False),
                    )
                )
        else:
            echo_block(format_value(value))
        for warning in warnings:
            print_warning(out, warning)

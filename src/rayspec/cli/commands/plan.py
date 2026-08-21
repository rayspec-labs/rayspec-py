# SPDX-License-Identifier: Apache-2.0
"""`rayspec plan <workflow> [--render] [--json]` — what a run would do, before it spends a token.

Two views over the same loaded workflow:

* the default plan — resolved inputs, agents, step order and the capability report;
* ``--risk`` — a static report of what the run would be *allowed* to do: agents that can leave
  the workspace, bodies that push/merge/delete/fetch, MCP servers, steps that work outside the
  workspace and gates anything could waive. The analysis is :mod:`rayspec.risk`, which reads
  the resolved workflow and runs nothing;
* ``--render`` — the prompt bodies and shell/python scripts *as the agent would receive
  them*, with upstream values taken from a ``--stubs`` script (or a visible ``<path output>``
  placeholder) and every ``${RAYSPEC_V<n>}`` slot printed next to its value. ``--step`` narrows
  it to one step. The scope rebuild is :mod:`rayspec.engine.context_rebuild` — the same helper
  ``rayspec explain`` and ``rayspec eval`` use, so a preview and a post-mortem agree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from rayspec import risk as risk_report
from rayspec.cli.commands import _loader_common as common
from rayspec.cli.commands import _pricing_common as pricing
from rayspec.cli.commands._loader_common import (
    AllowUnsupportedOption,
    JsonOption,
    OutputOption,
    RootOption,
    console,
    error_lines,
    fail,
    make_context,
    report_lines,
    resolve_output,
)
from rayspec.cli.commands.eval import echo_block
from rayspec.config import Config
from rayspec.engine import context_rebuild
from rayspec.engine.approval_classes import ApprovalClasses, ClassRules
from rayspec.errors import InputError, RayspecError
from rayspec.loader import ResolvedWorkflow, load_workflow, resolve_inputs, validate_workflow
from rayspec.loader.inputs import SECRET_PLACEHOLDER
from rayspec.loader.validate import topological_order
from rayspec.schema import (
    ApproveStep,
    EachStep,
    IncludeStep,
    LoopStep,
    PromptStep,
    PythonStep,
    ShellStep,
    StepModel,
    StopStep,
)
from rayspec.textsafe import safe_markup

if TYPE_CHECKING:  # a type-only import: the CLI loads the stub script lazily
    from rayspec.providers.stub import StubScript


def _fmt_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _step_detail(step: StepModel, rw: ResolvedWorkflow, path: str) -> str:
    if isinstance(step, PromptStep):
        agent = rw.agents.get(rw.step_agents.get(path, ""))
        extra = f"agent={agent.name}" if agent else ""
        if step.session:
            extra += f" session={step.session}"
        if step.output_schema is not None:
            extra += " structured"
        return extra.strip()
    if isinstance(step, LoopStep):
        return f"max_iterations={step.loop.max_iterations}" + (" until" if step.loop.until else "")
    if isinstance(step, EachStep):
        return f"each={step.each} as={step.as_}"
    if isinstance(step, IncludeStep):
        body = rw.includes.get(path)
        return f"include={body.workflow_name if body else step.include}"
    if isinstance(step, ApproveStep):
        return f"on_reject={step.approve.on_reject}"
    if isinstance(step, StopStep):
        return f"status={step.stop.status}"
    return ""


def _caps_line(rw: ResolvedWorkflow) -> str:
    """``  budget_usd $1.50  max_tokens 500,000`` for the run-level caps that are set."""
    defaults = rw.workflow.defaults
    parts: list[str] = []
    if defaults.budget_usd is not None:
        parts.append(f"budget_usd ${defaults.budget_usd:.2f}")
    if defaults.max_tokens is not None:
        parts.append(f"max_tokens {defaults.max_tokens:,}")
    return "".join(f"  {part}" for part in parts)


def _steps_table(rw: ResolvedWorkflow) -> Table:
    table = Table(show_edge=False, pad_edge=False)
    for col in ("#", "path", "kind", "needs", "join", "when", "detail"):
        table.add_column(col)
    bodies = {g.prefix: g for g in rw.graphs()}
    counter = 0

    def add(prefix: str, steps: list[StepModel], depth: int) -> None:
        nonlocal counter
        for step in topological_order(steps):
            counter += 1
            path = f"{prefix}{step.id}"
            table.add_row(
                str(counter),
                "  " * depth + path,
                type(step).kind,
                ", ".join(step.needs),
                step.join if step.join != "all" or step.needs else "",
                step.when or "",
                _step_detail(step, rw, path),
            )
            body = bodies.get(f"{path}/")
            if body is not None:
                add(f"{path}/", list(body.steps), depth + 1)

    add("", list(rw.workflow.steps), 0)
    return table


def _agents_table(rw: ResolvedWorkflow) -> Table:
    table = Table(show_edge=False, pad_edge=False)
    for col in ("agent", "provider", "model", "effort", "access", "used by", "source"):
        table.add_column(col, style="bold" if col == "agent" else None)
    used: dict[str, list[str]] = {}
    for path, key in rw.step_agents.items():
        used.setdefault(key, []).append(path)
    for key, agent in rw.agents.items():
        model = agent.model or "[dim](provider default)[/dim]"
        if agent.raw_model and agent.raw_model != agent.model:
            model = f"{model} [dim]({agent.raw_model})[/dim]"
        table.add_row(
            agent.name,
            agent.provider,
            model,
            agent.effort or "",
            agent.access,
            ", ".join(used.get(key, [])),
            agent.source,
        )
    return table


def _provider_report(
    rw: ResolvedWorkflow, caps: common.CapabilitySource, config: Config
) -> dict[str, dict[str, Any]]:
    """Per resolved provider: structured-output mode and where cost comes from.

    ``cost`` is ``provider`` (the provider reports USD), ``table`` (every model the workflow's
    agents resolve to has a pricing entry → ``~$`` estimates) or ``none`` (at least one model is
    unpriced or disabled → tokens only; ``priced_models`` / ``unpriced_models`` /
    ``disabled_models`` (``null`` entries) say which). Agents whose model is ``None`` (unknown
    tier — the loader already warns) are skipped, so a provider whose agents all lack a model
    reports ``none`` with empty lists. ``pricing_error`` carries a malformed ``pricing:`` table's
    message (a broken global table does not hide per-provider prices). Providers the registry
    does not know are skipped.
    """
    if caps.capabilities_for is None:
        return {}
    report: dict[str, dict[str, Any]] = {}
    for provider in sorted({a.provider for a in rw.agents.values()}):
        provider_caps = caps.capabilities_for(provider)
        if provider_caps is None:
            continue
        entry: dict[str, Any] = {
            "structured_output": provider_caps.structured_output,
            "cost_reporting": provider_caps.cost_reporting,
            "cost": "provider",
            "priced_models": [],
            "unpriced_models": [],
            "disabled_models": [],
        }
        if not provider_caps.cost_reporting:
            models = [a.model for a in rw.agents.values() if a.provider == provider and a.model]
            coverage = pricing.pricing_coverage(config, provider, models)
            entry["cost"] = "table" if coverage.complete else "none"
            entry["priced_models"] = list(coverage.priced)
            entry["unpriced_models"] = list(coverage.unpriced)
            entry["disabled_models"] = list(coverage.disabled)
            if coverage.error is not None:
                entry["pricing_error"] = coverage.error
        report[provider] = entry
    return report


def _cost_line(entry: dict[str, Any]) -> str:
    """Human form of one ``_provider_report`` entry's cost source (with the pricing nudge)."""
    if entry["cost"] == "provider":
        return "reported by the provider"
    coverage = pricing.PricingCoverage(
        priced=entry["priced_models"],
        unpriced=entry["unpriced_models"],
        disabled=entry["disabled_models"],
        error=entry.get("pricing_error"),
    )
    return pricing.describe(coverage)


def _input_rows(
    rw: ResolvedWorkflow, values: dict[str, Any], exc: InputError | None, cli_pairs: list[str]
) -> list[dict[str, Any]]:
    """One row per declared input: ``{name, type, value, state, problem, secret}``.

    ``state`` is ``ok`` (value resolved), ``missing`` (required, not given), ``invalid`` (given
    but rejected — ``problem`` names why) or ``undefined`` (optional without a default). A
    ``secret: true`` input has ``secret: true`` and its ``value`` is ``"<secret>"``.
    """
    raw_cli: dict[str, str] = {}
    for pair in cli_pairs:
        name, _, raw = pair.partition("=")
        raw_cli.setdefault(name, raw)
    resolved = dict(exc.partial) if exc is not None else values
    problems = dict(exc.problems) if exc is not None else {}
    rows: list[dict[str, Any]] = []
    for name, spec in rw.workflow.inputs.items():
        row: dict[str, Any] = {
            "name": name,
            "type": spec.type,
            "value": None,
            "problem": None,
            "secret": spec.secret,
        }
        if name in resolved:
            # a secret value is never printed — plan/--json show the placeholder
            row["value"] = SECRET_PLACEHOLDER if spec.secret else resolved[name]
            row["state"] = "ok"
        elif name in problems:
            messages = problems[name]
            if messages == ["missing (required)"]:
                row["state"] = "missing"
            else:
                row["state"] = "invalid"
                # the rejected raw value of a secret is not printed either
                raw = raw_cli.get(name)
                row["raw"] = SECRET_PLACEHOLDER if spec.secret and raw is not None else raw
                row["problem"] = "; ".join(_strip_prefix(m, name) for m in messages)
        elif spec.required:
            row["state"] = "missing"
        else:
            row["state"] = "undefined"
        rows.append(row)
    return rows


def _strip_prefix(message: str, name: str) -> str:
    prefix = f"input {name!r}: "
    return message[len(prefix) :] if message.startswith(prefix) else message


def _print_input_row(out: Any, row: dict[str, Any]) -> None:
    name, kind = row["name"], row["type"]
    if row.get("secret"):
        kind = f"{kind}, secret"  # marked (secret); the value itself is "<secret>"
    state = row["state"]
    if state == "ok":
        value = escape(_fmt_value(row["value"]))
        out.print(f"  {name} = {value}  [dim]({kind})[/dim]", highlight=False)
    elif state == "invalid":
        raw = f"{row['raw']!r}" if row.get("raw") is not None else "given"
        problem = escape(str(row["problem"]))
        out.print(
            f"  {name} = [red]{escape(raw)} (invalid: {problem})[/red]  [dim]({kind})[/dim]",
            highlight=False,
        )
    elif state == "missing":
        out.print(f"  {name} = [red]missing (required)[/red]  [dim]({kind})[/dim]")
    else:
        out.print(f"  {name} = [dim]undefined[/dim]  [dim]({kind})[/dim]")


#: What a leaf step's body is rendered with (``prompt`` uses the text environment).
_BODY_KINDS: dict[type, str] = {PromptStep: "prompt", ShellStep: "shell", PythonStep: "python"}


def body_of(step: StepModel, rw: ResolvedWorkflow, def_path: str) -> tuple[str, str] | None:
    """``(kind, template)`` of a leaf step's body — ``None`` for composites and gates."""
    kind = _BODY_KINDS.get(type(step))
    if kind is None:
        return None
    if isinstance(step, PromptStep):
        return kind, rw.prompt_text(def_path) or ""
    if isinstance(step, ShellStep):
        return kind, step.shell
    if isinstance(step, PythonStep):
        return kind, step.python
    return None  # pragma: no cover - _BODY_KINDS covers every leaf kind


def renderable_paths(rw: ResolvedWorkflow) -> list[str]:
    """Definition paths of every step that has a body to render (declaration order)."""
    return [path for path, step in rw.all_steps() if body_of(step, rw, path) is not None]


def render_rows(
    rw: ResolvedWorkflow,
    *,
    values: dict[str, Any],
    project_root: Path,
    script: StubScript | None,
    only: str | None,
) -> list[dict[str, Any]]:
    """One row per rendered step: ``{path, kind, agent, text, env, step_env, error, warnings}``.

    Upstream values come from ``script`` (a stub script) or from a visible placeholder; a
    ``secret: true`` input is redacted to ``"<secret>"`` before anything is rendered.
    Raises :class:`~rayspec.engine.context_rebuild.ContextRebuildError` when ``only`` names a
    step that does not exist or has no body.
    """
    rebuilder = context_rebuild.from_plan(
        rw, inputs=values, project_root=project_root, script=script
    )
    rows: list[dict[str, Any]] = []
    for path in [only] if only is not None else renderable_paths(rw):
        rebuilt = rebuilder.at(path)
        assert rebuilt.step is not None
        body = body_of(rebuilt.step, rw, rebuilt.def_path)
        if body is None:
            raise context_rebuild.ContextRebuildError(
                f"step {path!r} is a {type(rebuilt.step).kind} step and has no body to render",
                hint=f"renderable steps: {', '.join(renderable_paths(rw)) or '(none)'}",
            )
        kind, template = body
        rendered = context_rebuild.render_body(
            rebuilder.engine, template, rebuilt.context, kind=kind
        )
        agent = rw.agents.get(rw.step_agents.get(rebuilt.def_path, ""))
        rows.append(
            {
                "path": str(rebuilt.record_path),
                "def_path": rebuilt.def_path,
                "kind": kind,
                "agent": agent.name if agent is not None else None,
                "model": agent.model if agent is not None else None,
                "provider": agent.provider if agent is not None else None,
                "text": rendered.text,
                "env": rendered.env,
                "step_env": context_rebuild.render_step_env(
                    rebuilder.engine, rebuilt.step, rebuilt.context
                ),
                "error": rendered.error,
                "warnings": list(rebuilt.warnings),
            }
        )
    return rows


def print_render(out: Any, rows: list[dict[str, Any]], *, source: str) -> None:
    """The human ``--render`` view: one block per step, the body printed verbatim.

    Every interpolated value goes through :func:`~rayspec.textsafe.safe_markup` — a rendered
    body, a slot value or a warning may contain ``[/bold]``-looking text.
    """
    out.print(f"[dim]upstream values: {safe_markup(source)}[/dim]")
    for row in rows:
        head = f"\n[bold]{safe_markup(row['path'])}[/bold]  [dim]{safe_markup(row['kind'])}[/dim]"
        if row["agent"]:
            head += f"  [dim]agent {safe_markup(row['agent'])}"
            head += f" → {safe_markup(row['provider'] or '')}"
            head += f" {safe_markup(row['model'] or 'default')}[/dim]"
        out.print(head)
        if row["error"]:
            out.print(f"  [red]error:[/red] {safe_markup(row['error'], keep_newlines=False)}")
            continue
        echo_block(row["text"] or "")
        for name, value in row["env"].items():
            slot = safe_markup(value, keep_newlines=False)
            out.print(f"  [dim]slot[/dim] {safe_markup(name)} = {slot}")
        for name, value in row["step_env"].items():
            shown = safe_markup(value, keep_newlines=False)
            out.print(f"  [dim]env[/dim] {safe_markup(name)} = {shown}")
        for warning in row["warnings"]:
            out.print(f"  [yellow]warning:[/yellow] {safe_markup(warning, keep_newlines=False)}")


def policy_class_rules(project_root: Path, home: Path | None) -> dict[str, ClassRules]:
    """The operator's approval-class rules, so a gate the policy already holds shut is not
    reported as waivable.

    The seam itself lives with the run command (imported lazily: a plan should not pull the
    whole runner in to read a report).
    """
    from rayspec.cli.commands.run import policy_class_rules as impl

    return impl(project_root, home)


#: How each severity is coloured in the report.
_SEVERITY_STYLE = {"high": "red", "medium": "yellow", "low": "cyan"}


def print_risk(out: Any, rw: ResolvedWorkflow, findings: list[risk_report.Finding]) -> None:
    """The human ``--risk`` view: a count line, then one block per finding, worst first.

    Every interpolated value goes through :func:`~rayspec.textsafe.safe_markup`: a shell body is
    quoted verbatim as evidence and may contain anything at all.
    """
    out.print(f"[bold]risk report[/bold] {rw.workflow.name}  [dim]{safe_markup(rw.label)}[/dim]")
    if not findings:
        # a statement about the ANALYSIS, never about the workflow: an empty list means no rule
        # matched, which is not the same as "this workflow is safe"
        out.print(
            "  nothing matched: this report reads the workflow as written — a command a step "
            "assembles at run time, or anything an agent decides to do, is not covered"
        )
        return
    tally = risk_report.counts(findings)
    summary = " · ".join(f"{tally[name]} {name}" for name in risk_report.SEVERITIES)
    out.print(f"  {summary}")
    for finding in findings:
        style = _SEVERITY_STYLE.get(finding.severity, "white")
        out.print("")
        out.print(
            f"  [{style}]{finding.severity:<6}[/{style}] "
            f"[bold]{safe_markup(finding.where)}[/bold]  "
            f"[dim]{safe_markup(finding.category)}[/dim]"
        )
        out.print(f"         {safe_markup(finding.detail, keep_newlines=False)}", highlight=False)
        out.print(f"         [dim]→ {safe_markup(finding.advice, keep_newlines=False)}[/dim]")


def _load_stub_script(path: Path | None) -> StubScript | None:
    """Load a ``--stubs`` YAML script (usage error, exit 2, when it cannot be read/parsed)."""
    if path is None:
        return None
    from rayspec.providers.stub import StubScript, StubScriptError

    try:
        return StubScript.from_file(path)
    except (OSError, StubScriptError) as exc:
        fail(f"stubs file not usable: {exc}", hint="--stubs takes a YAML stub script")
        return None


def register(app: typer.Typer) -> None:
    @app.command()
    def plan(  # noqa: PLR0917 - Typer options are positional by construction
        workflow: Annotated[str, typer.Argument(help="Workflow name or path.")],
        inputs: Annotated[
            list[str] | None,
            typer.Option(
                "--input", "-i", help="Input as NAME=VALUE (repeatable).", show_default=False
            ),
        ] = None,
        inputs_file: Annotated[
            Path | None,
            typer.Option("--inputs-file", help="YAML/JSON file with inputs.", show_default=False),
        ] = None,
        render: Annotated[
            bool,
            typer.Option(
                "--render", help="Show the rendered prompt/script bodies instead of the plan."
            ),
        ] = False,
        risk: Annotated[
            bool,
            typer.Option(
                "--risk",
                help="Report what the run would be allowed to do (runs nothing).",
            ),
        ] = False,
        step: Annotated[
            str | None,
            typer.Option(
                "--step", help="With --render: render only this step path.", show_default=False
            ),
        ] = None,
        stubs: Annotated[
            Path | None,
            typer.Option(
                "--stubs",
                help="With --render: stub script supplying the upstream step outputs.",
                show_default=False,
            ),
        ] = None,
        root: RootOption = None,
        allow_unsupported: AllowUnsupportedOption = False,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """Show what a run would do: inputs, resolved agents, step order, capability report."""
        json_ = resolve_output(output, json_)
        if render and risk:
            fail(
                "--risk and --render are different views of the same workflow",
                hint="run them one at a time",
            )
        if (step is not None or stubs is not None) and not render:
            fail(
                "--step and --stubs only apply to --render",
                hint="add --render to preview the rendered bodies",
            )
        ctx = make_context(root)
        out = console()
        try:
            rw = load_workflow(
                workflow, project_root=ctx.project_root, home=ctx.home, config=ctx.config
            )
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return
        caps = common.capability_source()
        report = validate_workflow(
            rw,
            capabilities_for=caps.capabilities_for,
            template_checker=common.template_checker(),
            on_unsupported="warn" if allow_unsupported else "error",
            provider_ids=caps.provider_ids,
        )
        input_errors: list[str] = []
        input_exc: InputError | None = None
        try:
            values = resolve_inputs(rw.workflow, cli_pairs=inputs or [], inputs_file=inputs_file)
        except InputError as exc:
            values = {}
            input_errors = list(exc.errors)
            input_exc = exc
        input_rows = _input_rows(rw, values, input_exc, inputs or [])
        providers_report = _provider_report(rw, caps, ctx.config)
        warnings = [*rw.warnings, *report.warnings]
        if caps.warning:
            warnings.append(caps.warning)
        findings: list[risk_report.Finding] = []
        if risk:
            findings = risk_report.analyse(
                rw,
                classes=ApprovalClasses(rules=policy_class_rules(ctx.project_root, ctx.home)),
            )
        rendered: list[dict[str, Any]] = []
        if render:
            script = _load_stub_script(stubs)
            try:
                rendered = render_rows(
                    rw,
                    values=values,
                    project_root=ctx.project_root,
                    script=script,
                    only=step,
                )
            except RayspecError as exc:
                fail(str(exc), hint=exc.hint)
                return

        if json_:
            payload = {
                "workflow": rw.workflow.name,
                "path": rw.label,
                "hash": rw.hash,
                "isolation": rw.workflow.isolation,
                "budget_usd": rw.workflow.defaults.budget_usd,
                "max_tokens": rw.workflow.defaults.max_tokens,
                "description": rw.workflow.description,
                "inputs": {row["name"]: row for row in input_rows},
                "input_errors": input_errors,
                "agents": [
                    {
                        "name": a.name,
                        "provider": a.provider,
                        "model": a.model,
                        "effort": a.effort,
                        "access": a.access,
                        "used_by": [p for p, k in rw.step_agents.items() if k == key],
                        "source": a.source,
                    }
                    for key, a in rw.agents.items()
                ],
                "steps": _steps_json(rw),
                "providers": providers_report,
                "errors": list(report.errors),
                "warnings": warnings,
                "unsupported": len(report.unsupported),
            }
            if render:
                payload["render"] = rendered
                payload["stubs"] = str(stubs) if stubs is not None else None
            if risk:
                payload["risk"] = risk_report.to_json(findings)
            out.print(json.dumps(payload, ensure_ascii=False), markup=False, highlight=False)
            if report.errors or input_errors:
                raise typer.Exit(code=2)
            return

        if risk:
            print_risk(out, rw, findings)
            report_lines("warnings:", warnings, style="yellow", printer=out.print)
            if report.errors:
                error_lines(report.errors)
            if input_errors:
                error_lines(input_errors)
            if report.errors or input_errors:
                raise typer.Exit(code=2)
            return
        if render:
            out.print(f"[bold]workflow[/bold] {rw.workflow.name}  [dim]{rw.label}[/dim]")
            print_render(
                out,
                rendered,
                source=f"--stubs {stubs}" if stubs is not None else "placeholders (no --stubs)",
            )
            report_lines("warnings:", warnings, style="yellow", printer=out.print)
            if report.errors:
                error_lines(report.errors)
            if input_errors:
                error_lines(input_errors)
            if report.errors or input_errors:
                raise typer.Exit(code=2)
            return
        out.print(f"[bold]workflow[/bold] {rw.workflow.name}  [dim]{rw.label}[/dim]")
        if rw.workflow.description:
            out.print(f"  {rw.workflow.description}")
        out.print(f"  hash {rw.hash[:12]}  isolation {rw.workflow.isolation}{_caps_line(rw)}")
        out.print("")
        out.print("[bold]inputs[/bold]")
        if not rw.workflow.inputs:
            out.print("  (none declared)")
        for row in input_rows:
            _print_input_row(out, row)
        out.print("")
        out.print("[bold]agents[/bold]")
        if rw.agents:
            out.print(_agents_table(rw))
        else:
            out.print("  (no prompt steps)")
        out.print("")
        out.print("[bold]steps[/bold] (topological order; bodies indented)")
        out.print(_steps_table(rw))
        out.print("")
        out.print("[bold]capability report[/bold]")
        if report.unsupported:
            # entries land in report.errors or report.warnings (defaults.on_unsupported / flag)
            downgraded = str(report.unsupported[0]) in report.warnings
            verb = "warning(s)" if downgraded else "error(s)"
            out.print(f"  {len(report.unsupported)} unsupported feature {verb}")
        elif caps.capabilities_for is not None:
            out.print("  ok: every feature is supported by its resolved provider")
        for provider, entry in providers_report.items():
            out.print(
                f"  provider {provider}: structured output {entry['structured_output']}, "
                f"cost {_cost_line(entry)}",
                markup=False,
                highlight=False,
            )
        report_lines("warnings:", warnings, style="yellow", printer=out.print)
        if report.errors:
            error_lines(report.errors)
        if input_errors:
            error_lines(input_errors)
        if report.errors or input_errors:
            raise typer.Exit(code=2)


def _steps_json(rw: ResolvedWorkflow) -> list[dict[str, Any]]:
    """The ``steps`` list of ``plan --json`` (topological order, bodies after their composite)."""
    bodies = {g.prefix: g for g in rw.graphs()}
    rows: list[dict[str, Any]] = []

    def add(prefix: str, steps: list[StepModel], depth: int) -> None:
        for step in topological_order(steps):
            path = f"{prefix}{step.id}"
            rows.append(
                {
                    "path": path,
                    "kind": type(step).kind,
                    "needs": list(step.needs),
                    "join": step.join,
                    "when": step.when,
                    "depth": depth,
                    "detail": _step_detail(step, rw, path),
                }
            )
            body = bodies.get(f"{path}/")
            if body is not None:
                add(f"{path}/", list(body.steps), depth + 1)

    add("", list(rw.workflow.steps), 0)
    return rows

# SPDX-License-Identifier: Apache-2.0
"""`rayspec explain <run> <step> [--full] [--json]` — why did this step run, skip or fail.

One screen for the two questions every debugging session starts with — *why was this step
skipped?* and *what did the agent actually receive?* — assembled from what the run already
stored, without re-running anything:

* **status** — final status, ``skip_reason``, tolerated failure, error, timings, tokens/cost;
* **join** — every ``needs`` with its terminal outcome and the decision the join table made;
* **when** — the expression, its value re-evaluated in the step's own scope, and each operand
  with the value it had;
* **retries** — the ``step.retry`` events of the run (attempt, delay, error);
* **agent** — the resolved agent *after* the merge, next to the provider/model actually recorded;
* **env / rendered** — the env slots and the persisted ``prompt:`` body (from
  ``steps/<path>/prompt.txt``; the agent's rendered ``instructions`` are not persisted and are
  not shown) or the rendered shell/python script with its ``${RAYSPEC_V<n>}`` slots.

Module boundary: presentation over :mod:`rayspec.engine.context_rebuild` (the scope rebuild),
:mod:`rayspec.cli._runs_common` (store lookup, formatting) and the run's own events. Read-only:
no step runs, no provider is created, nothing is written.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, cast

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
    resolve_output,
)
from rayspec.cli.commands.eval import echo_block, format_value, print_warning
from rayspec.cli.commands.plan import body_of
from rayspec.engine import context_rebuild
from rayspec.engine.context import (
    BUDGET_SKIP_REASON,
    CAP_KNOBS,
    cap_reasons,
    is_cap_reason,
    totals_of,
    utcnow,
)
from rayspec.engine.context_rebuild import RebuiltContext
from rayspec.engine.graph import classify, join_decision
from rayspec.engine.paths import StepPath
from rayspec.errors import RayspecError
from rayspec.loader import ResolvedWorkflow
from rayspec.providers.base import Usage
from rayspec.schema import PromptStep, StepModel
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord, StepRecord
from rayspec.templating import Ref, ReferenceKind, TemplateEngine, to_jsonable
from rayspec.textsafe import safe_text

#: Lines of a rendered prompt/script shown without ``--full``.
PREVIEW_LINES = 20
#: Characters of an operand value shown in the human ``when:`` block.
OPERAND_CHARS = 120
#: ``rendered.source`` when nothing was persisted and the body was rendered again just now.
RE_RENDERED = "re-rendered now"


# --------------------------------------------------------------------------------------------------
# sections (each returns plain JSON-able data; the printer below renders them)
# --------------------------------------------------------------------------------------------------


def status_section(record: StepRecord | None) -> dict[str, Any]:
    """Final status, skip reason, error, timings and totals of the step."""
    if record is None:
        return {"status": "not recorded", "attempts": 0}
    return {
        "status": record.status.value,
        "skip_reason": record.skip_reason,
        "tolerated": record.tolerated,
        "attempts": record.attempts,
        "error": record.error.model_dump() if record.error is not None else None,
        "exit_code": record.exit_code,
        "approved": record.approved,
        "duration_ms": record.duration_ms,
        "tokens": record.usage.total,
        "cost_usd": record.cost_usd,
        "cost_source": record.cost_source,
        "usage_unknown": record.usage_unknown,
    }


def cap_section(
    record: StepRecord | None, run: RunRecord, resolved: ResolvedWorkflow
) -> dict[str, Any] | None:
    """Which run-level cap skipped this step, or ``None`` when no cap did.

    ``skip_reason: budget_exceeded`` names the circuit *breaker*: ``defaults.budget_usd``,
    ``defaults.max_tokens`` and ``defaults.timeout_total`` are one breaker and share that one
    reason, so on its own it points a reader at money for a run that ran out of time. The run's
    own ``reason`` is the authority when a cap ended the run; otherwise (interrupted, paused, or
    a cap raised since) the caps are recomputed from what ``run.json`` already stores — the
    totals of its step records and the wall clock between ``started_at`` and ``ended_at``.
    """
    if record is None or record.skip_reason != BUDGET_SKIP_REASON:
        return None
    if is_cap_reason(run.reason):
        reason = run.reason or ""
        return {
            "reason": reason,
            "knobs": [knob for knob in CAP_KNOBS if knob.split(".", 1)[1] in reason],
            "source": "run.reason",
        }
    usage, cost_usd, cost_source = totals_of(list(run.steps.values()))
    elapsed_s: float | None = None
    if run.started_at is not None:
        end = run.ended_at or utcnow()
        elapsed_s = max(0.0, (end - run.started_at).total_seconds())
    breaches = cap_reasons(usage, cost_usd, cost_source, elapsed_s, resolved.workflow.defaults)
    if not breaches:
        return None
    return {
        "reason": "; ".join(breach.reason for breach in breaches),
        "knobs": [knob for breach in breaches for knob in breach.knobs],
        "source": "recomputed",
    }


def join_section(step: StepModel, run: RunRecord, path: StepPath) -> dict[str, Any]:
    """The ``needs`` row that decided the step: each need's outcome and the join verdict."""
    needs: list[dict[str, Any]] = []
    records: list[StepRecord] = []
    for need in step.needs:
        rec = run.steps.get(str(path.parent.child(need)))
        outcome = "not recorded"
        if rec is not None:
            records.append(rec)
            try:
                outcome = classify(rec)
            except ValueError:  # a need that never reached a terminal status
                outcome = "unfinished"
        needs.append(
            {
                "step": need,
                "status": rec.status.value if rec is not None else None,
                "counts_as": outcome,
                "skip_reason": rec.skip_reason if rec is not None else None,
                "tolerated": rec.tolerated if rec is not None else None,
            }
        )
    section: dict[str, Any] = {"join": step.join, "needs": needs, "decision": None}
    if step.needs and len(records) == len(step.needs):
        try:
            decision = join_decision(step, records, draining=False)
        except ValueError:
            return section
        section["decision"] = "run" if decision.run else "skip"
        section["skip_reason"] = decision.skip_reason
    elif not step.needs:
        section["decision"] = "run"
    return section


def reference_text(ref: Ref) -> str:
    """``steps.assess.output.verdict`` for a :class:`~rayspec.templating.Ref`."""
    parts = [ref.root, *([] if ref.name is None else [ref.name]), *ref.attr_path]
    return ".".join(parts)


def when_section(
    step: StepModel, rebuilt: RebuiltContext, engine: TemplateEngine
) -> dict[str, Any] | None:
    """The ``when:`` expression re-evaluated in the step's own scope, plus every operand."""
    if step.when is None:
        return None
    section: dict[str, Any] = {"expression": step.when, "value": None, "error": None}
    try:
        section["value"] = engine.eval_bool(step.when, rebuilt.context)
    except RayspecError as exc:
        section["error"] = str(exc)
    operands: list[dict[str, Any]] = []
    try:
        refs = sorted(engine.references(step.when, kind="expr"), key=reference_text)
    except RayspecError:
        refs = []
    for ref in refs:
        entry: dict[str, Any] = {"reference": reference_text(ref), "value": None, "error": None}
        try:
            entry["value"] = to_jsonable(engine.eval_expr(entry["reference"], rebuilt.context))
        except RayspecError as exc:
            entry["error"] = str(exc)
        operands.append(entry)
    section["operands"] = operands
    return section


def event_summary(
    store: FileRunStore, run: RunRecord, path: str
) -> tuple[list[dict[str, Any]], bool]:
    """One pass over ``events.jsonl``: this step's retries and whether its last finish replayed.

    ``events.jsonl`` can be multi-MB; both answers come from the same stream rather than one
    scan each.
    """
    attempts: list[dict[str, Any]] = []
    reused = False
    for event in store.read_events(run.run_id):
        if event.step_path != path:
            continue
        if event.type.value == "step.retry":
            error = event.data.get("error") or {}
            attempts.append(
                {
                    "attempt": event.data.get("attempt"),
                    "delay_s": event.data.get("delay_s"),
                    "error": f"{error.get('type', '?')}: {error.get('message', '')}".strip(),
                }
            )
        elif event.type.value == "step.finished":
            reused = bool(event.data.get("reused"))
    return attempts, reused


def retry_section(store: FileRunStore, run: RunRecord, path: str) -> list[dict[str, Any]]:
    """The ``step.retry`` events of this step (attempt, delay, the error that caused it)."""
    return event_summary(store, run, path)[0]


def was_reused(store: FileRunStore, run: RunRecord, path: str) -> bool:
    """Whether the last ``step.finished`` event of this step replayed a stored record."""
    return event_summary(store, run, path)[1]


def agent_section(
    resolved: ResolvedWorkflow, def_path: str, record: StepRecord | None
) -> dict[str, Any] | None:
    """The resolved agent after the merge, next to what the record actually used."""
    try:
        agent = resolved.agent_for(def_path)
    except KeyError:
        return None
    return {
        "name": agent.name,
        "provider": agent.provider,
        "model": agent.model,
        "raw_model": agent.raw_model,
        "effort": agent.effort,
        "access": agent.access,
        "instructions_mode": agent.instructions_mode,
        "max_turns": agent.max_turns,
        "tools": {"allow": list(agent.tools.allow), "deny": list(agent.tools.deny)},
        "source": agent.source,
        "recorded_provider": record.provider if record is not None else None,
        "recorded_model": record.model if record is not None else None,
        "session": record.session_ref.id
        if record is not None and record.session_ref is not None
        else None,
    }


def rendered_section(
    step: StepModel,
    rebuilt: RebuiltContext,
    engine: TemplateEngine,
    *,
    resolved: ResolvedWorkflow,
    record: StepRecord | None,
    store: FileRunStore,
    run: RunRecord,
) -> dict[str, Any] | None:
    """The persisted ``prompt:`` body, or the shell/python script rendered with its slots."""
    body = body_of(step, resolved, rebuilt.def_path)
    if body is None:
        return None
    kind, template = body
    if isinstance(step, PromptStep):
        ref = record.prompt_ref if record is not None else None
        text = context_rebuild.read_ref(store, run.run_id, ref)
        if text is not None:
            return {"kind": "prompt", "source": ref, "text": text, "env": {}}
    return _render_now(engine, template, rebuilt, kind=kind)


def reevaluated_texts(
    step: StepModel, resolved: ResolvedWorkflow, def_path: str, rendered: dict[str, Any] | None
) -> list[tuple[str, ReferenceKind]]:
    """The templates this explanation *re-evaluates* rather than replays, as ``(text, kind)``.

    A persisted prompt is replayed, so its body is not in the list: only what is computed now
    can disagree with what the run saw.
    """
    pairs: list[tuple[str, ReferenceKind]] = []
    if step.when:
        pairs.append((step.when, "expr"))
    for value in (getattr(step, "env", None) or {}).values():
        if isinstance(value, str):
            pairs.append((value, "text"))
    if rendered is not None and rendered.get("source") == RE_RENDERED:
        body = body_of(step, resolved, def_path)
        if body is not None:
            kind, template = body
            pairs.append((template, "text" if kind == "prompt" else cast(ReferenceKind, kind)))
    return pairs


def _render_now(
    engine: TemplateEngine, body: str, rebuilt: RebuiltContext, *, kind: str
) -> dict[str, Any]:
    """Render a body in the rebuilt context (the fallback when nothing was persisted)."""
    rendered = context_rebuild.render_body(engine, body, rebuilt.context, kind=kind)
    return {
        "kind": kind,
        "source": RE_RENDERED,
        "text": rendered.text,
        "env": rendered.env,
        "error": rendered.error,
    }


def env_section(step: StepModel, rebuilt: RebuiltContext, engine: TemplateEngine) -> dict[str, str]:
    """The step's own ``env:`` mapping, rendered (secret inputs stay ``"<secret>"``)."""
    return context_rebuild.render_step_env(engine, step, rebuilt.context)


# --------------------------------------------------------------------------------------------------
# printing
# --------------------------------------------------------------------------------------------------


def _line(out: Console, label: str, value: Any) -> None:
    """``  <dim label> <value>`` — the value is untrusted text, never Rich markup."""
    prefix = f"  {label} " if label else "  "
    out.print(Text.assemble((prefix, "dim"), safe_text(value, keep_newlines=False)))


def print_status(out: Console, payload: dict[str, Any]) -> None:
    status = payload
    style = common.status_style(str(status["status"]))
    head = Text.assemble(
        ("step ", "dim"),
        (safe_text(payload["step"], keep_newlines=False), "bold"),
        "  ",
        safe_text(payload["kind"], keep_newlines=False),
        "  ",
        (safe_text(status["status"], keep_newlines=False), style),
    )
    if status.get("tolerated"):
        head.append(" (tolerated)", style="dim")
    out.print(head)
    _line(out, "run", f"{payload['run_id']}  {payload['workflow']}")
    if payload.get("location"):
        _line(out, "defined at", payload["location"])
    if status.get("skip_reason"):
        _line(out, "skip reason", status["skip_reason"])
    if payload.get("cap"):
        _line(out, "cap", payload["cap"]["reason"])
    if status.get("error"):
        error = status["error"]
        _line(out, "error", f"{error.get('type')}: {error.get('message')}")
    parts: list[str] = []
    if status.get("attempts"):
        parts.append(f"attempts {status['attempts']}")
    if status.get("duration_ms"):
        parts.append(f"duration {common.fmt_duration(status['duration_ms'])}")
    if status.get("tokens"):
        parts.append(common.fmt_tokens(int(status["tokens"])))
    if status.get("cost_usd") is not None:
        parts.append(common.fmt_cost(status["cost_usd"], str(status["cost_source"]), Usage()))
    if parts:
        _line(out, "", "  ".join(parts))


def print_join(out: Console, join: dict[str, Any]) -> None:
    if not join["needs"] and join["join"] == "all":
        return
    out.print(f"\n[bold]join[/bold] [dim]{join['join']}[/dim]")
    for need in join["needs"]:
        text = f"{need['step']}: {need['status'] or 'not recorded'}"
        if need.get("skip_reason"):
            text += f" ({need['skip_reason']})"
        if need.get("counts_as") and need["counts_as"] != need["status"]:
            text += f" → counts as {need['counts_as']}"
        _line(out, "-", text)
    if join.get("decision"):
        verdict = join["decision"]
        if join.get("skip_reason"):
            verdict += f" ({join['skip_reason']})"
        _line(out, "decision", verdict)


def print_when(out: Console, when: dict[str, Any]) -> None:
    out.print("\n[bold]when[/bold]")
    out.print(Text(f"  {safe_text(when['expression'], keep_newlines=False)}"))
    if when.get("error"):
        _line(out, "→", f"error: {when['error']}")
    else:
        _line(out, "→", format_value(when["value"]))
    for operand in when.get("operands", []):
        value = operand["error"] or format_value(operand["value"])
        if len(value) > OPERAND_CHARS:
            value = value[:OPERAND_CHARS] + " …"
        _line(out, "-", f"{operand['reference']} = {value}")


def print_retries(out: Console, retries: list[dict[str, Any]]) -> None:
    if not retries:
        return
    out.print("\n[bold]retries[/bold]")
    for entry in retries:
        _line(
            out,
            "-",
            f"attempt {entry['attempt']} after {entry['delay_s']}s — {entry['error']}",
        )


def print_agent(out: Console, agent: dict[str, Any]) -> None:
    out.print(
        Text.assemble(
            ("\nagent", "bold"),
            (" (after merge) ", "dim"),
            safe_text(agent["name"], keep_newlines=False),
        )
    )
    line = f"provider {agent['provider']}  model {agent['model'] or '(provider default)'}"
    if agent.get("raw_model") and agent["raw_model"] != agent["model"]:
        line += f" ({agent['raw_model']})"
    if agent.get("effort"):
        line += f"  effort {agent['effort']}"
    line += f"  access {agent['access']}"
    _line(out, "", line)
    tools = agent["tools"]
    if tools["allow"] or tools["deny"]:
        allow = ", ".join(tools["allow"]) or "(any)"
        deny = ", ".join(tools["deny"]) or "(none)"
        _line(out, "tools", f"allow: {allow}  deny: {deny}")
    if agent.get("recorded_model") and agent["recorded_model"] != agent["model"]:
        _line(out, "recorded", f"{agent['recorded_provider']} {agent['recorded_model']}")
    if agent.get("session"):
        _line(out, "session", agent["session"])
    _line(out, "defined at", agent["source"])


def print_rendered(out: Console, rendered: dict[str, Any], *, full: bool) -> None:
    label = {"prompt": "prompt", "shell": "script", "python": "script"}[rendered["kind"]]
    source = safe_text(rendered["source"] or "not persisted", keep_newlines=False)
    out.print(Text.assemble((f"\n{label} ", "bold"), (f"({source})", "dim")))
    if rendered.get("error"):
        _line(out, "error", rendered["error"])
        return
    text = rendered["text"] or ""
    lines = text.splitlines()
    shown = lines if full else lines[:PREVIEW_LINES]
    if full:
        echo_block(text)
    else:
        for line in shown:
            out.print(Text(f"  {safe_text(line, keep_newlines=False)}"))
        if len(lines) > len(shown):
            out.print(f"  [dim]… {len(lines) - len(shown)} more line(s) — use --full[/dim]")
    for name, value in rendered["env"].items():
        _line(out, "slot", f"{name} = {safe_text(value, keep_newlines=False)}")


def print_explain(out: Console, payload: dict[str, Any], *, full: bool) -> None:
    """Render the whole human view (sections that have nothing to say are omitted)."""
    print_status(out, payload)
    print_join(out, payload["join"])
    if payload.get("when"):
        print_when(out, payload["when"])
    print_retries(out, payload["retries"])
    if payload.get("agent"):
        print_agent(out, payload["agent"])
    if payload["env"]:
        out.print("\n[bold]env[/bold]")
        for name, value in payload["env"].items():
            _line(out, "-", f"{name} = {value}")
    if payload.get("rendered"):
        print_rendered(out, payload["rendered"], full=full)
    out.print("")
    if payload.get("fingerprint"):
        _line(out, "fingerprint", f"{payload['fingerprint'][:12]}  reused: {payload['reused']}")
    if payload.get("output_ref"):
        _line(out, "output", f"{payload['output_ref']} ({payload.get('output_kind')})")
    for warning in payload["warnings"]:
        print_warning(out, warning)


def register(app: typer.Typer) -> None:
    @app.command()
    def explain(  # noqa: PLR0917 - Typer options are positional by construction
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        step: Annotated[str, typer.Argument(help="Step path, e.g. review or build[2]/implement.")],
        full: Annotated[
            bool, typer.Option("--full", help="Print the whole prompt/script, not a preview.")
        ] = False,
        root: RootOption = None,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """Explain why one step of a run ran, skipped or failed."""
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
        if rebuilt.step is None:
            fail("a step path is required", hint="e.g. rayspec explain <run> build[2]/implement")
            return
        payload = build_payload(rebuilt, resolved=resolved, run=record, store=store, engine=engine)
        stale = context_rebuild.stale_workflow_warning(record, resolved)
        if stale is not None:
            payload["warnings"].insert(0, stale)
        out = console()
        if json_:
            out.print(json.dumps(payload, ensure_ascii=False, default=str), markup=False)
            return
        print_explain(out, payload, full=full)


def build_payload(
    rebuilt: RebuiltContext,
    *,
    resolved: ResolvedWorkflow,
    run: RunRecord,
    store: FileRunStore,
    engine: TemplateEngine,
) -> dict[str, Any]:
    """Everything ``rayspec explain`` knows about one step, as JSON-able data."""
    step = rebuilt.step
    assert step is not None
    path = str(rebuilt.record_path)
    record = rebuilt.record
    retries, reused = event_summary(store, run, path)
    rendered = rendered_section(
        step, rebuilt, engine, resolved=resolved, record=record, store=store, run=run
    )
    payload: dict[str, Any] = {
        "run_id": run.run_id,
        "workflow": run.workflow_name,
        "step": path,
        "def_path": rebuilt.def_path,
        "kind": type(step).kind,
        "location": resolved.location_of(rebuilt.def_path),
        **status_section(record),
        "cap": cap_section(record, run, resolved),
        "join": join_section(step, run, rebuilt.record_path),
        "when": when_section(step, rebuilt, engine),
        "retries": retries,
        "agent": agent_section(resolved, rebuilt.def_path, record),
        "env": env_section(step, rebuilt, engine),
        "rendered": rendered,
        "fingerprint": record.fingerprint if record is not None else None,
        "reused": reused,
        "output_ref": record.output_ref if record is not None else None,
        "output_kind": record.output_kind if record is not None else None,
        "prompt_ref": record.prompt_ref if record is not None else None,
        "warnings": list(rebuilt.warnings),
    }
    env_warning = context_rebuild.env_reference_warning(
        engine, reevaluated_texts(step, resolved, rebuilt.def_path, rendered)
    )
    if env_warning is not None:
        payload["warnings"].append(env_warning)
    if record is None:
        payload["warnings"].append(
            f"step {path} has no record — the run never reached it; "
            "the sections below are re-evaluated, not what happened"
        )
    return payload


__all__ = [
    "OPERAND_CHARS",
    "PREVIEW_LINES",
    "RE_RENDERED",
    "agent_section",
    "build_payload",
    "cap_section",
    "env_section",
    "event_summary",
    "join_section",
    "print_explain",
    "reevaluated_texts",
    "reference_text",
    "rendered_section",
    "retry_section",
    "status_section",
    "was_reused",
    "when_section",
]

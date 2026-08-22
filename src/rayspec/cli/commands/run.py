# SPDX-License-Identifier: Apache-2.0
"""`rayspec run <workflow>` — load, validate, resolve inputs, prepare the workspace, run.

Thin command: all execution logic lives in :mod:`rayspec.engine`. The workspace module
(worktrees, ``--repo``) is a parallel scope and is imported lazily when available; otherwise
the run happens in place with a notice.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TextIO, TypeVar

import anyio
import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from rayspec.cli import _runs_common as runs_common
from rayspec.cli.commands import _loader_common as common
from rayspec.cli.commands._loader_common import (
    AllowUnsupportedOption,
    OutputOption,
    RootOption,
    error_lines,
    fail,
    make_context,
    report_lines,
    resolve_output,
)
from rayspec.cli.commands.lock import LockedOption, enforce_lockfile
from rayspec.config import ExtensionsSpec
from rayspec.engine.approval import (
    ApprovalAnswer,
    ApprovalPrompt,
    ApprovalRequest,
    ConsoleApprovalPrompt,
)
from rayspec.engine.approval_classes import ApprovalClasses, ClassRules, rules_from_policy
from rayspec.engine.context import RunOptions
from rayspec.engine.errors import EngineError
from rayspec.engine.runner import Runner, RunResult, Workspace, fallback_project_slug
from rayspec.engine.runtime import EXIT_USAGE
from rayspec.errors import InputError, RayspecError
from rayspec.limits import (
    OPERATIONAL_PAUSE_REASONS,
    SlotBusyError,
    acquire_slots,
    limits_for,
    limits_policy,
    run_envelope,
    wait_seconds,
    workflow_providers,
)
from rayspec.loader import ResolvedWorkflow, load_workflow, resolve_inputs, validate_workflow
from rayspec.loader.inputs import secret_input_names
from rayspec.policy import EffectivePolicy, load_policy
from rayspec.providers.pricing import PriceTable, cost_marker
from rayspec.redact import MIN_REDACTABLE_LEN, NULL_REDACTOR, RedactingSink, Redactor
from rayspec.schema import ApproveStep, PromptStep, RunStatus
from rayspec.secrets import (
    SecretError,
    build_redactor,
    provider_for,
    secret_input_overlay,
    used_config_secrets,
)
from rayspec.store.file import FileRunStore, StoreError
from rayspec.store.model import new_run_id
from rayspec.textsafe import safe_text

WaitSlotOption = Annotated[
    str | None,
    typer.Option(
        "--wait-slot",
        metavar="DURATION",
        help="Queue for a free host run slot instead of failing: a duration "
        "(--wait-slot 30m, --wait-slot 90) or `forever`. `0` and the default do not wait.",
        show_default=False,
    ),
]


def pause_actions(run_id: str, reason: str) -> str:
    """What to type next for a paused run — which is not the same thing for the two kinds.

    An ``approve:`` gate is a question, and ``approve`` / ``reject`` answer it. An operational
    ceiling is not a question: ``resume`` re-evaluates it (raise the ceiling, or wait for the
    next day) and ``approve`` WAIVES it for this run. ``reject`` changes nothing at all, so it
    is not offered. Leading with ``approve`` there would recommend that every CI script written
    against "paused ⇒ approve" quietly waive the ceiling an operator had just installed.
    """
    if reason in OPERATIONAL_PAUSE_REASONS:
        return (
            f"continue with: rayspec resume {run_id} (re-checks the ceiling) · "
            f"rayspec approve {run_id} [comment] (run it anyway, waiving the ceiling)"
        )
    return (
        f"decide with: rayspec approve {run_id} [comment] · "
        f"rayspec reject {run_id} [reason] · rayspec resume {run_id}"
    )


if TYPE_CHECKING:
    from rayspec.providers.stub import StubScript
    from rayspec.store.model import RunRecord


#: What :func:`_built` returns — the extension object a third-party factory produced.
T = TypeVar("T")


def _err(message: str) -> None:
    common.err_console().print(f"[yellow]{message}[/yellow]")


def _dry_run_notice(message: str) -> None:
    """A pure dry run never creates a worktree: the in-place notice is noise, the rest is not."""
    if "not a git repository" in message:
        return
    _err(message)


def non_stub_agents(rw: ResolvedWorkflow) -> str:
    """``"agent 'writer' (claude)"`` / ``"agents 'a' (claude), 'b (inline)' (codex)"`` for every
    resolved agent of a prompt step whose provider is not the stub — ``""`` when all are stub.
    Names are the user-facing ``ResolvedAgent.name`` (the ``agents:`` key, the provider
    of a bare ``agent: claude``, ``<step> (inline)``), de-duplicated — never the loader's
    internal keys (``agents.writer``, ``provider:claude``)."""
    seen: dict[tuple[str, str], None] = {}
    for agent_key in rw.step_agents.values():
        agent = rw.agents.get(agent_key)
        if agent is not None and agent.provider != "stub":
            seen.setdefault((agent.name, agent.provider), None)
    if not seen:
        return ""
    listed = ", ".join(f"{name!r} ({provider})" for name, provider in seen)
    return f"{'agent' if len(seen) == 1 else 'agents'} {listed}"


#: ``--approve-class <name>`` — pre-authorise ONE kind of gate for this invocation. Shared by
#: ``run`` and ``resume`` so the flag reads the same wherever a gate can be reached.
ApproveClassOption = Annotated[
    list[str] | None,
    typer.Option(
        "--approve-class",
        help="Pre-approve approval gates of this class (repeatable); other gates still ask. "
        "A class the policy marks allow_yes: false is never pre-approved.",
        show_default=False,
    ),
]


def operator_policy(project_root: Path, home: Path | None) -> EffectivePolicy | None:
    """Load the operator's policy — ``None`` only when the search found no file at all.

    The policy file — its keys, its layering (environment over project over user, most
    restrictive wins) and its loader — belongs to :mod:`rayspec.policy`. This calls the SAME
    loader every other consumer uses (:mod:`rayspec.limits`, the load-time checks), so an
    operator cannot end up with a file that caps their spending and is invisible to their gates.

    It is the one seam the CLI reads that policy through for approval purposes, and SIX commands
    are behind it: ``run`` and ``resume`` via :func:`approval_classes_for`; ``approve`` and
    ``reject`` through the same call, reached via
    :func:`~rayspec.cli._runs_common.resume_run`; ``test`` through it as well; and ``plan``
    through :func:`~rayspec.cli.commands.plan.policy_in_force`. Count them from the callers, not
    from memory — an enumeration that missed two commands is what left this seam returning
    ``None`` while the policy file it was supposed to read already existed.

    A file that exists but cannot be read RAISES :class:`~rayspec.policy.PolicyError` rather
    than returning ``None``: a guardrail that silently disappears is the one failure mode this
    seam may not have. So ``None`` means "searched and found nothing", never "found something
    and gave up on it" — and every caller has to stand inside a
    :class:`~rayspec.errors.RayspecError` boundary that turns the raise into ``error: …`` and
    exit 2. A caller without one answers a one-character typo in ``policy.yaml`` with a
    traceback, which reads as rayspec being broken rather than the file.
    """
    effective = load_policy(project_root, home=home)
    return None if effective.is_empty else effective


def policy_class_rules(project_root: Path, home: Path | None) -> dict[str, ClassRules]:
    """The approval-class rules of that policy — the only part of it a gate reads.

    ``rules_from_policy`` reads ``.classes`` off what it is handed, so it is handed the merged
    ``approvals:`` block rather than the whole document.
    """
    policy = operator_policy(project_root, home)
    return rules_from_policy(None if policy is None else policy.approvals)


def gate_classes(rw: ResolvedWorkflow) -> list[tuple[str, str | None]]:
    """``(step path, approval class)`` of every gate in a workflow, includes and bodies too."""
    return [
        (path, step.approve.class_)
        for path, step in rw.all_steps()
        if isinstance(step, ApproveStep)
    ]


def approval_classes_for(
    project_root: Path,
    home: Path | None,
    *,
    pre_approved: Sequence[str] = (),
    terminal_prompt: bool = True,
) -> ApprovalClasses:
    """The approval-class rules and pre-authorisations one invocation runs under.

    ``policy_loaded`` is asked separately from the rules because it is a separate question:
    "there is no operator policy" and "the policy in force says nothing about this class" have
    different fixes, and a run that printed the path of its policy file must not then report
    that it has none. It is not derived from the rules — a file holding only ``budget:`` is in
    force and defines no class.
    """
    return ApprovalClasses(
        rules=policy_class_rules(project_root, home),
        pre_approved=frozenset(pre_approved),
        terminal_prompt=terminal_prompt,
        policy_loaded=operator_policy(project_root, home) is not None,
    )


def decide_hint(run_id: str, class_name: str | None, classes: ApprovalClasses) -> str:
    """The ``decide with:`` line of a paused run.

    A class that requires a terminal refuses a decision recorded by ``rayspec approve`` /
    ``rayspec reject``, so a run held by one must not recommend them: pointing at the command
    the tool is about to refuse is how a control teaches people to work around it.
    """
    if class_name is not None and not classes.may_decide_out_of_band(class_name):
        return (
            f"  decide with: rayspec resume {run_id} from a terminal "
            f"(approval class {class_name!r} requires one)"
        )
    return (
        f"  decide with: rayspec approve {run_id} [comment] · "
        f"rayspec reject {run_id} [reason] · rayspec resume {run_id}"
    )


def paused_gate_class(rw: ResolvedWorkflow | None, step_path: str) -> str | None:
    """The approval class of the gate a run paused at (its record path carries iteration
    indices; a definition path does not)."""
    if rw is None:
        return None
    wanted = "/".join(segment.split("[")[0] for segment in step_path.split("/"))
    for path, name in gate_classes(rw):
        if path == wanted:
            return name
    return None


#: ``RunRecord.stubs_path`` prefix for a run launched with ``--stubs-from <run>``: there is
#: no file to point at, so the DONOR RUN is recorded instead (``run:<run id>``) and every resume
#: entry rebuilds the very same script from it. Without it a replay that pauses at an approval
#: gate would answer the remaining prompt steps with the stub provider's built-in default and
#: still report success.
REPLAY_REF_PREFIX = "run:"


def replay_ref(stubs_path: str | None) -> str | None:
    """The donor run id of a ``--stubs-from`` launch (``run:<id>``), ``None`` for a file path."""
    if stubs_path and stubs_path.startswith(REPLAY_REF_PREFIX):
        return stubs_path[len(REPLAY_REF_PREFIX) :]
    return None


def refuse_stubs_for_real_agents(
    rw: ResolvedWorkflow, *, dry_run: bool, record: RunRecord | None = None
) -> None:
    """``--stubs`` (given or recorded) drives a real run only when every resolved prompt
    agent is ``provider: stub``; otherwise exit 2 naming the agents that would run for real.

    With ``record`` the stubs come from that run's ``run.json`` (``stubs_path`` —
    a file path or ``run:<donor id>``) rather than from the command line, and the message says
    so — the user never passed ``--stubs``.
    """
    non_stub = non_stub_agents(rw)
    if dry_run or not non_stub:
        return
    if record is not None and record.stubs_path:
        donor = replay_ref(record.stubs_path)
        flag = "--stubs-from" if donor is not None else "--stubs"
        launched = f"--dry-run {flag}" if record.dry_run else flag
        source = donor if donor is not None else record.stubs_path
        subject = "recorded replay source" if donor is not None else "recorded stubs file"
        fail(
            f"run {record.run_id} was launched with {launched} {source}; its {subject} "
            f"requires --dry-run (stub scripts only drive the stub provider; "
            f"{non_stub} would run for real)",
            hint="pass --dry-run to resume it as a dry run (`rayspec resume` does so "
            "automatically), or switch the agents to provider: stub",
        )
    fail(
        "--stubs requires --dry-run (stub scripts only drive the stub provider; "
        f"{non_stub} would run for real)",
        hint="pass --dry-run, or switch the agents to provider: stub",
    )


def refuse_unmatched_expectations(rw: ResolvedWorkflow, script: StubScript) -> None:
    """Exit 2 when a ``steps:`` key carrying an ``expect:`` block names no prompt step of
    this workflow — a renamed or typo'd key asserts nothing and the run stays green anyway."""
    from rayspec.providers.stub import unmatched_expect_keys

    stale = unmatched_expect_keys(script, rw.step_agents)
    if not stale:
        return
    known = sorted(rw.step_agents)
    listed = ", ".join(repr(key) for key in stale)
    plural = "entries" if len(stale) > 1 else "entry"
    fail(
        f"stub script {plural} {listed} carry `expect:` but match no prompt step of workflow "
        f"{rw.workflow.name!r} — the assertion would never run",
        hint=f"prompt steps: {', '.join(known) if known else '(none)'}",
    )


def load_stub_script(path: Path, *, hint: str | None = None) -> StubScript:
    """Read a ``--stubs`` file into a ``StubScript``; a missing/unreadable/malformed file is a
    usage error (exit 2). ``hint`` replaces the default ``--stubs-init`` pointer (the resume
    entries point at ``--stubs <path>`` instead).

    A recorded ``run:<id>`` reference (a ``--stubs-from`` launch) is rebuilt from that run's
    store instead of read from disk, so ``resume``/``approve``/``reject`` replay the same answers
    the launching command did. A donor run that is gone is the usual lookup error (exit 2).
    """
    from rayspec.providers.stub import StubScript

    donor = replay_ref(str(path))
    if donor is not None:
        from rayspec.cli import _runs_common as runs_common

        return runs_common.replay_script(donor, root=None)

    try:
        return StubScript.from_file(path)
    except RayspecError as exc:
        fail(str(exc), hint=exc.hint or hint)
    except OSError as exc:  # missing path, directory, unreadable file → usage error
        fail(
            f"stubs file not readable: {path} ({exc.strerror or exc})",
            hint=hint or "--stubs takes a YAML stub script (see --stubs-init)",
        )
    raise AssertionError("unreachable")  # pragma: no cover


def failed_leaf_paths(result: RunResult) -> list[str]:
    """Paths of the failed/interrupted LEAF steps (prompt/shell/python — the ones with a stream
    ``rayspec logs --step`` can show), the step named in ``result.reason`` first."""
    leaves = [
        path
        for path, rec in result.steps.items()
        if rec.kind in {"prompt", "shell", "python"}
        and rec.status.value in {"failed", "interrupted"}
        and not rec.tolerated
    ]
    match = re.search(r"step '([^']+)'", result.reason or "")
    named = match.group(1) if match else None
    if named in leaves:
        leaves.remove(named)
        leaves.insert(0, named)
    return leaves


def stub_scaffold_keys(rw: ResolvedWorkflow) -> list[tuple[str, PromptStep]]:
    """``(stub key, step)`` for every prompt step, keyed the way the ENGINE names records:
    loop and each bodies as ``build[*]/implement`` globs (nested: ``outer[*]/inner[*]/x``),
    include bodies as ``block/step`` — the loader's definition paths (``build/implement``) never
    match a run-time record path and would be silently ignored."""
    globs: dict[str, str] = {"": ""}
    out: list[tuple[str, PromptStep]] = []
    for graph in rw.graphs():
        if graph.kind != "root" and graph.parent is not None:
            parent_id = graph.parent.id
            parent_def_prefix = graph.prefix[: len(graph.prefix) - len(parent_id) - 1]
            parent_glob = globs.get(parent_def_prefix, parent_def_prefix) + parent_id
            globs[graph.prefix] = parent_glob + ("[*]/" if graph.kind in {"loop", "each"} else "/")
        prefix = globs.get(graph.prefix, graph.prefix)
        for step in graph.steps:
            if isinstance(step, PromptStep):
                out.append((f"{prefix}{step.id}", step))
    return out


def stub_scaffold(rw: ResolvedWorkflow) -> dict[str, Any]:
    """``--stubs-init``: one entry per prompt step (minimal instance when ``output_schema``),
    keyed by :func:`stub_scaffold_keys`."""
    from rayspec.providers.stub import minimal_instance

    steps: dict[str, Any] = {}
    for key, step in stub_scaffold_keys(rw):
        if step.output_schema is not None:
            steps[key] = {"output": minimal_instance(step.output_schema)}
        else:
            steps[key] = {"text": f"[stub] {step.id}"}
    return {"steps": steps, "defaults": {"latency_ms": 0}}


def prepare_workspace(
    *,
    project_root: Path,
    home: Path,
    workflow_name: str,
    run_id: str,
    isolation: str,
    base: str | None,
    repo: str | None,
    config: Any,
    notice: Callable[[str], None],
) -> tuple[Workspace, str | None, Path]:
    """Create the run's working directory through ``rayspec.workspace``.

    Returns ``(engine Workspace, project slug, project_root)`` — ``project_root`` differs from the
    input for ``--repo`` (the registered/cloned project is where workflows are loaded from). The
    workspace module is a hard dependency now; only its *absence* degrades to in-place (with a
    notice) — a signature mismatch is a bug and must surface, not be swallowed.
    """
    from rayspec.loader.loader import import_optional

    module = import_optional("rayspec.workspace")
    prepare = getattr(module, "prepare_workspace", None) if module is not None else None
    if prepare is None:
        if isolation != "none":
            notice("workspace isolation not available; running in place (isolation: none)")
        if repo:
            notice(f"--repo {repo!r} ignored: workspace module not available")
        return Workspace.in_place(project_root), None, project_root
    ws = prepare(
        project_root,
        home=home,
        workflow_name=workflow_name,
        run_id=run_id,
        isolation=isolation,
        base=base,
        repo_arg=repo,
        config=config,
    )
    ws_notice = getattr(ws, "notice", None)
    if ws_notice:
        notice(str(ws_notice))
    workspace = Workspace(
        isolation=str(getattr(ws, "isolation", isolation)),
        workdir=Path(getattr(ws, "workdir", project_root)),
        branch=getattr(ws, "branch", None),
        base_branch=getattr(ws, "base_branch", None),
        base_sha=getattr(ws, "base_sha", None),
        head_sha=getattr(ws, "head_sha", None),
    )
    slug = getattr(ws, "slug", None) or None
    return workspace, slug, Path(getattr(ws, "project_root", project_root))


def workspace_from_record(record: Any, fallback_root: Path) -> Workspace:
    """Rebuild the engine Workspace of a run being resumed from its stored ``run.json``."""
    info = getattr(record, "workspace", None)
    if info is None or not getattr(info, "workdir", None):
        return Workspace.in_place(fallback_root)
    return Workspace(
        isolation=str(info.isolation),
        workdir=Path(info.workdir),
        branch=info.branch,
        base_branch=info.base_branch,
        base_sha=info.base_sha,
        head_sha=info.head_sha,
    )


def project_slug_for(project_root: Path) -> str:
    """Slug from ``rayspec.workspace`` when available, else the engine fallback."""
    from rayspec.loader.loader import import_optional

    module = import_optional("rayspec.workspace")
    slug_fn = getattr(module, "project_slug", None) if module is not None else None
    if slug_fn is not None:
        try:
            return str(slug_fn(project_root))
        except Exception:
            pass
    return fallback_project_slug(project_root)


#: Keys of the ``--json`` summary object (the last stdout line); the tests and
#: ``scripts/check_examples.py`` import this instead of hand-copying the set. ``cost_source``
#: (additive) tells a consumer whether ``cost_usd`` is provider-reported, a table estimate
#: (``table``), a lower bound (``partial``) or absent (``none``).
SUMMARY_KEYS: frozenset[str] = frozenset(
    {
        "run_id",
        "status",
        "exit_code",
        "reason",
        "outputs",
        "usage",
        "cost_usd",
        "cost_source",
        "run_dir",
        "workspace",
        "pause",
    }
)


def cost_label(cost_usd: float | None, cost_source: str) -> str | None:
    """``$0.12`` (provider) · ``~$0.12`` (table estimate) · ``≥$0.12`` (partial: at least one
    step with tokens has no cost) · ``None`` when no cost is known.

    The marker comes from :func:`rayspec.providers.pricing.cost_marker` — the same helper the
    console sink, ``rayspec runs``/``show`` and the approval panel use — so the ``■ run`` line
    and this footer cannot drift.
    """
    if cost_usd is None:
        return None
    return f"{cost_marker(cost_source)}${cost_usd:.2f}"


def worktree_lines(workspace: Workspace) -> list[str]:
    """The summary lines of a worktree run: where it is, that the branch is checked out
    there (``git checkout <branch>`` in the main clone is refused by git), and the three ways
    to use or clean it up."""
    path = workspace.workdir
    branch = workspace.branch or "?"
    return [
        f"  worktree: {path} (branch {branch}, checked out there)",
        f"  hint: cd {path} · rayspec worktrees list|clean · git worktree remove {path}",
    ]


def print_summary(
    out: Console, result: RunResult, *, json_mode: bool, pause_hint: str | None = None
) -> None:
    """Outputs, workspace, pause hint, tokens/cost footer and next-step hints.

    ``pause_hint`` replaces the ``decide with:`` line for a paused run; callers that know the
    gate's approval class build it with :func:`decide_hint` so the line names only the commands
    the class accepts.

    The final ``run <id> <status>`` line itself is printed by the console sink (``run.finished``),
    so the text summary does not repeat it; ``--json`` prints the summary object instead.
    ``--json`` prints one JSON object whose keys are exactly :data:`SUMMARY_KEYS`.
    """
    if json_mode:
        payload: dict[str, Any] = {
            "run_id": result.run_id,
            "status": result.status.value,
            "exit_code": result.exit_code,
            "reason": result.reason,
            "outputs": result.outputs,
            "usage": {
                "input": result.usage.input,
                "cached_input": result.usage.cached_input,
                "cache_write": result.usage.cache_write,
                "output": result.usage.output,
                "reasoning": result.usage.reasoning,
            },
            "cost_usd": result.cost_usd,
            "cost_source": result.cost_source,
            "run_dir": str(result.run_dir),
            "workspace": {
                "isolation": result.workspace.isolation,
                "workdir": str(result.workspace.workdir),
                "branch": result.workspace.branch,
            },
            "pause": result.pause.model_dump(mode="json") if result.pause else None,
        }
        assert set(payload) == SUMMARY_KEYS, "SUMMARY_KEYS drifted from the payload"
        out.print(json.dumps(payload), markup=False, highlight=False)
        return
    if result.outputs:
        table = Table(show_edge=False, pad_edge=False, title="outputs", title_justify="left")
        table.add_column("name", style="bold")
        table.add_column("value")
        for name, value in result.outputs.items():
            text = value if isinstance(value, str) else json.dumps(value)
            # output values are run data, never console markup (``[stub] …`` must stay literal)
            # and never terminal control (safe_text strips ESC/OSC/CSI + control chars)
            table.add_row(Text(safe_text(str(name))), Text(safe_text(text)))
        out.print(table)
    if result.workspace.isolation != "none":
        for line in worktree_lines(result.workspace):
            # soft_wrap: paths stay on one line (copy-paste `cd …` / `git worktree remove …`)
            out.print(line, markup=False, highlight=False, soft_wrap=True)
    if result.status is RunStatus.PAUSED and result.pause is not None:
        # Two kinds of pause, two different next steps. An operational ceiling is not a question
        # an approval class governs, so it keeps its own `continue with:` line; an approval gate
        # takes the caller's class-aware hint (which already carries its indent).
        if result.pause.reason in OPERATIONAL_PAUSE_REASONS:
            hint = "  " + pause_actions(result.run_id, result.pause.reason)
        elif pause_hint is not None:
            hint = pause_hint
        else:
            hint = decide_hint(result.run_id, None, ApprovalClasses())
        out.print(hint, markup=False, highlight=False)
    footer: list[str] = []
    unknown = sum(1 for rec in result.steps.values() if rec.usage_unknown)
    if unknown:
        # an attempt was interrupted before the provider reported any usage
        noun = "step" if unknown == 1 else "steps"
        known = f"≥{result.usage.total}" if result.usage.total else "unknown"
        footer.append(f"tokens: {known} (usage of {unknown} {noun} unknown)")
    elif result.usage.total:
        footer.append(f"tokens: {result.usage.total}")
    cost = cost_label(result.cost_usd, result.cost_source)
    if cost is not None:
        footer.append(f"cost: {cost}")
    footer.append(f"run dir: {result.run_dir}")
    out.print("  " + " · ".join(footer), markup=False, highlight=False)
    if result.reused:
        out.print(f"  reused {len(result.reused)} step(s) from the previous attempt")
    if result.status in {RunStatus.FAILED, RunStatus.INTERRUPTED}:
        failed = failed_leaf_paths(result)  # a leaf path, never a composite
        step_hint = f" --step {failed[0]}" if failed else ""
        if len(failed) > 1:
            step_hint += f" (+{len(failed) - 1} more)"
        out.print(
            f"  hint: rayspec logs {result.run_id}{step_hint} · rayspec resume {result.run_id}",
            markup=False,
            highlight=False,
        )


def register(app: typer.Typer) -> None:
    @app.command()
    def run(  # noqa: PLR0917 - Typer options are positional by construction
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
        root: RootOption = None,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Stub providers, skip shell/python, auto-approve.")
        ] = False,
        stubs: Annotated[
            Path | None,
            typer.Option(
                "--stubs",
                help="Stub script (YAML) for --dry-run or a stub-only workflow; recorded in "
                "run.json and reused by resume/approve/reject.",
            ),
        ] = None,
        stubs_from: Annotated[
            str | None,
            typer.Option(
                "--stubs-from",
                help="Replay a stored run's recorded answers instead of a --stubs file "
                "(run id or unique prefix).",
            ),
        ] = None,
        stubs_init: Annotated[
            Path | None,
            typer.Option("--stubs-init", help="Write a stub script scaffold and exit."),
        ] = None,
        exec_shell: Annotated[
            bool, typer.Option("--exec-shell", help="Run shell/python steps even in --dry-run.")
        ] = False,
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Auto-approve gates.")] = False,
        approve_class: ApproveClassOption = None,
        no_interactive: Annotated[
            bool, typer.Option("--no-interactive", help="Never prompt; pause at gates (exit 3).")
        ] = False,
        json_: Annotated[bool, typer.Option("--json", help="JSONL events on stdout.")] = False,
        output: OutputOption = None,
        quiet: Annotated[
            bool,
            typer.Option("--quiet", help="Only problems and run-level lines (no per-step lines)."),
        ] = False,
        verbose: Annotated[bool, typer.Option("--verbose", help="Also show step starts.")] = False,
        allow_unsupported: AllowUnsupportedOption = False,
        fail_fast: Annotated[
            bool, typer.Option("--fail-fast", help="Cancel running siblings on failure.")
        ] = False,
        resume: Annotated[
            str | None, typer.Option("--resume", help="Resume run id (prefix ok).")
        ] = None,
        force: Annotated[
            bool,
            typer.Option(
                "--force", help="Resume even if the workflow changed; overwrite --stubs-init."
            ),
        ] = False,
        worktree: Annotated[
            bool | None,
            typer.Option("--worktree/--no-worktree", help="Override workflow isolation."),
        ] = None,
        base: Annotated[
            str | None, typer.Option("--base", help="Base branch for the worktree.")
        ] = None,
        locked: LockedOption = None,
        wait_slot: WaitSlotOption = None,
        repo: Annotated[
            str | None, typer.Option("--repo", help="Registered project / path / url.")
        ] = None,
    ) -> None:
        """Run a workflow (or resume one with --resume)."""
        json_ = resolve_output(output, json_)
        ctx = make_context(root)
        out = common.err_console() if json_ else common.console()
        project_root = ctx.project_root
        run_id = new_run_id()
        prepared: tuple[Workspace, str | None, Path] | None = None
        if repo and resume:
            fail(
                "--repo cannot be combined with --resume (the run's workspace is fixed)",
                hint=f"use `rayspec resume {resume}` — it finds the run in any project",
            )
            return
        pure_dry_run = dry_run and not exec_shell
        notice = _dry_run_notice if pure_dry_run else _err
        if repo:
            # --repo: the workspace (clone/worktree) decides where workflows are loaded from, so
            # prepare it first; isolation comes from the flag (URL sources are always worktrees).
            repo_isolation = (
                "none" if worktree is False or (dry_run and not exec_shell) else "worktree"
            )
            try:
                prepared = prepare_workspace(
                    project_root=project_root,
                    home=ctx.home,
                    workflow_name=Path(workflow).stem,
                    run_id=run_id,
                    isolation=repo_isolation,
                    base=base,
                    repo=repo,
                    config=ctx.config,
                    notice=notice,
                )
            except RayspecError as exc:
                fail(str(exc), hint=exc.hint)
                return
            project_root = prepared[2]
        try:
            rw = load_workflow(
                workflow, project_root=project_root, home=ctx.home, config=ctx.config
            )
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return
        if worktree:
            # --worktree can only ADD isolation, so the document the policy check reads carries
            # it: a restriction an operator imposes at the command line is a control like any
            # other, and `isolation` is where the check looks for it. The other half
            # (--no-worktree) is left alone on purpose — removing it from the document would
            # OPEN the escape hatch the file had already closed.
            rw.workflow.isolation = "worktree"
        caps = common.capability_source()
        report = validate_workflow(
            rw,
            capabilities_for=caps.capabilities_for,
            template_checker=common.template_checker(),
            on_unsupported="warn" if allow_unsupported else "error",
            provider_ids=caps.provider_ids,
        )
        warnings = [*rw.warnings, *report.warnings]
        if caps.warning:
            warnings.append(caps.warning)
        # warnings go to stderr in text mode too (stdout stays the run's own output), and so
        # does the line naming the policy layers this run is subject to
        err = common.err_console()
        err.print(f"[dim]{escape(report.policy_note)}[/dim]", soft_wrap=True)
        report_lines("warnings:", warnings, style="yellow", printer=err.print)
        if report.errors:
            error_lines(report.errors, json_mode=json_, kind="validation errors")
            raise typer.Exit(code=EXIT_USAGE)
        # the lockfile of the project the workflow came from (with --repo: the checkout)
        enforce_lockfile(ctx, rw, locked=locked, project_root=project_root, json_mode=json_)
        if stubs_init is not None:
            if stubs_init.exists() and not force:
                fail(
                    f"{stubs_init} already exists",
                    hint="pass --force to overwrite the stub scaffold",
                )
                return
            stubs_init.write_text(
                yaml.safe_dump(stub_scaffold(rw), sort_keys=False), encoding="utf-8"
            )
            out.print(f"wrote stub scaffold to {stubs_init}")
            return

        # the configured secret sources. They supply a `secret: true` input that was not
        # passed on the command line and become the shell/python step environment. Only the
        # entries this workflow can actually read are resolved (lazily, memoised): an unused
        # entry — a stale one in ~/.rayspec/config.yaml, a `cmd:` helper for another project —
        # neither fails the run nor is executed.
        secret_provider = provider_for(ctx.config, base_dir=project_root)
        try:
            config_secrets = used_config_secrets(
                secret_provider, [s for _, s in rw.all_steps()], ctx.config.secrets
            )
        except SecretError as exc:
            fail(str(exc), hint=exc.hint)
            return
        secret_names = secret_input_names(rw.workflow)

        values: dict[str, Any] = {}
        if resume:
            if inputs_file:
                fail("inputs are fixed per run; --resume does not accept --inputs-file")
                return
            # --input is accepted for secret inputs only; checked against the record below
        else:
            try:
                values = resolve_inputs(
                    rw.workflow,
                    cli_pairs=inputs or [],
                    inputs_file=inputs_file,
                    env=secret_input_overlay(secret_provider, secret_names),
                )
            except InputError as exc:
                error_lines(list(exc.errors), json_mode=json_, kind="input errors")
                raise typer.Exit(code=EXIT_USAGE) from None

        stub_script: StubScript | None = None
        stubs_path: str | None = None
        if stubs is not None and stubs_from is not None:
            fail(
                "--stubs and --stubs-from are mutually exclusive (both script the stub provider)",
                hint="pass one: --stubs <file>, or --stubs-from <run> to replay a stored run",
            )
            return
        if stubs is not None:
            refuse_stubs_for_real_agents(rw, dry_run=dry_run)
            stub_script = load_stub_script(stubs)
            stubs_path = str(stubs.resolve())  # recorded for resume/approve/reject
        elif stubs_from is not None:
            # the recorded answers of a stored run, as if they had been written to a file
            refuse_stubs_for_real_agents(rw, dry_run=dry_run)
            stub_script, donor_id = runs_common.replay_source(stubs_from, root=root)
            # the donor, not a file: a resume entry rebuilds the same script from it
            stubs_path = f"{REPLAY_REF_PREFIX}{donor_id}"
        if stub_script is not None:
            refuse_unmatched_expectations(rw, stub_script)  # no silently dead assertion

        isolation = rw.workflow.isolation
        if worktree is not None:
            isolation = "worktree" if worktree else "none"
        if pure_dry_run:
            isolation = "none"
        # with --repo the run store is the SOURCE's project (the slug the bare clone and
        # the worktrees use), not a slug minted from the worktree directory's origin
        slug = (prepared[1] if prepared is not None else None) or project_slug_for(project_root)
        store = FileRunStore(ctx.home / "projects" / slug)
        resume_id: str | None = None
        if resume:
            try:
                resume_id = store.resolve_run_id(resume)
                record = store.load(resume_id)
            except StoreError as exc:
                fail(str(exc), hint=exc.hint)
                return
            # the same guard as resume/approve/reject — refused before anything is touched
            # (the engine repeats the check; this one gives the identical answer up front)
            from rayspec.cli.commands.resume import (
                refuse_changed_workflow,
                resume_secret_inputs,
                resume_stub_script,
            )

            refuse_changed_workflow(record, rw, force=force)
            # secrets only; a configured source supplies them without --input
            values = resume_secret_inputs(record, rw, inputs or [], provider=secret_provider)
            if stubs is None and stubs_from is None and record.stubs_path:
                # the recorded --stubs file or replay source — an explicit --stubs /
                # --stubs-from on the resume entry keeps overriding it
                stub_script, _kept = resume_stub_script(record, rw, stubs=None, dry_run=dry_run)
            workspace = workspace_from_record(record, project_root)
            run_id = resume_id
        elif prepared is not None:
            workspace, _slug, _root = prepared
        else:
            try:
                workspace, ws_slug, project_root = prepare_workspace(
                    project_root=project_root,
                    home=ctx.home,
                    workflow_name=rw.workflow.name,
                    run_id=run_id,
                    isolation=isolation,
                    base=base,
                    repo=None,
                    config=ctx.config,
                    notice=notice,
                )
            except RayspecError as exc:
                fail(str(exc), hint=exc.hint)
                return
            if ws_slug and ws_slug != slug:
                slug = ws_slug
                store = FileRunStore(ctx.home / "projects" / slug)

        # --json does not imply --no-interactive (documented in cli.md): a TTY still prompts
        interactive = runs_common.stdin_is_tty() and not no_interactive and not yes
        # one redactor over every value this run knows — installed on the store (which
        # covers run.json, outputs, events and streams) and on every sink
        redactor = build_redactor(
            ctx.config,
            {**config_secrets, **{n: values[n] for n in secret_names if n in values}},
        )
        store.redactor = redactor
        warn_unredactable_secrets(out, redactor)  # a value too short to redact is named
        try:
            # an id in `extensions:` that names nothing is a usage error, not a crash mid-run
            sinks = _sinks(
                json_,
                out,
                verbose=verbose and not quiet,
                quiet=quiet,
                redactor=redactor,
                extensions=ctx.config.extensions,
            )
            configured = configured_approval(
                ctx.config.extensions, interactive=interactive, console=out
            )
            prompt = approval_prompt_for(sinks, interactive=interactive, prompt=configured)
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return
        options = RunOptions(
            dry_run=dry_run,
            exec_shell=exec_shell,
            yes=yes,
            interactive=interactive,
            fail_fast=fail_fast,
            force=force,
            resume=bool(resume_id),
            stub_script=stub_script,
            stubs_path=stubs_path,
            provider_settings=ctx.config.providers,
            config_secrets=config_secrets,  # shell/python step env only
            approval_classes=approval_classes_for(
                project_root,
                ctx.home,
                pre_approved=approve_class or (),
                # `require_tty` accepts the built-in terminal prompt and no substitute
                terminal_prompt=terminal_prompt_id(ctx.config.extensions, configured),
            ),
        )
        try:
            price_table = PriceTable.from_config(ctx.config.pricing)
        except RayspecError:
            price_table = None
        # The operator's ceilings for this machine (empty when no policy file applies). A dry
        # run maps every provider to the stub: it spends nothing and occupies no real agent, so
        # neither the envelope nor a host slot applies to it.
        policy = limits_policy(project_root, home=ctx.home)
        # a ceiling that cannot be read must be visible, not invisible: an operator who wrote
        # one and never sees it applied would otherwise believe the machine is capped
        report_lines(
            "policy warnings:",
            list(policy.warnings),
            style="yellow",
            printer=common.err_console().print,
        )
        providers_used = workflow_providers(rw)
        envelope = (
            None
            if dry_run
            else run_envelope(policy, store_root=ctx.home / "projects" / slug, run_id=run_id)
        )
        try:
            slot_wait = wait_seconds(wait_slot)
        except (RayspecError, ValueError) as exc:
            fail(
                f"--wait-slot: {exc}",
                hint="pass a duration (--wait-slot 30m) or `forever`",
            )
            return
        slot_limits = {} if dry_run else limits_for(policy.max_concurrent_runs, providers_used)
        runner = Runner(
            rw,
            inputs=values,
            store=store,
            project_root=project_root,
            project_slug=slug,
            run_id=run_id,
            sinks=sinks,
            workspace=workspace,
            options=options,
            approval_prompt=prompt,
            resume_run_id=resume_id,
            price_table=price_table,
            home=ctx.home,
            envelope=envelope,
        )
        try:
            with acquire_slots(
                ctx.home, providers_used, slot_limits, run_id=run_id, wait_s=slot_wait
            ):
                result = runner.run_sync()
        except SlotBusyError as exc:
            fail(str(exc), hint=exc.hint)
            return
        except EngineError as exc:
            fail(str(exc), hint=exc.hint)
            return
        finally:
            anyio.run(sinks.aclose, backend="asyncio")
        # --json: the summary object joins the JSONL events on stdout; console lines stay
        # on stderr
        print_summary(
            common.console() if json_ else out,
            result,
            json_mode=json_,
            pause_hint=decide_hint(
                result.run_id,
                paused_gate_class(rw, result.pause.step) if result.pause is not None else None,
                options.approval_classes,
            ),
        )
        raise typer.Exit(code=result.exit_code)


def warn_unredactable_secrets(out: Console, redactor: Redactor) -> None:
    """Name every secret whose value is too short to redact.

    :data:`~rayspec.redact.MIN_REDACTABLE_LEN` exists because replacing every ``ab`` in a
    transcript destroys the log without protecting anything — but silently *not* redacting a
    value the user declared secret is the opposite of what the feature promises. The names (never
    the values) are printed once per run, on the same stream as the other pre-run notes, so
    ``--json`` stays parseable.
    """
    if not redactor.skipped:
        return
    names = ", ".join(redactor.skipped)
    out.print(
        Text.assemble(
            ("warning", "yellow"),
            f": {names} is shorter than {MIN_REDACTABLE_LEN} characters and is therefore "
            "not redacted — it can appear in the run store, the logs and the console",
        ),
        highlight=False,
    )


def _sinks(
    json_mode: bool,
    out: Console,
    *,
    verbose: bool,
    quiet: bool,
    redactor: Redactor = NULL_REDACTOR,
    extensions: ExtensionsSpec | None = None,
) -> Any:
    """The event sinks of one run: JSONL on stdout, problems-only lines, or the console tree.

    The :class:`~rayspec.events.sinks.ConsoleSink` draws the Rich Live step tree when ``out`` is a
    terminal and degrades to one line per step otherwise; it never prints its own summary panel
    (``summary=False``) — :func:`print_summary` owns the final text (run dir, approve hint).

    This is the ONE place a sink is built, so it is the one place redaction is wired in.
    Every sink is wrapped in a :class:`~rayspec.redact.RedactingSink` when the run has known
    secret values, which covers the console tree, the quiet lines and ``--json`` alike — and any
    sink added later, without it having to remember the rule. That includes the sinks
    ``config.extensions.sinks`` names: a third-party observer is redacted by construction,
    because it is built here like every other one.
    """
    from rayspec.events.sinks import ConsoleSink, JsonStdoutSink, MultiSink

    def wrap(sink: Any) -> Any:
        return RedactingSink(sink, redactor) if redactor else sink

    if json_mode:
        primary: Any = JsonStdoutSink(sys.stdout)
    elif quiet:
        primary = _problems_only_sink(out)
    else:
        primary = ConsoleSink(out, verbose=verbose, summary=False)
    sinks = [wrap(primary)]
    sinks += [
        wrap(sink)
        for sink in configured_sinks(
            extensions,
            out,
            verbose=verbose,
            quiet=quiet,
            # under --json stdout carries the JSONL event stream and belongs to the CLI: a
            # plugin writing to the stream it was handed would corrupt the machine-readable
            # contract, so it is handed none
            stream=None if json_mode else sys.stdout,
        )
    ]
    return MultiSink(sinks)


def configured_sinks(
    extensions: ExtensionsSpec | None,
    out: Console,
    *,
    verbose: bool,
    quiet: bool,
    stream: TextIO | None = None,
) -> list[Any]:
    """The sinks ``config.extensions.sinks`` names, built through :mod:`rayspec.registry`.

    They are ADDITIONAL observers next to the CLI's own sink, in the order they are configured;
    an id that names nothing raises :class:`~rayspec.registry.UnknownExtensionError` (with
    did-you-mean) before the run starts. ``stream`` is what a stdout-shaped sink may write to —
    ``None`` whenever the CLI owns stdout itself.
    """
    if extensions is None or not extensions.sinks:
        return []
    from rayspec.registry import SinkContext, create_sink

    built: list[Any] = []
    for sink_id in extensions.sinks:
        context = SinkContext(
            console=out,
            stream=stream,
            verbose=verbose,
            quiet=quiet,
            settings=extensions.settings_for(sink_id),
        )
        built.append(
            _built("sink", sink_id, lambda sid=sink_id, ctx=context: create_sink(sid, ctx))
        )
    return built


def _built(kind: str, extension_id: str, build: Callable[[], T]) -> T:
    """``build()``, with anything a third-party factory raises turned into a usage error.

    A factory is code rayspec did not write: letting a ``ValueError`` out of it would end the
    command with a traceback and exit 1 — the code that means "the workflow failed", for a run
    that was never created. Naming the extension makes it what it is, a configuration or
    packaging problem, the way an unknown id already is.
    """
    try:
        return build()
    except RayspecError:
        raise  # an unknown id already says exactly what is wrong
    except Exception as exc:
        raise RayspecError(
            f"{kind} {extension_id!r} failed to build: {type(exc).__name__}: {exc}",
            hint=f"this comes from the package providing the {kind}, not from rayspec — check "
            f"`extensions.settings.{extension_id}` in config.yaml, or remove the id from "
            "`extensions:` to run without it",
        ) from exc


#: The registry id of the built-in terminal prompt — the one ``require_tty`` accepts.
TERMINAL_PROMPT_ID = "console"


def terminal_prompt_id(
    extensions: ExtensionsSpec | None, configured: ApprovalPrompt | None
) -> bool:
    """Whether this run's approval prompt is the built-in terminal one.

    ``configured is None`` means nothing was configured, so the builtin is used. Naming the
    builtin explicitly (``extensions.approval: console``) resolves it through the registry and
    hands back a prompt object — the same prompt, so it must not read as a replacement or
    ``require_tty`` would refuse the very prompt it exists to require.
    """
    if configured is None:
        return True
    return bool(extensions and extensions.approval == TERMINAL_PROMPT_ID)


def configured_approval(
    extensions: ExtensionsSpec | None, *, interactive: bool, console: Console | None = None
) -> ApprovalPrompt | None:
    """The approval prompt ``config.extensions.approval`` names, built through the registry.

    ``None`` — which :func:`approval_prompt_for` reads as "use the builtin
    :class:`~rayspec.engine.approval.ConsoleApprovalPrompt`" — when nothing is configured or the
    run cannot ask anyway. The builtin prompt is registered under the id ``console`` and can be
    named explicitly; it is not resolved through the registry by default so that the default
    path stays exactly what it was.

    A configured id is RESOLVED even when the run can never ask: a typo in ``config.yaml`` is a
    usage error on a machine without a TTY exactly as it is on one — which is where a policy or
    queue prompt is installed in the first place. Only the prompt's construction is skipped, so
    a factory never runs (and never opens anything) for a run that will not use it.
    """
    approval_id = extensions.approval if extensions else None
    if not approval_id:
        return None
    from rayspec.registry import ApprovalContext, create_approval, get_approval

    get_approval(approval_id)  # unknown id: exit 2 with did-you-mean, TTY or not
    if not interactive:
        return None
    settings = extensions.settings_for(approval_id) if extensions else {}
    return _built(
        "approval",
        approval_id,
        lambda: create_approval(
            approval_id, ApprovalContext(console=console, interactive=True, settings=settings)
        ),
    )


class SuspendingApprovalPrompt:
    """Run ``inner`` with every suspendable sink paused (``async with sink.suspended()``).

    The Live tree would otherwise redraw over the approval panel and swallow the key prompt.
    Sinks without ``suspended()`` (quiet lines, JSON) are left alone.
    """

    def __init__(self, inner: ApprovalPrompt, sinks: Any) -> None:
        self.inner = inner
        self.sinks = sinks

    async def __call__(self, request: ApprovalRequest) -> ApprovalAnswer | None:
        async with contextlib.AsyncExitStack() as stack:
            for sink in _iter_sinks(self.sinks):
                suspended: Callable[[], Any] | None = getattr(sink, "suspended", None)
                if callable(suspended):
                    await stack.enter_async_context(suspended())
            return await self.inner(request)


def _iter_sinks(sinks: Any) -> list[Any]:
    inner = getattr(sinks, "sinks", None)
    if inner is None:
        return [sinks]
    flat: list[Any] = []
    for sink in inner:
        flat.extend(_iter_sinks(sink))
    return flat


def approval_prompt_for(
    sinks: Any, *, interactive: bool, prompt: ApprovalPrompt | None = None
) -> ApprovalPrompt | None:
    """The approval prompt to inject into the runner: ``None`` (pause at gates) unless
    ``interactive``; then ``prompt`` (default :class:`ConsoleApprovalPrompt`) wrapped so that the
    console tree is suspended while it asks."""
    if not interactive:
        return None
    return SuspendingApprovalPrompt(prompt or ConsoleApprovalPrompt(), sinks)


def _problems_only_sink(out: Console) -> Any:
    """``--quiet``: run-level lines, warnings, retries and non-green step finishes only."""
    from rayspec.events.sinks import QuietConsoleSink

    class ProblemsOnlySink(QuietConsoleSink):
        def format_step_finished(self, event: Any) -> Any:
            status = str(event.data.get("status") or "")
            if status in {"succeeded", "skipped"} and not event.data.get("tolerated"):
                return None
            return super().format_step_finished(event)

    return ProblemsOnlySink(out, show_started=False)


__all__ = [
    "SUMMARY_KEYS",
    "TERMINAL_PROMPT_ID",
    "ApproveClassOption",
    "SuspendingApprovalPrompt",
    "WaitSlotOption",
    "approval_classes_for",
    "approval_prompt_for",
    "configured_approval",
    "configured_sinks",
    "cost_label",
    "decide_hint",
    "failed_leaf_paths",
    "gate_classes",
    "load_stub_script",
    "non_stub_agents",
    "operator_policy",
    "pause_actions",
    "policy_class_rules",
    "prepare_workspace",
    "print_summary",
    "project_slug_for",
    "refuse_stubs_for_real_agents",
    "refuse_unmatched_expectations",
    "register",
    "replay_ref",
    "stub_scaffold",
    "stub_scaffold_keys",
    "terminal_prompt_id",
    "workspace_from_record",
    "worktree_lines",
]

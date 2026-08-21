# SPDX-License-Identifier: Apache-2.0
"""`rayspec resume <run> [--force] [--yes] [--approve-class NAME] [--no-interactive] [--json]
[--quiet]` — continue a run in-process through the engine's resume entry point.

A paused run re-asks its pending gate on a TTY; without a TTY (or with ``--no-interactive``)
the command points to ``rayspec approve|reject`` and exits 3 (still paused) unless ``--yes``
auto-approves. Before any of that the workflow is re-loaded and a changed hash is refused
(exit 2, ``--force`` hint) — :func:`guard_workflow_unchanged` is the ONE guard every resume
entry point (``resume``, ``approve``, ``reject``, ``run --resume``) applies first.
Everything else (reuse cache, live-pid refusal) is the engine's.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.text import Text

from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    err_console,
    error_lines,
    fail,
    resolve_output,
)
from rayspec.cli.commands.lock import LockedOption, enforce_lockfile
from rayspec.cli.commands.run import (
    ApproveClassOption,
    WaitSlotOption,
    approval_classes_for,
    load_stub_script,
    paused_gate_class,
    refuse_stubs_for_real_agents,
)
from rayspec.engine.runtime import EXIT_PAUSED, EXIT_USAGE
from rayspec.errors import InputError, RayspecError
from rayspec.limits import OPERATIONAL_PAUSE_REASONS
from rayspec.loader import ResolvedWorkflow
from rayspec.loader.inputs import resolve_resume_secrets, secret_input_names
from rayspec.schema import RunStatus
from rayspec.secrets import SecretProvider, provider_for, secret_input_overlay
from rayspec.store.model import RunRecord

if TYPE_CHECKING:
    from rayspec.providers.stub import StubScript

SecretInputsOption = Annotated[
    list[str] | None,
    typer.Option(
        "--input",
        "-i",
        help="Re-supply a secret input as NAME=VALUE (repeatable; secret inputs only — "
        "other inputs are fixed per run). Else RAYSPEC_INPUT_<NAME> is read.",
        show_default=False,
    ),
]
StubsOption = Annotated[
    Path | None,
    typer.Option(
        "--stubs",
        help="Stub script (YAML) for the resumed run; default: the file recorded at launch.",
        show_default=False,
    ),
]


def secret_provider_for(ctx: Any, record: RunRecord) -> SecretProvider:
    """The ONE :class:`~rayspec.secrets.SecretProvider` a resume/approve/reject command uses.

    Built once and threaded through :func:`resume_secret_inputs` and
    :func:`rayspec.cli._runs_common.resume_run`, because memoisation is per instance: a second
    provider would run every ``cmd:`` helper a second time (a second Touch ID prompt for
    ``op read``) in the same command. Both the table it reads (``config.secrets``) and its
    ``base_dir`` are the RUN's — that is where the workflow lives, so a relative ``file:``
    source resolves the same way it did at launch, and a secret the caller's project happens to
    declare under the same name is not what the run gets.
    """
    root = common.record_root(ctx, record)
    return provider_for(common.record_context(ctx, record).config, base_dir=root)


def resume_secret_inputs(
    record: RunRecord,
    resolved: ResolvedWorkflow,
    cli_pairs: list[str],
    *,
    provider: SecretProvider | None = None,
) -> dict[str, Any]:
    """The secret inputs a resume entry must supply again — ``--input name=value`` for
    secret names only, then the configured secret source, else ``RAYSPEC_INPUT_<NAME>``; exit 2
    listing what is missing (or naming a non-secret ``--input``: inputs are fixed per run).
    Nothing is persisted before this.

    ``provider`` is the run's :class:`~rayspec.secrets.SecretProvider`: a secret with a
    ``config.secrets`` entry is re-fetched from its source, so a paused run continues without
    having to be re-fed by hand.

    A source that cannot resolve is NOT fatal here: the provider is only asked
    for names the command line did not supply, and a failure is remembered rather than raised —
    a vault helper that is not signed in must never strand a paused run when the user already
    has the value. The collected source messages are printed next to the ``missing secret
    input(s)`` list, and only when the name really is still missing.
    """
    env: Mapping[str, str] | None = None
    source_problems: list[str] = []
    if provider is not None:
        supplied = {pair.split("=", 1)[0].strip() for pair in cli_pairs}
        env = secret_input_overlay(
            provider,
            [n for n in secret_input_names(resolved.workflow) if n not in supplied],
            problems=source_problems,
        )
    try:
        return resolve_resume_secrets(
            resolved.workflow, record.inputs, cli_pairs=cli_pairs, env=env
        )
    except InputError as exc:
        error_lines([*exc.errors, *source_problems], kind="input errors")
        raise typer.Exit(code=EXIT_USAGE) from None


def resume_stub_script(
    record: RunRecord, resolved: ResolvedWorkflow, *, stubs: Path | None, dry_run: bool
) -> tuple[StubScript | None, str | None]:
    """``(stub script, absolute path)`` for a resume entry: ``--stubs`` when given, else
    the file recorded in ``run.json`` (``stubs_path``) — a missing/unreadable file is exit 2
    with a ``--stubs <path>`` hint; ``(None, None)`` when neither exists. The same rule
    applies as on ``run``: a non-stub agent may only be scripted in a dry run (the message names the
    recorded file when that is where the stubs come from)."""
    if stubs is not None:
        refuse_stubs_for_real_agents(resolved, dry_run=dry_run)
        return load_stub_script(stubs), str(stubs.resolve())
    if not record.stubs_path:
        return None, None
    refuse_stubs_for_real_agents(resolved, dry_run=dry_run, record=record)
    script = load_stub_script(
        Path(record.stubs_path),
        hint=f"run {record.run_id} was launched with --stubs {record.stubs_path}; "
        "pass --stubs <path> to use another file",
    )
    return script, None  # keep the recorded path


def refuse_changed_workflow(record: RunRecord, resolved: ResolvedWorkflow, *, force: bool) -> None:
    """Exit 2 with the engine's wording and ``--force`` hint when ``resolved`` no longer matches
    ``record.workflow_hash`` (unless ``force``). Nothing is persisted before this check."""
    from rayspec.engine.errors import ResumeError

    try:
        common.check_workflow_unchanged(record, resolved, force=force)
    except ResumeError as exc:
        fail(str(exc), hint=exc.hint)


def guard_workflow_unchanged(
    ctx: common.RunsContext, record: RunRecord, *, force: bool, locked: bool | None = None
) -> ResolvedWorkflow:
    """Re-load ``record``'s workflow, apply :func:`refuse_changed_workflow` and the lock gate.

    The shared first step of ``resume`` / ``approve`` / ``reject``: a CI job polling a paused
    run learns that the workflow drifted (exit 2) instead of "still paused" (exit 3). A workflow
    that cannot be loaded at all is also exit 2.

    The lockfile is checked here too. The workflow hash only covers the workflow's own files, so
    a model that moved because a *tier* was re-pointed leaves it untouched — and a poll-then-
    approve CI job is precisely the unattended run the lockfile exists to protect.

    The context is re-scoped to the run's project first
    (:func:`~rayspec.cli._runs_common.record_context`, a no-op when the run is the caller's own).
    Loading the workflow in one project and resolving its models in another is what turns
    ``--locked`` into a refusal of a run that never drifted.
    """
    ctx = common.record_context(ctx, record)
    try:
        resolved = common.load_resolved_for(ctx, record)
    except RayspecError as exc:
        fail(str(exc), hint=exc.hint)
        raise AssertionError("unreachable") from None  # pragma: no cover
    refuse_changed_workflow(record, resolved, force=force)
    enforce_lockfile(ctx.loader_context, resolved, locked=locked, project_root=ctx.project_root)
    return resolved


def register(app: typer.Typer) -> None:
    @app.command()
    def resume(  # noqa: PLR0917 - Typer options are positional by construction
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        force: Annotated[
            bool, typer.Option("--force", help="Resume even if the workflow changed.")
        ] = False,
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Auto-approve gates.")] = False,
        approve_class: ApproveClassOption = None,
        no_interactive: Annotated[
            bool, typer.Option("--no-interactive", help="Never prompt; pause at gates (exit 3).")
        ] = False,
        json_: JsonOption = False,
        output: OutputOption = None,
        quiet: Annotated[
            bool, typer.Option("--quiet", help="Only problems and run-level lines.")
        ] = False,
        verbose: Annotated[bool, typer.Option("--verbose", help="Also show step starts.")] = False,
        inputs: SecretInputsOption = None,
        stubs: StubsOption = None,
        locked: LockedOption = None,
        wait_slot: WaitSlotOption = None,
        root: RootOption = None,
    ) -> None:
        """Resume a paused/failed/interrupted run (steps that succeeded are reused).

        Succeeded and cancelled runs are refused (exit 2) unless --force. Secret inputs must be
        supplied again (--input / RAYSPEC_INPUT_<NAME>); a --stubs file given at launch is reused.
        """
        json_ = resolve_output(output, json_)
        ctx = common.make_runs_context(root)
        store, record = common.lookup_run(ctx, run)
        # the run may live in another project; from here on the command speaks for THAT one
        ctx = common.record_context(ctx, record)
        if record.status is RunStatus.SUCCEEDED and not force:
            fail(
                f"run {record.run_id} already succeeded — nothing to resume",
                hint=f"start a new run with `rayspec run {record.workflow_name}` "
                "(or pass --force to replay it)",
            )
            return
        if record.status is RunStatus.CANCELLED and not force:
            fail(
                f"run {record.run_id} was cancelled — nothing to resume",
                hint=f"start a new run with `rayspec run {record.workflow_name}` "
                "(or pass --force to continue it anyway)",
            )
            return
        # a changed workflow is refused before the paused/non-TTY short-circuit below
        resolved = guard_workflow_unchanged(ctx, record, force=force, locked=locked)
        interactive = common.stdin_is_tty() and not no_interactive and not yes
        # only an APPROVAL gate needs a person before the run may go on. A run the spending
        # envelope paused (``pause.reason == "budget"``) is continued by resuming it — the
        # ceiling is re-evaluated — so it must not be sent away to `approve`, least of all on
        # the non-TTY path, which is exactly where an unattended run lives.
        pending = (
            record.pause is not None
            and record.pause.decision is None
            and record.pause.reason not in OPERATIONAL_PAUSE_REASONS
        )
        # --approve-class may be able to answer the pending gate, so the short-circuit below
        # (which exists so a CI poller does not restart the engine to learn "still paused")
        # does not apply when it was given
        if (
            record.status is RunStatus.PAUSED
            and pending
            and not interactive
            and not yes
            and not approve_class
        ):
            assert record.pause is not None
            out = err_console()
            out.print(
                Text.assemble(
                    (f"run {record.run_id} is paused", "yellow"),
                    f" awaiting approval at {record.pause.step}: {record.pause.message}",
                ),
                highlight=False,
            )
            # the hint names only what this gate's approval class accepts: recommending a
            # command the class refuses is how a control teaches people to work around it
            classes = approval_classes_for(ctx.project_root, ctx.home)  # the RUN's policy
            gate_class = paused_gate_class(resolved, record.pause.step)
            if not classes.may_decide_out_of_band(gate_class):
                hint = (
                    f"  answer it with `rayspec resume {record.run_id}` from a terminal "
                    f"(approval class {gate_class!r} requires one)"
                )
            else:
                hint = (
                    f"  decide with `rayspec approve {record.run_id} [comment]` / "
                    f"`rayspec reject {record.run_id} [reason]`"
                )
                if classes.may_approve_automatically(gate_class):
                    hint += ", or pass --yes to auto-approve"
            out.print(hint, markup=False, highlight=False)
            raise typer.Exit(code=EXIT_PAUSED)
        # secrets come after the pending-gate pointer (that is the more useful answer for a run
        # that wants approve/reject), still before anything is written
        secret_provider = secret_provider_for(ctx, record)  # one per command
        secrets = resume_secret_inputs(record, resolved, inputs or [], provider=secret_provider)
        stub_script, stubs_path = resume_stub_script(
            record, resolved, stubs=stubs, dry_run=record.dry_run
        )
        code = common.resume_run(
            ctx,
            store,
            record,
            force=force,
            yes=yes,
            interactive=interactive,
            json_mode=json_,
            quiet=quiet,
            verbose=verbose,
            secret_provider=secret_provider,  # the same instance, no second helper run
            resolved=resolved,
            inputs=secrets,
            stub_script=stub_script,
            stubs_path=stubs_path,
            wait_slot=wait_slot,
            approve_classes=approve_class or (),
        )
        raise typer.Exit(code=code)


__all__ = [
    "SecretInputsOption",
    "StubsOption",
    "WaitSlotOption",
    "guard_workflow_unchanged",
    "refuse_changed_workflow",
    "register",
    "resume_secret_inputs",
    "resume_stub_script",
    "secret_provider_for",
]

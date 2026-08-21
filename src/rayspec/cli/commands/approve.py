# SPDX-License-Identifier: Apache-2.0
"""`rayspec approve <run> [comment]` — approve the pending gate of a paused run and resume.

Records ``pause.decision`` (``by: cli``) and resumes in-process; the gate consumes the decision
when its token matches (otherwise it asks again / pauses again). Exit code = how the resumed
run ends (0 succeeded · 1 failed · 3 paused again · 4 cancelled · 130 interrupted); 2 when
the run is not paused or cannot be resumed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import JsonOption, RootOption, fail
from rayspec.cli.commands.resume import (
    SecretInputsOption,
    StubsOption,
    guard_workflow_unchanged,
    resume_secret_inputs,
    resume_stub_script,
    secret_provider_for,
)
from rayspec.schema import RunStatus
from rayspec.store.file import FileRunStore
from rayspec.store.model import Decision, RunRecord


def record_decision(
    store: FileRunStore, run: RunRecord, *, approved: bool, comment: str, by: str = "cli"
) -> Decision:
    """Write ``pause.decision`` of a paused ``run`` (callers validate the status first)."""
    if run.pause is None:
        raise ValueError(f"run {run.run_id} has no pending gate")
    decision = Decision(approved=approved, comment=comment, by=by)
    run.pause.decision = decision
    store.save(run)
    return decision


def decide_and_resume(
    *,
    run: str,
    approved: bool,
    comment: str | None,
    root: Path | None,
    json_: bool,
    quiet: bool,
    force: bool,
    inputs: list[str] | None = None,
    stubs: Path | None = None,
) -> None:
    """Shared body of ``approve`` / ``reject``.

    Order matters (no half-applied state): validate the status, re-load the workflow and apply
    the shared hash guard (:func:`~rayspec.cli.commands.resume.guard_workflow_unchanged`: a
    changed workflow is exit 2 unless ``--force``), re-obtain the secret inputs and the
    stub script *before* the decision is written to ``run.json``; only then resume
    non-interactively.
    """
    ctx = common.make_runs_context(root)
    store, record = common.lookup_run(ctx, run)
    word = "approve" if approved else "reject"
    if record.status is not RunStatus.PAUSED or record.pause is None:
        fail(
            f"run {record.run_id} is {record.status.value}, not paused — nothing to {word}",
            hint="only a run awaiting approval (status paused) takes a decision",
        )
        return
    resolved = guard_workflow_unchanged(ctx, record, force=force)  # the shared guard
    secret_provider = secret_provider_for(ctx, record)  # one provider per command
    secrets = resume_secret_inputs(  # re-fetched from the configured source
        record, resolved, inputs or [], provider=secret_provider
    )
    stub_script, stubs_path = resume_stub_script(
        record, resolved, stubs=stubs, dry_run=record.dry_run
    )
    record_decision(store, record, approved=approved, comment=comment or "")
    code = common.resume_run(
        ctx,
        store,
        record,
        force=force,
        yes=False,
        interactive=False,
        json_mode=json_,
        quiet=quiet,
        resolved=resolved,
        inputs=secrets,
        stub_script=stub_script,
        stubs_path=stubs_path,
        secret_provider=secret_provider,  # the same instance, no second helper run
    )
    raise typer.Exit(code=code)


def register(app: typer.Typer) -> None:
    @app.command()
    def approve(  # noqa: PLR0917 - Typer options are positional by construction
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        comment: Annotated[
            str | None, typer.Argument(help="Approval comment (becomes the gate's output).")
        ] = None,
        json_: JsonOption = False,
        quiet: Annotated[
            bool, typer.Option("--quiet", help="Only problems and run-level lines.")
        ] = False,
        force: Annotated[
            bool, typer.Option("--force", help="Resume even if the workflow changed.")
        ] = False,
        inputs: SecretInputsOption = None,
        stubs: StubsOption = None,
        root: RootOption = None,
    ) -> None:
        """Approve the pending gate of a paused run and resume it."""
        decide_and_resume(
            run=run,
            approved=True,
            comment=comment,
            root=root,
            json_=json_,
            quiet=quiet,
            force=force,
            inputs=inputs,
            stubs=stubs,
        )


__all__ = ["decide_and_resume", "record_decision", "register"]

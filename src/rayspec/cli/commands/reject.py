# SPDX-License-Identifier: Apache-2.0
"""`rayspec reject <run> [reason]` — reject the pending gate of a paused run and resume.

The gate's ``on_reject`` decides what happens next (default ``cancel`` ⇒ run cancelled, exit 4;
``continue`` ⇒ the run goes on with ``approved: false``; ``fail`` ⇒ exit 1). Shares its body
with :mod:`rayspec.cli.commands.approve`.
"""

from __future__ import annotations

from typing import Annotated

import typer

from rayspec.cli.commands._loader_common import JsonOption, OutputOption, RootOption, resolve_output
from rayspec.cli.commands.approve import decide_and_resume
from rayspec.cli.commands.lock import LockedOption
from rayspec.cli.commands.resume import SecretInputsOption, StubsOption, WaitSlotOption


def register(app: typer.Typer) -> None:
    @app.command()
    def reject(  # noqa: PLR0917 - Typer options are positional by construction
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        reason: Annotated[
            str | None, typer.Argument(help="Rejection reason (becomes the gate's output).")
        ] = None,
        json_: JsonOption = False,
        output: OutputOption = None,
        quiet: Annotated[
            bool, typer.Option("--quiet", help="Only problems and run-level lines.")
        ] = False,
        force: Annotated[
            bool, typer.Option("--force", help="Resume even if the workflow changed.")
        ] = False,
        inputs: SecretInputsOption = None,
        stubs: StubsOption = None,
        locked: LockedOption = None,
        wait_slot: WaitSlotOption = None,
        root: RootOption = None,
    ) -> None:
        """Reject the pending gate of a paused run (on_reject decides: cancel/continue/fail)."""
        json_ = resolve_output(output, json_)
        decide_and_resume(
            run=run,
            approved=False,
            comment=reason,
            root=root,
            json_=json_,
            quiet=quiet,
            force=force,
            inputs=inputs,
            stubs=stubs,
            locked=locked,
            wait_slot=wait_slot,
        )


__all__ = ["register"]

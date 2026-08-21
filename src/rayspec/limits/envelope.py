# SPDX-License-Identifier: Apache-2.0
"""Cross-run spending envelopes and the consecutive-failure circuit breaker.

Module boundary: the *meaning* of the numbers :mod:`rayspec.limits.ledger` keeps. It decides
whether a run may keep spending and phrases the reason; it never schedules, retries or writes a
record.

The one design decision worth stating: an envelope **pauses** the run, it does not fail it.
``defaults.budget_usd`` inside a workflow is the author saying "this task is not worth more than
this" — exceeding it is a defect, so the run fails. A ceiling in the *policy* is the operator
saying "not more than this per day without me looking" — reaching it is not a defect, it is the
moment the machine was supposed to stop and ask. A paused run keeps its work, keeps its
worktree, and continues where it left off once a person has decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rayspec.limits.ledger import SpendLedger, SpendState

#: ``PauseInfo.reason`` of a run the envelope stopped (an ``approve:`` gate reads ``approval``).
ENVELOPE_PAUSE_REASON = "budget"

#: ``PauseInfo.step`` when the envelope stopped a run before any step finished. Not a step id
#: (no identifier may contain ``<``), so it can never be mistaken for one.
ENVELOPE_PAUSE_STEP = "<run>"


@dataclass(frozen=True, slots=True)
class BudgetEnvelope:
    """The operator's ceilings: ``policy.budget`` plus ``policy.max_consecutive_failures``.

    Every field is optional; ``None`` means "no ceiling". Amounts are USD.
    """

    per_run: float | None = None
    per_day: float | None = None
    per_month: float | None = None
    max_consecutive_failures: int | None = None

    @property
    def active(self) -> bool:
        """Whether anything at all is capped (an inactive envelope costs nothing)."""
        return any(
            value is not None
            for value in (
                self.per_run,
                self.per_day,
                self.per_month,
                self.max_consecutive_failures,
            )
        )

    @property
    def spends(self) -> bool:
        """Whether a money ceiling is set — the ledger is only consulted when one is."""
        return any(v is not None for v in (self.per_run, self.per_day, self.per_month))


def envelope_reason(
    envelope: BudgetEnvelope, state: SpendState, run_usd: float | None
) -> str | None:
    """The first money ceiling ``state``/``run_usd`` exceed, phrased for a human, else ``None``.

    Strictly greater trips, the same rule the in-workflow circuit breaker uses. A run whose cost
    is unknown (no provider figure and no pricing table) cannot trip a money ceiling — there is
    nothing to compare.
    """
    checks = (
        ("per_run", run_usd, envelope.per_run, "this run"),
        ("per_day", state.day_usd, envelope.per_day, "today"),
        ("per_month", state.month_usd, envelope.per_month, "this month"),
    )
    for knob, amount, cap, label in checks:
        if cap is None or amount is None or amount <= cap:
            continue
        return (
            f"spending envelope reached ({label} ${amount:.3f} > policy budget.{knob} ${cap:.3f})"
        )
    return None


def failure_breaker_reason(envelope: BudgetEnvelope, state: SpendState) -> str | None:
    """The circuit-breaker reason when too many runs failed in a row, else ``None``.

    ``>=`` (not ``>``): ``max_consecutive_failures: 3`` means the fourth run does not start.
    """
    cap = envelope.max_consecutive_failures
    if cap is None or state.consecutive_failures < cap:
        return None
    return (
        f"circuit breaker open ({state.consecutive_failures} consecutive failed runs "
        f">= policy max_consecutive_failures {cap})"
    )


class RunEnvelope:
    """One run's view of the envelope: commit what it spent, ask whether it may go on.

    Created by the CLI (from the policy) and handed to the engine. Blocking file IO — the
    engine calls it from a worker thread.
    """

    def __init__(
        self,
        envelope: BudgetEnvelope,
        ledger: SpendLedger,
        *,
        run_id: str,
        started_at: datetime,
        waived: bool = False,
    ) -> None:
        self.envelope = envelope
        self.ledger = ledger
        self.run_id = run_id
        self.started_at = started_at
        #: set when an operator approved a paused run: the ceilings no longer stop THIS run
        self.waived = waived

    @property
    def active(self) -> bool:
        """Whether this run is subject to any ceiling at all."""
        return self.envelope.active and not self.waived

    def check(self, run_usd: float | None) -> str | None:
        """Commit ``run_usd`` as this run's total so far and return the ceiling it reached.

        Committing first is deliberate: the ledger must include this run's spend before the day
        total is read, or two runs racing each other would each see only the other's.
        """
        if not self.active:
            return None
        if self.envelope.spends:
            state = self.ledger.commit(self.run_id, run_usd, when=self.started_at)
        else:
            state = self.ledger.read(when=self.started_at)
        reason = failure_breaker_reason(self.envelope, state)
        if reason is not None:
            return reason
        return envelope_reason(self.envelope, state, run_usd)

    def commit_final(self, run_usd: float | None) -> None:
        """Record the run's final total (called once the run has a final status)."""
        if not self.envelope.spends:
            return
        self.ledger.commit(self.run_id, run_usd, when=self.started_at)

    def record_outcome(self, *, failed: bool) -> None:
        """Advance or reset the consecutive-failure counter."""
        if self.envelope.max_consecutive_failures is None:
            return
        self.ledger.record_outcome(failed=failed)

    def waive(self) -> None:
        """An operator approved the paused run: stop stopping it, and close the breaker."""
        self.waived = True
        if self.envelope.max_consecutive_failures is not None:
            self.ledger.reset_failures()


__all__ = [
    "ENVELOPE_PAUSE_REASON",
    "ENVELOPE_PAUSE_STEP",
    "BudgetEnvelope",
    "RunEnvelope",
    "envelope_reason",
    "failure_breaker_reason",
]

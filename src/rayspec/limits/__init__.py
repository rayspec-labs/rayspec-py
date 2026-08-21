# SPDX-License-Identifier: Apache-2.0
"""Operational limits: what keeps one machine running many workflows from hurting itself.

Four independent pieces, all of them **local, single-user, single-machine**:

* :mod:`rayspec.limits.lockfile` — ``.rayspec/rayspec.lock`` pins the literal model id and
  effort every agent resolves to, so a provider cannot silently change what ``sonnet`` means
  between a review and a merge;
* :mod:`rayspec.limits.ledger` — a flock-guarded spend ledger under the project's store, so a
  budget can span runs instead of restarting at zero every time;
* :mod:`rayspec.limits.envelope` — the cross-run spending envelope and the consecutive-failure
  circuit breaker, which **pause** a run (a ceiling is a "stop and look", not an error);
* :mod:`rayspec.limits.slots` — host-level run slots (``flock`` files), so a scheduler that
  fires five workflows at once does not start five agents at once.

Nothing here talks to a network, a server or another user's state. The ledger and the slot
files live under ``$RAYSPEC_HOME``; they hold ids, counts and amounts and never an input value.
"""

from __future__ import annotations

from rayspec.limits.envelope import (
    BudgetEnvelope,
    RunEnvelope,
    envelope_reason,
    failure_breaker_reason,
)
from rayspec.limits.ledger import SpendLedger, SpendState, ledger_path
from rayspec.limits.lockfile import (
    LOCKFILE_NAME,
    LOCKFILE_VERSION,
    LockDrift,
    LockEntry,
    Lockfile,
    LockfileError,
    check_locked,
    load_lockfile,
    lock_entries_for,
    locked_default,
    lockfile_path,
    merged_workflows,
    parse_lockfile,
    write_lockfile,
)
from rayspec.limits.policy import LimitsPolicy, limits_for, limits_policy
from rayspec.limits.slots import (
    SLOT_POLL_S,
    RunSlot,
    SlotBusyError,
    SlotHolder,
    acquire_slots,
    slot_dir,
)

__all__ = [
    "LOCKFILE_NAME",
    "LOCKFILE_VERSION",
    "SLOT_POLL_S",
    "BudgetEnvelope",
    "LimitsPolicy",
    "LockDrift",
    "LockEntry",
    "Lockfile",
    "LockfileError",
    "RunEnvelope",
    "RunSlot",
    "SlotBusyError",
    "SlotHolder",
    "SpendLedger",
    "SpendState",
    "acquire_slots",
    "check_locked",
    "envelope_reason",
    "failure_breaker_reason",
    "ledger_path",
    "limits_for",
    "limits_policy",
    "load_lockfile",
    "lock_entries_for",
    "locked_default",
    "lockfile_path",
    "merged_workflows",
    "parse_lockfile",
    "slot_dir",
    "write_lockfile",
]

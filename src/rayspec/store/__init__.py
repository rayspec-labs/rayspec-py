# SPDX-License-Identifier: Apache-2.0
"""Run store: the checkpoint (run.json + outputs + events) and its file-based implementation.

Exports only — see :mod:`rayspec.store.model` (records), :mod:`rayspec.store.base` (protocol),
:mod:`rayspec.store.file` (``FileRunStore``) and :mod:`rayspec.store.redacting` (the redaction
boundary a third-party store sits behind).

The discovery helpers are re-exported from :mod:`rayspec.registry`, which is where stores are
registered (builtin ``file``, plus whatever is installed under the ``rayspec.stores`` entry-point
group): ``create_store``, ``list_stores``, ``register_store``, ``StoreContext``,
``StoreRegistration``.
"""

from rayspec.registry import (
    StoreContext,
    StoreRegistration,
    create_store,
    list_stores,
    register_store,
)
from rayspec.store.base import RunStore
from rayspec.store.file import (
    AmbiguousRunIdError,
    CorruptRunError,
    FileRunStore,
    RunExistsError,
    StoreError,
    UnknownRunIdError,
    WrittenArtifact,
    WrittenOutput,
)
from rayspec.store.model import (
    RUN_RECORD_SCHEMA_VERSION,
    ArtifactRef,
    Decision,
    EachInfo,
    ErrorInfo,
    LoopInfo,
    PauseInfo,
    RunRecord,
    SessionRef,
    StepRecord,
    WorkspaceInfo,
    new_run_id,
    utcnow,
)
from rayspec.store.redacting import RedactingStore

__all__ = [
    "RUN_RECORD_SCHEMA_VERSION",
    "AmbiguousRunIdError",
    "ArtifactRef",
    "CorruptRunError",
    "Decision",
    "EachInfo",
    "ErrorInfo",
    "FileRunStore",
    "LoopInfo",
    "PauseInfo",
    "RedactingStore",
    "RunExistsError",
    "RunRecord",
    "RunStore",
    "SessionRef",
    "StepRecord",
    "StoreContext",
    "StoreError",
    "StoreRegistration",
    "UnknownRunIdError",
    "WorkspaceInfo",
    "WrittenArtifact",
    "WrittenOutput",
    "create_store",
    "list_stores",
    "new_run_id",
    "register_store",
    "utcnow",
]

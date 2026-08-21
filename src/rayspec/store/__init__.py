# SPDX-License-Identifier: Apache-2.0
"""Run store: the checkpoint (run.json + outputs + events) and its file-based implementation.

Exports only — see :mod:`rayspec.store.model` (records), :mod:`rayspec.store.base` (protocol)
and :mod:`rayspec.store.file` (``FileRunStore``).
"""

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
    "RunExistsError",
    "RunRecord",
    "RunStore",
    "SessionRef",
    "StepRecord",
    "StoreError",
    "UnknownRunIdError",
    "WorkspaceInfo",
    "WrittenArtifact",
    "WrittenOutput",
    "new_run_id",
    "utcnow",
]

# SPDX-License-Identifier: Apache-2.0
"""Policy: the guardrails a project or a machine puts around every rayspec run.

Boundary: this package owns ``policy.yaml`` — its schema, its layering and its enforcement — and
``.rayspec/trusted.yaml``. It depends on :mod:`rayspec.schema` (the strict model base),
:mod:`rayspec.config` (where ``$RAYSPEC_HOME`` is) and, lazily, on the loader's YAML reader.
It never imports the engine, a provider or the store, and it performs no IO beyond reading and
writing those two local files.

Everything here is local by construction: policy is read from files on this machine, and there is
no key that fetches it from elsewhere.
"""

from rayspec.policy.layers import (
    LAYER_NAMES,
    POLICY_ENV,
    POLICY_FILENAME,
    ChangeGuard,
    EffectivePolicy,
    PolicyError,
    PolicyLayer,
    PolicyPath,
    PolicySource,
    load_policy,
    merged_summary,
    policy_paths,
    sources_text,
)
from rayspec.policy.model import (
    ACCESS_ORDER,
    AccessPolicy,
    McpPolicy,
    ModelsPolicy,
    Policy,
    ProvidersPolicy,
    ToolsPolicy,
    TrustPolicy,
    WorkspacePolicy,
    access_rank,
)

__all__ = [
    "ACCESS_ORDER",
    "LAYER_NAMES",
    "POLICY_ENV",
    "POLICY_FILENAME",
    "AccessPolicy",
    "ChangeGuard",
    "EffectivePolicy",
    "McpPolicy",
    "ModelsPolicy",
    "Policy",
    "PolicyError",
    "PolicyLayer",
    "PolicyPath",
    "PolicySource",
    "ProvidersPolicy",
    "ToolsPolicy",
    "TrustPolicy",
    "WorkspacePolicy",
    "access_rank",
    "load_policy",
    "merged_summary",
    "policy_paths",
    "sources_text",
]

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

from rayspec.policy.apply import (
    apply_policy,
    policy_root,
    problem_line,
)
from rayspec.policy.enforce import (
    ALLOWED_PROVIDER_OPTIONS,
    COMMAND_POLICY_CAPABILITY,
    SAFE_APPROVAL_MODE,
    AllowedOption,
    ControlsInForce,
    PolicyProblem,
    PolicyReport,
    agent_control_sources,
    check_agent_controls,
    check_policy,
    check_provider_options,
)
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
    policy_note,
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
from rayspec.policy.trust import (
    TRUSTED_FILENAME,
    TrustEntry,
    TrustStore,
    trusted_path,
)

__all__ = [
    "ACCESS_ORDER",
    "ALLOWED_PROVIDER_OPTIONS",
    "COMMAND_POLICY_CAPABILITY",
    "LAYER_NAMES",
    "POLICY_ENV",
    "POLICY_FILENAME",
    "SAFE_APPROVAL_MODE",
    "TRUSTED_FILENAME",
    "AccessPolicy",
    "AllowedOption",
    "ChangeGuard",
    "ControlsInForce",
    "EffectivePolicy",
    "McpPolicy",
    "ModelsPolicy",
    "Policy",
    "PolicyError",
    "PolicyLayer",
    "PolicyPath",
    "PolicyProblem",
    "PolicyReport",
    "PolicySource",
    "ProvidersPolicy",
    "ToolsPolicy",
    "TrustEntry",
    "TrustPolicy",
    "TrustStore",
    "WorkspacePolicy",
    "access_rank",
    "agent_control_sources",
    "apply_policy",
    "check_agent_controls",
    "check_policy",
    "check_provider_options",
    "load_policy",
    "policy_note",
    "policy_paths",
    "policy_root",
    "problem_line",
    "sources_text",
    "trusted_path",
]

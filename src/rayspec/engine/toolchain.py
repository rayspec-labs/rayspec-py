# SPDX-License-Identifier: Apache-2.0
"""The toolchain record: which rayspec, Python, provider SDK/CLI and models ran a run.

Module boundary: one best-effort probe, called once at run start by
:mod:`rayspec.engine.runner`, whose result is stored as ``RunRecord.toolchain``. Until now that
information existed only transiently in ``rayspec doctor`` / ``rayspec plan`` output, so a run
finished months ago could not say which CLI or model produced it.

Best effort means exactly that: every provider is probed under its own timeout and every failure
is recorded as an ``error`` entry. A toolchain probe must never fail, slow down or otherwise
change a run.
"""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING, Any

import anyio

from rayspec import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rayspec.engine.context import RunContext

#: Seconds one provider's ``healthcheck(probe=False)`` may take before it is recorded as an error.
TOOLCHAIN_TIMEOUT_S = 10.0


async def _provider_entry(
    ctx: RunContext, provider_id: str, timeout_s: float, into: dict[str, Any]
) -> None:
    """Record ``{sdk_version, cli_version, cli_path}`` of one provider (``error`` when unreachable).

    The provider is taken from the pool with :meth:`ProviderPool.peek`, which does NOT open it:
    ``open()`` acquires per-run resources (a CLI subprocess, a worker pool) and would put that
    cost on every run for a metadata field, even for prompt steps that never execute.
    """
    entry: dict[str, Any] = {"sdk_version": None, "cli_version": None, "cli_path": None}
    into[provider_id] = entry
    try:
        with anyio.fail_after(timeout_s):
            provider = await ctx.providers.peek(provider_id)
            health = await provider.healthcheck(probe=False)
    except Exception as exc:  # unreachable/not installed/timed out — record it, never raise
        entry["error"] = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return
    entry["sdk_version"] = health.sdk_version
    entry["cli_version"] = health.cli_version
    entry["cli_path"] = health.cli_path


async def capture_toolchain(
    ctx: RunContext, *, timeout_s: float = TOOLCHAIN_TIMEOUT_S
) -> dict[str, Any]:
    """What is in effect for this run: rayspec, Python, platform, providers and resolved models.

    Only the agents the workflow's prompt steps actually resolve to are probed, so a workflow
    without prompt steps never instantiates a provider, and no provider is ever OPENED by the
    probe (see :func:`_provider_entry`). The providers are probed concurrently, so the whole
    capture is bounded by one ``timeout_s``. ``models`` maps the resolved agent key
    (``agents.reviewer``, ``file:.rayspec/agents/x.yaml``, ``inline:<step path>``) to the literal
    model id that was resolved for it — ``None`` when the provider's own default applies. In a
    dry run the provider entries describe the stub that stood in.
    """
    resolved = ctx.resolved
    models: dict[str, str | None] = {}
    provider_ids: list[str] = []
    for key in sorted(set(resolved.step_agents.values())):
        agent = resolved.agents.get(key)
        if agent is None:  # pragma: no cover - a step agent always resolves
            continue
        models[key] = agent.model
        effective = ctx.providers.key_for(agent.provider)
        if effective not in provider_ids:
            provider_ids.append(effective)
    entries: dict[str, Any] = {}
    async with anyio.create_task_group() as tg:  # one timeout for all of them, not one each
        for provider_id in sorted(provider_ids):
            tg.start_soon(_provider_entry, ctx, provider_id, timeout_s, entries)
    providers = {provider_id: entries[provider_id] for provider_id in sorted(entries)}
    return {
        "rayspec": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "providers": providers,
        "models": models,
    }


__all__ = ["TOOLCHAIN_TIMEOUT_S", "capture_toolchain"]

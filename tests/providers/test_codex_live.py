"""Live smoke test for the Codex adapter: a real ``codex app-server`` turn.

Opt in with ``RAYSPEC_LIVE=1`` (needs ``codex login`` or ``OPENAI_API_KEY``); deselected
otherwise. Run with ``uv run pytest -m live tests/providers/test_codex_live.py``.

The resume test checks the usage-baseline inference (``total - last`` of the first update on a
thread the provider instance has never seen) against the exact delta of the cumulative totals the
server reports; it creates one persisted (non-ephemeral) thread under ``~/.codex``.
"""

from __future__ import annotations

import os

import pytest

from rayspec.providers.base import AccessLevel, AgentEvent, AgentRequest, Usage
from rayspec.providers.codex import CodexProvider, usage_delta

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        not os.environ.get("RAYSPEC_LIVE"), reason="set RAYSPEC_LIVE=1 to hit Codex"
    ),
]


async def test_live_codex_smoke(tmp_path):
    events: list[AgentEvent] = []

    async def collect(event: AgentEvent) -> None:
        events.append(event)

    provider = CodexProvider({})
    await provider.open(run_id="live", workdir=str(tmp_path), env={}, max_parallel=1)
    try:
        health = await provider.healthcheck()
        assert health.ok, health.details
        assert health.cli_path and health.sdk_version
        result = await provider.run(
            AgentRequest(
                step_path="smoke",
                prompt="Reply with exactly OK",
                cwd=str(tmp_path),
                access=AccessLevel.READ_ONLY,
                timeout_s=180,
                provider_options={"codex": {"ephemeral": True}},
            ),
            collect,
        )
    finally:
        await provider.aclose()
    assert result.status == "success", result
    assert "OK" in result.text
    assert result.session_ref
    assert result.usage.total > 0
    assert any(e.kind == "text" for e in events)


def _request(tmp_path, prompt: str, **kw) -> AgentRequest:
    return AgentRequest(
        step_path="resume",
        prompt=prompt,
        cwd=str(tmp_path),
        access=AccessLevel.READ_ONLY,
        timeout_s=180,
        **kw,
    )


async def test_live_codex_resume_usage_inference_matches_server_totals(tmp_path):
    async def discard(_event: AgentEvent) -> None:
        return None

    first = CodexProvider({})
    await first.open(run_id="live-a", workdir=str(tmp_path), env={}, max_parallel=1)
    try:
        r1 = await first.run(_request(tmp_path, "Reply with exactly ONE"), discard)
    finally:
        await first.aclose()
    assert r1.status == "success" and r1.session_ref, r1
    total1 = r1.raw["usage_total"]
    assert total1 is not None and total1["input"] > 0

    # a fresh provider instance (= a later run) resumes the thread without a baseline: its usage
    # is inferred from the first tokenUsage update and must equal the exact delta of the totals
    second = CodexProvider({})
    await second.open(run_id="live-b", workdir=str(tmp_path), env={}, max_parallel=1)
    try:
        r2 = await second.run(
            _request(tmp_path, "Reply with exactly TWO", resume_session=r1.session_ref), discard
        )
    finally:
        await second.aclose()
    assert r2.status == "success" and r2.session_ref == r1.session_ref, r2
    total2 = r2.raw["usage_total"]
    assert total2 is not None
    exact = usage_delta(Usage(**total2), Usage(**total1))
    assert r2.usage == exact, (r2.usage, exact, total1, total2)

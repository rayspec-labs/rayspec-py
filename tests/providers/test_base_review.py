from __future__ import annotations

import dataclasses
import json
import time
from typing import get_args

from rayspec.events.model import StreamRecord
from rayspec.providers.base import (
    AccessLevel,
    AgentEvent,
    AgentRequest,
    EffortLevel,
    Provider,
    ProviderCapabilities,
    ProviderHealth,
)
from rayspec.schema.common import AccessLevelName, EffortName


def test_schema_literals_match_provider_literals():
    assert set(get_args(EffortName)) == set(get_args(EffortLevel))
    assert set(get_args(AccessLevelName)) == {a.value for a in AccessLevel}


def test_capabilities_to_dict_is_json_serialisable():
    caps = ProviderCapabilities(
        structured_output="enforced",
        session_resume=True,
        session_fork=True,
        instructions_modes=frozenset({"append"}),
        access_levels=frozenset({AccessLevel.FULL}),
        tool_groups=frozenset({"read"}),
        raw_tool_names=True,
        max_turns=True,
        budget_usd=True,
        cost_reporting=True,
        effort_levels=frozenset({"low"}),
        effort_aliases={"minimal": "low"},
    )
    data = caps.to_dict()
    assert json.dumps(data)
    assert data["effort_aliases"] == {"minimal": "low"} and data["access_levels"] == ["full"]
    assert hash(caps) and dataclasses.replace(caps, max_turns=False).max_turns is False


def test_agent_request_has_thinking():
    assert AgentRequest(step_path="a", prompt="p", cwd="/").thinking is None


def test_agent_event_ts_is_honoured_by_stream_record():
    now = time.time()
    rec = StreamRecord.from_agent_event(AgentEvent(kind="text", text="x", ts=now))
    assert abs(rec.ts.timestamp() - now) < 1e-3
    unset = StreamRecord.from_agent_event(AgentEvent(kind="text", text="x"))
    assert abs(unset.ts.timestamp() - time.time()) < 5


def test_provider_protocol_runtime_check():
    class Dummy:
        id = "dummy"
        capabilities = None

        async def open(self, *, run_id: str, workdir: str, env, max_parallel: int) -> None: ...
        async def run(self, req, emit): ...
        async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
            return ProviderHealth(ok=True)

        async def aclose(self) -> None: ...

    assert isinstance(Dummy(), Provider)

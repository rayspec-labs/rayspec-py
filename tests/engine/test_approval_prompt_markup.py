"""The approval key hints must survive Rich markup (seen live: `pprove / eject / iew`).

Uses the REAL ``Console.input`` (markup on) with ``builtins.input`` patched — the scripted
console in test_approval_prompt.py prints prompts with ``markup=False`` and could not catch this.
"""

from __future__ import annotations

import builtins
import io

import pytest
from rich.console import Console

from rayspec.engine.approval import ApprovalRequest, ConsoleApprovalPrompt

pytestmark = pytest.mark.anyio


async def test_key_hints_are_rendered_literally(monkeypatch, tmp_path):
    buf = io.StringIO()
    console = Console(file=buf, width=160, force_terminal=False, color_system=None)
    answers = iter(["a", ""])
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: next(answers))
    req = ApprovalRequest(
        run_id="r1", step_path="gate", message="Ship it?", attempt=1, workdir=str(tmp_path)
    )
    decision = await ConsoleApprovalPrompt(console)(req)
    text = buf.getvalue()
    assert "[a]pprove / [r]eject / [v]iew / [d]iff / [p]ause" in text, text
    assert decision is not None and decision.approved

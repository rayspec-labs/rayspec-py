"""Approval panel rendering: untrusted text is neutralised and the totals line uses the
run-level cost marker. The gate logic itself is covered in test_approval_prompt.py."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from rayspec.engine import approval
from rayspec.engine.approval import (
    ApprovalNeed,
    ApprovalRequest,
    ConsoleApprovalPrompt,
    format_totals,
)

pytestmark = pytest.mark.anyio

ESC = "\x1b"
NASTY = f"before{ESC}]0;PWNED\x07{ESC}[31mRED{ESC}[0m [bold red]MARKUP[/] {ESC}[2J after"
CLEAN = "beforeRED [bold red]MARKUP[/]  after"


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=200, force_terminal=False, color_system=None), buf


def _request(tmp_path) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        step_path="gate",
        message=f"Ship {NASTY}?",
        attempt=1,
        workdir=str(tmp_path),
        needs=(
            ApprovalNeed(
                path="build",
                status="succeeded",
                duration_ms=1200,
                cost_usd=0.5,
                cost_source="table",
                tail=f"tail {NASTY}\nsecond",
                output=f"full {NASTY}",
            ),
        ),
        totals={"steps": 2, "tokens": 70_100, "cost_usd": 0.02, "cost_source": "partial"},
    )


def test_panel_neutralises_escapes_in_message_needs_and_git_info(tmp_path) -> None:
    console, buf = _console()
    prompt = ConsoleApprovalPrompt(console)
    prompt.render(_request(tmp_path), git_info=f"$ git status --short\n M {NASTY}")
    text = buf.getvalue()
    assert ESC not in text and "\x07" not in text
    assert text.count(CLEAN) == 3, text  # message, tail, git info
    assert "~$0.50" in text  # the need's table estimate keeps its marker
    assert "cost: ≥$0.02" in text  # run totals: partial → lower bound


def test_view_prints_outputs_as_plain_text(tmp_path) -> None:
    console, buf = _console()
    ConsoleApprovalPrompt(console).view(_request(tmp_path))
    text = buf.getvalue()
    assert ESC not in text
    assert f"full {CLEAN}" in text  # markup literal, escapes gone


async def test_diff_output_is_neutralised(tmp_path, monkeypatch) -> None:
    console, buf = _console()
    monkeypatch.setattr(approval, "git_diff", lambda _workdir: f"+ {NASTY}")
    monkeypatch.setattr(approval, "git_summary", lambda _workdir: "")
    answers = iter(["d", "p"])
    monkeypatch.setattr(
        ConsoleApprovalPrompt, "_input", lambda self, prompt: _answer(next(answers))
    )
    decision = await ConsoleApprovalPrompt(console)(_request(tmp_path))
    assert decision is None
    text = buf.getvalue()
    assert ESC not in text and f"+ {CLEAN}" in text


async def _answer(value: str) -> str:
    return value


def test_format_totals_markers() -> None:
    assert "cost: $0.10" in format_totals({"cost_usd": 0.1})
    assert "cost: ~$0.10" in format_totals({"cost_usd": 0.1, "cost_source": "table"})
    assert "cost: ≥$0.10" in format_totals({"cost_usd": 0.1, "cost_source": "partial"})
    assert "cost: —" in format_totals({"cost_usd": None, "cost_source": "none"})

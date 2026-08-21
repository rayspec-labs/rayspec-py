"""ConsoleApprovalPrompt: panel contents (needs, git summary), keys a/r/v/d/p, best-effort git."""

from __future__ import annotations

import io
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from rich.console import Console

from rayspec.engine.approval import (
    ApprovalNeed,
    ApprovalRequest,
    ConsoleApprovalPrompt,
    git_diff,
    git_summary,
)

pytestmark = pytest.mark.anyio


class ScriptedConsole(Console):
    """A Rich console writing to a buffer whose ``input`` returns scripted answers."""

    def __init__(self, answers: list[str]) -> None:
        super().__init__(file=io.StringIO(), width=160, force_terminal=False, color_system=None)
        self._answers: Iterator[str] = iter(answers)

    def input(self, prompt: str = "", *args, **kwargs) -> str:  # type: ignore[override]
        self.print(prompt, end="", markup=False)
        try:
            answer = next(self._answers)
        except StopIteration:
            raise EOFError from None
        self.print(answer, markup=False)
        return answer

    @property
    def text(self) -> str:
        assert isinstance(self.file, io.StringIO)
        return self.file.getvalue()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("old\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "file.txt").write_text("new\n")
    return repo


def _request(workdir: Path) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        step_path="gate",
        message="Ship it?",
        attempt=1,
        workdir=str(workdir),
        needs=[
            ApprovalNeed(
                path="build",
                status="succeeded",
                duration_ms=1200,
                cost_usd=0.5,
                tail="line 99\nline 100",
                output="\n".join(f"line {i}" for i in range(1, 101)),
            )
        ],
        totals={"steps": 2, "tokens": 10, "cost_usd": 0.5},
    )


async def test_panel_view_diff_and_approve_with_comment(repo: Path) -> None:
    console = ScriptedConsole(["v", "d", "a", "ship it"])
    answer = await ConsoleApprovalPrompt(console)(_request(repo))
    assert answer is not None and answer.approved is True and answer.comment == "ship it"
    out = console.text
    assert "Approval required — gate (attempt 1)" in out
    assert "Ship it?" in out and "build  succeeded  1.2s  $0.50" in out
    assert "[a]pprove / [r]eject / [v]iew / [d]iff / [p]ause" in out
    # panel: git status --short / git diff --stat of the workdir
    assert "git status --short" in out and " M file.txt" in out
    assert "git diff --stat" in out and "1 file changed" in out
    # [v]iew prints the full upstream output (not just the tail)
    assert re.search(r"\bline 1\s", out) and "line 50" in out
    # [d]iff prints the diff of the workdir
    assert "-old" in out and "+new" in out


async def test_reject_with_reason_and_unknown_key(tmp_path: Path) -> None:
    console = ScriptedConsole(["x", "r", "not yet"])
    answer = await ConsoleApprovalPrompt(console)(_request(tmp_path))
    assert answer is not None and answer.approved is False and answer.comment == "not yet"
    assert "please answer a, r, v, d or p" in console.text
    # not a git repository: no git section in the panel, no crash
    assert "git status" not in console.text


async def test_pause_key_and_eof_return_none(tmp_path: Path) -> None:
    assert await ConsoleApprovalPrompt(ScriptedConsole(["p"]))(_request(tmp_path)) is None
    assert await ConsoleApprovalPrompt(ScriptedConsole([]))(_request(tmp_path)) is None


def test_git_helpers_are_best_effort(tmp_path: Path, repo: Path) -> None:
    assert git_summary(str(tmp_path)) == ""
    assert "not a git repository" in git_diff(str(tmp_path))
    assert " M file.txt" in git_summary(str(repo))
    assert "+new" in git_diff(str(repo))
    (repo / "file.txt").write_text("old\n")
    assert git_diff(str(repo)) == "(no changes in the workdir)"


# -- escape-sequence tolerant input + formatted panel numbers ----------------------------------


def test_clean_answer_strips_escape_sequences_and_control_chars() -> None:
    from rayspec.engine.approval import clean_answer

    # arrow keys (CSI), a lone ESC, bell/backspace noise: never part of the answer
    assert clean_answer("\x1b[C\x1b[Da") == "a"
    assert clean_answer("\x1b[1;5Dship it\x07") == "ship it"
    assert clean_answer("\x1bOA\x1b[200~yes\x1b[201~") == "yes"
    # Meta/Alt keys arrive as ``ESC <printable>`` without readline: Alt-a must not approve
    assert clean_answer("\x1ba") == ""
    assert clean_answer("\x1bar") == "r"
    # OSC sequences (``ESC ] … BEL`` / ``ESC ] … ESC \\``, e.g. a pasted terminal title)
    assert clean_answer("\x1b]0;title\x07a") == "a"
    assert clean_answer("\x1b]8;;http://x\x1b\\a") == "a"
    assert clean_answer("plain text") == "plain text"
    assert clean_answer("tabs\tkept and unicode ✓") == "tabs\tkept and unicode ✓"


def test_humanize_duration_and_cost_slot() -> None:
    from rayspec.engine.approval import fmt_cost, humanize_duration

    assert humanize_duration(300) == "0.3s"
    assert humanize_duration(1200) == "1.2s"
    assert humanize_duration(59_940) == "59.9s"
    assert humanize_duration(59_960) == "1m 0s"
    assert humanize_duration(1_911_900) == "31m 52s"
    assert humanize_duration(3_780_000) == "1h 3m"
    assert humanize_duration(None) == "—"
    assert fmt_cost(None) == "—"
    assert fmt_cost(0.5) == "$0.50"
    assert fmt_cost(0.5, "table") == "~$0.50"


async def test_arrow_keys_do_not_corrupt_the_answer(tmp_path: Path) -> None:
    console = ScriptedConsole(["\x1b[C\x1b[D", "\x1b[Aa", "\x1b[Dship\x1b[C it"])
    answer = await ConsoleApprovalPrompt(console)(_request(tmp_path))
    assert answer is not None and answer.approved is True and answer.comment == "ship it"
    # the first line was only arrow keys: re-asked, not treated as a reject/pause
    assert console.text.count("[a]pprove / [r]eject") == 2
    assert "^[" not in console.text


async def test_panel_formats_durations_and_unknown_costs(tmp_path: Path) -> None:
    request = ApprovalRequest(
        run_id="r1",
        step_path="gate",
        message="Ship it?",
        attempt=2,
        workdir=str(tmp_path),
        needs=[
            ApprovalNeed(path="slow", status="succeeded", duration_ms=1_911_900, cost_usd=None),
            ApprovalNeed(
                path="est", status="succeeded", duration_ms=1200, cost_usd=0.5, cost_source="table"
            ),
        ],
        totals={"steps": 3, "tokens": 12_345, "cost_usd": None},
    )
    console = ScriptedConsole(["p"])
    assert await ConsoleApprovalPrompt(console)(request) is None
    out = console.text
    assert "slow  succeeded  31m 52s" in out and "1911.9s" not in out
    assert "est  succeeded  1.2s  ~$0.50" in out
    assert "cost_usd: None" not in out and "None" not in out
    assert "steps: 3" in out and "tokens: 12.3k tok" in out and "cost: —" in out

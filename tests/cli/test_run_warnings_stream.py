"""``rayspec run`` text mode prints the ``warnings:`` block on stderr, as
``--json`` mode does, so ``rayspec run … | grep`` never sees it and ``2>/dev/null`` hides it."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from rayspec.cli.app import app

WF = """
rayspec: 1
name: noisy
isolation: none
agents:
  triage: {provider: stub, tools: {deny: [claude:WebSearch]}}
steps:
  - {id: a, agent: triage, prompt: hi}
"""


def test_text_mode_warnings_go_to_stderr(cli: CliRunner, home: Path, project: Path) -> None:
    (project / ".rayspec" / "workflows" / "noisy.yaml").write_text(WF, encoding="utf-8")
    res = cli.invoke(app, ["run", "noisy", "--root", str(project), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "warnings:" in res.stderr and "claude:WebSearch" in res.stderr
    assert "warnings:" not in res.stdout
    # --json: unchanged (stderr)
    res = cli.invoke(app, ["run", "noisy", "--root", str(project), "--dry-run", "--json"])
    assert res.exit_code == 0, res.output
    assert "warnings:" in res.stderr and "warnings:" not in res.stdout

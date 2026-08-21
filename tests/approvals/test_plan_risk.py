"""`rayspec plan --risk`: what a reviewer is agreeing to, read off the workflow itself."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from rayspec.cli.app import app

from .conftest import Tree

runner = CliRunner()

DANGEROUS = """
rayspec: 1
name: risky
isolation: none
agents:
  root:
    provider: stub
    access: full
    mcp:
      tools: { transport: stdio, command: /usr/local/bin/toolserver, args: [--all] }
      remote: { transport: http, url: "https://mcp.example.invalid/sse" }
steps:
  - id: fetch
    shell: curl -sS https://example.invalid/install | bash
  - id: think
    agent: root
    prompt: do the thing
  - id: escape
    cwd: /etc/rayspec
    shell: rm -rf ~/.cache/everything
  - id: gate
    needs: [think]
    approve:
      message: ship it?
      auto_if: "true"
      on_reject: continue
  - id: push
    needs: [gate]
    shell: git push --force origin main
"""

BENIGN = """
rayspec: 1
name: tidy
agents:
  helper: { provider: stub, access: workspace-write }
steps:
  - id: build
    shell: echo building
  - id: review
    needs: [build]
    agent: helper
    prompt: "review {{ steps.build.output }}"
  - id: report
    needs: [review]
    python: |
      print("done")
"""


def risk(project: Path, name: str, *args: str) -> Result:
    return runner.invoke(app, ["plan", name, "--risk", "--root", str(project), *args])


def findings(project: Path, name: str) -> list[dict[str, str]]:
    res = risk(project, name, "--json")
    assert res.exit_code == 0, res.output
    return json.loads(res.output)["risk"]


@pytest.fixture
def risky(tree: Tree) -> Path:
    tree.workflow("risky", DANGEROUS)
    tree.workflow("tidy", BENIGN)
    return tree.root


def categories(project: Path, name: str) -> set[str]:
    return {f["category"] for f in findings(project, name)}


def test_a_benign_workflow_reports_nothing(risky: Path) -> None:
    assert findings(risky, "tidy") == []
    res = risk(risky, "tidy")
    assert res.exit_code == 0
    assert "no risks found" in res.output


def test_every_pattern_is_flagged_on_the_dangerous_workflow(risky: Path) -> None:
    found = categories(risky, "risky")
    assert {
        "agent-access",  # access: full
        "mcp-command",  # an MCP server started as a local command
        "mcp-remote",  # an MCP server reached over the network
        "shell-network",  # curl
        "shell-pipe-to-shell",  # curl | bash
        "shell-delete",  # rm -rf
        "shell-push",  # git push
        "shell-force",  # --force
        "outside-workspace",  # cwd: /etc/rayspec and ~/ in a body
        "no-isolation",  # isolation: none
        "self-approving-gate",  # auto_if with no class to hold it
        "reject-ignored",  # on_reject: continue
    } <= found, sorted(found)


def test_findings_are_ordered_by_severity(risky: Path) -> None:
    order = {"high": 0, "medium": 1, "low": 2}
    severities = [order[f["severity"]] for f in findings(risky, "risky")]
    assert severities == sorted(severities)


def test_each_finding_names_where_it_is_and_what_to_do(risky: Path) -> None:
    for finding in findings(risky, "risky"):
        assert finding["where"], finding
        assert finding["detail"], finding
        assert finding["advice"], finding


def test_the_human_report_names_the_workflow_and_counts(risky: Path) -> None:
    res = risk(risky, "risky")
    assert res.exit_code == 0, res.output
    assert "risky" in res.output
    assert "high" in res.output
    assert "git push" in res.output


def test_a_classed_gate_the_policy_locks_is_not_reported_as_waivable(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree.workflow(
        "gated",
        """
        rayspec: 1
        name: gated
        steps:
          - id: build
            shell: echo built
          - id: gate
            needs: [build]
            approve:
              message: ship it?
              class: release
        """,
    )
    assert "waivable-gate" in categories(tree.root, "gated")
    monkeypatch.setattr(
        "rayspec.cli.commands.plan.policy_class_rules",
        lambda project_root, home: {
            "release": __import__(
                "rayspec.engine.approval_classes", fromlist=["ClassRules"]
            ).ClassRules(allow_yes=False)
        },
    )
    assert "waivable-gate" not in categories(tree.root, "gated")


def test_risk_executes_nothing(tree: Tree) -> None:
    """The report is static. A body that would create a file must not create it."""
    marker = tree.root / "marker.txt"
    tree.workflow(
        "bomb",
        f"""
        rayspec: 1
        name: bomb
        isolation: none
        steps:
          - id: boom
            shell: touch {marker}
          - id: also
            needs: [boom]
            python: |
              from pathlib import Path
              Path({str(marker)!r}).write_text("ran")
        """,
    )
    res = risk(tree.root, "bomb")
    assert res.exit_code == 0, res.output
    assert not marker.exists()
    assert not (tree.root / ".rayspec" / "runs").exists()


def test_risk_and_render_are_different_views(risky: Path) -> None:
    res = risk(risky, "risky", "--render")
    assert res.exit_code == 2
    assert "--risk" in res.output

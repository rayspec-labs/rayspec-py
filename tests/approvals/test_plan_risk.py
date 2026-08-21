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
  helper:
    provider: stub
    access: workspace-write
    tools: { deny: [shell, edit] }
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
    assert "nothing matched" in res.output


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
    assert "unheld-class" in categories(tree.root, "gated")  # nothing holds it yet
    monkeypatch.setattr(
        "rayspec.cli.commands.plan.policy_class_rules",
        lambda project_root, home: {
            "release": __import__(
                "rayspec.engine.approval_classes", fromlist=["ClassRules"]
            ).ClassRules(allow_yes=False)
        },
    )
    held = categories(tree.root, "gated")
    assert "waivable-gate" not in held
    assert "unheld-class" not in held


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


def test_evidence_is_never_parsed_as_markup(tree: Tree) -> None:
    """A shell body is quoted verbatim; it may contain anything, including Rich markup."""
    tree.workflow(
        "markup",
        """
        rayspec: 1
        name: markup
        steps:
          - id: a
            shell: "git push [/bold] origin main"
        """,
    )
    res = risk(tree.root, "markup")
    assert res.exit_code == 0, res.output
    assert "git push [/bold] origin main" in res.output


# --------------------------------------------------------------------------------------------
# What the analysis cannot read
# --------------------------------------------------------------------------------------------


TEMPLATED = """
rayspec: 1
name: templated
inputs:
  cmd: { type: string, default: "echo hi" }
steps:
  - id: go
    shell: "{{ inputs.cmd }}"
"""


def test_a_templated_body_is_a_finding_not_silence(tree: Tree) -> None:
    """A body assembled at run time is the one thing this report cannot read — so it says so."""
    tree.workflow("templated", TEMPLATED)
    found = [f for f in findings(tree.root, "templated") if f["category"] == "templated-body"]
    assert found, findings(tree.root, "templated")
    assert "{{ inputs.cmd }}" in found[0]["detail"]
    assert found[0]["severity"] == "medium"


def test_a_templated_cwd_is_a_finding(tree: Tree) -> None:
    tree.workflow(
        "tcwd",
        """
        rayspec: 1
        name: tcwd
        inputs:
          dir: { type: string, default: build }
        steps:
          - id: go
            cwd: "{{ inputs.dir }}"
            shell: echo hi
        """,
    )
    cats = categories(tree.root, "tcwd")
    assert "templated-body" in cats, cats


def test_the_empty_report_talks_about_the_analysis_not_about_the_workflow(risky: Path) -> None:
    """An empty finding list means nothing matched, never that the workflow is safe."""
    res = risk(risky, "tidy")
    assert res.exit_code == 0, res.output
    assert "leaves the workspace by itself" not in res.output
    assert "nothing matched" in res.output
    assert "assembles at run time" in res.output


AGENTIC = """
rayspec: 1
name: agentic
agents:
  worker:
    provider: stub
    access: workspace-write
    tools: { allow: [shell, edit, web] }
steps:
  - id: work
    agent: worker
    prompt: release the package however you see fit
"""


def test_an_agent_that_may_run_commands_is_reported_with_the_steps_that_use_it(
    tree: Tree,
) -> None:
    tree.workflow("agentic", AGENTIC)
    found = [f for f in findings(tree.root, "agentic") if f["category"] == "agent-tools"]
    assert found, findings(tree.root, "agentic")
    assert "shell" in found[0]["detail"]
    assert "work" in found[0]["detail"]  # the step that drives it


def test_an_agent_may_run_commands_by_default_too(tree: Tree) -> None:
    """No `tools:` at all is not a restriction: the provider's defaults include running
    commands, and a reviewer needs to know that."""
    tree.workflow(
        "default_tools",
        """
        rayspec: 1
        name: default_tools
        agents:
          worker: { provider: stub }
        steps:
          - id: work
            agent: worker
            prompt: do the thing
        """,
    )
    assert "agent-tools" in categories(tree.root, "default_tools")


def test_an_agent_that_cannot_run_commands_is_not_reported(tree: Tree) -> None:
    tree.workflow(
        "read_only",
        """
        rayspec: 1
        name: read_only
        agents:
          reader:
            provider: stub
            tools: { deny: [shell, edit] }
        steps:
          - id: work
            agent: reader
            prompt: read the code
        """,
    )
    assert "agent-tools" not in categories(tree.root, "read_only")


# --------------------------------------------------------------------------------------------
# Where a finding is filed
# --------------------------------------------------------------------------------------------


def test_a_private_key_read_is_outside_the_workspace_not_network(tree: Tree) -> None:
    """`cat ~/.ssh/id_rsa` is not an ssh session; filing it under the network rule buries the
    most dangerous line in the body under the wrong heading."""
    tree.workflow(
        "keys",
        """
        rayspec: 1
        name: keys
        steps:
          - id: peek
            shell: cat /Users/someone/.ssh/id_rsa
        """,
    )
    cats = categories(tree.root, "keys")
    assert "outside-workspace" in cats, cats
    assert "shell-network" not in cats, cats


def test_a_relative_escape_in_a_body_is_reported(tree: Tree) -> None:
    tree.workflow(
        "escape",
        """
        rayspec: 1
        name: escape
        steps:
          - id: peek
            shell: cat ../../.env
        """,
    )
    assert "outside-workspace" in categories(tree.root, "escape")


def test_an_absolute_read_is_reported(tree: Tree) -> None:
    tree.workflow(
        "abs",
        """
        rayspec: 1
        name: abs
        steps:
          - id: peek
            shell: cat /etc/passwd
        """,
    )
    assert "outside-workspace" in categories(tree.root, "abs")


def test_a_real_network_command_is_still_reported(tree: Tree) -> None:
    tree.workflow(
        "net",
        """
        rayspec: 1
        name: net
        steps:
          - id: fetch
            shell: |
              curl -sS https://example.invalid/data
              ssh build@host uptime
        """,
    )
    assert "shell-network" in categories(tree.root, "net")


def test_tmp_and_dev_are_not_reported_as_leaving_the_workspace(tree: Tree) -> None:
    tree.workflow(
        "quiet",
        """
        rayspec: 1
        name: quiet
        steps:
          - id: go
            shell: |
              echo hi > /dev/null
              echo hi > /tmp/scratch
        """,
    )
    assert "outside-workspace" not in categories(tree.root, "quiet")


def test_every_match_of_a_rule_is_reported_not_only_the_first(tree: Tree) -> None:
    """Quoting the first, harmless `rm -rf` as the evidence for a body that also holds
    `rm -rf /` misleads in the direction that matters."""
    tree.workflow(
        "many",
        """
        rayspec: 1
        name: many
        steps:
          - id: clean
            shell: |
              rm -rf build
              echo tidying
              rm -rf /
        """,
    )
    details = " ".join(
        f["detail"] for f in findings(tree.root, "many") if f["category"] == "shell-delete"
    )
    assert "rm -rf build" in details
    assert "rm -rf /" in details


def test_a_raw_block_is_not_reported_as_templated(tree: Tree) -> None:
    """`{% raw %}` is how a body keeps braces the shell needs; nothing is assembled there."""
    tree.workflow(
        "raw",
        """
        rayspec: 1
        name: raw
        steps:
          - id: go
            shell: "gh release list --template '{% raw %}{{range .}}{{.tagName}}{{end}}{% endraw %}'"
        """,
    )
    assert "templated-body" not in categories(tree.root, "raw")


GATED = """
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
"""


def test_a_class_no_policy_holds_is_reported_as_such(tree: Tree) -> None:
    """A named class reads like a lock. When nothing defines it, the report says so instead of
    advising the reader to add the class that is already there."""
    tree.workflow("gated", GATED)
    found = [f for f in findings(tree.root, "gated") if f["category"] == "unheld-class"]
    assert found, findings(tree.root, "gated")
    assert "release" in found[0]["detail"]
    assert found[0]["severity"] == "medium"
    assert "release" in found[0]["advice"]
    assert "give it a class" not in found[0]["advice"]


def test_a_gate_with_no_class_still_reads_as_waivable(tree: Tree) -> None:
    tree.workflow("plain", GATED.replace("      class: release\n", ""))
    found = [f for f in findings(tree.root, "plain") if f["category"] == "waivable-gate"]
    assert found, findings(tree.root, "plain")
    assert "give it a class" in found[0]["advice"]

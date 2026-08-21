"""End-to-end: a realistic .rayspec tree (include + agent file + prompt_file + inputs)."""

from __future__ import annotations

from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import _loader_common
from rayspec.loader import load_workflow, validate_workflow

from .conftest import Tree
from .fakes import FakeChecker, capabilities_for

REVIEW_BLOCK = """
rayspec: 1
name: review_block
description: Reusable review block
inputs:
  target: {type: string, required: true}
  strict: {type: boolean, default: false}
agents:
  reviewer:
    provider: claude
    model: small
    access: read-only
    instructions_file: prompts/reviewer.md
steps:
  - id: lint
    shell: ruff check {{ inputs.target }}
    allow_failure: true
  - id: judge
    needs: [lint]
    agent: reviewer
    prompt_file: prompts/judge.md
    output_schema:
      type: object
      properties: {verdict: {enum: [ok, fix]}}
      required: [verdict]
outputs:
  verdict: "{{ steps.judge.output.verdict }}"
  lint_ok: "{{ steps.lint.ok }}"
"""

FIX_ISSUE = """
rayspec: 1
name: fix_issue
description: Fix an issue in a loop, then review and open a PR.
inputs:
  issue: {type: integer, required: true, description: Issue number}
  base: {type: string, default: main}
defaults:
  agent: implementer
  on_unsupported: error
agents:
  triage:
    provider: claude
    model: small
    access: read-only
steps:
  - id: fetch
    shell: gh issue view "$RAYSPEC_INPUT_ISSUE" --json title,body
  - id: assess
    needs: [fetch]
    agent: triage
    prompt: |
      {{ steps.fetch.output }}
      Worth fixing?
    output_schema: {type: object, properties: {verdict: {enum: [fix, skip]}}, required: [verdict]}
  - id: bail
    needs: [assess]
    when: steps.assess.output.verdict == 'skip'
    stop: {status: cancelled, reason: "skip: {{ steps.assess.output.verdict }}"}
  - id: build
    needs: [assess]
    when: steps.assess.output.verdict == 'fix'
    loop:
      max_iterations: 3
      until: steps.review.output.verdict == 'ok'
      steps:
        - id: implement
          session: implement
          prompt: "Fix it. {{ steps.fetch.output }} prev: {{ iteration.prev.review.output }}"
        - id: review
          needs: [implement]
          include: review_block
          with: {target: "src/", strict: "{{ inputs.base == 'main' }}"}
  - id: confirm
    needs: [build]
    approve: "Open a PR for #{{ inputs.issue }}?"
  - id: pr
    needs: [confirm]
    shell: |
      gh pr create --base "{{ inputs.base }}" --title "fix: #{{ inputs.issue }}"
outputs:
  pr_url: "{{ steps.pr.output }}"
  verdict: "{{ steps.assess.output.verdict }}"
"""


def _populate(tree: Tree) -> None:
    tree.workflow("review_block", REVIEW_BLOCK)
    tree.workflow("fix_issue", FIX_ISSUE)
    tree.agent(
        "implementer",
        "provider: codex\nmodel: medium\neffort: high\nmax_turns: 60\ntools: {deny: [web]}\n",
    )
    tree.write("prompts/reviewer.md", "You review code for {{ inputs.target }}.")
    tree.write("prompts/judge.md", "Lint said: {{ steps.lint.output }}. Verdict?")


def test_end_to_end_load_validate(tree: Tree):
    _populate(tree)
    rw = load_workflow("fix_issue", project_root=tree.root, home=tree.home)
    assert rw.workflow.name == "fix_issue"
    assert "build/review" in rw.includes
    assert rw.includes["build/review"].workflow_name == "review_block"
    assert rw.agent_for("build/implement").name == "implementer"
    assert rw.agent_for("build/implement").max_turns == 60
    assert (
        rw.agent_for("build/review/judge").instructions
        == "You review code for {{ inputs.target }}."
    )
    assert rw.prompt_text("build/review/judge") == "Lint said: {{ steps.lint.output }}. Verdict?"
    assert {p.name for p in rw.source_files} == {
        "fix_issue.yaml",
        "review_block.yaml",
        "implementer.yaml",
        "reviewer.md",
        "judge.md",
    }

    report = validate_workflow(
        rw,
        capabilities_for=capabilities_for,
        template_checker=FakeChecker(),
        provider_ids=["claude", "codex", "stub"],
    )
    assert [e for e in report.errors if not e.startswith("unsupported:")] == []
    assert len(report.unsupported) == 1
    assert report.errors[0] == (
        "unsupported: agents.implementer.max_turns = 60\n"
        "  provider 'codex' does not support `max_turns` (capability max_turns=False)\n"
        "  fix: remove it, use a provider that supports it (claude, stub), or set "
        "defaults.on_unsupported: warn / --allow-unsupported\n"
        "  at .rayspec/agents/implementer.yaml:4"
    )
    # downgraded with --allow-unsupported semantics
    report = validate_workflow(
        rw, capabilities_for=capabilities_for, template_checker=FakeChecker(), on_unsupported="warn"
    )
    assert report.errors == []
    assert len(report.unsupported) == 1


def test_end_to_end_plan_renders(tree: Tree, monkeypatch):
    _populate(tree)
    monkeypatch.setattr(
        _loader_common,
        "capability_source",
        lambda: _loader_common.CapabilitySource(capabilities_for, ["claude", "codex", "stub"]),
    )
    res = CliRunner().invoke(
        app,
        [
            "plan",
            "fix_issue",
            "--input",
            "issue=42",
            "--allow-unsupported",
            "--root",
            str(tree.root),
        ],
        env={"RAYSPEC_HOME": str(tree.home)},
    )
    assert res.exit_code == 0, res.output
    out = res.output
    assert "workflow fix_issue" in out
    assert "issue = 42" in out and "base = main" in out
    assert "implementer" in out and "codex" in out and "gpt-5.4" in out
    assert "triage" in out and "haiku" in out
    assert "reviewer" in out
    assert "build/review/lint" in out and "build/review/judge" in out
    assert "include=review_block" in out
    assert "1 unsupported feature warning(s)" in out
    assert "unsupported: agents.implementer.max_turns = 60" in out
    res = CliRunner().invoke(
        app,
        ["validate", "--root", str(tree.root)],
        env={"RAYSPEC_HOME": str(tree.home)},
    )
    assert res.exit_code == 2
    assert "fix_issue" in res.output and "review_block" in res.output
    assert "unsupported: agents.implementer.max_turns = 60" in res.output

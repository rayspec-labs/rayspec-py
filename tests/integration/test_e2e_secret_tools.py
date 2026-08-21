"""The ``examples/secret_via_tool`` pattern, verified end to end.

The example's claim is that an agent can be given a *capability* instead of a *credential*: a
``shell:`` step holds the secret, calls the API and emits only derived data, and the agent — plus
everything under ``RAYSPEC_HOME`` — never sees the token. ``scripts/check_examples.py`` runs every
example as a ``--dry-run``, so it can assert the workflow's shape but not that (the shell step is
simulated and there is no network in tests). This test runs the same shape for real against a
local stand-in for ``curl`` and greps the whole store afterwards.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ._helpers import invoke

TOKEN = "ghp_EXAMPLESTUBTOKEN0123456789"

WORKFLOW = """
rayspec: 1
name: tool
isolation: none
agents:
  triager:
    provider: stub
steps:
  # the tool: the only place the credential exists
  - id: open_issues
    shell: |
      set -euo pipefail
      test -n "${GITHUB_TOKEN:-}"
      # stands in for `curl -H "Authorization: Bearer $GITHUB_TOKEN" …`
      "$FAKE_API" "$GITHUB_TOKEN"
    env:
      FAKE_API: "{{ inputs.api }}"
    output_schema:
      type: object
      required: [issues]
      properties:
        issues: { type: array, items: { type: integer } }
  # the agent: sees the issue numbers, never the token
  - id: triage
    needs: [open_issues]
    agent: triager
    prompt: "Pick one of {{ steps.open_issues.output.issues | tojson }}."
inputs:
  api: { type: string, required: true }
outputs:
  first: "{{ steps.open_issues.output.issues[0] }}"
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "tool.yaml").write_text(textwrap.dedent(WORKFLOW))
    (root / ".rayspec" / "config.yaml").write_text(
        "secrets:\n  GITHUB_TOKEN: {env: EXAMPLE_TOKEN_SOURCE}\n"
    )
    api = tmp_path / "fake-api"
    api.write_text(
        '#!/bin/sh\ntest "$1" = "' + TOKEN + '" || exit 22\nprintf \'{"issues": [41, 42]}\\n\'\n'
    )
    api.chmod(0o755)
    return root


def test_the_credential_never_leaves_the_tool_step(project: Path, home: Path) -> None:
    api = project.parent / "fake-api"
    res = invoke(
        ["run", "tool", "--root", str(project), "--input", f"api={api}", "--yes"],
        home,
        EXAMPLE_TOKEN_SOURCE=TOKEN,
    )
    assert res.exit_code == 0, res.output
    leaked = [
        str(p.relative_to(home))
        for p in home.rglob("*")
        if p.is_file() and TOKEN in p.read_text(errors="replace")
    ]
    assert leaked == [], leaked
    assert TOKEN not in res.output
    (run_dir,) = list(home.rglob("runs/*/"))
    # the agent really did see the derived data (so the absence above is not an empty run)
    assert "41" in (run_dir / "steps" / "open_issues" / "output.json").read_text()

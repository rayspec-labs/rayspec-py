# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the ``rayspec.testing`` harness tests.

``home`` comes from ``tests/conftest.py`` (it exports ``RAYSPEC_HOME``): the harness resolves the
store from the path it is handed, but a case runs the real loader, which also looks at the home
directory for agents and config — so the exported variable keeps a developer's own
``~/.rayspec`` out of the way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOW = """
rayspec: 1
name: demo
description: A two-step demo workflow used by the harness tests.

inputs:
  target: { type: string, default: "src/" }

isolation: none

agents:
  reviewer:
    provider: claude
    model: small
    access: read-only

steps:
  - id: review
    agent: reviewer
    prompt: "Review {{ inputs.target }}"
  - id: bail
    when: "false"
    shell: "echo nope"
  - id: note
    needs: [review]
    shell: "echo noted"

outputs:
  verdict: "{{ steps.review.output }}"
  noted: "{{ steps.note.output | trim | default('(dry)', true) }}"
"""

STUBS = """
steps:
  review: { text: "LGTM" }
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A self-contained project root with one workflow and a stub script."""
    root = tmp_path / "project"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "demo.yaml").write_text(WORKFLOW, encoding="utf-8")
    (root / "stubs.yaml").write_text(STUBS, encoding="utf-8")
    return root

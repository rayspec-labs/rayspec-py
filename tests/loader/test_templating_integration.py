"""Integration of the loader's TemplateChecker protocol with the real templating engine.

The two scopes were built in parallel; this pins the seam: ``references()`` returns ``Ref``
objects (root/name/attr_path), shell/python bodies are compiled with their own environments.
"""

from __future__ import annotations

import textwrap

from rayspec.loader import load_workflow, validate_workflow
from rayspec.templating import TemplateEngine


def _project(tmp_path, workflow_yaml: str):
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "wf.yaml").write_text(textwrap.dedent(workflow_yaml))
    return root


def test_real_engine_refs_are_accepted_and_checked(tmp_path):
    root = _project(
        tmp_path,
        """
        rayspec: 1
        name: wf
        inputs: { n: { type: integer, default: 1 } }
        steps:
          - id: a
            shell: |
              arr=(x y); echo ${#arr[@]} {{ inputs.n }}
            output_schema: { type: object }
          - id: b
            needs: [a]
            prompt: "{{ steps.a.output.count }} and {{ inputs.n }}"
            agent: { provider: stub }
        """,
    )
    resolved = load_workflow("wf", project_root=root, home=tmp_path / "home")
    report = validate_workflow(resolved, capabilities_for=None, template_checker=TemplateEngine())
    assert report.errors == [], report.errors


def test_real_engine_reports_unknown_step_reference(tmp_path):
    root = _project(
        tmp_path,
        """
        rayspec: 1
        name: wf
        steps:
          - id: a
            shell: echo hi
          - id: b
            needs: [a]
            prompt: "{{ steps.missing.output }}"
            agent: { provider: stub }
        """,
    )
    resolved = load_workflow("wf", project_root=root, home=tmp_path / "home")
    report = validate_workflow(resolved, capabilities_for=None, template_checker=TemplateEngine())
    assert any("missing" in e for e in report.errors), report.errors

"""``rayspec run --stubs-init`` writes keys that match the paths the engine uses at run
time: loop/each bodies as ``build[*]/implement`` globs (nested accordingly), include bodies as
``inc/step``; the scaffold then drives every prompt step of ``--dry-run --stubs``."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.run import stub_scaffold
from rayspec.config import Config
from rayspec.loader import load_workflow

KITCHEN = """
rayspec: 1
name: kitchen
isolation: none
steps:
  - id: assess
    prompt: "assess"
  - id: build
    needs: [assess]
    loop:
      max_iterations: 3
      until: steps.review.output | has_signal('BUILD-CLEAN')
      steps:
        - id: implement
          prompt: "implement"
        - id: review
          needs: [implement]
          prompt: "review"
  - id: fanout
    needs: [build]
    each: "[1, 2]"
    steps:
      - id: label
        prompt: "label {{ item }}"
        output_schema: {type: object, properties: {label: {type: string}}, required: [label]}
  - id: nested
    needs: [fanout]
    loop:
      max_iterations: 1
      steps:
        - id: inner
          each: "[1]"
          steps:
            - id: deep
              prompt: "deep"
  - id: block
    needs: [nested]
    include: inc
outputs:
  iterations: "{{ steps.build.iterations }}"
  converged: "{{ steps.build.converged }}"
"""

INCLUDE = """
rayspec: 1
name: inc
steps:
  - id: inside
    prompt: "inside the include"
"""


def _project(project: Path) -> Path:
    (project / ".rayspec" / "workflows" / "kitchen.yaml").write_text(KITCHEN, encoding="utf-8")
    (project / ".rayspec" / "workflows" / "inc.yaml").write_text(INCLUDE, encoding="utf-8")
    return project


def test_scaffold_keys_are_globs_for_loop_and_each_bodies(home: Path, project: Path) -> None:
    root = _project(project)
    rw = load_workflow("kitchen", project_root=root, home=home, config=Config())
    keys = list(stub_scaffold(rw)["steps"])
    assert keys == [
        "assess",
        "build[*]/implement",
        "build[*]/review",
        "fanout[*]/label",
        "nested[*]/inner[*]/deep",
        "block/inside",
    ]
    assert stub_scaffold(rw)["steps"]["fanout[*]/label"] == {"output": {"label": ""}}


def test_scaffold_drives_every_prompt_step_of_a_dry_run(
    cli: CliRunner, home: Path, project: Path
) -> None:
    root = _project(project)
    stubs = root / "stubs.yaml"
    init = cli.invoke(
        app, ["run", "kitchen", "--root", str(root), "--dry-run", "--stubs-init", str(stubs)]
    )
    assert init.exit_code == 0, init.output
    data = yaml.safe_load(stubs.read_text(encoding="utf-8"))
    # a sequence on the loop-body review stub makes the loop converge on iteration 2
    data["steps"]["build[*]/review"] = {"sequence": ["Fix the flaky test", "BUILD-CLEAN"]}
    data["steps"]["fanout[*]/label"] = {"output": {"label": "bug"}}
    stubs.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    res = cli.invoke(
        app,
        ["run", "kitchen", "--root", str(root), "--dry-run", "--stubs", str(stubs), "--json"],
    )
    assert res.exit_code == 0, res.output
    summary = json.loads(res.stdout.strip().splitlines()[-1])
    assert summary["outputs"] == {"iterations": 2, "converged": True}
    # every scripted entry was used: no step fell back to the "[stub] …" default answer
    lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    paths = {e["step_path"] for e in lines if e.get("type") == "step.finished"}
    assert {"build[1]/review", "build[2]/review", "fanout[0]/label", "fanout[1]/label"} <= paths
    streams = [e for e in lines if e.get("type") == "stream"]
    texts = [s["record"]["text"] for s in streams if s["record"]["kind"] == "text"]
    assert "BUILD-CLEAN" in texts and "[stub] implement" in texts and "[stub] deep" in texts
    assert "[stub] inside" in texts
    assert not any(t.startswith("[stub] review") or t.startswith("[stub] label") for t in texts)

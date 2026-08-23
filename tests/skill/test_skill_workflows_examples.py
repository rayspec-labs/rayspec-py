"""The authoring page's worked examples are executable, not decorative.

``test_skill_content.py`` already proves every workflow on the page **loads and validates**
without warnings. That is not enough for an example an agent will copy: a graph can validate and
still never reach its last step, and prose next to it ("the run ends `iterations 2`", "the sweep
still succeeds", "the body steps are `lints/grade`") is exactly the kind of claim that rots.

So each worked example is driven here the way the page tells a reader to drive it — ``--stubs-init``
for the scaffold, then ``--dry-run --stubs`` with the answers the page shows — and the outcome the
page promises is asserted. A pasted example that stops working fails this file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app

from .test_skill_content import workflow_blocks


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A git project holding every workflow the page shows, plus the one prompt file it names."""
    root, home = tmp_path / "proj", tmp_path / "home"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "prompts").mkdir()
    (root / ".rayspec" / "prompts" / "implementer.md").write_text("You implement fixes.\n")
    home.mkdir()
    for name, text in workflow_blocks().items():
        (root / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    return root, {"RAYSPEC_HOME": str(home)}


def _invoke(args: list[str], env: dict[str, str]) -> Any:
    result = CliRunner().invoke(app, args, env=env)
    return result


def _summary(result: Any) -> dict[str, Any]:
    """The last ``--json`` line of a run: the run summary."""
    assert result.stdout.strip(), result.output
    return json.loads(result.stdout.splitlines()[-1])


def _scaffold(project: tuple[Path, dict[str, str]], name: str, extra: list[str]) -> set[str]:
    """``--stubs-init`` for one workflow; returns the step keys it wrote."""
    root, env = project
    stubs = root / "stubs.yaml"
    result = _invoke(
        ["run", name, "--root", str(root), "--dry-run", "--stubs-init", str(stubs), *extra], env
    )
    assert result.exit_code == 0, result.output
    return set(yaml.safe_load(stubs.read_text())["steps"])


def _run(
    project: tuple[Path, dict[str, str]], name: str, stubs: dict[str, Any], extra: list[str]
) -> dict[str, Any]:
    root, env = project
    path = root / f"{name}-stubs.yaml"
    path.write_text(yaml.safe_dump({"defaults": {"latency_ms": 0}, "steps": stubs}))
    result = _invoke(
        ["run", name, "--root", str(root), "--dry-run", "--stubs", str(path), "--json", *extra], env
    )
    summary = _summary(result)
    summary["_exit_code"] = result.exit_code
    return summary


def _statuses(project: tuple[Path, dict[str, str]], run_id: str) -> dict[str, str]:
    _root, env = project
    result = _invoke(["show", run_id, "--json"], env)
    assert result.exit_code == 0, result.output
    return {step["path"]: step["status"] for step in json.loads(result.stdout)["steps"]}


def test_selfheal_converges_on_the_signal_the_page_stubs(
    project: tuple[Path, dict[str, str]],
) -> None:
    """Worked example 1: the loop ends because the *second* review says SHIP-IT, not because
    ``max_iterations`` ran out — so ``iterations`` is 2 and ``converged`` is true."""
    task = ["-i", "task=add a --verbose flag"]
    assert _scaffold(project, "selfheal", task) == {"build[*]/implement", "build[*]/review"}
    summary = _run(
        project,
        "selfheal",
        {
            "build[*]/implement": {"text": "patched src/thing.py"},
            "build[*]/review": {
                "sequence": ["Rename the helper; it shadows a builtin.", "SHIP-IT"]
            },
        },
        task,
    )
    assert summary["_exit_code"] == 0, summary
    assert summary["status"] == "succeeded"
    assert summary["outputs"] == {"iterations": 2, "converged": True}


def test_audit_sweep_tolerates_one_failed_item_and_still_reports(
    project: tuple[Path, dict[str, str]],
) -> None:
    """Worked example 2: ``on_failure: continue`` keeps the fan-out alive when one item fails,
    the ``join: always`` report still runs, and it counts 2 of 3 — the null slot is visible."""
    assert _scaffold(project, "audit_sweep", []) == {"audit[*]/judge"}
    summary = _run(
        project,
        "audit_sweep",
        {
            "audit[*]/judge": {
                "sequence": [
                    {"output": {"risk": "low"}},
                    {"fail": {"kind": "api", "message": "model refused", "transient": False}},
                    {"output": {"risk": "high"}},
                ]
            }
        },
        ["--exec-shell", "--no-worktree"],  # the report is a python: step; run it for real
    )
    assert summary["_exit_code"] == 0, summary
    assert summary["status"] == "succeeded"
    assert summary["outputs"] == {"audited": 3}
    statuses = _statuses(project, summary["run_id"])
    assert statuses["audit[1]/judge"] == "failed"
    assert statuses["audit"] == "succeeded"
    assert statuses["report"] == "succeeded"
    assert statuses["gate"] == "skipped"  # `when: inputs.publish` is false by default


def test_pipeline_addresses_the_included_body_by_the_paths_the_page_prints(
    project: tuple[Path, dict[str, str]],
) -> None:
    """Worked example 3: the block is instantiated twice, its body steps are addressed
    ``<include id>/<body id>`` — which is what the stub scaffold keys them by — and the caller
    sees only the block's ``outputs:``."""
    assert _scaffold(project, "pipeline", []) == {"lints/grade", "types/grade"}
    summary = _run(
        project,
        "pipeline",
        {
            "lints/grade": {"output": {"status": "pass", "comment": "clean"}},
            "types/grade": {"output": {"status": "pass", "comment": "clean"}},
        },
        [],
    )
    assert summary["_exit_code"] == 0, summary
    assert summary["status"] == "succeeded"
    assert summary["outputs"] == {"clean": True}
    statuses = _statuses(project, summary["run_id"])
    assert statuses["lints/run_check"] == "succeeded"
    assert statuses["types/grade"] == "succeeded"
    assert statuses["halt"] == "skipped"  # `when: steps.verdict.output.blocked` is false

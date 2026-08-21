"""CLI ↔ workspace seam: `rayspec run` must drive the REAL workspace module.

Seam regression: the CLI adapter called ``prepare_workspace(repo=...)`` without
``home``/``run_id`` while the workspace module had a different signature; the TypeError fallback
silently ran in place. These tests would have caught it.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from rayspec.cli.app import app

WORKFLOW = """
rayspec: 1
name: demo
steps:
  - id: hello
    shell: echo hi
  - id: think
    needs: [hello]
    agent: { provider: stub }
    prompt: "{{ steps.hello.output }}"
outputs:
  said: "{{ steps.think.output }}"
"""


def _git_repo(tmp_path: Path, name: str = "proj") -> Path:
    root = tmp_path / name
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "demo.yaml").write_text(textwrap.dedent(WORKFLOW))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=root,
        check=True,
    )
    return root


def _invoke(args: list[str], home: Path):
    return CliRunner().invoke(app, args, env={"RAYSPEC_HOME": str(home)})


def _run_records(home: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in home.rglob("runs/*/run.json")]


def test_default_isolation_creates_a_worktree_via_the_real_workspace_module(tmp_path):
    root = _git_repo(tmp_path)
    home = tmp_path / "home"
    res = _invoke(["run", "demo", "--root", str(root), "--quiet", "--no-interactive"], home)
    assert res.exit_code == 0, res.output
    assert "workspace module unavailable" not in res.output
    assert "running in place" not in res.output
    [rec] = _run_records(home)
    ws = rec["workspace"]
    assert ws["isolation"] == "worktree"
    assert ws["branch"] and ws["branch"].startswith("rayspec/demo-")
    assert ws["workdir"] != str(root)
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout
    assert ws["workdir"] in listing
    assert rec["status"] == "succeeded" and rec["outputs"]["said"]


def test_no_worktree_runs_in_place(tmp_path):
    root = _git_repo(tmp_path)
    home = tmp_path / "home"
    res = _invoke(
        ["run", "demo", "--root", str(root), "--no-worktree", "--quiet", "--no-interactive"], home
    )
    assert res.exit_code == 0, res.output
    [rec] = _run_records(home)
    assert rec["workspace"]["isolation"] == "none"
    assert Path(rec["workspace"]["workdir"]).resolve() == root.resolve()


def test_repo_path_switches_the_project_root(tmp_path):
    target = _git_repo(tmp_path, "target")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    home = tmp_path / "home"
    res = _invoke(
        [
            "run",
            "demo",
            "--root",
            str(elsewhere),
            "--repo",
            str(target),
            "--quiet",
            "--no-interactive",
        ],
        home,
    )
    assert res.exit_code == 0, res.output
    [rec] = _run_records(home)
    assert rec["workspace"]["isolation"] == "worktree"
    assert str(target.resolve()) in (rec["project_root"] or "") or rec["project_root"] == str(
        target
    )

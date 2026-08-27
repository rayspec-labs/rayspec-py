"""Live: the bundled ``prd_to_pr`` end to end with real Claude agents.

Opt-in and never part of CI (``RAYSPEC_LIVE=1 uv run pytest -m live
tests/workflows/test_prd_to_pr_live.py -s``): one real run of the CLI on the throw-away repository
of ``test_prd_to_pr.py`` — a green baseline, a two-requirement PRD, a bare ``origin`` to push into
and a ``gh`` first on ``PATH`` that records ``pr create`` instead of opening anything. Both gates
are pre-approved by class, so nothing asks. What the fake provider cannot show is what this
proves: a planner reading a real PRD, a tester writing tests that code then proves red, an
implementer converging under the typecheck + tests loop, a fresh reviewer, the tests-first
history on the pushed branch, and the review, coverage and assumptions in the PR body. Costs a
few dollars; the operator policy caps a runaway at ``budget.per_run``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from .test_prd_to_pr import (
    FAKE_GH,
    GIT_ENV,
    IMPL_COMMIT,
    TEST_COMMAND,
    TESTS_COMMIT,
    TITLE,
    TYPECHECK_COMMAND,
    add_origin,
    git,
    make_repo,
    remote_branches,
)

pytestmark = [
    pytest.mark.live,  # the conftest live gate skips these unless RAYSPEC_LIVE=1
]

#: the rayspec-py checkout: `uv run rayspec` resolves the project from here
CHECKOUT = Path(__file__).resolve().parents[2]
PR_LINE = "gh pr create --base main --head {branch} --title " + TITLE + " --body-file -\n"


def _live_env(bin_dir: Path, log: Path, home: Path, policy: Path) -> dict[str, str]:
    # a nested Claude session hangs unless every CLAUDE* variable but CLAUDE_CONFIG_DIR is gone
    env = {
        name: value
        for name, value in os.environ.items()
        if not (name.startswith("CLAUDE") and name != "CLAUDE_CONFIG_DIR")
    }
    env.update(GIT_ENV)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        GH_LOG=str(log),
        RAYSPEC_HOME=str(home),
        RAYSPEC_POLICY=str(policy),
        NO_COLOR="1",
    )
    env.pop("GH_FAIL", None)
    return env


def test_a_real_run_turns_the_prd_into_a_pushed_branch_and_a_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)
    repo = make_repo(tmp_path / "repo")
    origin = add_origin(repo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(FAKE_GH, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "gh.log"
    home = tmp_path / "home"
    home.mkdir()
    policy = tmp_path / "policy.yaml"
    policy.write_text("budget:\n  per_run: 10.00\n", encoding="utf-8")

    command = [
        "uv", "run", "rayspec", "run", "prd_to_pr",
        "--root", str(repo),
        "--input", "prd=docs/prd.md",
        "--input", f"typecheck_command={TYPECHECK_COMMAND}",
        "--input", f"test_command={TEST_COMMAND}",
        "--approve-class", "scope",
        "--approve-class", "chore",
        "--json",
    ]  # fmt: skip
    proc = subprocess.run(
        command,
        cwd=CHECKOUT,
        env=_live_env(bin_dir, log, home, policy),
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, proc.stderr[-4000:]
    summary = json.loads(lines[-1])
    print(
        f"\nprd_to_pr live: status={summary['status']} exit={summary['exit_code']} "
        f"cost={summary.get('cost_usd')} ({summary.get('cost_source')}) "
        f"outputs={json.dumps(summary.get('outputs'))} reason={summary.get('reason')}"
    )
    assert summary["exit_code"] == 0, (summary.get("reason"), proc.stderr[-4000:])
    outputs = summary["outputs"]
    short_id = summary["run_id"].rsplit("-", 1)[-1]
    branch = f"prd/prd-{short_id}"
    assert outputs["branch"] == branch
    assert outputs["attempts"] >= 1
    assert outputs["verdict"] in {"complete", "partial"}
    assert outputs["tests"], "the tester wrote nothing"
    assert outputs["pr_url"] == "https://github.com/example/repo/pull/99"

    # code, not an agent, proved the tests red before the implementer was asked
    run_dir = Path(summary["run_dir"])
    red = json.loads((run_dir / "steps" / "red" / "output.json").read_text(encoding="utf-8"))
    assert red["verdict"] == "red" and red["exit_code"] != 0, red
    assert sorted(red["files"]) == sorted(outputs["tests"])

    # the tests-first history sits on the pushed branch; the tree it came from is green
    workdir = Path(summary["workspace"]["workdir"])
    subjects = git(workdir, "log", "--format=%s").splitlines()
    assert subjects[-1] == "init" and subjects[0] == IMPL_COMMIT and TESTS_COMMIT in subjects
    assert branch in remote_branches(origin)
    assert git(origin, "rev-parse", branch) == git(workdir, "rev-parse", "HEAD")
    assert "def total" in (workdir / "app.py").read_text(encoding="utf-8")
    for check in (TYPECHECK_COMMAND, TEST_COMMAND):
        subprocess.run(check.split(), cwd=workdir, check=True, capture_output=True)

    # the PR: the review, the coverage and the assumptions are in its body
    logged = log.read_text(encoding="utf-8")
    assert PR_LINE.format(branch=branch) in logged
    assert "## Summary" in logged and "## Coverage (reviewer's verdict: " in logged
    assert "## Assumptions" in logged
    assert "The plan gate was approved automatically" in logged

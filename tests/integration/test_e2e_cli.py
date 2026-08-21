"""End-to-end: ``rayspec`` through the real modules (loader, engine, workspace, store, cli-runs).

Only the stub provider stands in for the SDKs. Everything else — worktrees, run store, events,
``runs/show/logs``, approval pause + ``approve``, ``--json`` — is the shipped code.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ._helpers import (
    assert_json_stream,
    git,
    git_project,
    invoke,
    jsonl,
    run_records,
)

WORKTREE_WF = """
rayspec: 1
name: demo
description: shell → stub prompt (structured) → python → outputs
inputs:
  topic: { type: string, default: "worktrees" }
steps:
  - id: hello
    shell: |
      echo "hello {{ inputs.topic }}"
      git rev-parse --abbrev-ref HEAD > branch.txt
  - id: think
    needs: [hello]
    agent: { provider: stub }
    prompt: "Summarise: {{ steps.hello.output }}"
    output_schema:
      type: object
      properties:
        verdict: { type: string, enum: [ok, meh] }
        note: { type: string }
      required: [verdict, note]
  - id: count
    needs: [think]
    python: |
      import json, os
      ctx = json.load(open(os.environ["RAYSPEC_CONTEXT"]))
      print(len(ctx["steps"]["think"]["output"]["note"]))
outputs:
  verdict: "{{ steps.think.output.verdict }}"
  note: "{{ steps.think.output.note }}"
  length: "{{ steps.count.output | int }}"
  branch_file: "{{ steps.hello.output }}"
"""

STUBS = """
steps:
  think:
    output: { verdict: ok, note: "[stub] think about worktrees" }
defaults: { latency_ms: 0 }
"""

CONFIG = """
providers:
  stub:
    script_path: {stubs}
"""


def _worktree_project(tmp_path: Path) -> Path:
    stubs = tmp_path / "stubs.yaml"
    stubs.write_text(STUBS)
    return git_project(
        tmp_path,
        {"demo": WORKTREE_WF},
        extra={".rayspec/config.yaml": CONFIG.format(stubs=stubs)},
    )


# -- full worktree run + runs/show/logs -----------------------------------------------------------


def test_full_worktree_run_then_runs_show_logs(tmp_path: Path, home: Path) -> None:
    root = _worktree_project(tmp_path)
    res = invoke(["run", "demo", "--root", str(root), "--no-interactive"], home)
    assert res.exit_code == 0, res.output
    [rec] = run_records(home)
    run_id = rec["run_id"]
    assert rec["status"] == "succeeded"
    ws = rec["workspace"]
    assert ws["isolation"] == "worktree" and ws["branch"].startswith("rayspec/demo-")
    workdir = Path(ws["workdir"])
    assert workdir.is_dir() and str(home) in str(workdir), workdir
    assert "worktrees" in workdir.parts
    # the shell step ran inside the worktree on the run branch
    assert (workdir / "branch.txt").read_text().strip() == ws["branch"]
    assert rec["outputs"] == {
        "verdict": "ok",
        "note": "[stub] think about worktrees",
        "length": len("[stub] think about worktrees"),
        "branch_file": "hello worktrees",
    }
    assert "[stub] think about worktrees" in res.stdout  # outputs table, markup-safe
    assert f"branch {ws['branch']}" in res.stdout
    # the summary says where the worktree is, that the branch is checked out there, and
    # how to use / clean it (the main clone cannot `git checkout` that branch meanwhile)
    assert f"worktree: {workdir}" in res.stdout and "checked out there" in res.stdout
    assert f"cd {workdir}" in res.stdout and "rayspec worktrees list|clean" in res.stdout
    assert f"git worktree remove {workdir}" in res.stdout
    listing = git("worktree", "list", "--porcelain", cwd=root)
    assert str(workdir) in listing
    # the store layout of the run
    run_dir = next(home.rglob(f"runs/{run_id}"))  # RunRecord has no run_dir field; the store does
    assert (run_dir / "events.jsonl").is_file()
    assert (run_dir / "steps" / "think" / "output.json").is_file()
    assert (run_dir / "steps" / "hello" / "stdout.log").is_file()
    for step in ("hello", "think", "count"):
        assert rec["steps"][step]["status"] == "succeeded", step

    # runs
    res = invoke(["runs", "--root", str(root)], home)
    assert res.exit_code == 0, res.output
    assert run_id in res.stdout and "demo" in res.stdout and "succeeded" in res.stdout
    res = invoke(["runs", "--root", str(root), "--json"], home)
    assert res.exit_code == 0, res.output
    rows = json.loads(res.stdout)
    assert [r["run_id"] for r in rows] == [run_id]
    assert rows[0]["status"] == "succeeded" and rows[0]["steps_done"] == 3
    assert rows[0]["workspace"]["branch"] == ws["branch"]

    # show (full id and prefix)
    res = invoke(["show", run_id, "--root", str(root)], home)
    assert res.exit_code == 0, res.output
    for needle in ("succeeded", "hello", "think", "count", ws["branch"], "verdict", "ok"):
        assert needle in res.stdout, needle
    assert "[stub] think about worktrees" in res.stdout
    res = invoke(["show", run_id[:9], "--root", str(root), "--json"], home)
    assert res.exit_code == 0, res.output
    shown = json.loads(res.stdout)
    assert shown["run_id"] == run_id
    assert {s["path"] for s in shown["steps"]} == {"hello", "think", "count"}
    assert shown["outputs"]["verdict"] == "ok"
    assert shown["record"]["workflow_name"] == "demo" and shown["workflow"] == "demo"

    # logs: the lifecycle events in order (quiet-sink rendering, one stamped line each)
    res = invoke(["logs", run_id, "--root", str(root)], home)
    assert res.exit_code == 0, res.output
    expected = [
        f"▶ run {run_id} started (demo)",
        f"● workspace {workdir} ({ws['branch']})",
        "→ hello (shell)",
        "✓ hello succeeded",
        "→ think (prompt)",
        "✓ think succeeded",
        "→ count (python)",
        "✓ count succeeded",
        f"■ run {run_id} succeeded",
    ]
    positions = [res.stdout.find(needle) for needle in expected]
    assert all(pos >= 0 for pos in positions), (expected, res.stdout)
    assert positions == sorted(positions), "events are rendered in stored order"
    assert "hello worktrees" not in res.stdout, "stream records are not part of the event log"
    # logs --step: only that step's stream.jsonl (the structured stub answer, nothing of hello/count)
    res = invoke(["logs", run_id, "--root", str(root), "--step", "think"], home)
    assert res.exit_code == 0, res.output
    assert "session stub:think:1" in res.stdout, res.stdout
    assert '"note": "[stub] think about worktrees"' in res.stdout, res.stdout
    assert '"verdict": "ok"' in res.stdout, res.stdout
    assert "hello worktrees" not in res.stdout and "exit 0" not in res.stdout, res.stdout
    assert "count" not in res.stdout and "started" not in res.stdout, res.stdout
    res = invoke(["logs", run_id, "--root", str(root), "--step", "hello"], home)
    assert res.exit_code == 0, res.output
    lines = [line.split("  ", 1)[1] for line in res.stdout.splitlines() if line.strip()]
    assert lines == ["hello worktrees", "exit 0"], res.stdout
    res = invoke(["logs", run_id, "--root", str(root), "--stream"], home)
    assert res.exit_code == 0, res.output
    assert "[hello]" in res.stdout and "hello worktrees" in res.stdout
    res = invoke(["logs", run_id, "--root", str(root), "--json"], home)
    assert res.exit_code == 0, res.output
    events = jsonl(res.stdout)
    assert events[0]["type"] in {"run.started", "workspace.created"}
    assert events[-1]["type"] == "run.finished"
    assert {e["type"] for e in events} >= {"step.started", "step.finished", "run.finished"}
    res = invoke(["logs", run_id, "--root", str(root), "--step", "nope"], home)
    assert res.exit_code == 2
    res = invoke(["show", "zzzz", "--root", str(root)], home)
    assert res.exit_code == 2


# -- --json stream sanity ---------------------------------------------------------------------------


def test_json_stream_shape_on_a_worktree_run(tmp_path: Path, home: Path) -> None:
    root = _worktree_project(tmp_path)
    res = invoke(["run", "demo", "--root", str(root), "--no-interactive", "--json"], home)
    assert res.exit_code == 0, res.output
    body, summary = assert_json_stream(res.stdout)
    assert summary["status"] == "succeeded" and summary["exit_code"] == 0
    assert summary["workspace"]["isolation"] == "worktree"
    assert summary["outputs"]["verdict"] == "ok"
    types = [line["type"] for line in body]
    assert "workspace.created" in types
    finished = {e["step_path"]: e["data"] for e in body if e["type"] == "step.finished"}
    assert set(finished) == {"hello", "think", "count"}
    for data in finished.values():
        assert set(data) >= {"status", "duration_ms", "usage", "cost_usd", "error", "skip_reason"}
        assert data["status"] == "succeeded"
    streams = [line for line in body if line["type"] == "stream"]
    kinds = {(s["step_path"], s["record"]["kind"]) for s in streams}
    assert ("hello", "stdout") in kinds and ("hello", "exit") in kinds
    assert any(path == "think" for path, _kind in kinds)
    # console lines never leak into the JSON stream
    assert not res.stderr.strip() or all(not ln.startswith("{") for ln in res.stderr.splitlines())


# -- approval pause → approve / reject --------------------------------------------------------------

GATE_WF = """
rayspec: 1
name: gated
steps:
  - id: draft
    agent: { provider: stub }
    prompt: "draft it"
  - id: gate
    needs: [draft]
    approve: "Ship {{ steps.draft.output }}?"
  - id: ship
    needs: [gate]
    shell: echo "shipped after '{{ steps.gate.output }}'"
outputs:
  shipped: "{{ steps.ship.output }}"
  comment: "{{ steps.gate.output }}"
"""


def _gated_project(tmp_path: Path) -> Path:
    return git_project(tmp_path, {"gated": GATE_WF}, name="gated")


def test_no_interactive_pauses_then_approve_resumes(tmp_path: Path, home: Path) -> None:
    root = _gated_project(tmp_path)
    res = invoke(["run", "gated", "--root", str(root), "--no-worktree", "--no-interactive"], home)
    assert res.exit_code == 3, res.output
    [rec] = run_records(home)
    run_id = rec["run_id"]
    assert rec["status"] == "paused"
    assert rec["pause"]["step"] == "gate" and rec["pause"]["token"] == "gate#1"
    assert rec["steps"]["gate"]["status"] == "paused"
    assert rec["steps"]["draft"]["status"] == "succeeded"
    assert "ship" not in rec["steps"] or rec["steps"]["ship"]["status"] in {"skipped", "pending"}
    assert f"rayspec approve {run_id}" in res.stdout

    # show reports the pause
    res = invoke(["show", run_id, "--root", str(root)], home)
    assert res.exit_code == 0
    assert "paused" in res.stdout and "gate" in res.stdout

    # a non-interactive resume does not run: it points at approve/reject
    res = invoke(["resume", run_id, "--root", str(root), "--no-interactive"], home)
    assert res.exit_code == 3, res.output
    assert "approve" in res.output

    res = invoke(["approve", run_id, "looks good", "--root", str(root)], home)
    assert res.exit_code == 0, res.output
    [rec] = run_records(home)
    assert rec["status"] == "succeeded"
    assert rec["pause"] is None, "the gate consumed the decision and cleared the pause block"
    assert rec["steps"]["gate"]["status"] == "succeeded"
    assert rec["steps"]["gate"]["approved"] is True
    assert rec["outputs"] == {"shipped": "shipped after 'looks good'", "comment": "looks good"}
    assert rec["resume_count"] == 1
    assert rec["pid"] is None
    run_dir = next(home.rglob(f"runs/{run_id}"))
    events = jsonl((run_dir / "events.jsonl").read_text())
    types = [e["type"] for e in events]
    assert "run.paused" in types and "run.resumed" in types and "run.decision" in types
    decision = next(e for e in events if e["type"] == "run.decision")
    assert decision["data"]["approved"] is True and decision["data"]["by"] == "cli"
    assert decision["data"]["comment"] == "looks good" and decision["step_path"] == "gate"
    # draft was replayed from the cache, not re-run
    replays = [e for e in events if e["type"] == "step.finished" and e["step_path"] == "draft"]
    assert replays[-1]["data"].get("reused") is True

    # approving again is a usage error (no longer paused)
    res = invoke(["approve", run_id, "--root", str(root)], home)
    assert res.exit_code == 2
    assert "not paused" in res.output


def test_reject_cancels_the_run(tmp_path: Path, home: Path) -> None:
    root = _gated_project(tmp_path)
    res = invoke(
        ["run", "gated", "--root", str(root), "--no-worktree", "--no-interactive", "--json"], home
    )
    assert res.exit_code == 3, res.output
    body, summary = assert_json_stream(res.stdout)
    assert summary["status"] == "paused" and summary["exit_code"] == 3
    assert summary["pause"]["step"] == "gate"
    assert body[-1]["data"]["status"] == "paused"
    assert any(e["type"] == "run.paused" for e in body)
    run_id = summary["run_id"]

    res = invoke(["reject", run_id, "not now", "--root", str(root), "--json"], home)
    assert res.exit_code == 4, res.output
    body, summary = assert_json_stream(res.stdout)
    assert summary["status"] == "cancelled" and summary["exit_code"] == 4
    [rec] = run_records(home)
    assert rec["status"] == "cancelled"
    assert rec["steps"]["gate"]["status"] == "rejected"
    assert rec["steps"]["ship"]["status"] == "skipped"
    assert rec["pause"] is None
    decision = next(e for e in body if e["type"] == "run.decision")
    assert decision["data"]["approved"] is False and decision["data"]["comment"] == "not now"


def test_yes_auto_approves(tmp_path: Path, home: Path) -> None:
    root = _gated_project(tmp_path)
    res = invoke(["run", "gated", "--root", str(root), "--no-worktree", "--yes", "--json"], home)
    assert res.exit_code == 0, res.output
    body, summary = assert_json_stream(res.stdout)
    decision = next(e for e in body if e["type"] == "run.decision")
    assert decision["data"]["by"] == "--yes" and decision["data"]["approved"] is True
    assert summary["outputs"]["shipped"] == "shipped after ''"


# -- --repo <file:// url> ----------------------------------------------------------------------------


def test_repo_file_url_clones_bare_and_runs_in_a_worktree(tmp_path: Path, home: Path) -> None:
    upstream = git_project(tmp_path, {"demo": WORKTREE_WF}, name="upstream")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    url = upstream.resolve().as_uri()  # file:///…/upstream
    assert url.startswith("file://")
    res = invoke(
        [
            "run",
            "demo",
            "--root",
            str(elsewhere),
            "--repo",
            url,
            "--no-interactive",
            "--input",
            "topic=bare",
        ],
        home,
    )
    assert res.exit_code == 0, res.output
    [rec] = run_records(home)
    ws = rec["workspace"]
    assert rec["status"] == "succeeded"
    assert ws["isolation"] == "worktree"
    assert ws["base_branch"] == "origin/main"
    assert ws["branch"].startswith("rayspec/demo-")
    workdir = Path(ws["workdir"])
    assert workdir.is_dir() and "worktrees" in workdir.parts and str(home) in str(workdir)
    # workflows were loaded from the checkout, not from --root
    assert Path(rec["project_root"]).resolve() == workdir.resolve()
    # the bare source lives next to the worktrees and is never checked out
    project_dir = workdir.parents[1]
    source = project_dir / "source.git"
    assert source.is_dir()
    assert (
        subprocess.run(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=source,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == "true"
    )
    assert (source / "refs").is_dir() or (source / "packed-refs").exists()
    assert rec["outputs"]["branch_file"] == "hello bare"
    assert (workdir / "branch.txt").read_text().strip() == ws["branch"]
    # base sha == upstream main
    assert ws["base_sha"] == git("rev-parse", "HEAD", cwd=upstream).strip()
    # the upstream checkout itself was never touched
    assert not (upstream / "branch.txt").exists()


def test_repo_file_url_runs_share_one_project_slug(tmp_path: Path, home: Path) -> None:
    """Every run of ``--repo file://…`` lives under the URL source's slug (the bare clone's
    project dir) — runs, locks and worktrees together — never under a per-run
    ``local/<worktree-dir>-<sha>`` slug derived from the worktree's origin."""
    upstream = git_project(tmp_path, {"demo": WORKTREE_WF}, name="upstream")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    url = upstream.resolve().as_uri()
    for _ in range(2):
        res = invoke(
            [
                "run",
                "demo",
                "--root",
                str(elsewhere),
                "--repo",
                url,
                "--no-interactive",
                "-i",
                "topic=x",
            ],
            home,
        )
        assert res.exit_code == 0, res.output
    records = run_records(home)
    assert len(records) == 2
    slugs = {rec["project_slug"] for rec in records}
    assert len(slugs) == 1, slugs
    (slug,) = slugs
    project_dir = home / "projects" / Path(*slug.split("/"))
    # one project directory holds the bare source, both worktrees, both runs and the locks
    assert (project_dir / "source.git").is_dir()
    assert len(list((project_dir / "worktrees").iterdir())) == 2
    assert len(list((project_dir / "runs").iterdir())) == 2
    assert (project_dir / "locks").is_dir()
    local_dirs = sorted(p.name for p in (home / "projects" / "local").iterdir())
    assert local_dirs == [project_dir.name], local_dirs
    # both runs are listed under that project
    listed = invoke(["runs", "--all", "--json", "--root", str(elsewhere)], home)
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)
    assert {row["project_slug"] for row in rows} == {slug} and len(rows) == 2
    # and the worktrees of the source are both visible through --repo
    wts = invoke(["worktrees", "list", "--repo", url, "--json", "--root", str(elsewhere)], home)
    assert wts.exit_code == 0, wts.output
    assert len(json.loads(wts.stdout)) == 2

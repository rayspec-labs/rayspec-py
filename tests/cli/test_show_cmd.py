"""`rayspec show <run|prefix> [--json]`."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.store.model import PauseInfo

from .conftest import FAILED_ID, OTHER_ID, PAUSED_ID, SUCCEEDED_ID, Seeded


def test_show_succeeded_run_header_steps_outputs(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["show", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert f"run {SUCCEEDED_ID}" in out and "succeeded" in out
    assert "fixit" in out and "1m35s" in out
    assert "worktree" in out and "rayspec/fixit-aaaa" in out
    # nested step paths, status, duration, cost and output preview
    assert "build[1]/implement" in out and "patched the thing" in out
    assert "~$0.21" in out and "$0.05" in out and "12.3s" in out
    assert "build[1]/check" in out and "issue 7" in out
    # outputs
    assert "verdict" in out and "fix" in out and '{"files": 2}' in out
    assert "awaiting approval" not in out


def test_show_accepts_prefix_and_reports_ambiguity(cli: CliRunner, seeded: Seeded) -> None:
    ok = cli.invoke(app, ["show", "20260820-12", "--root", str(seeded.project)])
    assert ok.exit_code == 0, ok.output
    assert PAUSED_ID in ok.output
    ambiguous = cli.invoke(app, ["show", "20260820-1", "--root", str(seeded.project)])
    assert ambiguous.exit_code == 2
    assert "ambiguous" in ambiguous.output and PAUSED_ID in ambiguous.output
    unknown = cli.invoke(app, ["show", "zzz", "--root", str(seeded.project)])
    assert unknown.exit_code == 2 and "no run matches" in unknown.output
    other = cli.invoke(app, ["show", OTHER_ID, "--root", str(seeded.project)])
    assert other.exit_code == 0 and "other" in other.output


def test_show_paused_run_shows_pause_info(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["show", PAUSED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "paused" in result.output and "ship it?" in result.output
    assert "ok#1" in result.output
    assert f"rayspec approve {PAUSED_ID}" in result.output
    assert f"rayspec reject {PAUSED_ID}" in result.output


def test_show_failed_run_shows_error_and_skip_reason(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["show", FAILED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "exit code 1" in result.output
    assert "upstream_failed" in result.output
    assert "step 'b' failed" in result.output


def test_show_json_shape(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["show", SUCCEEDED_ID, "--json", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["run_id"] == SUCCEEDED_ID
    assert data["workflow"] == "fixit" and data["status"] == "succeeded"
    assert data["duration_ms"] == 95_000 and data["cost_usd"] == 0.2556
    assert data["usage"]["input"] == 6200 and data["tokens"] == 8500
    assert data["workspace"]["branch"] == "rayspec/fixit-aaaa"
    assert data["outputs"] == {"verdict": "fix", "summary": {"files": 2}}
    assert data["run_dir"].endswith(SUCCEEDED_ID)
    assert data["pause"] is None
    steps = {s["path"]: s for s in data["steps"]}
    assert list(steps) == ["fetch", "assess", "build[1]/implement", "build[1]/check", "build"]
    assert steps["build[1]/implement"]["output_preview"] == "patched the thing …"
    assert steps["assess"]["cost_usd"] == 0.0456 and steps["assess"]["tokens"] == 1500
    assert steps["build"]["loop"] == {"iterations": 1, "converged": True}
    assert data["inputs"] == {"issue": 7}
    assert data["record"]["schema"] == 1 and data["record"]["run_id"] == SUCCEEDED_ID
    paused = json.loads(
        cli.invoke(app, ["show", PAUSED_ID, "--json", "--root", str(seeded.project)]).output
    )
    assert paused["pause"]["token"] == "ok#1" and paused["pause"]["decision"] is None


def test_show_is_safe_against_rich_markup_in_run_data(cli: CliRunner, seeded: Seeded) -> None:
    # agent outputs / error messages routinely contain `[/…]`-style text: it must be shown
    # verbatim, never parsed as Rich markup
    run = seeded.store.load(FAILED_ID)
    run.steps["a"].output_ref = seeded.store.write_output(
        FAILED_ID, "a", "[/bold] not markup [link=x]", kind="text"
    )
    assert run.steps["b"].error is not None
    run.steps["b"].error.message = "bad [/x] thing"
    run.outputs = {"note": "[/bold] value", "nested": {"k": "[red]v"}}
    run.pause = PauseInfo(token="ok[1]#1", step="ok[/bold]", message="ship [x]?")
    seeded.store.save(run)
    result = cli.invoke(app, ["show", FAILED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "[/bold] not markup [link=x]" in result.output
    assert "exit: bad [/x] thing" in result.output
    assert "[/bold] value" in result.output and '{"k": "[red]v"}' in result.output
    assert "ok[/bold]" in result.output and "ok[1]#1" in result.output


def test_show_prints_the_toolchain_block(cli: CliRunner, seeded: Seeded) -> None:
    """Which rayspec/python/SDK/CLI/model produced the run is visible in `show`."""
    record = seeded.store.load(SUCCEEDED_ID)
    record.toolchain = {
        "rayspec": "1.0.0",
        "python": "3.12.8",
        "platform": "macOS-15.5-arm64",
        "providers": {
            "claude": {"sdk_version": "0.2.142", "cli_version": "2.1.0", "cli_path": "/bin/claude"},
            "codex": {"sdk_version": None, "cli_version": None, "cli_path": None, "error": "boom"},
        },
        "models": {"agents.reviewer": "haiku", "inline:build/fix": None},
    }
    seeded.store.save(record)
    res = cli.invoke(app, ["show", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert res.exit_code == 0, res.output
    assert "toolchain:" in res.output
    assert "rayspec 1.0.0" in res.output and "python 3.12.8" in res.output
    assert "claude" in res.output and "2.1.0" in res.output
    assert "boom" in res.output
    assert "agents.reviewer" in res.output and "haiku" in res.output


def test_show_without_a_toolchain_prints_no_block(cli: CliRunner, seeded: Seeded) -> None:
    res = cli.invoke(app, ["show", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert res.exit_code == 0
    assert "toolchain:" not in res.output


def _running_detached(seeded: Seeded, run_id: str, *, heartbeat_ago_s: float, launch_log: bool):
    import socket
    from datetime import UTC, datetime, timedelta

    from rayspec.schema import RunStatus
    from rayspec.store.model import RunRecord

    run = RunRecord(
        run_id=run_id,
        workflow_name="live",
        workflow_path="x.yaml",
        workflow_hash="f" * 64,
        project_slug=seeded.slug,
        project_root=str(seeded.project),
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC),
        pid=__import__("os").getpid(),
        host=socket.gethostname(),
        heartbeat_at=datetime.now(UTC) - timedelta(seconds=heartbeat_ago_s),
    )
    seeded.store.create(run)
    if launch_log:
        (seeded.store.run_dir(run_id) / "detach-launch.log").write_text("boot\n", encoding="utf-8")
    return run


def test_show_reports_heartbeat_age_for_a_live_run(cli: CliRunner, seeded: Seeded) -> None:
    run = _running_detached(seeded, "20260827-130000-live", heartbeat_ago_s=3, launch_log=False)
    result = cli.invoke(app, ["show", run.run_id, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "heartbeat" in result.output, result.output


def test_show_marks_a_stale_heartbeat(cli: CliRunner, seeded: Seeded) -> None:
    run = _running_detached(seeded, "20260827-130100-stal", heartbeat_ago_s=3600, launch_log=False)
    result = cli.invoke(app, ["show", run.run_id, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    # a stale heartbeat is reconciled to interrupted; the header must say the beat went stale
    assert "stale" in result.output.lower(), result.output


def test_show_marks_a_detached_launch(cli: CliRunner, seeded: Seeded) -> None:
    run = _running_detached(seeded, "20260827-130200-dtch", heartbeat_ago_s=2, launch_log=True)
    result = cli.invoke(app, ["show", run.run_id, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "detached" in result.output.lower(), result.output
    assert "detach-launch.log" in result.output, result.output

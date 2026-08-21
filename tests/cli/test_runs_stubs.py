# SPDX-License-Identifier: Apache-2.0
"""``rayspec runs stubs <run>`` and ``rayspec run --stubs-from <run>`` — record & replay.

A run dir is the cheapest realistic fixture there is: these tests pin the round trip (run →
record → replay produces the same step statuses and outputs), how loop iterations and ``each``
items are keyed, and the secret refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.schema import StepStatus
from rayspec.store.model import ErrorInfo

from .conftest import SUCCEEDED_ID, Seeded

# A workflow with BOTH a loop body (ordered iterations) and an each body (parallel items).
MIXER = """
rayspec: 1
name: mixer
isolation: none
agents:
  bot: {provider: stub}
inputs:
  files: {type: array, default: ["a.py", "b.py"]}
steps:
  - id: build
    loop:
      max_iterations: 2
      on_exhausted: continue
      steps:
        - id: implement
          agent: bot
          prompt: "iteration {{ iteration.n }}"
  - id: fan
    needs: [build]
    each: inputs.files
    as: file
    steps:
      - id: patch
        agent: bot
        prompt: "patch {{ file }}"
outputs:
  last: "{{ steps.build.output.implement }}"
  first_patch: "{{ steps.fan.output[0].patch }}"
"""

MIXER_STUBS = """
steps:
  "build[*]/implement":
    sequence: ["first pass", "second pass"]
  "fan[0]/patch": {text: "patched a.py", usage: {input: 11, output: 7}}
  "fan[1]/patch": {text: "patched b.py"}
"""


def _write(project: Path, name: str, text: str) -> None:
    (project / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text, encoding="utf-8")


def _dry_run(cli: CliRunner, project: Path, *extra: str) -> dict:
    result = cli.invoke(
        app, ["run", "mixer", "--root", str(project), "--dry-run", "--json", *extra]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip().splitlines()[-1])


def _steps_of(home: Path, run_id: str) -> dict:
    (run_json,) = home.rglob(f"runs/{run_id}/run.json")
    record = json.loads(run_json.read_text(encoding="utf-8"))
    return {path: rec["status"] for path, rec in record["steps"].items()}


# -- recording --------------------------------------------------------------------------------


def test_stubs_records_prompt_steps_only(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["runs", "stubs", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    script = yaml.safe_load(result.stdout)
    # shell steps and the loop composite are not agent calls
    assert set(script["steps"]) == {"assess", "build[*]/implement"}
    assert script["steps"]["assess"]["output"] == {"verdict": "fix", "reason": "real bug"}
    assert script["steps"]["assess"]["usage"] == {"input": 1200, "output": 300}
    assert script["steps"]["build[*]/implement"]["text"].startswith("patched the thing")


def test_stubs_writes_a_file_and_refuses_to_overwrite(cli: CliRunner, seeded: Seeded) -> None:
    target = seeded.project / "recorded.yaml"
    first = cli.invoke(
        app, ["runs", "stubs", SUCCEEDED_ID, "-o", str(target), "--root", str(seeded.project)]
    )
    assert first.exit_code == 0, first.output
    assert "assess" in target.read_text(encoding="utf-8")
    again = cli.invoke(
        app, ["runs", "stubs", SUCCEEDED_ID, "-o", str(target), "--root", str(seeded.project)]
    )
    assert again.exit_code == 2
    assert "already exists" in again.output
    forced = cli.invoke(
        app,
        [
            "runs",
            "stubs",
            SUCCEEDED_ID,
            "-o",
            str(target),
            "--force",
            "--root",
            str(seeded.project),
        ],
    )
    assert forced.exit_code == 0, forced.output


def test_stubs_refuses_a_run_with_secret_inputs(cli: CliRunner, seeded: Seeded) -> None:
    run = seeded.store.load(SUCCEEDED_ID)
    run.secret_inputs = ("token", "api_key")
    run.inputs = {"token": "<secret>", "api_key": "<secret>"}
    seeded.store.save(run)
    result = cli.invoke(app, ["runs", "stubs", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 2
    assert "token" in result.output and "api_key" in result.output
    assert "secret" in result.output


def test_stubs_on_an_unknown_run_exits_2(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["runs", "stubs", "nope", "--root", str(seeded.project)])
    assert result.exit_code == 2


def test_stubs_warns_when_the_workflow_changed(cli: CliRunner, seeded: Seeded) -> None:
    _write(seeded.project, "fixit", MIXER.replace("name: mixer", "name: fixit"))
    result = cli.invoke(app, ["runs", "stubs", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "changed since" in result.output


# -- round trip -------------------------------------------------------------------------------


def test_round_trip_loop_and_each(cli: CliRunner, home: Path, project: Path) -> None:
    _write(project, "mixer", MIXER)
    stubs = project / "stubs.yaml"
    stubs.write_text(MIXER_STUBS, encoding="utf-8")
    first = _dry_run(cli, project, "--stubs", str(stubs))
    assert first["outputs"] == {"last": "second pass", "first_patch": "patched a.py"}

    recorded = project / "recorded.yaml"
    rec = cli.invoke(
        app, ["runs", "stubs", first["run_id"], "-o", str(recorded), "--root", str(project)]
    )
    assert rec.exit_code == 0, rec.output
    script = yaml.safe_load(recorded.read_text(encoding="utf-8"))
    # ordered loop iterations collapse into a sequence under one glob…
    sequence = script["steps"]["build[*]/implement"]["sequence"]
    assert [item["text"] for item in sequence] == ["first pass", "second pass"]
    assert all("usage" in item for item in sequence)
    # …while parallel `each` items keep their own indexed keys (replay order is not guaranteed)
    assert script["steps"]["fan[0]/patch"]["text"] == "patched a.py"
    assert script["steps"]["fan[1]/patch"]["text"] == "patched b.py"
    assert script["steps"]["fan[0]/patch"]["usage"] == {"input": 11, "output": 7}

    replay = _dry_run(cli, project, "--stubs", str(recorded))
    assert replay["outputs"] == first["outputs"]
    assert _steps_of(home, replay["run_id"]) == _steps_of(home, first["run_id"])


def test_round_trip_via_stubs_from(cli: CliRunner, home: Path, project: Path) -> None:
    _write(project, "mixer", MIXER)
    stubs = project / "stubs.yaml"
    stubs.write_text(MIXER_STUBS, encoding="utf-8")
    first = _dry_run(cli, project, "--stubs", str(stubs))
    replay = _dry_run(cli, project, "--stubs-from", first["run_id"][:8])
    assert replay["outputs"] == first["outputs"]
    assert _steps_of(home, replay["run_id"]) == _steps_of(home, first["run_id"])


def test_stubs_from_and_stubs_are_mutually_exclusive(cli: CliRunner, project: Path) -> None:
    _write(project, "mixer", MIXER)
    stubs = project / "stubs.yaml"
    stubs.write_text(MIXER_STUBS, encoding="utf-8")
    result = cli.invoke(
        app,
        [
            "run",
            "mixer",
            "--root",
            str(project),
            "--dry-run",
            "--stubs",
            str(stubs),
            "--stubs-from",
            "whatever",
        ],
    )
    assert result.exit_code == 2
    assert "--stubs-from" in result.output


def test_stubs_from_an_unknown_run_exits_2(cli: CliRunner, project: Path) -> None:
    _write(project, "mixer", MIXER)
    result = cli.invoke(
        app, ["run", "mixer", "--root", str(project), "--dry-run", "--stubs-from", "nope"]
    )
    assert result.exit_code == 2


def test_recorded_failures_replay_as_failures(cli: CliRunner, home: Path, project: Path) -> None:
    _write(project, "mixer", MIXER)
    stubs = project / "stubs.yaml"
    stubs.write_text(
        'steps:\n  "build[*]/implement": {fail: {kind: api, message: nope}}\n', encoding="utf-8"
    )
    result = cli.invoke(
        app,
        ["run", "mixer", "--root", str(project), "--dry-run", "--json", "--stubs", str(stubs)],
    )
    assert result.exit_code == 1, result.output
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    recorded = project / "recorded.yaml"
    rec = cli.invoke(
        app, ["runs", "stubs", summary["run_id"], "-o", str(recorded), "--root", str(project)]
    )
    assert rec.exit_code == 0, rec.output
    script = yaml.safe_load(recorded.read_text(encoding="utf-8"))
    entry = script["steps"]["build[*]/implement"]
    assert entry["fail"]["message"] == "nope"
    replay = cli.invoke(
        app,
        ["run", "mixer", "--root", str(project), "--dry-run", "--json", "--stubs", str(recorded)],
    )
    assert replay.exit_code == 1, replay.output


# -- recorder fidelity ------------------------------------------------------------------------


def test_stubs_refuses_a_step_whose_output_file_is_gone(cli: CliRunner, seeded: Seeded) -> None:
    """A prompt step that claims an output rayspec cannot read must not become an answerless
    entry — the replay would fall through to the stub provider's default and look faithful."""
    (output,) = seeded.home.rglob(f"runs/{SUCCEEDED_ID}/steps/assess/output.json")
    output.unlink()
    result = cli.invoke(app, ["runs", "stubs", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 2, result.output
    assert "assess" in result.output
    assert "steps/assess/output.json" in result.output


def test_stubs_from_refuses_a_step_whose_output_file_is_gone(
    cli: CliRunner, seeded: Seeded
) -> None:
    """The replay entry point refuses the same recording (nothing is executed)."""
    _write(seeded.project, "mixer", MIXER)
    (output,) = seeded.home.rglob(f"runs/{SUCCEEDED_ID}/steps/assess/output.json")
    output.unlink()
    result = cli.invoke(
        app,
        ["run", "mixer", "--root", str(seeded.project), "--dry-run", "--stubs-from", SUCCEEDED_ID],
    )
    assert result.exit_code == 2, result.output
    assert "assess" in result.output


def test_a_never_answered_prompt_step_is_left_out(cli: CliRunner, seeded: Seeded) -> None:
    """An interrupted prompt step never got an answer: no entry at all, rather than an empty one."""
    run = seeded.store.load(SUCCEEDED_ID)
    rec = run.steps["build[1]/implement"]
    rec.status = StepStatus.INTERRUPTED
    rec.output_ref = None
    rec.output_kind = None
    run.steps["build[1]/implement"] = rec
    seeded.store.save(run)
    result = cli.invoke(app, ["runs", "stubs", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    script = yaml.safe_load(result.stdout)
    assert set(script["steps"]) == {"assess"}


def test_stubs_notes_an_error_type_the_stub_cannot_express(cli: CliRunner, seeded: Seeded) -> None:
    """`_recorded_failure` rewrites an unknown error type to `api` — say so instead of pretending
    the recorded script is a faithful copy."""
    run = seeded.store.load(SUCCEEDED_ID)
    rec = run.steps["assess"]
    rec.status = StepStatus.FAILED
    rec.error = ErrorInfo(type="rejected", message="human veto")
    run.steps["assess"] = rec
    seeded.store.save(run)
    result = cli.invoke(app, ["runs", "stubs", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "assess" in result.output
    assert "rejected" in result.output and "api" in result.output
    script = yaml.safe_load(result.stdout)
    assert script["steps"]["assess"]["fail"]["kind"] == "api"


# -- retry-aware recording --------------------------------------------------------------------

FLAKY = """
rayspec: 1
name: flaky
isolation: none
agents:
  bot: {provider: stub}
steps:
  - id: build
    loop:
      max_iterations: 2
      on_exhausted: continue
      steps:
        - id: implement
          agent: bot
          prompt: "iteration {{ iteration.n }}"
          retry: {attempts: 2, delay: 0}
          allow_failure: true
outputs:
  last: "{{ steps.build.output.implement | default('') }}"
"""

FLAKY_STUBS = """
steps:
  "build[1]/implement": {fail: {kind: api, message: flaky, transient: true}}
  "build[2]/implement": {text: DONE}
"""


def _dry_run_named(cli: CliRunner, project: Path, name: str, *extra: str) -> dict:
    result = cli.invoke(app, ["run", name, "--root", str(project), "--dry-run", "--json", *extra])
    assert result.exit_code in {0, 1}, result.output
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_a_retried_failure_is_not_recorded_as_a_sequence(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """`sequence:` counts CALLS, not iterations: a retried transient failure would consume the
    next slot and shift every later iteration's answer by one."""
    _write(project, "flaky", FLAKY)
    stubs = project / "stubs.yaml"
    stubs.write_text(FLAKY_STUBS, encoding="utf-8")
    first = _dry_run_named(cli, project, "flaky", "--stubs", str(stubs))
    assert _steps_of(home, first["run_id"])["build[1]/implement"] == "failed"

    recorded = project / "recorded.yaml"
    rec = cli.invoke(
        app, ["runs", "stubs", first["run_id"], "-o", str(recorded), "--root", str(project)]
    )
    assert rec.exit_code == 0, rec.output
    script = yaml.safe_load(recorded.read_text(encoding="utf-8"))
    assert "build[*]/implement" not in script["steps"], script["steps"]
    assert script["steps"]["build[1]/implement"]["fail"]["transient"] is True
    assert script["steps"]["build[2]/implement"]["text"] == "DONE"

    replay = _dry_run_named(cli, project, "flaky", "--stubs", str(recorded))
    assert _steps_of(home, replay["run_id"]) == _steps_of(home, first["run_id"])
    assert replay["outputs"] == first["outputs"]


# -- CLI surface ------------------------------------------------------------------------------


def test_redact_is_not_a_silent_no_op(cli: CliRunner, seeded: Seeded) -> None:
    """`--redact` is not wired yet: it must never look like it worked.

    The Redactor itself ships — but it can only replace values it is *given*, and secret
    values are deliberately never persisted, so recording cannot recover them.
    """
    result = cli.invoke(
        app, ["runs", "stubs", SUCCEEDED_ID, "--redact", "--root", str(seeded.project)]
    )
    assert result.exit_code == 2, result.output
    assert "not wired yet" in result.output
    # the reason must be the real one, not the stale "redactor does not exist" claim
    assert "never persisted" in result.output
    assert "steps:" not in result.output  # and it printed no script


def test_writing_to_a_missing_directory_is_a_usage_error(cli: CliRunner, seeded: Seeded) -> None:
    """Every usage error in this CLI is exit 2 with a hint — never a traceback and exit 1."""
    target = seeded.project / "nodir" / "script.yaml"
    result = cli.invoke(
        app, ["runs", "stubs", SUCCEEDED_ID, "-o", str(target), "--root", str(seeded.project)]
    )
    assert result.exit_code == 2, result.output
    assert "cannot write" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_writing_onto_a_directory_is_a_usage_error(cli: CliRunner, seeded: Seeded) -> None:
    target = seeded.project / "adir"
    target.mkdir()
    result = cli.invoke(
        app,
        [
            "runs",
            "stubs",
            SUCCEEDED_ID,
            "-o",
            str(target),
            "--force",
            "--root",
            str(seeded.project),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "cannot write" in result.output


def test_a_failed_write_leaves_no_truncated_script(cli: CliRunner, seeded: Seeded) -> None:
    """The write is atomic: the previous file survives a failure mid-write."""
    target = seeded.project / "script.yaml"
    target.write_text("steps: {}\n", encoding="utf-8")
    (seeded.home / "sentinel").write_text("x", encoding="utf-8")
    result = cli.invoke(
        app,
        [
            "runs",
            "stubs",
            SUCCEEDED_ID,
            "-o",
            str(target),
            "--force",
            "--root",
            str(seeded.project),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "assess" in target.read_text(encoding="utf-8")
    assert not list(seeded.project.glob("script.yaml.*"))  # no temp file left behind

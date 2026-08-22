# SPDX-License-Identifier: Apache-2.0
"""`rayspec explain <run> <step> [--full] [--json]` — why did this step run, skip or fail.

One screen that answers the two most common debugging questions ("why was `patch` skipped?",
"what prompt did `assess` actually get?"): final status and `skip_reason`, the join row that
decided it, the evaluated `when:` with its operands, the retry history, the resolved agent after
merge, the env slots, the exact rendered prompt/script and the fingerprint.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.explain import print_agent, print_explain
from rayspec.cli.commands.run import project_slug_for
from rayspec.store.file import FileRunStore

WF = """
rayspec: 1
name: t
isolation: none
inputs:
  topic: {type: string, default: bugs}
agents:
  reviewer: {provider: stub, model: small, access: read-only, tools: {deny: [web]}}
steps:
  - id: fetch
    shell: "printf fix"
  - id: assess
    needs: [fetch]
    agent: reviewer
    env: {TOPIC: "{{ inputs.topic }}"}
    prompt: "Assess {{ steps.fetch.output }} about {{ inputs.topic }}"
  - id: patch
    needs: [assess]
    when: "steps.assess.output == 'nope'"
    shell: "printf patched {{ inputs.topic }}"
  - id: report
    needs: [patch]
    shell: "printf done"
  - id: flaky
    needs: [fetch]
    allow_failure: true
    retry: {attempts: 2, delay: 10ms, on_error: all}
    shell: "exit 3"
"""

STUBS = """
steps:
  assess: {text: "LGTM"}
"""


@pytest.fixture
def ran(cli: CliRunner, home: Path, project: Path) -> tuple[str, Path, FileRunStore]:
    """A finished run of ``WF`` driven by the stub provider."""
    rayspec = project / ".rayspec"
    (rayspec / "workflows" / "t.yaml").write_text(WF, encoding="utf-8")
    (rayspec / "stubs.yaml").write_text(STUBS, encoding="utf-8")
    result = cli.invoke(
        app,
        ["run", "t", "--root", str(project), "--quiet", "--stubs", str(rayspec / "stubs.yaml")],
    )
    assert result.exit_code == 0, result.output
    store = FileRunStore(home / "projects" / project_slug_for(project))
    return store.list_run_ids()[0], project, store


def explain(cli: CliRunner, ran, *args: str):
    run_id, project, _store = ran
    return cli.invoke(app, ["explain", run_id, *args, "--root", str(project)])


def test_explain_a_skipped_step_shows_the_reason_and_the_evaluated_when(cli, ran) -> None:
    result = explain(cli, ran, "patch")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "skipped" in out and "when_false" in out
    assert "steps.assess.output == 'nope'" in out
    # the operand that decided it is shown with its value
    assert "LGTM" in out


def test_explain_shows_the_join_row_that_decided_a_skip(cli, ran) -> None:
    result = explain(cli, ran, "report")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "upstream_skipped" in out
    assert "patch" in out and "skipped" in out
    assert "join" in out


def test_explain_a_failed_step_shows_the_error_and_retry_history(cli, ran) -> None:
    result = explain(cli, ran, "flaky")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "failed" in out and "tolerated" in out
    assert "exit code 3" in out
    assert "attempt 2" in out  # the step.retry event of the second attempt


def test_explain_a_prompt_step_shows_the_agent_env_and_prompt(cli, ran) -> None:
    result = explain(cli, ran, "assess")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "reviewer" in out and "stub" in out
    assert "read-only" in out
    assert "TOPIC" in out and "bugs" in out
    assert "Assess fix about bugs" in out
    assert "succeeded" in out


def test_full_prints_the_persisted_prompt_byte_for_byte(cli, ran) -> None:
    run_id, _project, store = ran
    persisted = store.read_output(run_id, "steps/assess/prompt.txt")
    assert persisted == "Assess fix about bugs"
    result = explain(cli, ran, "assess", "--full")
    assert result.exit_code == 0, result.output
    assert persisted in result.output


def test_explain_a_shell_step_shows_the_rendered_script_and_slots(cli, ran) -> None:
    result = explain(cli, ran, "patch", "--full")
    assert result.exit_code == 0, result.output
    assert "${RAYSPEC_V1}" in result.output
    assert "bugs" in result.output


def test_explain_json_shape(cli, ran) -> None:
    result = explain(cli, ran, "patch", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["step"] == "patch" and data["status"] == "skipped"
    assert data["skip_reason"] == "when_false"
    assert data["when"]["expression"] == "steps.assess.output == 'nope'"
    assert data["when"]["value"] is False
    operands = {op["reference"]: op["value"] for op in data["when"]["operands"]}
    assert operands["steps.assess.output"] == "LGTM"
    assert data["join"]["join"] == "all"
    assert data["join"]["needs"][0]["step"] == "assess"
    assert data["run_id"] and data["kind"] == "shell"

    prompt = json.loads(explain(cli, ran, "assess", "--json", "--full").output)
    assert prompt["agent"]["provider"] == "stub" and prompt["agent"]["name"] == "reviewer"
    assert prompt["rendered"]["text"] == "Assess fix about bugs"
    assert prompt["rendered"]["source"] == "steps/assess/prompt.txt"
    assert prompt["env"] == {"TOPIC": "bugs"}
    assert prompt["fingerprint"]


def test_explain_reports_unknown_steps_and_runs(cli, ran) -> None:
    unknown_step = explain(cli, ran, "nope")
    assert unknown_step.exit_code == 2 and "nope" in unknown_step.output
    _run_id, project, _store = ran
    unknown_run = cli.invoke(app, ["explain", "zzzz", "fetch", "--root", str(project)])
    assert unknown_run.exit_code == 2 and "no run matches" in unknown_run.output


def test_explain_never_touches_the_run(cli, ran) -> None:
    run_id, _project, store = ran
    before = (store.run_dir(run_id) / "run.json").read_bytes()
    assert explain(cli, ran, "assess", "--full").exit_code == 0
    assert (store.run_dir(run_id) / "run.json").read_bytes() == before


LOOP_WF = """
rayspec: 1
name: lp
isolation: none
agents:
  reviewer: {provider: stub, model: small}
steps:
  - id: build
    loop:
      max_iterations: 2
      until: "iteration.n == 2"
      steps:
        - id: implement
          agent: reviewer
          prompt: "Iteration {{ iteration.n }} of {{ run.workflow }}"
"""


@pytest.fixture
def looped(cli: CliRunner, home: Path, project: Path) -> tuple[str, Path, FileRunStore]:
    rayspec = project / ".rayspec"
    (rayspec / "workflows" / "lp.yaml").write_text(LOOP_WF, encoding="utf-8")
    (rayspec / "stubs.yaml").write_text("steps: {'build[*]/implement': {text: done}}\n")
    result = cli.invoke(
        app,
        ["run", "lp", "--root", str(project), "--quiet", "--stubs", str(rayspec / "stubs.yaml")],
    )
    assert result.exit_code == 0, result.output
    store = FileRunStore(home / "projects" / project_slug_for(project))
    return store.list_run_ids()[0], project, store


def test_explain_a_loop_body_step_shows_that_iteration(cli, looped) -> None:
    """'What prompt did the step get in iteration 2?' — the whole point of the command."""
    run_id, _project, store = looped
    assert store.read_output(run_id, "steps/build[2]/implement/prompt.txt") == "Iteration 2 of lp"
    result = explain(cli, looped, "build[2]/implement", "--full")
    assert result.exit_code == 0, result.output
    assert "Iteration 2 of lp" in result.output
    assert "Iteration 1 of lp" not in result.output
    first = explain(cli, looped, "build[1]/implement", "--full")
    assert first.exit_code == 0 and "Iteration 1 of lp" in first.output


def test_explain_json_of_a_loop_body_step_keeps_both_paths(cli, looped) -> None:
    data = json.loads(explain(cli, looped, "build[2]/implement", "--json").output)
    assert data["step"] == "build[2]/implement" and data["def_path"] == "build/implement"
    assert data["prompt_ref"] == "steps/build[2]/implement/prompt.txt"
    assert data["rendered"]["text"] == "Iteration 2 of lp"


def test_a_changed_workflow_is_flagged_before_anything_is_re_evaluated(cli, ran) -> None:
    """Re-evaluated sections come from the file as it is now — say so, never pretend."""
    _run_id, project, _store = ran
    path = project / ".rayspec" / "workflows" / "t.yaml"
    path.write_text(WF.replace("printf patched", "printf repatched"), encoding="utf-8")
    result = explain(cli, ran, "patch")
    assert result.exit_code == 0, result.output
    assert "changed since this run" in result.output
    data = json.loads(explain(cli, ran, "patch", "--json").output)
    assert any("changed since this run" in w for w in data["warnings"])


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=200, force_terminal=False, color_system=None), buf


def test_the_agent_line_and_the_warnings_are_never_parsed_as_markup() -> None:
    """Agent names and warnings quote user/agent text — `[/bold]` must print, not raise."""
    out, buf = _console()
    print_agent(
        out,
        {
            "name": "rev[/bold]iewer",
            "provider": "stub",
            "model": None,
            "raw_model": None,
            "effort": None,
            "access": "read-only",
            "tools": {"allow": [], "deny": []},
            "source": "agents.reviewer",
            "recorded_provider": None,
            "recorded_model": None,
            "session": None,
        },
    )
    assert "rev[/bold]iewer" in buf.getvalue()

    out2, buf2 = _console()
    payload = {
        "run_id": "r",
        "workflow": "t",
        "step": "a",
        "kind": "shell",
        "status": "succeeded",
        "attempts": 1,
        "join": {"join": "all", "needs": [], "decision": "run"},
        "retries": [],
        "env": {},
        "warnings": ["each: '[/bold]' could not be evaluated"],
    }
    print_explain(out2, payload, full=False)
    assert "[/bold]" in buf2.getvalue()


BIG_WF = """
rayspec: 1
name: big
isolation: none
steps:
  - id: produce
    shell: "head -c 70000 /dev/zero | tr '\\\\0' 'x'"
  - id: consume
    needs: [produce]
    shell: "printf '{{ steps.produce.output }}' | wc -c"
"""


@pytest.fixture
def big(cli: CliRunner, home: Path, project: Path) -> tuple[str, Path, FileRunStore]:
    (project / ".rayspec" / "workflows" / "big.yaml").write_text(BIG_WF, encoding="utf-8")
    result = cli.invoke(app, ["run", "big", "--root", str(project), "--quiet"])
    assert result.exit_code == 0, result.output
    store = FileRunStore(home / "projects" / project_slug_for(project))
    return store.list_run_ids()[0], project, store


def test_a_value_over_the_spill_threshold_still_shows_the_script(cli, big) -> None:
    """>64 KiB upstream output is when a user reaches for `explain` — answer, don't apologise."""
    run_id, project, store = big
    assert len(store.read_output(run_id, "steps/produce/output.txt")) == 70_000
    result = cli.invoke(app, ["explain", run_id, "consume", "--json", "--root", str(project)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["rendered"]["error"] is None
    text = data["rendered"]["text"]
    assert text is not None
    lines = text.splitlines()
    assert lines[-1].startswith("printf ") and "wc -c" in lines[-1], "the body is shown as written"
    # above it, the preamble that reads the spilled value back — with the scratch path it would
    # read replaced by the placeholder, because an explain output is pasted into bug reports
    assert lines[0].startswith("unset RAYSPEC_V1; ") and "70000 bytes" in lines[0]
    assert "spill_dir" not in text and "/v1-" not in text
    human = cli.invoke(app, ["explain", run_id, "consume", "--full", "--root", str(project)])
    assert human.exit_code == 0 and "70000 bytes" in human.output


def test_explain_reads_the_event_log_once(cli, ran, monkeypatch: pytest.MonkeyPatch) -> None:
    """events.jsonl can be multi-MB; the retry rows and `reused` come from one pass."""
    calls = 0
    original = FileRunStore.read_events

    def counting(self, run_id: str):
        nonlocal calls
        calls += 1
        return original(self, run_id)

    monkeypatch.setattr(FileRunStore, "read_events", counting)
    result = explain(cli, ran, "flaky", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["retries"] and data["reused"] is False
    assert calls == 1


ENV_WF = """
rayspec: 1
name: ev
isolation: none
steps:
  - id: uses_env
    shell: "echo {{ env.HOME }}"
  - id: plain
    shell: "echo hello"
"""


@pytest.fixture
def env_run(cli: CliRunner, home: Path, project: Path) -> tuple[str, Path, FileRunStore]:
    (project / ".rayspec" / "workflows" / "ev.yaml").write_text(ENV_WF, encoding="utf-8")
    result = cli.invoke(app, ["run", "ev", "--root", str(project), "--quiet"])
    assert result.exit_code == 0, result.output
    store = FileRunStore(home / "projects" / project_slug_for(project))
    return store.list_run_ids()[0], project, store


def test_a_re_rendered_body_that_reads_env_says_where_env_came_from(cli, env_run) -> None:
    """The run's environment is not recorded — a re-render silently uses this shell's."""
    run_id, project, _store = env_run
    data = json.loads(
        cli.invoke(app, ["explain", run_id, "uses_env", "--json", "--root", str(project)]).output
    )
    assert any("env." in w and "this shell" in w for w in data["warnings"]), data["warnings"]
    plain = json.loads(
        cli.invoke(app, ["explain", run_id, "plain", "--json", "--root", str(project)]).output
    )
    assert not [w for w in plain["warnings"] if "this shell" in w]

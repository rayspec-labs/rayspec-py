"""End to end: configured secret sources + the Redactor at every writer.

The leak test is the point of this suite: a workflow with a ``secret: true`` input **and** a
``config.secrets`` entry, consumed by a shell step that deliberately echoes both — afterwards
neither value appears anywhere under ``RAYSPEC_HOME`` or on stdout. Before the echoed
value was persisted verbatim.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from ._helpers import invoke, run_records

INPUT_SECRET = "ghp_INPUTSECRET_ABCDEFGH"
CONFIG_SECRET = "cfg-SECRETVALUE-0123456789"

WORKFLOW = """
rayspec: 1
name: sec
isolation: none
inputs:
  token: { type: string, secret: true, required: true }
steps:
  - id: leak
    shell: |
      echo "input=$RAYSPEC_INPUT_TOKEN"
      echo "config=$DEPLOY_KEY"
  - id: gate
    needs: [leak]
    approve: "ship?"
  - id: after
    needs: [gate]
    shell: echo "still=${#DEPLOY_KEY}"
outputs:
  v: "{{ steps.leak.output }}"
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "sec.yaml").write_text(textwrap.dedent(WORKFLOW))
    (root / ".rayspec" / "config.yaml").write_text(
        "secrets:\n  DEPLOY_KEY: {env: DEPLOY_KEY_SOURCE}\n"
    )
    return root


def _grep(root: Path, needle: str) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and needle in p.read_text(errors="replace")
    )


def _env() -> dict[str, str]:
    return {"DEPLOY_KEY_SOURCE": CONFIG_SECRET}


def test_neither_secret_lands_anywhere_under_the_home(project: Path, home: Path) -> None:
    res = invoke(
        [
            "run",
            "sec",
            "--root",
            str(project),
            "--input",
            f"token={INPUT_SECRET}",
            "--yes",
            "--json",
        ],
        home,
        **_env(),
    )
    assert res.exit_code == 0, res.output
    assert _grep(home, INPUT_SECRET) == []
    assert _grep(home, CONFIG_SECRET) == []
    assert INPUT_SECRET not in res.output and CONFIG_SECRET not in res.output
    assert INPUT_SECRET not in res.stdout and CONFIG_SECRET not in res.stdout
    # the values WERE seen by the step — the markers prove the writers ran, not that nothing did
    hits = _grep(home, "[REDACTED:token]") + _grep(home, "[REDACTED:DEPLOY_KEY]")
    assert hits, sorted(p.name for p in home.rglob("*") if p.is_file())
    (record,) = run_records(home)
    assert record["status"] == "succeeded"
    assert record["inputs"]["token"] == "<secret>"


def test_the_redacted_marker_reaches_every_writer(project: Path, home: Path) -> None:
    invoke(
        ["run", "sec", "--root", str(project), "--input", f"token={INPUT_SECRET}", "--yes"],
        home,
        **_env(),
    )
    (run_dir,) = list(home.rglob("runs/*/"))
    step = run_dir / "steps" / "leak"
    assert "[REDACTED:token]" in (step / "output.txt").read_text()
    assert "[REDACTED:token]" in (step / "stdout.log").read_text()
    assert "[REDACTED:token]" in (step / "stream.jsonl").read_text()
    assert "[REDACTED:DEPLOY_KEY]" in (step / "output.txt").read_text()
    assert INPUT_SECRET not in (run_dir / "run.json").read_text()


def _paused(project: Path, home: Path) -> str:
    res = invoke(
        ["run", "sec", "--root", str(project), "--input", f"token={INPUT_SECRET}"],
        home,
        TOKEN_SOURCE=INPUT_SECRET,
        **_env(),
    )
    assert res.exit_code == 3, res.output
    (record,) = run_records(home)
    return str(record["run_id"])


def test_a_configured_source_supplies_a_secret_input_without_input_flags(
    project: Path, home: Path
) -> None:
    """``secrets:`` may name a ``secret: true`` input; then no --input is needed at all."""
    (project / ".rayspec" / "config.yaml").write_text(
        "secrets:\n  token: {env: TOKEN_SOURCE}\n  DEPLOY_KEY: {env: DEPLOY_KEY_SOURCE}\n"
    )
    res = invoke(
        ["run", "sec", "--root", str(project), "--yes"],
        home,
        TOKEN_SOURCE=INPUT_SECRET,
        **_env(),
    )
    assert res.exit_code == 0, res.output
    assert _grep(home, INPUT_SECRET) == []


def test_approve_refetches_the_secret_instead_of_asking(project: Path, home: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        "secrets:\n  token: {env: TOKEN_SOURCE}\n  DEPLOY_KEY: {env: DEPLOY_KEY_SOURCE}\n"
    )
    run_id = _paused(project, home)
    res = invoke(
        ["approve", run_id, "--root", str(project)], home, TOKEN_SOURCE=INPUT_SECRET, **_env()
    )
    assert res.exit_code == 0, res.output
    assert _grep(home, INPUT_SECRET) == [] and _grep(home, CONFIG_SECRET) == []


def test_resume_refetches_the_secret_instead_of_asking(project: Path, home: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        "secrets:\n  token: {env: TOKEN_SOURCE}\n  DEPLOY_KEY: {env: DEPLOY_KEY_SOURCE}\n"
    )
    run_id = _paused(project, home)
    res = invoke(
        ["resume", run_id, "--root", str(project), "--yes"],
        home,
        TOKEN_SOURCE=INPUT_SECRET,
        **_env(),
    )
    assert res.exit_code == 0, res.output


def test_reject_refetches_the_secret_instead_of_asking(project: Path, home: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        "secrets:\n  token: {env: TOKEN_SOURCE}\n  DEPLOY_KEY: {env: DEPLOY_KEY_SOURCE}\n"
    )
    run_id = _paused(project, home)
    res = invoke(
        ["reject", run_id, "nope", "--root", str(project)],
        home,
        TOKEN_SOURCE=INPUT_SECRET,
        **_env(),
    )
    assert res.exit_code in {1, 4}, res.output
    assert "missing secret input" not in res.output


def test_a_missing_source_is_an_actionable_error(project: Path, home: Path) -> None:
    res = invoke(
        ["run", "sec", "--root", str(project), "--input", f"token={INPUT_SECRET}", "--yes"], home
    )
    assert res.exit_code == 2, res.output
    assert "secrets.DEPLOY_KEY" in res.output and "DEPLOY_KEY_SOURCE" in res.output


def test_a_world_readable_file_source_is_refused(project: Path, home: Path, tmp_path: Path) -> None:
    path = tmp_path / "deploy.key"
    path.write_text(CONFIG_SECRET)
    path.chmod(0o644)
    (project / ".rayspec" / "config.yaml").write_text(f"secrets:\n  DEPLOY_KEY: {{file: {path}}}\n")
    res = invoke(
        ["run", "sec", "--root", str(project), "--input", f"token={INPUT_SECRET}", "--yes"], home
    )
    assert res.exit_code == 2, res.output
    assert "0644" in res.output and "chmod 600" in res.output
    assert CONFIG_SECRET not in res.output


def test_json_mode_stream_lines_carry_no_secret(project: Path, home: Path) -> None:
    res = invoke(
        [
            "run",
            "sec",
            "--root",
            str(project),
            "--input",
            f"token={INPUT_SECRET}",
            "--yes",
            "--json",
        ],
        home,
        **_env(),
    )
    assert res.exit_code == 0, res.output
    for raw in res.stdout.splitlines():
        if raw.strip():
            json.loads(raw)  # still valid JSON after redaction
    assert INPUT_SECRET not in res.stdout


# -- included workflows -----------------------------------------------------------------------

CHILD = """
rayspec: 1
name: child
inputs:
  label: { type: string, required: true }
steps:
  - id: inner
    shell: echo "{{ inputs.label }} input=${#RAYSPEC_INPUT_TOKEN} config=${#DEPLOY_KEY}"
outputs:
  seen: "{{ steps.inner.output }}"
"""

PARENT = """
rayspec: 1
name: parent
isolation: none
inputs:
  token: { type: string, secret: true, required: true }
steps:
  - id: body
    include: child
    with: { label: hi }
outputs:
  v: "{{ steps.body.output.seen }}"
"""


def test_an_included_body_already_has_the_secrets_without_a_with_binding(
    tmp_path: Path, home: Path
) -> None:
    """Binding a secret through ``with:`` stays refused because it is not needed —
    every secret of the run reaches an include body's shell steps as ``RAYSPEC_INPUT_<NAME>``,
    and every ``config.secrets`` entry under its own name."""
    root = tmp_path / "inc"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "child.yaml").write_text(textwrap.dedent(CHILD))
    (root / ".rayspec" / "workflows" / "parent.yaml").write_text(textwrap.dedent(PARENT))
    (root / ".rayspec" / "config.yaml").write_text(
        "secrets:\n  DEPLOY_KEY: {env: DEPLOY_KEY_SOURCE}\n"
    )
    res = invoke(
        ["run", "parent", "--root", str(root), "--input", f"token={INPUT_SECRET}", "--yes"],
        home,
        **_env(),
    )
    assert res.exit_code == 0, res.output
    output = next(home.rglob("runs/*/steps/body/inner/output.txt")).read_text()
    assert output.strip() == f"hi input={len(INPUT_SECRET)} config={len(CONFIG_SECRET)}"
    assert _grep(home, INPUT_SECRET) == [] and _grep(home, CONFIG_SECRET) == []


# -- ergonomics -------------------------------------------------------------------------------


def test_show_names_the_secrets_a_paused_run_still_needs(project: Path, home: Path) -> None:
    run_id = _paused(project, home)
    res = invoke(["show", run_id, "--root", str(project)], home)
    assert res.exit_code == 0, res.output
    assert "secret inputs to re-supply" in res.output
    assert "token" in res.output and INPUT_SECRET not in res.output
    as_json = invoke(["show", run_id, "--root", str(project), "--json"], home)
    payload = json.loads(as_json.stdout)
    assert payload["pending_secret_inputs"] == ["token"]


def test_show_of_a_finished_run_has_no_secret_line(project: Path, home: Path) -> None:
    invoke(
        ["run", "sec", "--root", str(project), "--input", f"token={INPUT_SECRET}", "--yes"],
        home,
        **_env(),
    )
    (record,) = run_records(home)
    res = invoke(["show", str(record["run_id"]), "--root", str(project)], home)
    assert "secret inputs to re-supply" not in res.output


def test_doctor_lists_the_configured_sources_without_their_values(
    project: Path, home: Path
) -> None:
    res = invoke(["doctor", "--root", str(project), "--json"], home, **_env())
    checks = {c["id"]: c for c in json.loads(res.stdout)["checks"]}
    assert "secrets" in checks, sorted(checks)
    detail = checks["secrets"]["detail"]
    assert "DEPLOY_KEY" in detail and "env DEPLOY_KEY_SOURCE" in detail
    assert CONFIG_SECRET not in detail


def test_doctor_says_nothing_when_no_secrets_are_configured(tmp_path: Path, home: Path) -> None:
    root = tmp_path / "bare"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    res = invoke(["doctor", "--root", str(root), "--json"], home)
    checks = {c["id"] for c in json.loads(res.stdout)["checks"]}
    assert "secrets" not in checks


def _with_token_source(project: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        "secrets:\n  token: {env: TOKEN_SOURCE}\n  DEPLOY_KEY: {env: DEPLOY_KEY_SOURCE}\n"
    )


def test_input_still_resumes_a_run_whose_secret_source_broke(project: Path, home: Path) -> None:
    """A vault helper being unavailable must not strand a paused run: the user has the value in
    hand and ``--input`` has to win."""
    _with_token_source(project)
    run_id = _paused(project, home)
    res = invoke(  # TOKEN_SOURCE is NOT set: the configured source cannot resolve
        ["approve", run_id, "--root", str(project), "--input", f"token={INPUT_SECRET}"],
        home,
        **_env(),
    )
    assert res.exit_code == 0, res.output
    assert _grep(home, INPUT_SECRET) == []


def test_the_env_var_still_resumes_a_run_whose_secret_source_broke(
    project: Path, home: Path
) -> None:
    _with_token_source(project)
    run_id = _paused(project, home)
    res = invoke(
        ["resume", run_id, "--root", str(project), "--yes"],
        home,
        RAYSPEC_INPUT_TOKEN=INPUT_SECRET,
        **_env(),
    )
    assert res.exit_code == 0, res.output


def test_a_broken_source_is_reported_next_to_the_missing_secret(project: Path, home: Path) -> None:
    _with_token_source(project)
    run_id = _paused(project, home)
    res = invoke(["approve", run_id, "--root", str(project)], home, **_env())
    assert res.exit_code == 2, res.output
    assert "missing secret input" in res.output
    assert "secrets.token" in res.output and "TOKEN_SOURCE" in res.output


def _plain_project(tmp_path: Path, config: str) -> Path:
    root = tmp_path / "plain"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "hi.yaml").write_text(
        "rayspec: 1\nname: hi\nisolation: none\nsteps:\n  - id: greet\n    shell: echo hi\n"
    )
    (root / ".rayspec" / "config.yaml").write_text(config)
    return root


def test_a_secret_entry_the_workflow_never_reads_does_not_block_the_run(
    tmp_path: Path, home: Path
) -> None:
    """Config secrets are resolved lazily: one stale user-level entry must not make every
    workflow on the machine unrunnable."""
    root = _plain_project(tmp_path, "secrets:\n  UNUSED_TOKEN: {env: NEVER_SET_XYZ}\n")
    res = invoke(["run", "hi", "--root", str(root), "--yes"], home)
    assert res.exit_code == 0, res.output


def test_a_cmd_source_the_workflow_never_reads_is_never_run(tmp_path: Path, home: Path) -> None:
    marker = tmp_path / "helper-ran"
    root = _plain_project(
        tmp_path,
        f"secrets:\n  UNUSED_TOKEN: {{cmd: ['touch', '{marker}']}}\n",
    )
    res = invoke(["run", "hi", "--root", str(root), "--yes"], home)
    assert res.exit_code == 0, res.output
    assert not marker.exists(), "an unused cmd: helper must not run on every rayspec run"


def test_a_secret_entry_the_workflow_reads_still_fails_loudly(tmp_path: Path, home: Path) -> None:
    root = _plain_project(tmp_path, "secrets:\n  NEEDED: {env: NEVER_SET_XYZ}\n")
    (root / ".rayspec" / "workflows" / "hi.yaml").write_text(
        'rayspec: 1\nname: hi\nisolation: none\nsteps:\n  - id: greet\n    shell: echo "$NEEDED"\n'
    )
    res = invoke(["run", "hi", "--root", str(root), "--yes"], home)
    assert res.exit_code == 2, res.output
    assert "secrets.NEEDED" in res.output


def test_a_cmd_source_runs_once_per_approve(tmp_path: Path, home: Path) -> None:
    """One provider per command: a second one
    re-runs every ``cmd:`` helper — a second Touch ID prompt on the same `rayspec approve`."""
    counter = tmp_path / "helper.calls"
    root = tmp_path / "both"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "both.yaml").write_text(
        "rayspec: 1\nname: both\nisolation: none\n"
        "inputs:\n  api_key: {type: string, secret: true, required: true}\n"
        "steps:\n"
        '  - id: use\n    shell: echo "len=${#api_key}"\n'
        "  - id: gate\n    needs: [use]\n    approve: ship?\n"
        "  - id: after\n    needs: [gate]\n    shell: echo done\n"
    )
    helper = f"echo x >> {counter}; printf '%s' '{INPUT_SECRET}'"
    (root / ".rayspec" / "config.yaml").write_text(
        f'secrets:\n  api_key: {{cmd: ["sh", "-c", "{helper}"]}}\n'
    )
    res = invoke(["run", "both", "--root", str(root)], home)
    assert res.exit_code == 3, res.output
    (record,) = run_records(home)
    counter.write_text("")  # count only what `approve` does
    res = invoke(["approve", str(record["run_id"]), "--root", str(root)], home)
    assert res.exit_code == 0, res.output
    assert counter.read_text().count("x") == 1, counter.read_text()


def test_doctor_exits_non_zero_when_a_source_cannot_resolve(project: Path, home: Path) -> None:
    """A source `rayspec run` refuses to start on must not read as 'all required checks passed'."""
    res = invoke(["doctor", "--root", str(project)], home)  # DEPLOY_KEY_SOURCE is unset
    assert res.exit_code != 0, res.output
    assert "secrets" in res.output


def test_doctor_flags_a_secret_too_short_to_redact(project: Path, home: Path) -> None:
    res = invoke(["doctor", "--root", str(project), "--json"], home, DEPLOY_KEY_SOURCE="ab7")
    detail = {c["id"]: c for c in json.loads(res.stdout)["checks"]}["secrets"]["detail"]
    assert "too short to redact" in detail
    assert "ab7" not in detail


def test_a_secret_too_short_to_redact_is_called_out_at_run_start(project: Path, home: Path) -> None:
    """`.skipped` had no consumer: a short value was written verbatim with no signal at all."""
    res = invoke(
        ["run", "sec", "--root", str(project), "--input", f"token={INPUT_SECRET}", "--yes"],
        home,
        DEPLOY_KEY_SOURCE="ab7",
    )
    assert res.exit_code == 0, res.output
    assert "DEPLOY_KEY" in res.output and "not redacted" in res.output


def test_the_warning_repeats_on_resume(project: Path, home: Path) -> None:
    run_id = _paused(project, home)
    res = invoke(
        ["approve", run_id, "--root", str(project)],
        home,
        DEPLOY_KEY_SOURCE="ab7",
        TOKEN_SOURCE=INPUT_SECRET,
        RAYSPEC_INPUT_TOKEN=INPUT_SECRET,
    )
    assert res.exit_code == 0, res.output
    assert "DEPLOY_KEY" in res.output and "not redacted" in res.output


def test_a_json_output_broken_by_redaction_says_so(tmp_path: Path, home: Path) -> None:
    """A bare-token secret (a numeric PIN) makes the printed document invalid; the message must
    point at redaction instead of at a document that looks broken for no reason."""
    root = tmp_path / "jsonsec"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "j.yaml").write_text(
        "rayspec: 1\nname: j\nisolation: none\nsteps:\n"
        "  - id: emit\n"
        "    shell: |\n"
        '      printf \'{"pin": %s, "ok": true}\\n\' "$PIN"\n'
        "    output_schema: {type: object}\n"
    )
    (root / ".rayspec" / "config.yaml").write_text("secrets:\n  PIN: {env: PIN_SOURCE}\n")
    res = invoke(["run", "j", "--root", str(root), "--yes"], home, PIN_SOURCE="12345678")
    assert res.exit_code == 1, res.output
    assert "redact" in res.output.lower(), res.output
    assert "PIN" in res.output


def test_show_does_not_ask_for_a_secret_the_config_supplies(project: Path, home: Path) -> None:
    """Telling the user to pass --input for a name `config.secrets` re-fetches by itself is the
    opposite of the feature's headline."""
    _with_token_source(project)
    run_id = _paused(project, home)
    res = invoke(["show", run_id, "--root", str(project)], home)
    assert "supplied by config.secrets: token" in res.output
    assert "secret inputs to re-supply" not in res.output

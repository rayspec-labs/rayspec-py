# SPDX-License-Identifier: Apache-2.0
"""`rayspec eval <run> '<expr>' [--step PATH] [--shell] [--json]` — the expression REPL.

A read-only Jinja prompt over a finished run's context: the same lexical scoping and the same
``RayspecUndefined`` hints the engine raises, so writing a `when:`/`until:` stops being trial
and error. It must never run a step or call a provider.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.eval import print_warning
from rayspec.cli.commands.run import project_slug_for
from rayspec.store.file import FileRunStore

WF = """
rayspec: 1
name: t
isolation: none
inputs:
  topic: {type: string, default: bugs}
steps:
  - id: fetch
    shell: "printf 'alpha\\nbeta'"
  - id: build
    needs: [fetch]
    loop:
      max_iterations: 2
      until: "iteration.n == 2"
      steps:
        - id: implement
          shell: "printf i{{ iteration.n }}"
  - id: fan
    needs: [fetch]
    each: "['a', 'b']"
    as: letter
    steps:
      - id: patch
        shell: "printf p-{{ letter }}"
  - id: never
    needs: [fetch]
    when: "false"
    shell: "printf nope"
  - id: noisy
    needs: [fetch]
    shell: "printf '[/INST] done'"
"""


@pytest.fixture
def ran(cli: CliRunner, home: Path, project: Path) -> tuple[str, Path]:
    """A finished run of ``WF`` in the CLI's own store; returns ``(run id, project root)``."""
    (project / ".rayspec" / "workflows" / "t.yaml").write_text(WF, encoding="utf-8")
    result = cli.invoke(app, ["run", "t", "--root", str(project), "--quiet"])
    assert result.exit_code == 0, result.output
    store = FileRunStore(home / "projects" / project_slug_for(project))
    return store.list_run_ids()[0], project


def run_eval(cli: CliRunner, ran: tuple[str, Path], *args: str):
    run_id, project = ran
    return cli.invoke(app, ["eval", run_id, *args, "--root", str(project)])


def test_eval_prints_a_value_from_the_run_context(cli: CliRunner, ran: tuple[str, Path]) -> None:
    result = run_eval(cli, ran, "steps.fetch.output | length")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "10"
    text = run_eval(cli, ran, "steps.fetch.output.splitlines() | first")
    assert text.exit_code == 0 and text.output.strip() == "alpha"


def test_eval_resolves_inputs_run_and_project_roots(cli: CliRunner, ran: tuple[str, Path]) -> None:
    assert run_eval(cli, ran, "run.workflow").output.strip() == "t"
    assert run_eval(cli, ran, "inputs.topic | default('none')").output.strip() == "bugs"


def test_eval_is_lexically_scoped_to_step(cli: CliRunner, ran: tuple[str, Path]) -> None:
    outside = run_eval(cli, ran, "steps.implement.output")
    assert outside.exit_code == 2
    assert "inside loop 'build'" in outside.output
    inside = run_eval(cli, ran, "steps.implement.output", "--step", "build[2]/implement")
    assert inside.exit_code == 0 and inside.output.strip() == "i2"


def test_eval_resolves_iteration_prev_inside_a_loop_body(
    cli: CliRunner, ran: tuple[str, Path]
) -> None:
    result = run_eval(cli, ran, "iteration.prev.implement.output", "--step", "build[2]/implement")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "i1"
    first = run_eval(cli, ran, "iteration.n", "--step", "build[1]/implement")
    assert first.exit_code == 0 and first.output.strip() == "1"


def test_eval_binds_the_each_item(cli: CliRunner, ran: tuple[str, Path]) -> None:
    result = run_eval(cli, ran, "letter", "--step", "fan[1]/patch")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "b"


def test_undefined_reference_prints_the_hint_not_a_traceback(
    cli: CliRunner, ran: tuple[str, Path]
) -> None:
    result = run_eval(cli, ran, "steps.never.output")
    assert result.exit_code == 2
    assert "was skipped" in result.output and "status == 'succeeded'" in result.output
    assert "Traceback" not in result.output
    ok = run_eval(cli, ran, "steps.never.ok")
    assert ok.exit_code == 2 and "was skipped" in ok.output


def test_eval_json_envelope(cli: CliRunner, ran: tuple[str, Path]) -> None:
    result = run_eval(cli, ran, "steps.fetch.output", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["value"] == "alpha\nbeta" and data["type"] == "string"
    assert data["run_id"] == ran[0] and data["step"] is None
    assert data["expr"] == "steps.fetch.output"


def test_eval_shell_shows_the_env_slot(cli: CliRunner, ran: tuple[str, Path]) -> None:
    result = run_eval(cli, ran, "inputs.topic", "--shell")
    assert result.exit_code == 0, result.output
    assert "${RAYSPEC_V1}" in result.output and "bugs" in result.output
    data = json.loads(run_eval(cli, ran, "inputs.topic", "--shell", "--json").output)
    assert data["shell"] == "${RAYSPEC_V1}" and data["env"] == {"RAYSPEC_V1": "bugs"}


def test_eval_never_touches_the_run(cli: CliRunner, ran: tuple[str, Path], home: Path) -> None:
    run_id, project = ran
    store = FileRunStore(home / "projects" / project_slug_for(project))
    before = (store.run_dir(run_id) / "run.json").read_bytes()
    assert run_eval(cli, ran, "steps.fetch.output").exit_code == 0
    assert store.list_run_ids() == [run_id]
    assert (store.run_dir(run_id) / "run.json").read_bytes() == before


def test_eval_reports_an_unknown_step_and_an_unknown_run(
    cli: CliRunner, ran: tuple[str, Path]
) -> None:
    bad_step = run_eval(cli, ran, "1", "--step", "nope")
    assert bad_step.exit_code == 2 and "nope" in bad_step.output
    _run_id, project = ran
    unknown = cli.invoke(app, ["eval", "zzzz", "1", "--root", str(project)])
    assert unknown.exit_code == 2 and "no run matches" in unknown.output


def test_eval_reports_a_syntax_error_in_the_expression(
    cli: CliRunner, ran: tuple[str, Path]
) -> None:
    result = run_eval(cli, ran, "steps.fetch.output |")
    assert result.exit_code == 2 and "Traceback" not in result.output


def test_eval_warns_when_the_workflow_changed_since_the_run(
    cli: CliRunner, ran: tuple[str, Path]
) -> None:
    _run_id, project = ran
    path = project / ".rayspec" / "workflows" / "t.yaml"
    path.write_text(WF.replace("printf 'alpha\\nbeta'", "printf 'gamma'"), encoding="utf-8")
    result = run_eval(cli, ran, "steps.fetch.output")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("alpha\nbeta")  # the stored output, not the new template
    assert "changed since this run" in result.output


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=200, force_terminal=False, color_system=None), buf


def test_eval_shell_prints_a_markup_looking_value_literally(
    cli: CliRunner, ran: tuple[str, Path]
) -> None:
    """`[/INST]` is ordinary agent output — printing it must not raise a Rich MarkupError."""
    result = run_eval(cli, ran, "steps.noisy.output", "--shell")
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "[/INST] done" in result.output
    assert "Traceback" not in result.output


def test_print_warning_never_parses_the_message_as_markup() -> None:
    out, buf = _console()
    print_warning(out, "each: ['[/bold]'] could not be evaluated")
    text = buf.getvalue()
    assert "warning:" in text
    assert "[/bold]" in text


def test_eval_shell_of_a_huge_value_shows_a_placeholder_not_an_internal_error(
    cli: CliRunner, home: Path, project: Path
) -> None:
    (project / ".rayspec" / "workflows" / "big.yaml").write_text(
        "rayspec: 1\nname: big\nisolation: none\nsteps:\n"
        "  - id: produce\n    shell: \"head -c 70000 /dev/zero | tr '\\\\0' 'x'\"\n",
        encoding="utf-8",
    )
    ran = cli.invoke(app, ["run", "big", "--root", str(project), "--quiet"])
    assert ran.exit_code == 0, ran.output
    run_id = FileRunStore(home / "projects" / project_slug_for(project)).list_run_ids()[0]
    result = cli.invoke(
        app, ["eval", run_id, "steps.produce.output", "--shell", "--root", str(project)]
    )
    assert result.exit_code == 0, result.output
    assert "70000 bytes" in result.output and "spill_dir" not in result.output


def test_eval_of_an_env_reference_says_the_value_is_this_shell_s(
    cli: CliRunner, ran: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAYSPEC_TEST_VAR", "from-this-shell")
    result = run_eval(cli, ran, "env.RAYSPEC_TEST_VAR", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["value"] == "from-this-shell"
    assert any("this shell" in w for w in data["warnings"]), data["warnings"]
    quiet = json.loads(run_eval(cli, ran, "inputs.topic", "--json").output)
    assert not [w for w in quiet["warnings"] if "this shell" in w]


def test_eval_shell_of_an_env_reference_warns_too(
    cli: CliRunner, ran: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAYSPEC_TEST_VAR", "from-this-shell")
    data = json.loads(run_eval(cli, ran, "env.RAYSPEC_TEST_VAR", "--shell", "--json").output)
    assert any("this shell" in w for w in data["warnings"]), data["warnings"]

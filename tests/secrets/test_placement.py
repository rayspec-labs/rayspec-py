"""The placement rules live in ``rayspec.loader.secrets`` and stay narrow."""

from __future__ import annotations

import textwrap
from pathlib import Path

import yaml

from rayspec.loader import load_workflow, validate_workflow
from rayspec.loader.secrets import (
    check_secret_reference,
    config_secrets_in_use,
    include_secret_input_message,
    secret_reference_message,
    secret_whole_inputs_message,
)
from rayspec.schema import InputSpec
from rayspec.templating import TemplateEngine

SECRET_INPUTS = {
    "token": InputSpec(type="string", secret=True),
    "issue": InputSpec(type="integer"),
}


def test_a_secret_reference_is_refused_outside_an_env_mapping() -> None:
    verdict = check_secret_reference(None, ("inputs", "token", ()), SECRET_INPUTS, secret_ok=False)
    assert verdict.stop and verdict.message == secret_reference_message("token")


def test_the_env_position_is_the_one_exception() -> None:
    verdict = check_secret_reference(None, ("inputs", "token", ()), SECRET_INPUTS, secret_ok=True)
    assert verdict.message is None and not verdict.stop


def test_a_non_secret_input_is_never_flagged() -> None:
    verdict = check_secret_reference(None, ("inputs", "issue", ()), SECRET_INPUTS, secret_ok=False)
    assert verdict.message is None and not verdict.stop


def test_inputs_as_a_whole_carries_every_secret_along() -> None:
    verdict = check_secret_reference("inputs", None, SECRET_INPUTS, secret_ok=False)
    assert verdict.stop and verdict.message == secret_whole_inputs_message(["token"])


def test_inputs_as_a_whole_is_fine_without_secrets() -> None:
    verdict = check_secret_reference(
        "inputs", None, {"issue": InputSpec(type="integer")}, secret_ok=False
    )
    assert verdict.message is None and verdict.stop  # nothing to resolve, nothing to report


def test_a_step_reference_is_not_a_secret_question() -> None:
    verdict = check_secret_reference(None, ("steps", "build", ()), SECRET_INPUTS, secret_ok=False)
    assert verdict.message is None and not verdict.stop


def test_the_include_message_points_at_the_env_var_the_body_already_gets() -> None:
    message = include_secret_input_message("child", ["token"])
    assert "RAYSPEC_INPUT_TOKEN" in message and "already reaches" in message


# -- the rules through the real validator ----------------------------------------------------

WORKFLOW = """
rayspec: 1
name: sec
isolation: none
inputs:
  token: { type: string, secret: true, required: true }
agents:
  r: { provider: stub }
steps:
  - id: ask
    agent: r
    prompt: PROMPT
    EXTRA
"""


def _errors(tmp_path: Path, *, extra: str = "", prompt: str = '"hi"') -> list[str]:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    text = textwrap.dedent(WORKFLOW).replace("PROMPT", prompt).replace("EXTRA", extra)
    (root / ".rayspec" / "workflows" / "sec.yaml").write_text(text)
    resolved = load_workflow("sec", project_root=root, home=tmp_path / "home")
    return list(validate_workflow(resolved, template_checker=TemplateEngine()).errors)


def test_env_on_a_prompt_step_still_refuses_a_secret(tmp_path: Path) -> None:
    """Deliberately NOT relaxed. The Codex CLI writes the child environment to a 0644 file
    under ``~/.codex/shell_snapshots/`` that outlives the run and no redactor can reach; see
    ``tests/integration/test_secret_placement_live.py`` for the reproduction."""
    errors = _errors(tmp_path, extra='env: { T: "{{ inputs.token }}" }')
    assert any("declared secret: true" in e for e in errors), errors


def test_a_prompt_body_still_refuses_a_secret(tmp_path: Path) -> None:
    errors = _errors(tmp_path, prompt='"{{ inputs.token }}"')
    assert any("declared secret: true" in e for e in errors), errors


def test_cwd_still_refuses_a_secret(tmp_path: Path) -> None:
    """``cwd:`` is rendered into the fingerprint and ``context.json``; it stays refused."""
    errors = _errors(tmp_path, prompt='"hi"')
    assert not any("cwd" in e for e in errors)  # the baseline workflow is clean


# -- which config.secrets a run can read (lazy resolution) -----------------------------------


def _steps(text: str) -> list[object]:
    from rayspec.schema import parse_workflow

    return list(parse_workflow(yaml.safe_load(text), source="t").steps)


def test_only_names_a_shell_step_mentions_count_as_in_use() -> None:
    steps = _steps(
        "rayspec: 1\nname: t\nsteps:\n"
        '  - id: a\n    shell: echo "$DEPLOY_KEY"\n'
        "  - id: b\n    prompt: mentions OTHER_KEY only in a prompt\n"
    )
    assert config_secrets_in_use(steps, ["DEPLOY_KEY", "OTHER_KEY"]) == ("DEPLOY_KEY",)


def test_a_partial_word_is_not_a_use() -> None:
    steps = _steps("rayspec: 1\nname: t\nsteps:\n  - id: a\n    shell: echo $DEPLOY_KEY_2\n")
    assert config_secrets_in_use(steps, ["DEPLOY_KEY"]) == ()


def test_an_env_mapping_counts_as_a_use() -> None:
    steps = _steps(
        "rayspec: 1\nname: t\nsteps:\n"
        "  - id: a\n    python: pass\n    env: {TOKEN: 'x'}\n"
        "  - id: b\n    shell: 'true'\n    env: {OTHER: '$KEY_B'}\n"
    )
    assert config_secrets_in_use(steps, ["TOKEN", "KEY_B"]) == ("TOKEN", "KEY_B")

"""The **authoring** skill teaches the secret-input seam: its snippet and its hand-written
paragraph must match the implementation.

``inputs.<name>.secret`` shipped in 1.0.0, so these tests always run. The skill text is
hand-written, so nothing else catches drift between what it promises and what the loader and
engine actually do.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.schema import InputSpec
from rayspec.skill import WORKFLOWS_SKILL, skill_dir

assert "secret" in InputSpec.model_fields, (
    "inputs.<name>.secret shipped in 1.0.0 — this suite must never be skipped again"
)

SKILL_MD = (skill_dir(WORKFLOWS_SKILL) / "SKILL.md").read_text(encoding="utf-8")
_FENCE_RE = re.compile(r"```yaml\n(?P<body>.*?)```", re.DOTALL)
#: The whole ``## Secrets`` section. Anchoring on the section rather than on one sentence
#: keeps the needles below pinned to the place that teaches the seam, wherever inside it
#: they are written — and still fails if the section is dropped or emptied.
PARAGRAPH = SKILL_MD.split("\n## Secrets\n", 1)[1].split("\n## ", 1)[0]


def secret_snippet() -> str:
    """The ``secret: true`` fence of SKILL.md, wrapped into a complete workflow."""
    [body] = [
        m.group("body") for m in _FENCE_RE.finditer(SKILL_MD) if "secret: true" in m.group("body")
    ]
    return "rayspec: 1\nname: sec\n" + body


def _project(tmp_path: Path, workflows: dict[str, str]) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    home.mkdir()
    for name, text in workflows.items():
        (root / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    return root, {"RAYSPEC_HOME": str(home)}


def test_skill_text_states_the_seam() -> None:
    assert "`inputs.<name>.secret: true`" in SKILL_MD
    for needle in (
        "load-time validation error",
        "never persisted",
        "`<secret>`",
        "`(secret)`",
        "RAYSPEC_INPUT_<NAME>",
        "exit 2 listing the missing secret inputs",
    ):
        assert needle in PARAGRAPH, needle


def test_snippet_validates_and_plan_validate_mark_the_input_secret(tmp_path: Path) -> None:
    root, env = _project(tmp_path, {"sec": secret_snippet()})
    runner = CliRunner()
    res = runner.invoke(app, ["validate", "--root", str(root)], env=env)
    assert res.exit_code == 0, res.output
    assert "token" in res.output and re.search(r"\([^)]*secret[^)]*\)", res.output), res.output
    res = runner.invoke(app, ["plan", "sec", "--root", str(root), "-i", "token=hunter2"], env=env)
    assert res.exit_code == 0, res.output
    assert "hunter2" not in res.output
    assert "<secret>" in res.output or re.search(r"token.*\([^)]*secret[^)]*\)", res.output), (
        res.output
    )
    res = runner.invoke(
        app, ["plan", "sec", "--root", str(root), "-i", "token=hunter2", "--json"], env=env
    )
    assert res.exit_code == 0, res.output
    assert "hunter2" not in res.stdout


@pytest.mark.parametrize(
    ("field", "patch"),
    [
        ("prompt", '  - id: ask\n    prompt: "Use {{ inputs.token }}"\n'),
        ("when", "  - id: gated\n    when: inputs.token == 'x'\n    shell: echo hi\n"),
        ("outputs", 'outputs:\n  leak: "{{ inputs.token }}"\n'),
        ("approve", '  - id: gate\n    approve: "Ship {{ inputs.token }}?"\n'),
    ],
)
def test_secret_outside_shell_python_env_is_a_load_time_error(
    tmp_path: Path, field: str, patch: str
) -> None:
    """Claim (c): `{{ inputs.token }}` anywhere but a shell/python step's env is refused at
    validate time, naming the rule (RAYSPEC_INPUT_<NAME>)."""
    text = secret_snippet()
    text = text + patch if field == "outputs" else text.rstrip("\n") + "\n" + patch
    root, env = _project(tmp_path, {"sec": text})
    res = CliRunner().invoke(app, ["validate", "--root", str(root)], env=env)
    assert res.exit_code == 2, (field, res.output)
    assert "RAYSPEC_INPUT_TOKEN" in res.output, (field, res.output)
    assert "token" in res.output


def test_secret_with_default_is_a_load_time_error(tmp_path: Path) -> None:
    text = secret_snippet().replace(
        "token: { type: string, secret: true }", "token: { type: string, secret: true, default: x }"
    )
    assert "default: x" in text
    root, env = _project(tmp_path, {"sec": text})
    res = CliRunner().invoke(app, ["validate", "--root", str(root)], env=env)
    assert res.exit_code == 2, res.output
    assert "default" in res.output and "secret" in res.output


def test_secret_reaches_the_shell_step_as_env_and_is_not_persisted(tmp_path: Path) -> None:
    """Claims (a)+(b): the value arrives as RAYSPEC_INPUT_TOKEN in the shell step and run.json /
    show print `<secret>`; the plain `--input` works on `run` and env fallback too."""
    wf = secret_snippet().replace(
        'curl -H "Authorization: Bearer $RAYSPEC_INPUT_TOKEN" https://example/api',
        'echo "len=${#RAYSPEC_INPUT_TOKEN}"',
    )
    assert "len=" in wf
    root, env = _project(tmp_path, {"sec": wf})
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "run",
            "sec",
            "--root",
            str(root),
            "--dry-run",
            "--exec-shell",
            "--no-worktree",
            "-i",
            "token=hunter2",
            "--json",
        ],
        env=env,
    )
    assert res.exit_code == 0, res.output
    summary = json.loads(res.stdout.splitlines()[-1])
    assert summary["status"] == "succeeded"
    assert "hunter2" not in json.dumps(summary.get("inputs", {}))
    res = runner.invoke(app, ["show", summary["run_id"], "--json"], env=env)
    assert res.exit_code == 0, res.output
    shown = json.loads(res.stdout)
    assert shown["inputs"]["token"] == "<secret>"
    assert "hunter2" not in res.stdout
    step = next(s for s in shown["steps"] if s["path"] == "publish")
    assert step["status"] == "succeeded"
    assert "len=7" in (step.get("output_preview") or "")
    run_dir = Path(shown["run_dir"])
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert "hunter2" not in path.read_bytes().decode("utf-8", "replace"), path
    # env fallback: RAYSPEC_INPUT_TOKEN instead of --input
    res = runner.invoke(
        app,
        ["run", "sec", "--root", str(root), "--dry-run", "--exec-shell", "--no-worktree", "--json"],
        env={**env, "RAYSPEC_INPUT_TOKEN": "viaenv"},
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout.splitlines()[-1])["status"] == "succeeded"


def test_resume_requires_the_secret_again(tmp_path: Path) -> None:
    """Claim (d): a run that needs resuming fails with exit 2 listing the missing secret input
    unless `--input token=…` (allowed on resume for secrets only) or the env var is given."""
    wf = (
        "rayspec: 1\nname: sec_resume\n"
        "inputs:\n  token: { type: string, secret: true }\n"
        "steps:\n"
        '  - id: gate\n    approve: "go?"\n'
        "  - id: publish\n    needs: [gate]\n    shell: |\n      printf '%s' \"$RAYSPEC_INPUT_TOKEN\"\n"
    )
    root, env = _project(tmp_path, {"sec_resume": wf})
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "run",
            "sec_resume",
            "--root",
            str(root),
            "--no-worktree",
            "--no-interactive",
            "-i",
            "token=hunter2",
            "--json",
        ],
        env=env,
    )
    assert res.exit_code == 3, res.output  # a real (non-dry) run: the gate pauses
    run_id = json.loads(res.stdout.splitlines()[-1])["run_id"]
    res = runner.invoke(app, ["approve", run_id, "--json"], env=env)
    assert res.exit_code == 2, res.output
    assert "token" in res.output
    res = runner.invoke(app, ["approve", run_id, "--json", "-i", "token=hunter2"], env=env)
    assert res.exit_code == 0, res.output
    data = yaml.safe_load((root / ".rayspec" / "workflows" / "sec_resume.yaml").read_text())
    assert data["inputs"]["token"]["secret"] is True

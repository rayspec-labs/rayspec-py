# SPDX-License-Identifier: Apache-2.0
"""``expect:`` blocks inside a real run: stray keys and tolerated mismatches.

An assertion that never fires is worse than no assertion: these tests pin that a ``steps:`` key
carrying an ``expect:`` block which no step of the workflow can ever resolve is refused before
the run starts, and that the one way a mismatch *can* still be swallowed — the step's own
``allow_failure``/``each.on_failure`` — is a documented, deliberate choice rather than an
accident.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rayspec.cli.app import app

TWO_ASKS = """
rayspec: 1
name: asks
isolation: none
agents:
  bot: {provider: stub}
steps:
  - id: ask
    agent: bot
    prompt: "how long?"
  - id: ask2
    needs: [ask]
    agent: bot
    prompt: "how wide?"
outputs:
  a: "{{ steps.ask.output }}"
"""


def _write(project: Path, name: str, text: str) -> None:
    (project / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text, encoding="utf-8")


def _run(cli: CliRunner, project: Path, stubs: Path, *extra: str):
    return cli.invoke(
        app,
        [
            "run",
            "asks",
            "--root",
            str(project),
            "--dry-run",
            "--json",
            "--stubs",
            str(stubs),
            *extra,
        ],
    )


def test_an_expect_key_no_step_resolves_is_refused(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """A typo'd (or renamed-away) key carrying `expect:` would assert nothing, silently."""
    _write(project, "asks", TWO_ASKS)
    stubs = project / "stubs.yaml"
    stubs.write_text(
        "steps:\n"
        '  ask: {text: "5m"}\n'
        '  asq2: {text: "wide", expect: {prompt_contains: "impossible"}}\n',
        encoding="utf-8",
    )
    result = _run(cli, project, stubs)
    assert result.exit_code == 2, result.output
    assert "asq2" in result.output
    assert "ask2" in result.output  # the hint names the paths that do exist


def test_a_glob_expect_key_that_matches_a_step_is_accepted(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """The check must not fire on a legitimate glob (or an indexed loop-body key)."""
    _write(project, "asks", TWO_ASKS)
    stubs = project / "stubs.yaml"
    stubs.write_text(
        'steps:\n  "ask*": {text: "5m", expect: {prompt_contains: "how"}}\n', encoding="utf-8"
    )
    result = _run(cli, project, stubs)
    assert result.exit_code == 0, result.output


def test_an_expect_key_without_expect_block_is_not_refused(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """Only assertions are refused: a stale answer key stays a no-op (scripts are shared)."""
    _write(project, "asks", TWO_ASKS)
    stubs = project / "stubs.yaml"
    stubs.write_text('steps:\n  ask: {text: "5m"}\n  gone: {text: "stale"}\n', encoding="utf-8")
    result = _run(cli, project, stubs)
    assert result.exit_code == 0, result.output


def test_allow_failure_still_tolerates_an_expectation_mismatch(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """PINNED: `expect:` outranks everything the SCRIPT can do, but it is still an ordinary
    step failure, so the step's own `allow_failure` tolerates it. Documented in providers.md."""
    tolerant = TWO_ASKS.replace(
        "  - id: ask\n    agent: bot\n", "  - id: ask\n    allow_failure: true\n    agent: bot\n"
    ).replace('  a: "{{ steps.ask.output }}"', '  a: "{{ steps.ask2.output }}"')
    _write(project, "asks", tolerant)
    stubs = project / "stubs.yaml"
    stubs.write_text(
        'steps:\n  ask: {text: "5m", expect: {prompt_contains: "NOT IN THE PROMPT"}}\n'
        '  ask2: {text: "wide"}\n',
        encoding="utf-8",
    )
    result = _run(cli, project, stubs)
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["status"] == "succeeded"
    (run_json,) = home.rglob(f"runs/{summary['run_id']}/run.json")
    record = json.loads(run_json.read_text(encoding="utf-8"))
    step = record["steps"]["ask"]
    assert step["status"] == "failed" and step["tolerated"] is True
    assert step["error"]["type"] == "stub_expectation"

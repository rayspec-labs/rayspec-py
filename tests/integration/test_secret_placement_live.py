"""The live verification behind "``env:`` on a ``prompt:`` step stays refused".

Opt in with ``RAYSPEC_LIVE=1`` (needs a logged-in ``claude`` and ``codex``); deselected by
default (``-m 'not live'``). The experiment feeds a unique probe value through a **prompt**
step's ``env:`` — which the schema allows for a literal — and then greps for it.

Result on 2026-08-21 (claude-agent-sdk 0.2.142 / bundled CLI 2.1.237, openai-codex 0.147.0):

* both adapters DELIVER the variable to the child (the agents reported its length, 27);
* nothing under ``$RAYSPEC_HOME`` contains the value for either adapter — rayspec's own
  writers (``run.json``, ``events.jsonl``, ``stream.jsonl``, outputs) are clean;
* the Codex CLI writes ``~/.codex/shell_snapshots/<id>.<ts>.sh``, mode ``0644``, containing a
  literal ``export PROBE_TOKEN=<value>`` line. It outlives the run, sits outside
  ``$RAYSPEC_HOME`` and no rayspec redactor can reach it.

The third point is why the rule is NOT relaxed: a secret input still may not be named in a
prompt step's ``env:``. If a future Codex release stops snapshotting the environment,
:func:`test_codex_snapshots_the_child_environment_outside_the_run_store` starts failing — that
is the signal to revisit the decision, not a flake.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from ._helpers import invoke

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RAYSPEC_LIVE") != "1", reason="set RAYSPEC_LIVE=1 to run"),
]

PROBE = "rayspec-live-probe-QQZZ7788"
LEN_PROBE = str(len(PROBE))

WORKFLOW = """
rayspec: 1
name: probe
isolation: none
agents:
  c: {{ provider: claude, model: haiku, access: workspace-write }}
  x: {{ provider: codex, model: gpt-5.4, effort: low, access: workspace-write }}
steps:
  - id: ask
    agent: {agent}
    prompt: |
      Run the shell command
      `python3 -c "import os;print(len(os.environ.get('PROBE_TOKEN','')))"`
      and reply with ONLY the number it prints. Never print the value itself.
    env: {{ PROBE_TOKEN: "{probe}" }}
"""


def _project(tmp_path: Path, agent: str) -> Path:
    root = tmp_path / f"probe-{agent}"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "probe.yaml").write_text(
        textwrap.dedent(WORKFLOW).format(agent=agent, probe=PROBE)
    )
    return root


def _grep(root: Path, needle: str) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and needle in p.read_text(errors="replace")
    )


@pytest.mark.parametrize("agent", ["c", "x"])
def test_the_adapter_delivers_prompt_step_env_but_never_records_it(
    tmp_path: Path, home: Path, agent: str
) -> None:
    project = _project(tmp_path, agent)
    res = invoke(["run", "probe", "--root", str(project), "--yes", "--quiet"], home)
    assert res.exit_code == 0, res.output
    output = next(home.rglob("runs/*/steps/ask/output.txt")).read_text()
    assert LEN_PROBE in output, output  # the child really saw the variable
    assert _grep(home, PROBE) == []  # …and rayspec recorded none of it


def test_codex_snapshots_the_child_environment_outside_the_run_store(
    tmp_path: Path, home: Path
) -> None:
    """The finding that keeps the rule in place — see the module docstring."""
    snapshots = Path.home() / ".codex" / "shell_snapshots"
    before = set(snapshots.glob("*.sh")) if snapshots.is_dir() else set()
    project = _project(tmp_path, "x")
    assert invoke(["run", "probe", "--root", str(project), "--yes", "--quiet"], home).exit_code == 0
    if not snapshots.is_dir():
        pytest.skip("this codex build writes no shell snapshots")
    new = [p for p in snapshots.glob("*.sh") if p not in before]
    leaked = [p for p in new if PROBE in p.read_text(errors="replace")]
    assert leaked, (
        "codex no longer snapshots the child environment — the decision "
        "(env: on prompt steps stays refused for secret inputs) can be revisited"
    )
    assert all(p.stat().st_mode & 0o077 for p in leaked), "…and it is world/group readable"

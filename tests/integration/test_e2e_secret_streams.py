"""A run with a secret must persist its streams **completely**.

The leak tests assert that a secret is *absent* — an implementation that silently drops the tail
of every stream satisfies them trivially. These tests assert the other half: with a redactor
installed, what ``stream.jsonl`` reassembles to is byte-for-byte what ``stdout.log`` holds, and
``rayspec logs`` still ends on the step's real last line — for a step that succeeds, a step that
fails, and a run that pauses mid-stream.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ._helpers import invoke, jsonl

CONFIG_SECRET = "cfg-BIGSECRETVALUE-0123456789"
LAST_LINE = "the-real-last-line"

WORKFLOW = """
rayspec: 1
name: streams
isolation: none
steps:
  - id: big
    shell: |
      python3 -c "
      import sys
      for i in range(4000):
          sys.stdout.write('x' * 70 + ' line %d\\n' % i)
      sys.stdout.write('__LAST__\\n')
      "
    allow_failure: true
  - id: after
    needs: [big]
    shell: echo done
""".replace("__LAST__", LAST_LINE)

FAILING = """
rayspec: 1
name: boom
isolation: none
steps:
  - id: big
    shell: |
      python3 -c "
      import sys
      for i in range(2000):
          sys.stdout.write('y' * 70 + ' line %d\\n' % i)
      sys.stdout.write('__LAST__\\n')
      sys.exit(7)
      "
    allow_failure: true
""".replace("__LAST__", LAST_LINE)

PARTIAL = """
rayspec: 1
name: partial
isolation: none
steps:
  - id: tail
    shell: |
      echo "a normal line"
      printf 'cfg-BIG'
"""

PAUSING = """
rayspec: 1
name: pausing
isolation: none
steps:
  - id: big
    shell: |
      python3 -c "
      import sys
      for i in range(2000):
          sys.stdout.write('z' * 70 + ' line %d\\n' % i)
      sys.stdout.write('__LAST__\\n')
      "
  - id: gate
    needs: [big]
    approve: "ship?"
""".replace("__LAST__", LAST_LINE)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    for name, text in (
        ("streams", WORKFLOW),
        ("boom", FAILING),
        ("pausing", PAUSING),
        ("partial", PARTIAL),
    ):
        (root / ".rayspec" / "workflows" / f"{name}.yaml").write_text(textwrap.dedent(text))
    (root / ".rayspec" / "config.yaml").write_text(
        "secrets:\n  DEPLOY_KEY: {env: DEPLOY_KEY_SOURCE}\n"
    )
    return root


def _stream_text(step_dir: Path, kind: str = "stdout") -> str:
    records = jsonl((step_dir / "stream.jsonl").read_text())
    return "".join(r.get("text") or "" for r in records if r["kind"] == kind)


def _run(project: Path, home: Path, name: str, *extra: str) -> None:
    invoke(
        ["run", name, "--root", str(project), "--yes", *extra],
        home,
        DEPLOY_KEY_SOURCE=CONFIG_SECRET,
    )


def test_stream_jsonl_reassembles_to_stdout_log(project: Path, home: Path) -> None:
    _run(project, home, "streams")
    (run_dir,) = list(home.rglob("runs/*/"))
    step = run_dir / "steps" / "big"
    log = (step / "stdout.log").read_text()
    assert log.endswith(f"{LAST_LINE}\n")
    assert _stream_text(step) == log


def test_logs_ends_on_the_steps_real_last_line(project: Path, home: Path) -> None:
    _run(project, home, "streams")
    (run_dir,) = list(home.rglob("runs/*/"))
    res = invoke(["logs", run_dir.name, "--step", "big", "--root", str(project)], home)
    assert LAST_LINE in res.output, res.output[-400:]


def test_a_failing_steps_stream_is_complete(project: Path, home: Path) -> None:
    _run(project, home, "boom")
    (run_dir,) = list(home.rglob("runs/*/"))
    step = run_dir / "steps" / "big"
    log = (step / "stdout.log").read_text()
    assert log.endswith(f"{LAST_LINE}\n")
    assert _stream_text(step) == log


def test_a_paused_runs_stream_is_complete(project: Path, home: Path) -> None:
    invoke(["run", "pausing", "--root", str(project)], home, DEPLOY_KEY_SOURCE=CONFIG_SECRET)
    (run_dir,) = list(home.rglob("runs/*/"))
    step = run_dir / "steps" / "big"
    log = (step / "stdout.log").read_text()
    assert log.endswith(f"{LAST_LINE}\n")
    assert _stream_text(step) == log


def test_a_stream_ending_on_a_partial_secret_prefix_is_not_swallowed(
    project: Path, home: Path
) -> None:
    """The boundary buffer must be flushed when the step ends — a step whose last bytes happen
    to be a prefix of a secret must not lose them."""
    _run(project, home, "partial")
    (run_dir,) = list(home.rglob("runs/*/"))
    step = run_dir / "steps" / "tail"
    log = (step / "stdout.log").read_text()
    assert log.endswith("cfg-BIG")
    assert _stream_text(step) == log

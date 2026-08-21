"""No shape of secret value survives into the run directory.

The store used to redact ``run.json`` as serialised TEXT, which is fine for a value that is a
plain JSON string and destroys the file for anything else — a numeric secret rewrote
``"budget": 4242`` to an unquoted marker and the checkpoint stopped parsing. This module runs
a real workflow once per value shape and looks at every byte the run left behind: the value
must be gone, and everything the run wrote must still parse.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from rayspec.config import Config
from rayspec.engine.runner import Runner
from rayspec.loader import load_workflow
from rayspec.store.file import FileRunStore

#: One entry per shape the redactor has to survive. Each is fed in as a ``secret: true`` input,
#: echoed by a shell step (stdout, stderr, the step output and the stream transcript), and
#: re-emitted by a python step both on its own and wrapped inside a longer string — so a secret
#: that is a substring of another value has to be found there too.
VALUES: dict[str, str] = {
    "numeric": "4242424242",
    "quoted": 'he said "hi" and \\ then',
    "multiline": "line one\nline two\r\nline three",
    "unicode": "pässwörd-ﬁ-🔐-Ω",
    "json_document": '{"k": [1, "v"], "n": 5}',
    "regex_metacharacters": "a.*b+c?(d)|e[f]^$",
    "tabbed": "tok\tsep\tvalue",
    "leading_zeroes": "0000123456",
}

WORKFLOW = """
rayspec: 1
name: spill
isolation: none
inputs:
  token: { type: string, secret: true, required: true }
steps:
  - id: echo
    shell: |
      printf '%s' "$RAYSPEC_INPUT_TOKEN"
      printf 'stderr: %s\\n' "$T" >&2
    env: { T: "{{ inputs.token }}" }
  - id: shape
    needs: [echo]
    python: |
      import json, os
      raw = os.environ["RAYSPEC_INPUT_TOKEN"]
      print(json.dumps({"value": raw, "wrapped": "id-" + raw + "-x"}))
    output_schema: { type: object }
outputs:
  shape: "{{ steps.shape.output }}"
  echoed: "{{ steps.echo.output }}"
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "spill.yaml").write_text(textwrap.dedent(WORKFLOW))
    return root


def _files_containing(root: Path, needle: bytes) -> list[str]:
    """Every file under ``root`` whose raw bytes contain ``needle`` (no newline translation)."""
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and needle in path.read_bytes()
    )


@pytest.mark.parametrize("shape", sorted(VALUES))
def test_no_shape_of_secret_reaches_the_run_directory(
    tmp_path: Path, project: Path, shape: str
) -> None:
    value = VALUES[shape]
    home = tmp_path / "home"
    home.mkdir()
    store = FileRunStore(tmp_path / "store")
    result = Runner(
        load_workflow("spill", project_root=project, home=home, config=Config()),
        inputs={"token": value},
        store=store,
        project_root=project,
    ).run_sync()
    assert result.exit_code == 0, result.reason

    run_dir = store.run_dir(result.run_id)
    assert _files_containing(run_dir, value.encode("utf-8")) == []
    # the step really did echo it — otherwise the test would pass by doing nothing
    assert b"[REDACTED:token]" in (run_dir / "steps" / "echo" / "output.txt").read_bytes()

    # and everything the run wrote is still readable
    record = store.load(result.run_id)
    assert record.outputs is not None
    assert json.loads((run_dir / "run.json").read_text())
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        json.loads(line)
    for stream in run_dir.rglob("stream.jsonl"):
        for line in stream.read_text().splitlines():
            json.loads(line)


@pytest.mark.parametrize("shape", sorted(VALUES))
def test_the_stored_json_output_keeps_its_shape(tmp_path: Path, project: Path, shape: str) -> None:
    """The step output is a JSON document; redaction must leave it one, whatever the value was."""
    value = VALUES[shape]
    home = tmp_path / "home"
    home.mkdir()
    store = FileRunStore(tmp_path / "store")
    result = Runner(
        load_workflow("spill", project_root=project, home=home, config=Config()),
        inputs={"token": value},
        store=store,
        project_root=project,
    ).run_sync()
    assert result.exit_code == 0, result.reason
    assert json.loads(store.read_output(result.run_id, "steps/shape/output.json")) == {
        "value": "[REDACTED:token]",
        "wrapped": "id-[REDACTED:token]-x",
    }
    record = store.load(result.run_id)
    assert record.outputs is not None
    assert record.outputs["shape"]["value"] == "[REDACTED:token]"

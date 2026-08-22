"""One JSON rendering and one table style behind every `--json` / `--output json` command.

The point is not that JSON is pretty: it is that `rayspec workflows --json | jq` and
`rayspec runs --json | jq` behave the same way, whatever produced the stream. So the rules are
pinned here rather than in each command's own tests, and the two scans below fail when a new
command serialises or tabulates its own output instead of going through the shared helpers.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import _loader_common as common

SRC = Path(__file__).resolve().parents[2] / "src" / "rayspec" / "cli"

#: The one module that renders a JSON *document* itself: `rayspec schema` prints a published
#: JSON Schema, which must stay byte-identical to the checked-in `schemas/*.schema.json` and is
#: therefore not a presentation choice at all.
JSON_DUMPS_EXCEPTIONS = {"commands/schema.py"}


def _printing_dumps(path: Path) -> list[int]:
    """Lines of ``path`` where a ``json.dumps(...)`` call is printed straight to stdout."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"print", "echo"}:
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "dumps"
            ):
                found.append(node.lineno)
    return found


def test_no_command_serialises_its_own_json_output() -> None:
    offenders = {
        path.relative_to(SRC).as_posix(): lines
        for path in sorted(SRC.rglob("*.py"))
        if (lines := _printing_dumps(path))
    }
    unexpected = {k: v for k, v in offenders.items() if k not in JSON_DUMPS_EXCEPTIONS}
    assert not unexpected, (
        f"print json_text(...)/print_json(...) instead of json.dumps: {unexpected}"
    )


def test_json_text_is_compact_when_stdout_is_redirected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, "stdout_is_tty", lambda: False)
    assert common.json_text({"b": [1, 2], "a": "ä"}) == '{"b":[1,2],"a":"ä"}'


def test_json_text_is_indented_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    assert common.json_text({"a": 1}) == '{\n  "a": 1\n}'


def test_json_line_stays_compact_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A line-delimited stream is one object per line — on a terminal too, or `rayspec run
    --json | tail -1 | jq` would read a fragment."""
    monkeypatch.setattr(common, "stdout_is_tty", lambda: True)
    assert common.json_line({"a": 1, "b": None}) == '{"a":1,"b":null}'


@pytest.fixture
def project(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    res = CliRunner().invoke(app, ["init", "--root", str(root), "--no-skill"])
    assert res.exit_code == 0, res.output
    return root


#: One representative `--json` invocation per command that renders a document in a fresh project.
DOCUMENTS = [
    ("workflows", []),
    ("agents", []),
    ("validate", []),
    ("plan", ["example"]),
    ("providers", []),
    ("projects list", []),
    ("worktrees list", []),
    ("skill show", []),
    ("runs", []),
    ("plugins", []),
    ("doctor", []),
    ("costs", []),
]


def _flags(command: str) -> set[str]:
    root = get_command(app)
    cmd: Any = root
    for part in command.split():
        cmd = cmd.commands[part]
    return {opt for param in cmd.params for opt in getattr(param, "opts", [])}


@pytest.mark.parametrize(("command", "extra"), DOCUMENTS, ids=lambda case: str(case))
def test_json_documents_share_one_rendering(
    command: str, extra: list[str], project: Path, home: Path
) -> None:
    argv = [*command.split(), *extra, "--json"]
    if "--root" in _flags(command):
        argv += ["--root", str(project)]
    res = CliRunner().invoke(app, argv)
    assert res.stdout, res.output
    payload = json.loads(res.stdout)
    assert res.stdout == common.json_text(payload) + "\n"

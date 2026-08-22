"""One JSON rendering and one table style behind every `--json` / `--output json` command.

The point is not that JSON is pretty: it is that `rayspec workflows --json | jq` and
`rayspec runs --json | jq` behave the same way, whatever produced the stream. So the rules are
pinned here rather than in each command's own tests, and the two scans below fail when a new
command serialises or tabulates its own output instead of going through the shared helpers.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import _loader_common as common

SRC = Path(__file__).resolve().parents[2] / "src" / "rayspec" / "cli"

#: The one module that renders a JSON *document* itself: `_loader_common` is the house renderer
#: every other command goes through. `rayspec schema` used to be the second, and no longer is —
#: it prints `schemagen.schema_text`, the same text the file holds, rather than re-serialising the
#: parsed document. Re-serialising was not a presentation choice at all: `json.dumps` defaults to
#: `ensure_ascii=True`, so printing escaped every character the file wrote literally and the two
#: disagreed on any description holding an em dash.
JSON_DUMPS_EXCEPTIONS = {"commands/_loader_common.py"}

#: Every call that turns a payload into JSON text. The house renderers (`json_text`,
#: `json_line`, `print_json`) are deliberately not among them: going through those is the point.
SERIALISER_NAMES = {"dumps", "model_dump_json"}

#: Calls that put a string on stdout. `write`/`writelines` count only when the receiver names
#: stdout — `rayspec runs stubs` legitimately writes a generated script to a file handle.
PRINTER_NAMES = {"print", "echo", "secho"}


def _dotted(node: ast.expr) -> str:
    """``rich.table.Table`` for a name/attribute chain, ``""`` for anything else.

    A base the scan cannot name becomes ``?``, so ``console().print(...)`` reads as ``?.print``
    and still counts: the receiver is not what makes a call a print.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not parts and not isinstance(node, ast.Name):
        return ""
    parts.append(node.id if isinstance(node, ast.Name) else "?")
    return ".".join(reversed(parts))


def _last(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _json_aliases(tree: ast.Module) -> set[str]:
    """Local names bound by ``from json import dumps [as x]`` — `json.` is not the only spelling."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "json"
        for alias in node.names
        if alias.name in SERIALISER_NAMES
    }


def _is_serialiser(node: ast.expr | None, names: set[str]) -> bool:
    """Whether ``node`` is a call that produces a JSON document as text."""
    if not isinstance(node, ast.Call):
        return False
    dotted = _dotted(node.func)
    return bool(dotted) and (_last(dotted) in SERIALISER_NAMES or dotted in names)


def _is_printer(call: ast.Call) -> bool:
    dotted = _dotted(call.func)
    if not dotted:
        return False
    last = _last(dotted)
    return last in PRINTER_NAMES or (last in {"write", "writelines"} and "stdout" in dotted)


def _serialising_names(tree: ast.Module) -> set[str]:
    """Module-local names that hold, or return, a serialised document.

    Grown to a fixed point so a chain of one-line helpers is not a way around the scan.
    """
    names = _json_aliases(tree)
    while True:
        grown = set(names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_serialiser(node.value, grown):
                grown |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and _is_serialiser(node.value, grown):
                if isinstance(node.target, ast.Name):
                    grown.add(node.target.id)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                isinstance(child, ast.Return)
                and (
                    _is_serialiser(child.value, grown)
                    or (isinstance(child.value, ast.Name) and child.value.id in grown)
                )
                for child in ast.walk(node)
            ):
                grown.add(node.name)
        if grown == names:
            return names
        names = grown


def _printing_dumps(path: Path) -> list[int]:
    """Lines of ``path`` that print a self-serialised JSON document on stdout.

    Three spellings, because the assign-then-print one is what several of the call sites this
    scan replaced actually looked like: the serialiser inside the print (``print(json.dumps(x))``,
    ``sys.stdout.write(json.dumps(x))``), a local bound to one and printed bare (``text =
    json.dumps(x)`` … ``typer.echo(text)``), and a helper that returns one (``def _doc(x): return
    json.dumps(x)`` … ``out.print(_doc(x))``). ``from json import dumps`` and any alias of it
    count, and so does pydantic's ``model_dump_json``.

    A serialised *value* embedded in a line (``out.print(f"usage {json.dumps(u)}")``) renders one
    field of a human view rather than a document, and is left alone.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = _serialising_names(tree)
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_printer(node):
            continue
        for arg in node.args:
            if (
                _is_serialiser(arg, names)
                or (isinstance(arg, ast.Name) and arg.id in names)
                or (isinstance(arg, ast.Call) and _dotted(arg.func) in names)
            ):
                found.append(node.lineno)
    return sorted(set(found))


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
    assert set(offenders) >= JSON_DUMPS_EXCEPTIONS, (
        "the scan no longer sees the modules it is meant to see — it has stopped working"
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


#: A workflow description that only a UTF-8 stdout can write as itself.
NON_ASCII_DESCRIPTION = "Prüfung fürs Änderungsprotokoll"


def _cli(args: list[str], *, home: Path, encoding: str) -> subprocess.CompletedProcess[str]:
    """Run the real CLI in a child process whose stdout can only encode ``encoding``."""
    env = {
        **os.environ,
        "RAYSPEC_HOME": str(home),
        "PYTHONIOENCODING": encoding,
        "NO_COLOR": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "rayspec.cli.app", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_json_prints_on_a_stdout_that_cannot_encode_it(project: Path, home: Path) -> None:
    """A stdout that cannot encode the payload gets ``\\uXXXX`` escapes, not a traceback.

    ``PYTHONIOENCODING``, a C/POSIX locale and the legacy Windows code pages all produce a
    stdout that is not UTF-8. Writing ``ä`` into one raises ``UnicodeEncodeError`` from inside
    the write, which would take the command down after it has already done its work — so the
    renderer asks stdout what it can encode and falls back to escapes when the answer is no.
    """
    workflow = project / ".rayspec" / "workflows" / "example.yaml"
    lines = workflow.read_text(encoding="utf-8").splitlines()
    described = [
        f"description: {NON_ASCII_DESCRIPTION}" if ln.startswith("description:") else ln
        for ln in lines
    ]
    assert described != lines, "the scaffolded workflow no longer carries a description"
    workflow.write_text("\n".join(described) + "\n", encoding="utf-8")
    argv = ["workflows", "--json", "--root", str(project)]

    ascii_ = _cli(argv, home=home, encoding="ascii")
    assert ascii_.returncode == 0, ascii_.stderr
    assert ascii_.stdout.isascii(), ascii_.stdout
    descriptions = [w["description"] for w in json.loads(ascii_.stdout)]
    assert NON_ASCII_DESCRIPTION in descriptions

    utf8 = _cli(argv, home=home, encoding="utf-8")
    assert utf8.returncode == 0, utf8.stderr
    assert NON_ASCII_DESCRIPTION in utf8.stdout, "a UTF-8 stdout still gets the characters"
    assert json.loads(utf8.stdout) == json.loads(ascii_.stdout), "same document either way"


def _table_names(tree: ast.Module) -> set[str]:
    """``Table`` plus every local name bound to it (``from rich.table import Table as T``)."""
    names = {"Table"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rich"):
            names |= {alias.asname or alias.name for alias in node.names if alias.name == "Table"}
    return names


def _table_calls(path: Path) -> list[int]:
    """Lines of ``path`` that build a ``rich.table.Table`` — under any of its spellings.

    The constructor by any dotted path (``Table(...)``, ``rich.table.Table(...)``) or alias, the
    ``Table.grid(...)`` shortcut, and a subclass. ``PriceTable(...)`` is a different class and is
    not caught: the last segment of the dotted name has to *be* ``Table``, not end with it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = _table_names(tree)
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if any(_last(_dotted(base)) in names for base in node.bases):
                found.append(node.lineno)
            continue
        if not isinstance(node, ast.Call):
            continue
        parts = _dotted(node.func).split(".")
        if parts[-1] in names or (len(parts) > 1 and parts[-2] in names):
            found.append(node.lineno)
    return sorted(set(found))


#: The one module that constructs a ``Table``: it is the factory every listing goes through.
TABLE_EXCEPTIONS = {"commands/_loader_common.py"}


def test_no_command_builds_its_own_table() -> None:
    """Six variations of "a table" is five too many: a listing must not decide its own box."""
    offenders: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        lines = _table_calls(path)
        if lines and rel not in TABLE_EXCEPTIONS:
            offenders[rel] = lines
    assert not offenders, f"build tables with _loader_common.new_table(): {offenders}"
    assert all(_table_calls(SRC / rel) for rel in TABLE_EXCEPTIONS), (
        "the scan no longer sees the factory itself — it has stopped working"
    )


#: Box drawing, block elements and the shaded blocks Rich borders are made of.
BOX_DRAWING = {chr(cp) for cp in range(0x2500, 0x2580)}


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
    ("trust list", []),
]

#: `--json` commands the parametrisation does not reach: they need a run that already happened
#: (`show`, `logs`, `audit`, `eval`, `explain`, `lock`, `runs diff`), they start or steer one
#: (`run`, `resume`, `approve`, `reject`, `cancel`, `test`), or they change state
#: (`worktrees clean`, `trust check`). Each is covered by its own command's tests. Naming them
#: here is what makes the list above total: a *new* `--json` command has to appear in one list or
#: the other, and the one that renders a document in a fresh project belongs in the first.
NOT_A_FRESH_PROJECT_DOCUMENT = {
    "approve",
    "audit",
    "cancel",
    "eval",
    "explain",
    "lock",
    "logs",
    "reject",
    "resume",
    "run",
    "runs diff",
    "show",
    "test",
    "trust check",
    "worktrees clean",
}


def _json_commands() -> set[str]:
    """Every command (leaf or group) that takes ``--json``."""
    found: set[str] = set()

    def walk(group: Any, prefix: str) -> None:
        for name in sorted(group.commands):
            command = group.commands[name]
            full = f"{prefix}{name}"
            if "--json" in {opt for param in command.params for opt in param.opts}:
                found.add(full)
            if hasattr(command, "commands"):
                walk(command, f"{full} ")

    walk(get_command(app), "")
    return found


def test_every_json_command_is_listed() -> None:
    """The two lists above cover every ``--json`` command, so a new one cannot slip past both."""
    listed = {command for command, _ in DOCUMENTS} | NOT_A_FRESH_PROJECT_DOCUMENT
    assert _json_commands() == listed, (
        "add the command to DOCUMENTS if it renders a document in a fresh project, "
        f"else to NOT_A_FRESH_PROJECT_DOCUMENT: {sorted(_json_commands() ^ listed)}"
    )


def _flags(command: str) -> set[str]:
    root = get_command(app)
    cmd: Any = root
    for part in command.split():
        cmd = cmd.commands[part]
    return {opt for param in cmd.params for opt in getattr(param, "opts", [])}


@pytest.mark.parametrize(("command", "extra"), DOCUMENTS, ids=lambda case: str(case))
def test_json_documents_share_one_rendering(
    command: str, extra: list[str], project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One rendering, and a payload that is JSON without help.

    ``json_text`` passes ``default=str`` so a value the builder left unserialisable does not take
    the command down after it has already done its work — but that also turns a stray ``Path``
    into ``"PosixPath('/x')"`` where nobody notices. The stand-in below re-renders every payload
    without a ``default=``, so the suite still fails on one; and it counts the calls, so a command
    that prints its document some other way fails here rather than escaping the check.
    """
    render = common.json_text
    payloads: list[Any] = []

    def _strict(payload: Any) -> str:
        payloads.append(payload)
        json.dumps(payload, ensure_ascii=False)
        return render(payload)

    monkeypatch.setattr(common, "json_text", _strict)
    argv = [*command.split(), *extra, "--json"]
    if "--root" in _flags(command):
        argv += ["--root", str(project)]
    res = CliRunner().invoke(app, argv)
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    assert res.stdout, res.output
    assert len(payloads) == 1, f"{command} --json did not render through print_json: {payloads}"
    payload = json.loads(res.stdout)
    assert res.stdout == render(payload) + "\n"


#: `--json` commands that print no document at all when they cannot do their work outside a
#: project: they exit non-zero with a plain-text `error:` on stderr, so a `--json` consumer gets
#: an empty stdout and has to parse English to find out why. Not a regression — this is what they
#: have always done — but a recorded gap: `error_lines(..., json_mode=True)` already renders the
#: error document, and a command that adopts it should drop out of this set rather than be added
#: to it. Two today: `worktrees list` outside a git repository, and `plan <name>` for a workflow
#: that is not there.
NO_DOCUMENT_ON_ERROR = {"plan", "worktrees list"}


@pytest.mark.parametrize(("command", "extra"), DOCUMENTS, ids=lambda case: str(case))
def test_json_documents_outside_a_project(
    command: str, extra: list[str], tmp_path: Path, home: Path
) -> None:
    """The same rendering in a bare directory — or a recorded gap, never a third answer.

    The parametrisation above runs every command inside a project, which is the case that works.
    A directory that is neither a project nor a git repository is where a `--json` consumer finds
    out whether the contract holds when the command cannot answer.
    """
    bare = tmp_path / "bare"
    bare.mkdir()
    argv = [*command.split(), *extra, "--json"]
    if "--root" in _flags(command):
        argv += ["--root", str(bare)]
    res = CliRunner().invoke(app, argv)
    if command in NO_DOCUMENT_ON_ERROR:
        assert res.exit_code != 0 and not res.stdout, (
            f"{command} now prints a document — drop it from NO_DOCUMENT_ON_ERROR"
        )
        return
    assert res.stdout, f"{command} printed nothing: exit {res.exit_code}, {res.stderr!r}"
    payload = json.loads(res.stdout)
    assert res.stdout == common.json_text(payload) + "\n"


#: Commands whose default (table) rendering is a listing.
TABLES = [
    ("workflows", []),
    ("agents", []),
    ("providers", []),
    ("projects list", []),
    ("worktrees list", []),
    ("plugins", []),
    ("doctor", []),
    ("runs", []),
    ("trust list", []),
]


@pytest.mark.parametrize(("command", "extra"), TABLES, ids=lambda case: str(case))
def test_tables_are_plain_when_stdout_is_redirected(
    command: str, extra: list[str], project: Path, home: Path
) -> None:
    """A redirected listing is text a person diffs or greps, so it carries no borders — and the
    same borders for every command, which is none.

    Nor any trailing whitespace: with the right border gone, the padding Rich puts after the last
    cell has nothing to sit behind. It is what makes `git diff` complain, an editor rewrite the
    file on save and a pasted snippet look wrong — invisible noise on most lines of a file whose
    whole point is that it diffs cleanly against yesterday's.
    """
    argv = [*command.split(), *extra]
    if "--root" in _flags(command):
        argv += ["--root", str(project)]
    res = CliRunner().invoke(app, argv)
    drawn = sorted(BOX_DRAWING & set(res.stdout))
    assert not drawn, f"{command} drew {drawn} into a redirected listing"
    padded = [line for line in res.stdout.splitlines() if line != line.rstrip()]
    assert not padded, f"{command} left trailing whitespace on {len(padded)} line(s): {padded[:3]}"

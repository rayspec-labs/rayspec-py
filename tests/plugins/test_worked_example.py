"""The worked example in ``docs/extending.md`` is installed verbatim and has to work.

The page promises a reader can copy the package into a directory, install it and be done. This
test takes the fenced blocks of that section — the ``pyproject.toml`` stanza included, parsed for
its entry points — installs them as a distribution and drives the result through the CLI. If the
example drifts from the code, this fails.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from .conftest import InstallPlugin

DOCS = Path(__file__).resolve().parents[2] / "docs" / "extending.md"
_FENCE_RE = re.compile(r"```(?P<lang>[a-z]+)\n(?P<body>.*?)```", re.DOTALL)
_MODULE_RE = re.compile(r"^#\s*(?P<path>[\w/]+\.py)\b", re.MULTILINE)

WORKFLOW = """
rayspec: 1
name: demo
isolation: none
steps:
  - {id: hello, shell: echo hi}
"""


def _worked_example() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """``({module: source}, {group: {name: value}})`` from the worked-example section."""
    text = DOCS.read_text(encoding="utf-8")
    section = text.split("## A worked example", 1)[1].split("\n## ", 1)[0]
    modules: dict[str, str] = {}
    entry_points: dict[str, dict[str, str]] = {}
    for fence in _FENCE_RE.finditer(section):
        body = fence.group("body")
        if fence.group("lang") == "toml":
            data = tomllib.loads(body)
            entry_points = {
                group: dict(entries)
                for group, entries in data["project"]["entry-points"].items()
                if group.startswith("rayspec.")
            }
        elif fence.group("lang") == "python":
            match = _MODULE_RE.search(body)
            assert match, (
                f"a python block of the worked example has no `# path/to/module.py` line:\n{body}"
            )
            modules[match.group("path").removesuffix(".py").replace("/", ".")] = body
    assert modules and entry_points, "the worked example lost its code or its entry points"
    return modules, entry_points


def _project(tmp_path: Path, config: str) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "demo.yaml").write_text(WORKFLOW, encoding="utf-8")
    (root / ".rayspec" / "config.yaml").write_text(config, encoding="utf-8")
    return root


def _run(args: list[str]):
    from rayspec.cli.app import build_app

    return CliRunner().invoke(build_app(), args)


def test_the_documented_package_adds_a_command_and_a_sink(
    install_plugin: InstallPlugin, tmp_path: Path, home: Path
) -> None:
    modules, entry_points = _worked_example()
    install_plugin("acme-rayspec", modules=modules, entry_points=entry_points)
    log = tmp_path / "acme.log"
    project = _project(
        tmp_path,
        f"extensions:\n  sinks: [acme-log]\n  settings:\n    acme-log: {{path: {log}}}\n",
    )

    # the documented command exists and answers before anything has run
    listing = _run(["acme-runs", "--root", str(project), "--json"])
    assert listing.exit_code == 0, listing.output
    assert json.loads(listing.output) == []

    # the documented sink observes a real run, configured exactly as the page shows
    result = _run(["run", "demo", "--root", str(project)])
    assert result.exit_code == 0, result.output
    lines = log.read_text(encoding="utf-8").splitlines()
    assert any(line.endswith("run.started -") for line in lines)
    assert any("step.finished hello" in line for line in lines)

    # and the new run is what the documented command now lists
    listing = _run(["acme-runs", "--root", str(project), "--json"])
    assert [row["workflow"] for row in json.loads(listing.output)] == ["demo"]

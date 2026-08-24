"""The workflow snippets the docs ship (README quickstart, ``docs/*.md``) load, validate and run.

Every fenced ``yaml`` block (and every ``cat > .rayspec/workflows/<name>.yaml <<'EOF'`` heredoc
in a ``bash`` block) that is a complete workflow (``rayspec: 1`` + at least one step) is written
to a temporary project and pushed through ``load_workflow`` + ``validate_workflow`` exactly as
``rayspec validate`` does. The README quickstart is additionally driven end to end through the
CLI (``workflows`` / ``validate`` / ``plan`` / ``run --dry-run --stubs-init`` / ``--stubs``), and
the ``--json`` stream shape documented in ``docs/cli.md`` is pinned against the real output.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import _loader_common as common
from rayspec.loader import load_workflow, validate_workflow

from .conftest import DOCS_DIR, README

_FENCE_RE = re.compile(r"```(?P<lang>[a-z]*)\n(?P<body>.*?)```", re.DOTALL)
_HEREDOC_RE = re.compile(
    r"cat > \S+/workflows/(?P<name>[a-z_]+)\.yaml <<'EOF'\n(?P<body>.*?)\nEOF\n", re.DOTALL
)


@dataclass(frozen=True)
class Snippet:
    """One complete workflow document found in the docs."""

    source: str
    name: str
    text: str

    def __str__(self) -> str:  # pytest id
        return f"{self.source}:{self.name}"


def _is_workflow(body: str) -> bool:
    return "rayspec: 1" in body and "- id:" in body


def find_snippets() -> list[Snippet]:
    """Collect every complete workflow from ``README.md`` and ``docs/*.md``."""
    found: list[Snippet] = []
    for path in [README, *sorted(DOCS_DIR.glob("*.md"))]:
        text = path.read_text(encoding="utf-8")
        for match in _FENCE_RE.finditer(text):
            lang, body = match.group("lang"), match.group("body")
            if lang in {"yaml", "yml"} and _is_workflow(body):
                name = yaml.safe_load(body)["name"]
                found.append(Snippet(path.name, name, body))
            elif lang in {"bash", "sh", "shell", "console"}:
                for heredoc in _HEREDOC_RE.finditer(body):
                    if _is_workflow(heredoc.group("body")):
                        found.append(
                            Snippet(path.name, heredoc.group("name"), heredoc.group("body") + "\n")
                        )
    return found


SNIPPETS = find_snippets()


def _project(tmp_path: Path, snippets: list[Snippet]) -> tuple[Path, Path]:
    """A git project under ``tmp_path`` with the snippets as workflows; returns (root, home)."""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    home.mkdir()
    for snippet in snippets:
        (root / ".rayspec" / "workflows" / f"{snippet.name}.yaml").write_text(snippet.text)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root, home


def _invoke(args: list[str], home: Path):
    return CliRunner().invoke(app, args, env={"RAYSPEC_HOME": str(home)})


def test_docs_ship_the_expected_workflow_snippets() -> None:
    """The README quickstart (`review`), the README pitch (`fix_issue`) and examples.md exist."""
    ids = {str(s) for s in SNIPPETS}
    assert {"README.md:review", "README.md:fix_issue", "examples.md:review"} <= ids, ids


@pytest.mark.parametrize("snippet", SNIPPETS, ids=str)
def test_every_documented_workflow_loads_and_validates(snippet: Snippet, tmp_path: Path) -> None:
    root, home = _project(tmp_path, [snippet])
    caps = common.capability_source()
    rw = load_workflow(snippet.name, project_root=root, home=home)
    report = validate_workflow(
        rw,
        capabilities_for=caps.capabilities_for,
        template_checker=common.template_checker(),
        provider_ids=caps.provider_ids,
    )
    assert report.ok, f"{snippet}: {report.errors}"
    assert not rw.warnings and not report.warnings, f"{snippet}: {rw.warnings + report.warnings}"


def _readme_review() -> Snippet:
    [snippet] = [s for s in SNIPPETS if s.source == "README.md" and s.name == "review"]
    return snippet


def test_readme_quickstart_runs_end_to_end_with_the_cli(tmp_path: Path) -> None:
    """Steps 3 and 4 of the README quickstart, in order, against the real CLI (no login)."""
    root, home = _project(tmp_path, [_readme_review()])
    stubs = tmp_path / "stubs.yaml"

    res = _invoke(["workflows", "--root", str(root)], home)
    assert res.exit_code == 0 and "review" in res.stdout, res.output
    res = _invoke(["validate", "--root", str(root)], home)
    assert res.exit_code == 0 and "OK" in res.stdout, res.output
    res = _invoke(["plan", "review", "--root", str(root)], home)
    assert res.exit_code == 0 and "reviewer" in res.stdout, res.output
    res = _invoke(
        ["run", "review", "--root", str(root), "--dry-run", "--stubs-init", str(stubs)], home
    )
    assert res.exit_code == 0 and stubs.exists(), res.output
    res = _invoke(["run", "review", "--root", str(root), "--dry-run", "--stubs", str(stubs)], home)
    assert res.exit_code == 0, res.output
    assert "succeeded" in res.output
    # a dry run stays in place: no rayspec/* branch was created
    branches = subprocess.run(
        ["git", "branch", "--list", "rayspec/*"], cwd=root, capture_output=True, text=True
    ).stdout
    assert branches.strip() == ""


def test_readme_fix_issue_example_plans(tmp_path: Path) -> None:
    [snippet] = [s for s in SNIPPETS if s.source == "README.md" and s.name == "fix_issue"]
    root, home = _project(tmp_path, [snippet])
    res = _invoke(["plan", "fix_issue", "--root", str(root), "--input", "issue=123"], home)
    assert res.exit_code == 0, res.output
    assert "implement" in res.stdout and "codex" in res.stdout


def test_json_mode_stream_shapes_match_cli_md(tmp_path: Path) -> None:
    """``--json`` as documented in cli.md: JSONL events on stdout, then the summary object as the
    last stdout line; Rich console lines stay on stderr."""
    root, home = _project(tmp_path, [_readme_review()])
    stubs = tmp_path / "stubs.yaml"
    res = _invoke(
        ["run", "review", "--root", str(root), "--dry-run", "--stubs-init", str(stubs)], home
    )
    assert res.exit_code == 0, res.output
    res = _invoke(
        ["run", "review", "--root", str(root), "--dry-run", "--stubs", str(stubs), "--json"], home
    )
    assert res.exit_code == 0, res.output
    stdout_lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    events = [line for line in stdout_lines if "type" in line]
    assert {e["type"] for e in events} >= {"run.started", "step.finished", "run.finished"}
    for event in events:
        if event["type"] != "stream":
            assert set(event) == {"type", "run_id", "ts", "step_path", "data"}, event
            # ISO-8601 UTC with a literal ``Z`` and no offset form. The fraction is optional
            # because the serialiser drops it whole when a timestamp lands on an exact
            # microsecond (``…T03:04:05Z``) — a shape every clock value must be allowed to take,
            # and the same optional-fraction form the golden capture normaliser accepts.
            ts = event["ts"]
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", ts), event
    summary = stdout_lines[-1]
    assert "type" not in summary, "the summary object is the last stdout line"
    assert stdout_lines[-2]["type"] == "run.finished"
    assert all("type" in line for line in stdout_lines[:-1]), "events precede the summary"
    assert not any(line.lstrip().startswith("{") for line in res.stderr.splitlines())
    assert set(summary) == {
        "run_id",
        "status",
        "exit_code",
        "reason",
        "outputs",
        "usage",
        "cost_usd",
        "cost_source",
        "run_dir",
        "workspace",
        "pause",
    }, summary
    assert summary["exit_code"] == 0 and summary["status"] == "succeeded"
    assert summary["outputs"]["verdict"] == stdout_lines[-2]["data"]["outputs"]["verdict"]

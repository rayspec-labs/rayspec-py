"""`rayspec new workflow|agent <name>` — add one file to an existing project."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.new import agent_text, workflow_text


@pytest.fixture
def project(tmp_path: Path, home: Path) -> Path:
    """A scaffolded project in a git checkout (what `rayspec init` leaves behind)."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    res = CliRunner().invoke(app, ["init", "--root", str(root), "--no-skill"])
    assert res.exit_code == 0, res.output
    return root


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_new_workflow_adds_exactly_one_file_and_it_validates(project: Path, home: Path) -> None:
    before = _files(project)
    res = CliRunner().invoke(app, ["new", "workflow", "triage", "--root", str(project)])
    assert res.exit_code == 0, res.output
    added = _files(project) - before
    assert added == {".rayspec/workflows/triage.yaml"}, added
    assert "created" in res.output and ".rayspec/workflows/triage.yaml" in res.output
    data = yaml.safe_load((project / ".rayspec" / "workflows" / "triage.yaml").read_text())
    assert data["name"] == "triage"
    assert CliRunner().invoke(app, ["validate", "--root", str(project)]).exit_code == 0
    listing = CliRunner().invoke(app, ["workflows", "--root", str(project)])
    assert "triage" in listing.output


def test_new_workflow_dry_runs_without_credentials(
    project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        CliRunner().invoke(app, ["new", "workflow", "triage", "--root", str(project)]).exit_code
        == 0
    )
    monkeypatch.chdir(project)
    res = CliRunner().invoke(app, ["run", "triage", "--dry-run"])
    assert res.exit_code == 0, res.output


def test_new_workflow_refuses_to_clobber_without_force(project: Path, home: Path) -> None:
    assert (
        CliRunner().invoke(app, ["new", "workflow", "triage", "--root", str(project)]).exit_code
        == 0
    )
    target = project / ".rayspec" / "workflows" / "triage.yaml"
    target.write_text("mine\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["new", "workflow", "triage", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "Traceback" not in res.output
    assert "error:" in res.output and "already exists" in res.output and "--force" in res.output
    assert target.read_text(encoding="utf-8") == "mine\n"
    res = CliRunner().invoke(app, ["new", "workflow", "triage", "--root", str(project), "--force"])
    assert res.exit_code == 0, res.output
    assert "overwrote" in res.output
    assert target.read_text(encoding="utf-8") != "mine\n"


def test_new_workflow_can_use_a_named_agent(project: Path, home: Path) -> None:
    res = CliRunner().invoke(
        app, ["new", "workflow", "triage", "--agent", "reviewer", "--root", str(project)]
    )
    assert res.exit_code == 0, res.output
    data = yaml.safe_load((project / ".rayspec" / "workflows" / "triage.yaml").read_text())
    assert "agents" not in data
    assert data["steps"][0]["agent"] == "reviewer"
    assert CliRunner().invoke(app, ["validate", "--root", str(project)]).exit_code == 0


def test_new_workflow_description_is_yaml_safe(project: Path, home: Path) -> None:
    res = CliRunner().invoke(
        app,
        [
            "new",
            "workflow",
            "triage",
            "--description",
            'triage: "hard" cases',
            "--root",
            str(project),
        ],
    )
    assert res.exit_code == 0, res.output
    data = yaml.safe_load((project / ".rayspec" / "workflows" / "triage.yaml").read_text())
    assert data["description"] == 'triage: "hard" cases'
    assert CliRunner().invoke(app, ["validate", "--root", str(project)]).exit_code == 0


@pytest.mark.parametrize("name", ["Triage", "1st", "with-dash", "run", ""])
def test_new_workflow_rejects_a_name_the_loader_would_refuse(
    name: str, project: Path, home: Path
) -> None:
    res = CliRunner().invoke(app, ["new", "workflow", name, "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "Traceback" not in res.output
    assert "error:" in res.output
    assert (
        not list((project / ".rayspec" / "workflows").glob("*.yaml"))
        or not (project / ".rayspec" / "workflows" / f"{name}.yaml").exists()
    )


def test_new_outside_a_project_points_at_init(tmp_path: Path, home: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    res = CliRunner().invoke(app, ["new", "workflow", "triage", "--root", str(empty)])
    assert res.exit_code == 2, res.output
    assert "error:" in res.output and "rayspec init" in res.output
    assert not (empty / ".rayspec").exists()


def test_new_workflow_refuses_a_root_that_is_not_a_project(project: Path, home: Path) -> None:
    """An explicit --root names the project, it is not a place to start searching from.

    Walking up from it turns a typo'd path into a write into the enclosing project, reported as
    a relative path the user never named.
    """
    nested = project / "src" / "deeper"
    nested.mkdir(parents=True)
    res = CliRunner().invoke(app, ["new", "workflow", "sneaky", "--root", str(nested)])
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "Traceback" not in res.output
    assert "error:" in res.output and "rayspec init" in res.output
    assert not (project / ".rayspec" / "workflows" / "sneaky.yaml").exists()
    assert not (nested / ".rayspec").exists()


def test_new_workflow_refuses_an_agent_that_is_not_there(project: Path, home: Path) -> None:
    """--agent names an agent file. Writing a workflow that references a missing one exits 0 and
    hands the user a document whose very next printed step (`rayspec validate`) fails."""
    res = CliRunner().invoke(
        app, ["new", "workflow", "wf1", "--agent", "does_not_exist", "--root", str(project)]
    )
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "Traceback" not in res.output
    assert "does_not_exist" in res.output and "rayspec new agent" in res.output
    assert not (project / ".rayspec" / "workflows" / "wf1.yaml").exists()


def test_new_workflow_agent_near_miss_names_the_real_one(project: Path, home: Path) -> None:
    res = CliRunner().invoke(
        app, ["new", "workflow", "wf1", "--agent", "reviewr", "--root", str(project)]
    )
    assert res.exit_code == 2, res.output
    assert "did you mean 'reviewer'?" in res.output


def test_new_workflow_accepts_an_agent_from_the_user_scope(project: Path, home: Path) -> None:
    """`~/.rayspec/agents/<name>.yaml` resolves for every workflow of every project, so it is a
    valid --agent target too."""
    (home / "agents").mkdir(parents=True, exist_ok=True)
    (home / "agents" / "helper.yaml").write_text(agent_text("helper"), encoding="utf-8")
    res = CliRunner().invoke(
        app, ["new", "workflow", "wf1", "--agent", "helper", "--root", str(project)]
    )
    assert res.exit_code == 0, res.output
    assert CliRunner().invoke(app, ["validate", "--root", str(project)]).exit_code == 0


def test_new_agent_adds_one_file_a_workflow_can_reference(project: Path, home: Path) -> None:
    before = _files(project)
    res = CliRunner().invoke(app, ["new", "agent", "critic", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert _files(project) - before == {".rayspec/agents/critic.yaml"}
    res = CliRunner().invoke(
        app, ["new", "workflow", "triage", "--agent", "critic", "--root", str(project)]
    )
    assert res.exit_code == 0, res.output
    assert CliRunner().invoke(app, ["validate", "--root", str(project)]).exit_code == 0
    agents = CliRunner().invoke(app, ["agents", "--root", str(project)])
    assert "critic" in agents.output


def test_new_agent_refuses_to_clobber_without_force(project: Path, home: Path) -> None:
    assert (
        CliRunner().invoke(app, ["new", "agent", "critic", "--root", str(project)]).exit_code == 0
    )
    target = project / ".rayspec" / "agents" / "critic.yaml"
    target.write_text("mine\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["new", "agent", "critic", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "already exists" in res.output and "--force" in res.output
    assert target.read_text(encoding="utf-8") == "mine\n"


def test_new_without_a_subcommand_is_help(project: Path, home: Path) -> None:
    res = CliRunner().invoke(app, ["new"])
    assert res.exit_code == 2, res.output
    assert "workflow" in res.output and "agent" in res.output


def test_text_helpers_render_the_placeholders() -> None:
    """The Python surface is what the tests and the docs quote; it never leaves a placeholder."""
    text = workflow_text("triage", agent=None, description="")
    assert "__NAME__" not in text and "__AGENT__" not in text and "__DESCRIPTION__" not in text
    assert "name: triage" in text
    assert "__NAME__" not in agent_text("critic")


def test_new_workflow_defaults_to_the_walked_up_project_root(
    project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --root: `new` uses the project-command walk-up, so it works from a sub-directory."""
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    res = CliRunner().invoke(app, ["new", "workflow", "triage"])
    assert res.exit_code == 0, res.output
    assert (project / ".rayspec" / "workflows" / "triage.yaml").is_file()

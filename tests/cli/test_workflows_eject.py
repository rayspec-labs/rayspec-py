"""``rayspec workflows`` as an invokable group: provenance in the listing, the richer ``--json``
row, and ``rayspec workflows eject <name>`` — the copy that takes precedence over a bundled one."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import typer
import typer.core
from typer.testing import CliRunner

from rayspec import __version__
from rayspec.cli.app import app
from rayspec.loader import discover_workflows
from rayspec.loader.bundled import bundled_digest, parse_eject_header

WF = "rayspec: 1\nname: {name}\nsteps:\n  - {{id: a, shell: echo}}\n"


@pytest.fixture
def bundled_name(project: Path, home: Path) -> str:
    names = [r.name for r in discover_workflows(project, home=home) if r.scope == "bundled"]
    assert names, "the package ships no bundled workflow"
    return "pr_review" if "pr_review" in names else names[0]


def _rows(cli: CliRunner, project: Path) -> dict[str, dict]:
    res = cli.invoke(app, ["workflows", "--json", "--root", str(project)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert isinstance(data, list)  # still an array: `rayspec workflows --json | jq '.[]'`
    return {d["name"]: d for d in data}


# --- the group -------------------------------------------------------------------------------


def test_group_root_is_shared() -> None:
    from rayspec.cli.commands import _loader_common, runs

    assert runs.group_root is _loader_common.group_root


def test_workflows_is_an_invokable_group() -> None:
    group = typer.main.get_command(app).commands["workflows"]  # type: ignore[attr-defined]
    assert isinstance(group, typer.core.TyperGroup) and group.invoke_without_command
    assert "eject" in group.commands


def test_listing_flags_before_eject_are_refused(
    cli: CliRunner, project: Path, home: Path, bundled_name: str
) -> None:
    for flags in (["--json"], ["--output", "json"]):
        res = cli.invoke(app, ["workflows", *flags, "eject", bundled_name, "--root", str(project)])
        assert res.exit_code == 2, res.output
        assert flags[0] in res.output and "listing" in res.output
        assert "rayspec workflows eject" in res.output
    assert not (project / ".rayspec" / "workflows" / f"{bundled_name}.yaml").exists()


def test_root_before_eject_is_honoured(
    cli: CliRunner, project: Path, home: Path, bundled_name: str
) -> None:
    res = cli.invoke(app, ["workflows", "--root", str(project), "eject", bundled_name])
    assert res.exit_code == 0, res.output
    assert (project / ".rayspec" / "workflows" / f"{bundled_name}.yaml").is_file()


# --- the listing -----------------------------------------------------------------------------


def test_listing_shows_source_and_no_path_for_bundled_rows(
    cli: CliRunner, project: Path, home: Path
) -> None:
    (project / ".rayspec" / "workflows" / "mine.yaml").write_text(WF.format(name="mine"))
    res = cli.invoke(app, ["workflows", "--root", str(project)])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert "source" in lines[0] and "scope" not in lines[0]
    mine = next(line for line in lines if line.startswith("mine"))
    assert "project" in mine and ".rayspec/workflows/mine.yaml" in mine
    bundled = next(line for line in lines if line.startswith("pr_review"))
    assert "bundled" in bundled and "/" not in bundled.split("bundled", 1)[1]


def test_a_project_without_workflows_of_its_own_still_lists_the_library(
    cli: CliRunner, project: Path, home: Path
) -> None:
    res = cli.invoke(app, ["workflows", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "pr_review" in res.output and "bundled" in res.output
    assert "no project workflows yet" in res.output
    assert "rayspec init" in res.output and "workflows eject" in res.output


def test_json_rows_carry_normalised_inputs(cli: CliRunner, project: Path, home: Path) -> None:
    (project / ".rayspec" / "workflows" / "inp.yaml").write_text(
        "rayspec: 1\nname: inp\ninputs:\n  issue: {type: integer, required: true}\n"
        "  mode: {enum: [a, b], default: a, description: which}\n  token: {secret: true}\n"
        "  junk: 3\nsteps:\n  - {id: a, shell: echo}\n"
    )
    row = _rows(cli, project)["inp"]
    assert set(row) == {
        "name",
        "scope",
        "source",
        "description",
        "path",
        "error",
        "overrides",
        "ejected",
        "inputs",
    }
    assert row["scope"] == row["source"] == "project"
    assert row["overrides"] is None and row["ejected"] is None
    assert row["inputs"] == {
        "issue": {
            "type": "integer",
            "required": True,
            "default": None,
            "enum": None,
            "description": None,
            "secret": False,
        },
        "mode": {
            "type": "string",
            "required": False,
            "default": "a",
            "enum": ["a", "b"],
            "description": "which",
            "secret": False,
        },
        "token": {
            "type": "string",
            "required": False,
            "default": None,
            "enum": None,
            "description": None,
            "secret": True,
        },
    }


def test_json_bundled_rows(cli: CliRunner, project: Path, home: Path) -> None:
    row = _rows(cli, project)["pr_review"]
    assert row["scope"] == row["source"] == "bundled"
    assert Path(row["path"]).is_file() and row["overrides"] is None and row["ejected"] is None
    assert row["inputs"]["pr"]["required"] is True and row["inputs"]["pr"]["type"] == "integer"


# --- eject -----------------------------------------------------------------------------------


def test_eject_writes_a_headed_copy_that_takes_precedence(
    cli: CliRunner, project: Path, home: Path, bundled_name: str
) -> None:
    bundled = next(r for r in discover_workflows(project, home=home) if r.name == bundled_name)
    res = cli.invoke(app, ["workflows", "eject", bundled_name, "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert f"ejected {bundled_name}" in res.output and "takes precedence" in res.output
    assert f"rayspec {__version__}" in res.output
    target = project / ".rayspec" / "workflows" / f"{bundled_name}.yaml"
    text = target.read_text(encoding="utf-8")
    header = parse_eject_header(text)
    assert header is not None and header.version == __version__
    assert header.workflow == bundled_name and header.sha256 == bundled_digest(bundled.path)
    modeline, _, body = bundled.path.read_text(encoding="utf-8").partition("\n")
    assert text.startswith(modeline + "\n# rayspec-eject: ")  # header after the modeline
    assert text.endswith(body)  # the bundled document itself, verbatim
    # the listing now says so
    table = cli.invoke(app, ["workflows", "--root", str(project)])
    assert "overridden" in table.output and "changed since" not in table.output
    rows = _rows(cli, project)
    row = rows[bundled_name]
    assert row["scope"] == "project" and row["source"] == "overridden"
    assert row["overrides"] == str(bundled.path) and row["path"] == str(target)
    assert row["ejected"] == {
        "version": __version__,
        "sha256": header.sha256,
        "bundled_changed": False,
    }
    assert sum(name == bundled_name for name in rows) == 1


def test_eject_names_the_bundled_includes(cli: CliRunner, project: Path, home: Path) -> None:
    res = cli.invoke(app, ["workflows", "eject", "pr_review", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "includes bundled review_block" in res.output
    assert "rayspec workflows eject review_block" in res.output


def test_eject_creates_the_rayspec_dir_in_a_project_without_one(
    cli: CliRunner, home: Path, tmp_path: Path
) -> None:
    """The headline flow: a fresh repo, no `.rayspec/` yet, eject to start customising."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    res = cli.invoke(app, ["workflows", "eject", "pr_review", "--root", str(repo)])
    assert res.exit_code == 0, res.output
    assert (repo / ".rayspec" / "workflows" / "pr_review.yaml").is_file()


def test_eject_refuses_an_existing_file_without_force(
    cli: CliRunner, project: Path, home: Path, bundled_name: str
) -> None:
    target = project / ".rayspec" / "workflows" / f"{bundled_name}.yaml"
    target.write_text("mine\n", encoding="utf-8")
    res = cli.invoke(app, ["workflows", "eject", bundled_name, "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "error:" in res.output and "already exists" in res.output and "--force" in res.output
    assert f".rayspec/workflows/{bundled_name}.yaml" in res.output
    assert target.read_text(encoding="utf-8") == "mine\n"
    res = cli.invoke(app, ["workflows", "eject", bundled_name, "--root", str(project), "--force"])
    assert res.exit_code == 0, res.output
    assert "overwrote" in res.output
    assert parse_eject_header(target.read_text(encoding="utf-8")) is not None


def test_eject_refuses_a_symlink_even_with_force(
    cli: CliRunner, project: Path, home: Path, bundled_name: str, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text("x\n")
    target = project / ".rayspec" / "workflows" / f"{bundled_name}.yaml"
    target.symlink_to(elsewhere)
    res = cli.invoke(app, ["workflows", "eject", bundled_name, "--root", str(project), "--force"])
    assert res.exit_code == 2, res.output
    assert "symbolic link" in res.output
    assert elsewhere.read_text() == "x\n"


def test_eject_refuses_a_directory(
    cli: CliRunner, project: Path, home: Path, bundled_name: str
) -> None:
    (project / ".rayspec" / "workflows" / f"{bundled_name}.yaml").mkdir()
    res = cli.invoke(app, ["workflows", "eject", bundled_name, "--root", str(project), "--force"])
    assert res.exit_code == 2, res.output
    assert "is a directory" in res.output


def test_eject_unknown_name_suggests_a_bundled_one(
    cli: CliRunner, project: Path, home: Path
) -> None:
    res = cli.invoke(app, ["workflows", "eject", "pr_reviw", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "unknown workflow 'pr_reviw'" in res.output
    assert "did you mean 'pr_review'" in res.output and "rayspec workflows" in res.output
    assert not (project / ".rayspec" / "workflows" / "pr_reviw.yaml").exists()


def test_eject_refuses_a_project_workflow(cli: CliRunner, project: Path, home: Path) -> None:
    (project / ".rayspec" / "workflows" / "mine.yaml").write_text(WF.format(name="mine"))
    res = cli.invoke(app, ["workflows", "eject", "mine", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "'mine' is not a bundled workflow" in res.output
    assert "project workflow at .rayspec/workflows/mine.yaml" in res.output


def test_eject_with_a_root_that_is_not_a_directory_writes_nothing(
    cli: CliRunner, home: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "typo"
    res = cli.invoke(app, ["workflows", "eject", "pr_review", "--root", str(missing)])
    assert res.exit_code == 2, res.output
    assert f"--root '{missing}' is not a directory" in res.output
    assert not missing.exists()


def test_listing_notes_when_the_bundled_workflow_changed(
    cli: CliRunner, project: Path, home: Path, bundled_name: str
) -> None:
    res = cli.invoke(app, ["workflows", "eject", bundled_name, "--root", str(project)])
    assert res.exit_code == 0, res.output
    target = project / ".rayspec" / "workflows" / f"{bundled_name}.yaml"
    text = target.read_text(encoding="utf-8")
    header = parse_eject_header(text)
    assert header is not None
    target.write_text(text.replace(header.sha256, "0" * 64, 1), encoding="utf-8")
    res = cli.invoke(app, ["workflows", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert f"note: {bundled_name} was ejected from rayspec {__version__}" in res.output
    assert "the bundled workflow has changed since" in res.output
    assert _rows(cli, project)[bundled_name]["ejected"]["bundled_changed"] is True


def test_bundled_includes_names_the_bundled_bodies_by_stem(
    project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rayspec.cli.commands import workflows as mod
    from rayspec.cli.commands._loader_common import make_context

    wfs = project / ".rayspec" / "workflows"
    (wfs / "outer.yaml").write_text(
        "rayspec: 1\nname: outer\nsteps:\n  - {id: blk, include: inner}\n"
    )
    (wfs / "inner.yaml").write_text(
        "rayspec: 1\nname: inner_doc\nsteps:\n  - {id: a, shell: echo}\n"
    )
    monkeypatch.setattr(mod, "is_bundled", lambda p: p.name == "inner.yaml")
    assert mod.bundled_includes(wfs / "outer.yaml", make_context(project)) == ["inner"]
    (wfs / "broken.yaml").write_text("a: [\n")
    assert mod.bundled_includes(wfs / "broken.yaml", make_context(project)) == []

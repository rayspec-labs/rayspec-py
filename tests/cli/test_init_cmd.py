"""`rayspec init [--kind code|content] [--force] [--root DIR]` — scaffold a `.rayspec/` project."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import _loader_common as common
from rayspec.cli.commands.init import SCAFFOLD_FILES, TEMPLATE_KINDS, scaffold
from rayspec.loader import load_workflow, validate_workflow

EXPECTED = {
    ".rayspec/workflows/example.yaml",
    ".rayspec/agents/reviewer.yaml",
    ".rayspec/config.yaml",
    ".rayspec/stubs/example.yaml",
}


@pytest.fixture
def target(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "hello.py").write_text("print('hi')\n")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def _validate(root: Path, home: Path) -> None:
    caps = common.capability_source()
    rw = load_workflow("example", project_root=root, home=home)
    report = validate_workflow(
        rw,
        capabilities_for=caps.capabilities_for,
        template_checker=common.template_checker(),
        provider_ids=caps.provider_ids,
    )
    assert report.ok, report.errors
    assert not rw.warnings and not report.warnings, rw.warnings + report.warnings


@pytest.mark.parametrize("kind", sorted(TEMPLATE_KINDS))
def test_init_scaffolds_every_kind_and_it_validates(kind: str, target: Path, home: Path) -> None:
    result = CliRunner().invoke(app, ["init", "--kind", kind, "--root", str(target)])
    assert result.exit_code == 0, result.output
    written = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert written >= EXPECTED, written
    assert any(p.startswith(".rayspec/prompts/") for p in written), written
    assert (target / ".rayspec" / "prompts").is_dir()
    for rel in sorted(EXPECTED):
        assert rel in result.output
    for hint in ("rayspec validate", "rayspec plan example", "--dry-run"):
        assert hint in result.output
    _validate(target, home)
    res = CliRunner().invoke(app, ["validate", "--root", str(target)])
    assert res.exit_code == 0, res.output


@pytest.mark.parametrize("kind", sorted(TEMPLATE_KINDS))
def test_init_scaffold_dry_runs_with_its_stubs(kind: str, target: Path, home: Path) -> None:
    assert CliRunner().invoke(app, ["init", "--kind", kind, "--root", str(target)]).exit_code == 0
    stubs = target / ".rayspec" / "stubs" / "example.yaml"
    res = CliRunner().invoke(
        app, ["run", "example", "--root", str(target), "--dry-run", "--stubs", str(stubs)]
    )
    assert res.exit_code == 0, res.output
    assert "succeeded" in res.output
    assert "verdict" in res.output
    # plan works too (no inputs required)
    res = CliRunner().invoke(app, ["plan", "example", "--root", str(target)])
    assert res.exit_code == 0, res.output
    assert "reviewer" in res.output


def test_content_kind_is_not_about_code(target: Path, home: Path) -> None:
    assert (
        CliRunner().invoke(app, ["init", "--kind", "content", "--root", str(target)]).exit_code == 0
    )
    data = yaml.safe_load((target / ".rayspec" / "workflows" / "example.yaml").read_text())
    assert data["isolation"] == "none"
    assert all("shell" not in step and "python" not in step for step in data["steps"])
    text = (target / ".rayspec" / "workflows" / "example.yaml").read_text()
    assert "git " not in text


def test_init_never_overwrites_without_force(target: Path, home: Path) -> None:
    assert CliRunner().invoke(app, ["init", "--root", str(target)]).exit_code == 0
    config = target / ".rayspec" / "config.yaml"
    config.write_text("default_provider: codex\n")
    res = CliRunner().invoke(app, ["init", "--root", str(target)])
    assert res.exit_code == 0, res.output
    assert config.read_text() == "default_provider: codex\n"
    assert "exists" in res.output and "--force" in res.output
    res = CliRunner().invoke(app, ["init", "--root", str(target), "--force"])
    assert res.exit_code == 0, res.output
    assert config.read_text() != "default_provider: codex\n"
    assert "default_provider: claude" in config.read_text()


def test_init_defaults_to_the_cwd(
    target: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(target)
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 0, res.output
    assert (target / ".rayspec" / "workflows" / "example.yaml").is_file()


def test_init_rejects_unknown_kind(target: Path, home: Path) -> None:
    res = CliRunner().invoke(app, ["init", "--kind", "video", "--root", str(target)])
    assert res.exit_code == 2


def test_scaffold_api_reports_written_and_skipped(target: Path, home: Path) -> None:
    first = scaffold(target, kind="code", force=False)
    assert {f.relative for f in first if f.action == "created"} >= set(SCAFFOLD_FILES["code"])
    second = scaffold(target, kind="code", force=False)
    assert all(f.action == "skipped" for f in second)
    third = scaffold(target, kind="content", force=True)
    assert all(f.action == "overwritten" for f in third if f.relative in EXPECTED)


def test_init_root_that_is_a_file_is_a_usage_error(tmp_path: Path, home: Path) -> None:
    """`--root <existing file>` must be exit 2 with a message, not a traceback."""
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x")
    res = CliRunner().invoke(app, ["init", "--root", str(not_a_dir)])
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception
    assert "error:" in res.output and "not a directory" in res.output
    assert "Traceback" not in res.output


def test_init_directory_at_a_template_path_is_a_clean_error(target: Path, home: Path) -> None:
    """A directory where a template file goes is an error with or without --force."""
    (target / ".rayspec" / "config.yaml").mkdir(parents=True)
    res = CliRunner().invoke(app, ["init", "--root", str(target), "--force"])
    assert res.exit_code == 2, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), res.exception
    assert "error:" in res.output and "config.yaml" in res.output and "directory" in res.output
    assert "Traceback" not in res.output
    # without --force it is not silently `exists … skipped` either
    res = CliRunner().invoke(app, ["init", "--root", str(target)])
    assert res.exit_code == 2, res.output
    assert "config.yaml" in res.output and "directory" in res.output
    assert "skipped" not in res.output
    # the Python surface raises IsADirectoryError (an OSError) for the same case
    with pytest.raises(IsADirectoryError):
        scaffold(target, kind="code", force=True)


def test_init_warns_when_nothing_was_written(target: Path, home: Path) -> None:
    """Review: a no-op init (every file exists) says so on stderr; exit stays 0 (documented)."""
    assert CliRunner().invoke(app, ["init", "--root", str(target)]).exit_code == 0
    res = CliRunner().invoke(app, ["init", "--root", str(target)])
    assert res.exit_code == 0, res.output
    assert "0 file(s) written" in res.output
    assert "warning:" in res.output and "nothing written" in res.output and "--force" in res.output
    assert "nothing written" not in res.stdout  # the warning goes to stderr
    # a first init (in a git checkout) writes everything and never warns
    fresh = target.parent / "fresh"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=fresh, check=True)
    res = CliRunner().invoke(app, ["init", "--root", str(fresh)])
    assert res.exit_code == 0 and "warning:" not in res.output, res.output


def test_init_detects_a_scaffold_of_the_other_kind(target: Path, home: Path) -> None:
    """Review: `--kind content` over a `code` scaffold warns about the mixed result (and --force
    about the orphan files of the old kind) instead of silently producing a hybrid project."""
    assert CliRunner().invoke(app, ["init", "--root", str(target)]).exit_code == 0
    res = CliRunner().invoke(app, ["init", "--kind", "content", "--root", str(target)])
    assert res.exit_code == 0, res.output
    assert "warning:" in res.output and "code" in res.output and "content" in res.output
    assert "mixed" in res.output and "--force" in res.output
    assert "warning" not in res.stdout
    res = CliRunner().invoke(app, ["init", "--kind", "content", "--root", str(target), "--force"])
    assert res.exit_code == 0, res.output
    assert "warning:" in res.output and ".rayspec/prompts/review.md" in res.output
    assert "orphan" in res.output or "left over" in res.output
    # same kind again: no kind warning (only the no-op one)
    res = CliRunner().invoke(app, ["init", "--kind", "content", "--root", str(target)])
    assert "mixed" not in res.output and "left over" not in res.output
    assert "nothing written" in res.output


def test_init_code_scaffold_outside_git_warns_but_succeeds(tmp_path: Path, home: Path) -> None:
    """The `code` scaffold's `files` step runs `git ls-files`; outside a git checkout init
    says so on stderr (exit 0) and points at `git init` / `--kind content`."""
    notes = tmp_path / "notes"
    notes.mkdir()
    res = CliRunner().invoke(app, ["init", "--root", str(notes)])
    assert res.exit_code == 0, res.output
    assert "warning:" in res.output and "not a git repository" in res.output
    assert "git ls-files" in res.output and "git init" in res.output
    assert "rayspec init --kind content" in res.output
    assert "not a git repository" not in res.stdout  # stderr
    assert (notes / ".rayspec" / "workflows" / "example.yaml").is_file()


def test_init_content_scaffold_outside_git_is_silent(tmp_path: Path, home: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    res = CliRunner().invoke(app, ["init", "--kind", "content", "--root", str(notes)])
    assert res.exit_code == 0, res.output
    assert "not a git repository" not in res.output


def test_init_code_scaffold_in_a_nested_dir_of_a_checkout_does_not_warn(
    target: Path, home: Path
) -> None:
    nested = target / "sub" / "dir"
    nested.mkdir(parents=True)
    res = CliRunner().invoke(app, ["init", "--root", str(nested)])
    assert res.exit_code == 0, res.output
    assert "not a git repository" not in res.output


def test_in_git_checkout_detects_dir_and_worktree_file(tmp_path: Path) -> None:
    from rayspec.cli.commands.init import in_git_checkout

    plain = tmp_path / "plain"
    plain.mkdir()
    assert in_git_checkout(plain) is False
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert in_git_checkout(repo / "a" / "b") is True  # walks up; dirs need not exist
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    assert in_git_checkout(wt) is True


def test_init_also_installs_the_project_skill(target: Path, home: Path) -> None:
    res = CliRunner().invoke(app, ["init", "--root", str(target)])
    assert res.exit_code == 0, res.output
    skill = target / ".claude" / "skills" / "rayspec"
    assert (skill / "SKILL.md").is_file()
    assert (skill / "references" / "schema.md").is_file()
    assert ".claude/skills/rayspec/SKILL.md" in res.output
    assert "Claude Code" in res.output
    # idempotent: the second init keeps an edited skill
    (skill / "SKILL.md").write_text("edited\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["init", "--root", str(target)])
    assert res.exit_code == 0, res.output
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "edited\n"
    assert "exists" in res.output
    res = CliRunner().invoke(app, ["init", "--root", str(target), "--force"])
    assert res.exit_code == 0, res.output
    assert (skill / "SKILL.md").read_text(encoding="utf-8") != "edited\n"


def test_init_no_skill_opts_out(target: Path, home: Path) -> None:
    res = CliRunner().invoke(app, ["init", "--root", str(target), "--no-skill"])
    assert res.exit_code == 0, res.output
    assert not (target / ".claude").exists()
    assert ".claude/skills" not in res.output
    assert (target / ".rayspec" / "workflows" / "example.yaml").is_file()


def test_init_nothing_written_warning_counts_the_skill_too(target: Path, home: Path) -> None:
    assert CliRunner().invoke(app, ["init", "--root", str(target)]).exit_code == 0
    res = CliRunner().invoke(app, ["init", "--root", str(target)])
    assert res.exit_code == 0, res.output
    assert "nothing written" in res.output
    # the skill alone missing: no "nothing written" (something was created)
    import shutil

    shutil.rmtree(target / ".claude")
    res = CliRunner().invoke(app, ["init", "--root", str(target)])
    assert res.exit_code == 0, res.output
    assert "nothing written" not in res.output
    assert (target / ".claude" / "skills" / "rayspec" / "SKILL.md").is_file()


def test_init_skill_write_failure_names_the_scaffold_that_was_written(
    target: Path, home: Path
) -> None:
    """`.claude` is a regular file: the skill cannot be written (exit 2), but `.rayspec/` was
    already scaffolded — the error says so and points at --no-skill."""
    (target / ".claude").write_text("not a directory\n")
    res = CliRunner().invoke(app, ["init", "--root", str(target)])
    assert res.exit_code == 2, res.output
    assert "error: cannot write the skill:" in res.output
    assert "Traceback" not in res.output
    assert (target / ".rayspec" / "workflows" / "example.yaml").is_file()
    assert ".rayspec/ scaffold was written" in res.output
    assert "--no-skill" in res.output


def test_init_does_not_import_the_skill_command_module() -> None:
    """Command modules are independent plug-ins: `init` shares presentation helpers with
    `skill` through `rayspec.cli.commands._skill_common`, not by importing the other command."""
    code = (
        "import sys, rayspec.cli.commands.init as m; "
        "assert 'rayspec.cli.commands.skill' not in sys.modules, 'init imports the skill command'; "
        "assert m.print_install_result.__module__ == 'rayspec.cli.commands._skill_common'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

"""`rayspec quickstart` — the first command on a machine that has only installed rayspec.

Every test below names the defect it fails on. The three that matter most, because they are the
ones a first-run command can get catastrophically wrong, are: it must never hang (nothing is
asked without a terminal, and a login is never spawned without one), it must never overwrite a
project that already exists, and it must actually finish with a green dry run — the whole point
of the command is that the authoring loop needs no login and costs nothing.

The SDK fixtures mirror ``tests/cli/test_doctor_cmd.py::sdks``: fake ``claude_agent_sdk`` /
``openai_codex`` / ``codex_cli_bin`` modules in ``sys.modules``, a canned ``doctor.version_of``,
a patched ``shutil.which`` and a temporary ``HOME`` + ``RAYSPEC_HOME``, so no test depends on
what is installed or logged in on the machine running the suite.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayspec.cli import _runs_common as runs_common
from rayspec.cli.app import app
from rayspec.cli.commands import doctor as doctor_mod
from rayspec.cli.commands import init as init_mod
from rayspec.cli.commands import quickstart as quickstart_mod

CLAUDE_VERSION = "2.1.999"
CODEX_VERSION = "0.147.9"

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


class FakeSdks:
    """Handles to the fake SDK layout a test can poke at."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.claude_bin = root / "claude_agent_sdk" / "_bundled" / "claude"
        self.codex_bin = root / "codex_cli_bin" / "bin" / "codex"


def _module(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture
def sdks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSdks:
    """Fake `claude_agent_sdk`, `openai_codex`, `codex_cli_bin`; canned versions; git + uv."""
    fake = FakeSdks(tmp_path / "site")
    fake.claude_bin.parent.mkdir(parents=True)
    fake.claude_bin.write_text("#!/bin/sh\necho 2.1.999 (Claude Code)\n")
    fake.claude_bin.chmod(0o755)
    fake.codex_bin.parent.mkdir(parents=True)
    fake.codex_bin.write_text("#!/bin/sh\necho codex-cli 0.147.9\n")
    fake.codex_bin.chmod(0o755)
    (fake.root / "claude_agent_sdk" / "__init__.py").write_text("")
    claude = _module(
        "claude_agent_sdk",
        __version__="0.2.900",
        __file__=str(fake.root / "claude_agent_sdk" / "__init__.py"),
    )
    cli_version = _module("claude_agent_sdk._cli_version", __cli_version__=CLAUDE_VERSION)
    codex_sdk = _module("openai_codex", __version__="0.147.9")
    codex_bin = _module("codex_cli_bin", bundled_codex_path=lambda: fake.codex_bin)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", claude)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk._cli_version", cli_version)
    monkeypatch.setitem(sys.modules, "openai_codex", codex_sdk)
    monkeypatch.setitem(sys.modules, "codex_cli_bin", codex_bin)

    def fake_version(cmd: list[str], *, timeout_s: float = 5.0) -> str | None:
        if cmd[0] == str(fake.claude_bin):
            return f"{CLAUDE_VERSION} (Claude Code)"
        if cmd[0] == str(fake.codex_bin):
            return f"codex-cli {CODEX_VERSION}"
        if cmd[0].endswith("git"):
            return "git version 2.45.0"
        if cmd[0].endswith("uv"):
            return "uv 0.8.0"
        return None

    monkeypatch.setattr(doctor_mod, "version_of", fake_version)
    real_which = shutil.which
    tools = {"git": real_which("git"), "uv": "/usr/local/bin/uv"}
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: tools.get(name))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    (tmp_path / "userhome").mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(tmp_path / "userhome" / ".rayspec"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(doctor_mod, "claude_login_source", lambda: None)
    return fake


@pytest.fixture
def no_git(monkeypatch: pytest.MonkeyPatch, sdks: FakeSdks) -> None:
    """A machine with no `git` binary at all — `shutil.which("git")` answers None."""
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)


@pytest.fixture
def plain_dir(tmp_path: Path) -> Path:
    """An empty directory that is not a git repository and holds no project."""
    path = tmp_path / "plain"
    path.mkdir()
    return path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """An empty directory that IS a git repository, with one commit."""
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run([*GIT, "add", "-A"], cwd=path, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def quickstart(*args: str, tty: bool = False, stdin: str = "") -> Any:
    """Invoke `rayspec quickstart` with the args given; `tty` fakes an answerable terminal."""
    runner = CliRunner()
    return runner.invoke(app, ["quickstart", *args], input=stdin)


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the CLI believe stdin can be answered (CliRunner's stdin is a pipe)."""
    monkeypatch.setattr(runs_common, "stdin_is_tty", lambda: True)


def run_dirs(home: Path) -> list[Path]:
    """Every run directory under a RAYSPEC_HOME."""
    return sorted((home / "projects").glob("*/*/runs/*"))


def rayspec_home(sdks: FakeSdks) -> Path:
    return sdks.root.parent / "userhome" / ".rayspec"


# --------------------------------------------------------------------------------------------
# happy path and idempotence
# --------------------------------------------------------------------------------------------


def test_empty_git_repo_is_scaffolded_and_dry_run_green(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if the scaffold or the run is skipped, or the run is not green."""
    res = quickstart("--root", str(git_repo), "--no-skill")
    assert res.exit_code == 0, res.output
    assert (git_repo / ".rayspec" / "workflows" / "example.yaml").is_file()
    assert "succeeded" in res.output
    for command in (
        "rayspec validate",
        "rayspec plan example",
        "rayspec run example --dry-run --stubs",
        "rayspec run example ",
    ):
        assert command in res.output, command


def test_running_it_twice_changes_nothing(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if quickstart passes force=True or re-scaffolds over an existing project."""
    first = quickstart("--root", str(git_repo), "--no-skill")
    assert first.exit_code == 0, first.output
    config = git_repo / ".rayspec" / "config.yaml"
    config.write_text("default_provider: claude\n# edited by hand\n", encoding="utf-8")
    before = config.read_bytes()

    second = quickstart("--root", str(git_repo), "--no-skill")

    assert second.exit_code == 0, second.output
    assert config.read_bytes() == before
    assert "already exists" in second.output
    assert "succeeded" in second.output


def test_an_enclosing_project_is_respected(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if init's cwd-only --root rule is used unguarded and a project is nested."""
    res = quickstart("--root", str(git_repo), "--no-skill")
    assert res.exit_code == 0, res.output
    child = git_repo / "service"
    child.mkdir()

    res = quickstart("--root", str(child), "--no-skill")

    assert res.exit_code == 0, res.output
    assert not (child / ".rayspec").exists()
    assert str(git_repo) in res.output


# --------------------------------------------------------------------------------------------
# not hanging, not asking
# --------------------------------------------------------------------------------------------


def test_without_a_tty_nothing_is_asked_and_it_still_works(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if it prompts (CliRunner surfaces the prompt text) or skips the work."""
    res = quickstart("--root", str(git_repo), "--no-skill", stdin="")

    assert res.exit_code == 0, res.output
    assert "Log in now?" not in res.output
    assert "[y/N]" not in res.output
    assert "nothing will be asked" in res.output
    assert (git_repo / ".rayspec").is_dir()
    assert run_dirs(rayspec_home(sdks)), "the dry run left no record"


def test_no_interactive_never_spawns_a_login(
    sdks: FakeSdks, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if a browser/device-code flow is started in a container."""

    def explode(argv: list[str]) -> tuple[bool, str | None]:
        raise AssertionError(f"a login was spawned without a terminal: {argv}")

    monkeypatch.setattr(quickstart_mod, "run_login", explode)

    res = quickstart(
        "--root", str(git_repo), "--no-skill", "--no-interactive", "--provider", "claude"
    )

    assert res.exit_code == 0, res.output
    assert shlex.join([str(sdks.claude_bin), "auth", "login"]) in res.output


def test_ctrl_c_at_a_question_is_130_without_a_traceback(
    sdks: FakeSdks, git_repo: Path, tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if the exception escapes as a traceback or a different code."""

    def interrupt(*args: Any, **kwargs: Any) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(quickstart_mod.typer, "prompt", interrupt)

    res = quickstart("--root", str(git_repo), "--no-skill")

    assert res.exit_code == 130, res.output
    assert "Traceback" not in res.output


# --------------------------------------------------------------------------------------------
# machine-readable
# --------------------------------------------------------------------------------------------


def _payload(res: Any) -> dict[str, Any]:
    assert res.stdout.strip().startswith("{"), res.output
    return json.loads(res.stdout)


def test_json_shape(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if a field is renamed or added without updating the docs and the key set."""
    res = quickstart("--root", str(git_repo), "--no-skill", "--json")
    assert res.exit_code == 0, res.output
    payload = _payload(res)

    assert set(payload) == quickstart_mod.QUICKSTART_KEYS
    assert payload["ok"] is True and payload["exit_code"] == 0
    assert payload["root"] == str(git_repo.resolve())
    assert payload["project_root"] == str(git_repo.resolve())
    assert payload["interactive"] is False

    env = payload["environment"]
    assert set(env) == {"python", "git", "providers"}
    assert set(env["git"]) == {"binary", "version", "repository", "commits", "install_command"}
    assert env["git"]["repository"] is True and env["git"]["commits"] is True
    assert env["git"]["install_command"] is None
    assert [p["id"] for p in env["providers"]] == ["claude", "codex"]
    for provider in env["providers"]:
        assert set(provider) == {
            "id",
            "cli_path",
            "cli_source",
            "cli_version",
            "cli_ok",
            "credentials",
            "credentials_source",
            "credentials_verified",
            "login_command",
        }
        assert provider["credentials_verified"] is False

    assert {s["id"] for s in payload["steps"]} <= {"login", "git_init", "scaffold", "dry_run"}
    assert {s["action"] for s in payload["steps"]} <= {"done", "skipped", "failed"}
    assert set(payload["project"]) == {
        "path",
        "existed",
        "kind",
        "files_written",
        "skills_written",
    }
    assert payload["project"]["kind"] == "code" and payload["project"]["skills_written"] == 0
    assert set(payload["run"]) == {
        "attempted",
        "skipped_reason",
        "workflow",
        "command",
        "run_id",
        "status",
        "exit_code",
        "ok",
        "reason",
    }
    assert payload["run"]["attempted"] is True and payload["run"]["ok"] is True
    assert payload["run"]["status"] == "succeeded"
    assert payload["run"]["skipped_reason"] is None
    assert set(payload["isolation"]) == {"next_run", "worktree_available", "reason"}
    assert payload["isolation"]["next_run"] in {"worktree", "none", "blocked"}
    assert len(payload["next_steps"]) == 4
    for step in payload["next_steps"]:
        assert set(step) == {"command", "note", "cost"}
        assert step["cost"] in {"free", "provider"}
    assert [s["cost"] for s in payload["next_steps"]] == ["free", "free", "free", "provider"]
    assert set(payload["doctor"]) == {"ok", "exit_code", "failed_required"}


def test_json_is_the_only_thing_on_stdout(sdks: FakeSdks, git_repo: Path, tty: None) -> None:
    """Fails if the dry run's tree or JSONL leaks into stdout (the redirect is missing)."""
    res = quickstart("--root", str(git_repo), "--no-skill", "--json")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)  # the WHOLE stream, not the last line
    assert payload["run"]["run_id"], payload["run"]


def test_json_and_output_table_conflict_is_exit_2(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if resolve_output is not called."""
    res = quickstart("--root", str(git_repo), "--json", "--output", "table")
    assert res.exit_code == 2, res.output
    assert "--json" in res.output and "--output" in res.output


def test_root_that_is_not_a_directory_is_exit_2(sdks: FakeSdks, tmp_path: Path) -> None:
    """Fails if checked_root is not called first — a typo must not become a project."""
    missing = tmp_path / "typo"
    res = quickstart("--root", str(missing))
    assert res.exit_code == 2, res.output
    assert "is not a directory" in res.output
    assert not missing.exists()


# --------------------------------------------------------------------------------------------
# git — the three conditions
# --------------------------------------------------------------------------------------------


def test_missing_git_names_the_consequence_and_the_install_command(
    no_git: None, sdks: FakeSdks, plain_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if it exits 0, or dies with a bare `error: git is not installed`."""
    monkeypatch.setattr(quickstart_mod.platform, "system", lambda: "Darwin")

    res = quickstart("--root", str(plain_dir), "--no-skill")

    assert res.exit_code == 1, res.output
    assert "refuses without git" in res.output
    assert "dry runs included" in res.output
    assert "xcode-select --install" in res.output
    assert (plain_dir / ".rayspec").is_dir(), "the scaffold must be kept"
    assert "error: git is not installed" not in res.output


def test_missing_git_json_marks_the_run_skipped(
    no_git: None, sdks: FakeSdks, plain_dir: Path
) -> None:
    res = quickstart("--root", str(plain_dir), "--no-skill", "--json")
    assert res.exit_code == 1, res.output
    payload = _payload(res)
    assert payload["ok"] is False and payload["exit_code"] == 1
    assert payload["environment"]["git"]["binary"] is None
    assert payload["environment"]["git"]["commits"] is None
    assert payload["environment"]["git"]["install_command"]
    [dry] = [s for s in payload["steps"] if s["id"] == "dry_run"]
    assert dry["action"] == "skipped"
    assert payload["run"]["attempted"] is False and payload["run"]["skipped_reason"]
    assert payload["isolation"]["next_run"] == "blocked"


@pytest.mark.parametrize(
    ("system", "os_release", "expected"),
    [
        ("Darwin", None, "xcode-select --install"),
        ("Windows", None, "winget install --id Git.Git"),
        ("Linux", "ID=debian\n", "sudo apt install git"),
        ("Linux", "ID=ubuntu\nID_LIKE=debian\n", "sudo apt install git"),
        ("Linux", 'ID=pop\nID_LIKE="ubuntu debian"\n', "sudo apt install git"),
        ("Linux", "ID=fedora\n", "sudo dnf install git"),
        ("Linux", 'ID=rocky\nID_LIKE="rhel fedora"\n', "sudo dnf install git"),
        ("Linux", "ID=alpine\n", "sudo apk add git"),
        ("Linux", "ID=plan9\n", "install git with your package manager"),
        ("Linux", "", "install git with your package manager"),
    ],
)
def test_git_install_command_per_platform(
    system: str, os_release: str | None, expected: str
) -> None:
    """Fails if a branch is wrong."""
    assert expected in quickstart_mod.git_install_command(system=system, os_release=os_release)


def test_git_install_command_survives_an_unreadable_os_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if an unreadable /etc/os-release raises instead of falling back."""
    monkeypatch.setattr(quickstart_mod, "OS_RELEASE_PATH", tmp_path)  # a directory: EISDIR
    assert quickstart_mod.git_install_command(system="Linux") == (
        "install git with your package manager"
    )


def test_git_init_only_after_consent(sdks: FakeSdks, plain_dir: Path) -> None:
    """Fails if consent is bypassed in either direction."""
    res = quickstart("--root", str(plain_dir), "--no-skill", "--no-interactive")
    assert res.exit_code == 0, res.output
    assert not (plain_dir / ".git").exists()
    assert "git init" in res.output

    res = quickstart("--root", str(plain_dir), "--no-skill", "--no-interactive", "--yes")
    assert res.exit_code == 0, res.output
    assert (plain_dir / ".git").is_dir()
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=plain_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert inside.stdout.strip() == "true"


def test_an_existing_repository_config_is_never_touched(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if `git init -b …` or any `git config` is issued over an existing repository."""
    before = (git_repo / ".git" / "config").read_bytes()
    res = quickstart("--root", str(git_repo), "--no-skill", "--yes")
    assert res.exit_code == 0, res.output
    assert (git_repo / ".git" / "config").read_bytes() == before


def test_a_fresh_repo_says_a_worktree_needs_a_commit(sdks: FakeSdks, plain_dir: Path) -> None:
    """Fails if F2 is not surfaced and the user is told isolation is now available."""
    res = quickstart("--root", str(plain_dir), "--no-skill", "--no-interactive", "--yes")
    assert res.exit_code == 0, res.output
    assert "no commits" in res.output
    assert "a worktree is created from a commit" in res.output


HAND_WRITTEN = """
rayspec: 1
name: solo
steps:
  - {id: a, shell: echo a}
"""

NEEDY = """
rayspec: 1
name: needy
isolation: none
inputs:
  subject: { type: string, required: true }
steps:
  - {id: a, shell: 'echo {{ inputs.subject }}'}
"""


def _project_with(root: Path, name: str, text: str) -> Path:
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text, encoding="utf-8")
    return root


def test_isolation_sentence_states_what_the_next_run_gets(
    sdks: FakeSdks, git_repo: Path, plain_dir: Path, tmp_path: Path
) -> None:
    """Fails if the sentence is generic or ignores the workflow document (F3)."""
    scaffolded = quickstart("--root", str(git_repo), "--no-skill")
    assert scaffolded.exit_code == 0, scaffolded.output
    assert "in place, in" in scaffolded.output
    assert "isolation: none" in scaffolded.output

    committed = tmp_path / "committed"
    committed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=committed, check=True)
    _project_with(committed, "solo", HAND_WRITTEN)
    subprocess.run([*GIT, "add", "-A"], cwd=committed, check=True)
    subprocess.run([*GIT, "commit", "-q", "-m", "init"], cwd=committed, check=True)
    res = quickstart("--root", str(committed), "--no-skill", "--no-run")
    assert res.exit_code == 0, res.output
    assert "its own git worktree" in res.output

    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=bare, check=True)
    _project_with(bare, "solo", HAND_WRITTEN)
    res = quickstart("--root", str(bare), "--no-skill", "--no-run")
    assert res.exit_code == 0, res.output
    assert "a worktree needs at least one commit" in res.output


# --------------------------------------------------------------------------------------------
# kind
# --------------------------------------------------------------------------------------------


def test_kind_follows_the_repository_question(
    sdks: FakeSdks, plain_dir: Path, git_repo: Path
) -> None:
    """Fails if a `code` scaffold lands in a non-repo, whose `files:` step would fail."""
    res = quickstart("--root", str(plain_dir), "--no-skill", "--no-interactive")
    assert res.exit_code == 0, res.output
    text = (plain_dir / ".rayspec" / "workflows" / "example.yaml").read_text(encoding="utf-8")
    assert "isolation: none" in text
    assert "git ls-files" not in text and "shell:" not in text

    res = quickstart("--root", str(git_repo), "--no-skill", "--no-interactive")
    assert res.exit_code == 0, res.output
    assert "git ls-files" in (
        (git_repo / ".rayspec" / "workflows" / "example.yaml").read_text(encoding="utf-8")
    )

    forced = plain_dir.parent / "forced"
    forced.mkdir()
    res = quickstart("--root", str(forced), "--no-skill", "--no-interactive", "--kind", "code")
    assert res.exit_code == 0, res.output
    assert "git ls-files" in (
        (forced / ".rayspec" / "workflows" / "example.yaml").read_text(encoding="utf-8")
    )


# --------------------------------------------------------------------------------------------
# login honesty
# --------------------------------------------------------------------------------------------


def test_present_credentials_skip_the_login(
    sdks: FakeSdks, git_repo: Path, tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if it logs in over an existing credential."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def explode(argv: list[str]) -> tuple[bool, str | None]:
        raise AssertionError("a login was spawned over an existing credential")

    monkeypatch.setattr(quickstart_mod, "run_login", explode)

    res = quickstart("--root", str(git_repo), "--no-skill", "--provider", "claude")

    assert res.exit_code == 0, res.output
    assert "ANTHROPIC_API_KEY" in res.output


def test_the_menu_offers_only_what_is_missing_and_runs_the_choice(
    sdks: FakeSdks, git_repo: Path, tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixed four-item menu of the brief is wrong once one provider is already logged in.

    Fails if the menu is printed rather than built: a provider that already has credentials, or
    whose CLI is not usable, must not be offered, and the remaining entries are renumbered.
    """
    seen: list[list[str]] = []

    def record(argv: list[str]) -> tuple[bool, str | None]:
        seen.append(list(argv))
        return True, None

    monkeypatch.setattr(quickstart_mod, "run_login", record)

    res = quickstart("--root", str(git_repo), "--no-skill", "--no-run", stdin="1\n")

    assert res.exit_code == 0, res.output
    assert "1) Claude" in res.output and "2) Codex" in res.output
    assert "3) Both" in res.output and "4) Not now" in res.output
    assert seen == [[str(sdks.claude_bin), "auth", "login"]], seen


def test_a_provider_that_is_already_logged_in_is_not_offered(
    sdks: FakeSdks, git_repo: Path, tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if the menu is a fixed list: with claude configured, codex must become entry 1."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen: list[list[str]] = []

    def record(argv: list[str]) -> tuple[bool, str | None]:
        seen.append(list(argv))
        return True, None

    monkeypatch.setattr(quickstart_mod, "run_login", record)

    res = quickstart("--root", str(git_repo), "--no-skill", "--no-run", stdin="1\n")

    assert res.exit_code == 0, res.output
    assert "1) Codex" in res.output
    assert "Claude" not in res.output.split("Log in now?", 1)[1].split("2)", 1)[0]
    assert "2) Not now" in res.output
    assert seen == [[str(sdks.codex_bin), "login"]], seen


def test_not_now_still_ends_with_a_green_dry_run(
    sdks: FakeSdks, git_repo: Path, tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The never-dead-end rule: declining the login changes nothing about the rest."""

    def explode(argv: list[str]) -> tuple[bool, str | None]:
        raise AssertionError("a login was spawned after the user declined")

    monkeypatch.setattr(quickstart_mod, "run_login", explode)

    res = quickstart("--root", str(git_repo), "--no-skill", stdin="4\n")

    assert res.exit_code == 0, res.output
    assert "not now" in res.output
    assert "succeeded" in res.output


def test_it_never_claims_a_login_was_verified(
    sdks: FakeSdks, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if the wording over-claims what doctor deliberately does not know."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    res = quickstart("--root", str(git_repo), "--no-skill")
    assert res.exit_code == 0, res.output
    lower = res.output.lower()
    assert "verified" not in lower
    assert "login works" not in lower
    assert "rayspec doctor --probe" in res.output

    res = quickstart("--root", str(git_repo), "--no-skill", "--json")
    payload = _payload(res)
    [claude] = [p for p in payload["environment"]["providers"] if p["id"] == "claude"]
    assert claude["credentials"] is True
    assert claude["credentials_verified"] is False

    # and with nothing found, the qualifier that only makes sense for a found credential is gone
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    res = quickstart("--root", str(git_repo), "--no-skill")
    assert res.exit_code == 0, res.output
    assert "nothing found here" in res.output
    assert "present, not checked" not in res.output


def test_a_failed_login_still_ends_with_a_dry_run(
    sdks: FakeSdks, git_repo: Path, tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if a failed login aborts the command (the never-dead-end rule)."""
    monkeypatch.setattr(
        quickstart_mod, "run_login", lambda argv: (False, "the login command exited 1")
    )

    res = quickstart("--root", str(git_repo), "--no-skill", "--provider", "codex")

    assert res.exit_code == 0, res.output
    assert "the login command exited 1" in res.output
    assert str(sdks.codex_bin) in res.output
    assert "succeeded" in res.output


def test_login_argv_is_the_bundled_binary_by_absolute_path(
    sdks: FakeSdks, git_repo: Path, tty: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if a bare `codex` (not on PATH) is spawned, or `claude login` (which does not
    exist) is used, or the path is not on screen before the browser opens."""
    assert quickstart_mod.login_command("claude", "/x/claude") == ["/x/claude", "auth", "login"]
    assert quickstart_mod.login_command("codex", "/x/codex") == ["/x/codex", "login"]

    seen: list[list[str]] = []
    printed: list[str] = []
    real_run = subprocess.run
    logins = {str(sdks.claude_bin), str(sdks.codex_bin)}

    def spy(argv: Any, **kwargs: Any) -> Any:
        if not (isinstance(argv, list) and argv and argv[0] in logins):
            return real_run(argv, **kwargs)  # git and friends still work
        seen.append(list(argv))
        sys.stdout.flush()
        buffer: Any = sys.stdout.buffer  # CliRunner's stdout wraps a BytesIO
        printed.append(buffer.getvalue().decode("utf-8", "replace"))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(quickstart_mod.subprocess, "run", spy)

    res = quickstart(
        "--root", str(git_repo), "--no-skill", "--no-init", "--no-run", "--provider", "both"
    )

    assert res.exit_code == 0, res.output
    assert seen == [
        [str(sdks.claude_bin), "auth", "login"],
        [str(sdks.codex_bin), "login"],
    ], seen
    assert str(sdks.claude_bin) in printed[0], printed[0]
    assert str(sdks.codex_bin) in printed[1], printed[1]


# --------------------------------------------------------------------------------------------
# boundaries
# --------------------------------------------------------------------------------------------


def test_the_project_env_file_is_never_applied(
    sdks: FakeSdks, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if a checkout's credential surface is applied by a first-run command."""
    res = quickstart("--root", str(git_repo), "--no-skill")
    assert res.exit_code == 0, res.output
    (git_repo / ".rayspec" / ".env").write_text("QUICKSTART_MARKER=1\n", encoding="utf-8")
    monkeypatch.delenv("QUICKSTART_MARKER", raising=False)

    res = quickstart("--root", str(git_repo), "--no-skill")

    assert res.exit_code == 0, res.output
    assert "QUICKSTART_MARKER" not in __import__("os").environ
    assert ".rayspec/.env" in res.output or "project .env" in res.output


def test_a_broken_config_does_not_make_it_exit_2(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if make_context is used instead of the tolerant environment_checks path."""
    res = quickstart("--root", str(git_repo), "--no-skill")
    assert res.exit_code == 0, res.output
    (git_repo / ".rayspec" / "config.yaml").write_text(
        "default_provider: [1, 2\n", encoding="utf-8"
    )

    res = quickstart("--root", str(git_repo), "--no-skill")

    assert res.exit_code != 2, res.output
    assert "config" in res.output
    assert "python" in res.output, "the state block must still print"


def test_the_printed_dry_run_command_is_the_command_it_ran(
    sdks: FakeSdks, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if the printed line and the invoked argv drift."""
    seen: list[list[str]] = []
    original = quickstart_mod.invoke_run

    def spy(argv: list[str], *, json_: bool) -> Any:
        seen.append(list(argv))
        return original(argv, json_=json_)

    monkeypatch.setattr(quickstart_mod, "invoke_run", spy)

    res = quickstart("--root", str(git_repo), "--no-skill")

    assert res.exit_code == 0, res.output
    [argv] = seen
    printed = [
        line.strip()[2:].strip()
        for line in res.output.splitlines()
        if line.strip().startswith("$ rayspec run ")
    ]
    assert printed == ["rayspec run " + shlex.join(argv)], (printed, argv)


def test_a_workflow_with_a_required_input_is_named_not_run(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if quickstart runs it and the first experience is `error: missing input`."""
    _project_with(git_repo, "needy", NEEDY)

    res = quickstart("--root", str(git_repo), "--no-skill", "--json")

    assert res.exit_code == 0, res.output
    payload = _payload(res)
    assert payload["run"]["attempted"] is False
    assert "needy" in (payload["run"]["skipped_reason"] or "")
    assert "-i subject=" in (payload["run"]["command"] or "")


BROKEN_STUBS = """
steps:
  judge:
    text: "this is not the JSON the schema asks for"
"""

BROKEN = """
rayspec: 1
name: broken
isolation: none
steps:
  - id: judge
    agent: { provider: claude, model: small, access: read-only }
    prompt: "verdict?"
    output_schema:
      type: object
      properties: { verdict: { type: string } }
      required: [verdict]
"""


def test_a_failed_dry_run_is_exit_1_with_a_next_step(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if a broken install reports success."""
    _project_with(git_repo, "broken", BROKEN)
    (git_repo / ".rayspec" / "stubs").mkdir()
    (git_repo / ".rayspec" / "stubs" / "broken.yaml").write_text(BROKEN_STUBS, encoding="utf-8")

    res = quickstart("--root", str(git_repo), "--no-skill")

    assert res.exit_code == 1, res.output
    assert "rayspec logs" in res.output

    res = quickstart("--root", str(git_repo), "--no-skill", "--json")
    assert res.exit_code == 1, res.output
    payload = _payload(res)
    assert payload["run"]["ok"] is False
    assert payload["ok"] is False


def test_no_init_no_run_no_skill(sdks: FakeSdks, git_repo: Path, plain_dir: Path) -> None:
    """Fails if a flag is ignored."""
    res = quickstart("--root", str(plain_dir), "--no-init", "--no-interactive")
    assert res.exit_code == 0, res.output
    assert not (plain_dir / ".rayspec").exists()
    assert not (plain_dir / ".claude").exists()

    res = quickstart("--root", str(git_repo), "--no-skill", "--no-run")
    assert res.exit_code == 0, res.output
    assert (git_repo / ".rayspec").is_dir()
    assert not run_dirs(rayspec_home(sdks))

    res = quickstart("--root", str(git_repo), "--no-skill")
    assert res.exit_code == 0, res.output
    assert not (git_repo / ".claude").exists()


def test_the_skills_are_written_unless_asked_otherwise(sdks: FakeSdks, git_repo: Path) -> None:
    res = quickstart("--root", str(git_repo))
    assert res.exit_code == 0, res.output
    assert (git_repo / ".claude" / "skills" / "rayspec-cli" / "SKILL.md").is_file()
    assert (git_repo / ".claude" / "skills" / "rayspec-workflows" / "SKILL.md").is_file()


def test_the_four_next_steps_are_inits_four(sdks: FakeSdks, git_repo: Path) -> None:
    """Fails if the two commands start teaching different commands."""
    res = quickstart("--root", str(git_repo), "--no-skill")
    assert res.exit_code == 0, res.output
    for line in init_mod.next_steps("code", skill=False, doctor=False):
        assert line in res.output, line


def test_quickstart_is_registered_and_documented() -> None:
    """A guard on the guards: the command has to be in the app for the totality rules to bite."""
    from typer.main import get_command

    root: Any = get_command(app)
    assert "quickstart" in root.commands

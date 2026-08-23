# SPDX-License-Identifier: Apache-2.0
"""`rayspec quickstart` — the first command on a machine that has only installed rayspec.

One word that ends with a scaffolded project, a green dry run and the four commands that matter.
Its promise is that it never dead-ends: the whole authoring loop — write, validate, plan,
dry-run — needs no login and costs nothing, so every branch below finishes with something that
works, whether or not the user ever logs in.

Boundary: presentation and orchestration only. Every fact it prints about the environment comes
from :mod:`rayspec.cli.commands.doctor` (the auth rows, the bundled CLI paths, their versions),
every file it writes goes through :mod:`rayspec.cli.commands.init` and :mod:`rayspec.skill`, git
is invoked only through :func:`rayspec.workspace.git.run_git`, and the dry run is the builtin
``rayspec run`` command invoked with a real argv — so the command it prints IS the command it
ran. Nothing here re-implements credential discovery, and nothing here reads, writes or echoes a
credential.

Three things it deliberately does **not** do: it never overwrites a file (``rayspec init
--force`` is for that), it never spawns a login without a terminal (a browser hand-off in a
container is the one thing that must never be started), and it never claims a login was checked —
``rayspec doctor --probe`` is the only thing that knows.
"""

from __future__ import annotations

import contextlib
import io
import json as jsonlib
import platform
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from rayspec import __version__
from rayspec.cli import _runs_common as runs_common
from rayspec.cli.commands import doctor, init
from rayspec.cli.commands import run as run_module
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    checked_root,
    console,
    err_console,
    fail,
    print_json,
    resolve_output,
    stdout_can_encode,
)
from rayspec.cli.commands._skill_common import print_install_result
from rayspec.config import Config, load_env, rayspec_home
from rayspec.engine.runtime import EXIT_INTERRUPTED
from rayspec.errors import RayspecError
from rayspec.loader import ResolvedWorkflow, discover_workflows, find_project_root, load_workflow
from rayspec.skill import SKILLS, install_skill, project_skill_dir
from rayspec.workspace.errors import GitError
from rayspec.workspace.git import ref_exists, run_git

#: The two providers a login flow exists for, in the order they are offered and reported.
LOGIN_PROVIDERS: tuple[str, ...] = ("claude", "codex")

#: How each provider's account is described in the login menu.
ACCOUNT_OF: Mapping[str, str] = {
    "claude": f"claude.ai account, or {doctor.CLAUDE_AUTH_VARS[0]}",
    "codex": f"ChatGPT account, or {doctor.CODEX_AUTH_VAR}",
}

#: Where a Linux distribution identifies itself (module level so a test can point it elsewhere).
OS_RELEASE_PATH = Path("/etc/os-release")

#: ``os-release`` ids (``ID`` / ``ID_LIKE``) → the command that installs git on that family.
_PACKAGE_MANAGERS: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"debian", "ubuntu"}), "sudo apt install git"),
    (frozenset({"fedora", "rhel", "centos"}), "sudo dnf install git"),
    (frozenset({"alpine"}), "sudo apk add git"),
)

#: Keys of the ``--json`` document, asserted against the payload where it is built.
QUICKSTART_KEYS: frozenset[str] = frozenset(
    {
        "ok",
        "exit_code",
        "rayspec",
        "root",
        "project_root",
        "interactive",
        "environment",
        "steps",
        "project",
        "run",
        "isolation",
        "next_steps",
        "doctor",
    }
)

#: The four things quickstart can DO, in the order they are reported.
STEP_IDS: tuple[str, ...] = ("login", "git_init", "scaffold", "dry_run")

#: The label each step prints under (one column, so the lines read as a list).
STEP_LABELS: Mapping[str, str] = {
    "login": "login",
    "git_init": "git init",
    "scaffold": "project",
    "dry_run": "dry run",
}


class LoginTarget(StrEnum):
    """``--provider`` values: which provider(s) to offer a login for."""

    claude = "claude"
    codex = "codex"
    both = "both"
    none = "none"

    def ids(self) -> tuple[str, ...]:
        """The provider ids this choice selects, in report order."""
        if self is LoginTarget.both:
            return LOGIN_PROVIDERS
        if self is LoginTarget.none:
            return ()
        return (self.value,)


# --------------------------------------------------------------------------------------------------
# glyphs
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Glyphs:
    """The one-character status column, in whichever alphabet stdout can print."""

    ok: str
    warn: str
    fail: str
    info: str

    @classmethod
    def current(cls) -> Glyphs:
        """UTF when stdout can encode it, ASCII otherwise — every glyph is width 1 either way,
        so the columns line up in both."""
        if stdout_can_encode("✓✗·"):
            return cls("✓", "!", "✗", "·")
        return cls("+", "!", "x", "-")

    def of(self, status: str) -> tuple[str, str]:
        """``(glyph, rich style)`` for a doctor status."""
        return {
            "ok": (self.ok, "green"),
            "warn": (self.warn, "yellow"),
            "fail": (self.fail, "red"),
            "info": (self.info, "dim"),
        }[status]


# --------------------------------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Step:
    """One thing quickstart can do, and what became of it."""

    id: str
    action: str = "skipped"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "action": self.action, "detail": self.detail}


@dataclass(slots=True)
class GitState:
    """The three git conditions: the binary, the repository, and at least one commit."""

    binary: str | None
    version: str | None
    repository: bool
    commits: bool | None
    install_command: str | None

    @property
    def worktree_available(self) -> bool:
        return bool(self.binary) and self.repository and self.commits is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "binary": self.binary,
            "version": self.version,
            "repository": self.repository,
            "commits": self.commits,
            "install_command": self.install_command,
        }


@dataclass(slots=True)
class ProviderState:
    """One provider's CLI and credential state, entirely as ``doctor`` reported it."""

    id: str
    cli_path: str | None = None
    cli_source: str | None = None
    cli_version: str | None = None
    cli_ok: bool = False
    cli_problem: str | None = None
    credentials: bool = False
    credentials_source: str | None = None

    @property
    def login_command(self) -> str | None:
        """The runnable login command on this machine, by absolute path."""
        if not self.cli_path:
            return None
        return shlex.join(login_command(self.id, self.cli_path))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cli_path": self.cli_path,
            "cli_source": self.cli_source,
            "cli_version": self.cli_version,
            "cli_ok": self.cli_ok,
            "credentials": self.credentials,
            "credentials_source": self.credentials_source,
            # ALWAYS false, on purpose: no consumer may read `credentials: true` as "the login
            # works". `rayspec doctor --probe` is the only thing that answers that question.
            "credentials_verified": False,
            "login_command": self.login_command,
        }


@dataclass(slots=True)
class State:
    """Everything quickstart collected before it did anything."""

    target: Path
    project_root: Path
    home: Path
    config: Config
    checks: list[doctor.Check]
    git: GitState
    providers: list[ProviderState]

    def check(self, check_id: str) -> doctor.Check | None:
        return next((c for c in self.checks if c.id == check_id), None)


@dataclass(slots=True)
class DryRun:
    """What became of the one dry run quickstart performs."""

    attempted: bool = False
    skipped_reason: str | None = None
    workflow: str | None = None
    command: str | None = None
    run_id: str | None = None
    status: str | None = None
    exit_code: int | None = None
    reason: str | None = None
    blocking: bool = False
    """Whether the reason the dry run did not happen is one quickstart could not fix.

    ``--no-run``, ``--no-init`` and "this workflow needs ``-i NAME=...``" are somebody's
    decision. No workflow at all, and a workflow that does not load, are not: quickstart set out
    to prove the install and could not. It is deliberately **not** in :meth:`to_dict` — the
    ``--json`` document already carries the whole answer as ``ok``, ``exit_code`` and
    ``run.skipped_reason``, and the documented key set does not change.
    """

    @property
    def ok(self) -> bool | None:
        return None if self.exit_code is None else self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "skipped_reason": self.skipped_reason,
            "workflow": self.workflow,
            "command": self.command,
            "run_id": self.run_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "ok": self.ok,
            "reason": self.reason,
        }


@dataclass(slots=True)
class Project:
    """What happened to ``<root>/.rayspec/``."""

    path: Path
    existed: bool = False
    kind: str | None = None
    files_written: int = 0
    skills_written: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "existed": self.existed,
            "kind": self.kind,
            "files_written": self.files_written,
            "skills_written": self.skills_written,
        }


@dataclass(slots=True)
class Isolation:
    """What the *next* real run gets — read from the workflow document, not only the machine."""

    next_run: str
    worktree_available: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_run": self.next_run,
            "worktree_available": self.worktree_available,
            "reason": self.reason,
        }


@dataclass(slots=True)
class Outcome:
    """One quickstart invocation: what it found, what it did, and what it says to do next."""

    state: State
    interactive: bool
    project_root: Path
    steps: dict[str, Step] = field(default_factory=lambda: {i: Step(i) for i in STEP_IDS})
    project: Project | None = None
    dry_run: DryRun = field(default_factory=DryRun)
    isolation: Isolation | None = None
    next_step_lines: list[str] = field(default_factory=list)

    def done(self, step_id: str, detail: str) -> None:
        self.steps[step_id] = Step(step_id, "done", detail)

    def skipped(self, step_id: str, detail: str) -> None:
        self.steps[step_id] = Step(step_id, "skipped", detail)

    def failed(self, step_id: str, detail: str) -> None:
        self.steps[step_id] = Step(step_id, "failed", detail)


# --------------------------------------------------------------------------------------------------
# git: the install command, and the three conditions
# --------------------------------------------------------------------------------------------------


def _read_os_release() -> str:
    """``/etc/os-release`` as text, or ``""`` when it cannot be read (never raises)."""
    try:
        return OS_RELEASE_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _os_release_ids(text: str) -> frozenset[str]:
    """The ``ID`` and ``ID_LIKE`` tokens of an ``os-release`` document, lowercased."""
    found: set[str] = set()
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in {"ID", "ID_LIKE"}:
            found.update(token.lower() for token in value.strip().strip("\"'").split())
    return frozenset(found)


def git_install_command(*, system: str | None = None, os_release: str | None = None) -> str:
    """The command that installs ``git`` here — best effort, never a guess that misleads.

    ``system`` defaults to :func:`platform.system`, ``os_release`` to the text of
    :data:`OS_RELEASE_PATH` (a file nobody can read is simply an unknown distribution). An
    unrecognised platform gets the honest answer rather than a command that would not work.
    """
    name = system if system is not None else platform.system()
    if name == "Darwin":
        return "xcode-select --install     (or: brew install git)"
    if name == "Windows":  # best effort, like every other Windows path in this codebase
        return "winget install --id Git.Git"
    ids = _os_release_ids(os_release if os_release is not None else _read_os_release())
    for family, command in _PACKAGE_MANAGERS:
        if ids & family:
            return command
    return "install git with your package manager"


def git_state(check: doctor.Check | None, target: Path) -> GitState:
    """The three git conditions for ``target``: doctor's ``git`` row plus two cheap probes.

    ``repository`` is :func:`rayspec.cli.commands.init.in_git_checkout` — a pure ``.git`` path
    walk that needs no binary — and ``commits`` is ``None`` (unknown) whenever there is no binary
    or no repository to ask. The third condition is the one that is easy to miss: ``git init``
    alone does not give a run its own worktree, because a worktree is created from a commit.
    """
    binary: str | None = None
    version: str | None = None
    if check is not None and check.status != "fail":
        binary, _, rest = check.detail.partition(" · ")
        version = doctor.parse_version(rest)
    repository = init.in_git_checkout(target)
    commits: bool | None = None
    if binary and repository:
        try:
            commits = ref_exists(target, "HEAD")
        except GitError:  # a repository git itself refuses to answer about
            commits = None
    return GitState(
        binary=binary,
        version=version,
        repository=repository,
        commits=commits,
        install_command=None if binary else git_install_command(),
    )


# --------------------------------------------------------------------------------------------------
# providers: every answer comes from doctor
# --------------------------------------------------------------------------------------------------


def provider_state(
    provider_id: str, checks: Sequence[doctor.Check], settings: Mapping[str, Any]
) -> ProviderState:
    """Fold doctor's ``<id>.cli`` and ``<id>.auth`` rows into one answer about one provider."""
    state = ProviderState(id=provider_id)
    found = (
        doctor.claude_cli(settings) if provider_id == "claude" else doctor.find_codex_cli(settings)
    )
    cli_row = next((c for c in checks if c.id == f"{provider_id}.cli"), None)
    if isinstance(found, tuple):
        state.cli_path, state.cli_source = found
        prefix = f"{state.cli_path} · "
        if cli_row is not None and cli_row.detail.startswith(prefix):
            shown = cli_row.detail[len(prefix) :].split(" (")[0]
            state.cli_version = None if shown == "version unknown" else shown
    if cli_row is not None:
        state.cli_ok = cli_row.status != "fail"
        if cli_row.status == "fail":
            state.cli_problem = cli_row.detail + (f" — {cli_row.hint}" if cli_row.hint else "")
    auth_row = next((c for c in checks if c.id == f"{provider_id}.auth"), None)
    if auth_row is not None and auth_row.status in doctor.CONFIGURED_AUTH_STATUSES:
        state.credentials = True
        state.credentials_source = auth_row.detail
    return state


def auth_row_after_login(provider_id: str, settings: Mapping[str, Any]) -> doctor.Check | None:
    """Re-read one provider's ``<id>.auth`` row — the only way quickstart learns a login landed."""
    checks = (
        doctor.claude_checks(settings) if provider_id == "claude" else doctor.codex_checks(settings)
    )
    return next((c for c in checks if c.id == f"{provider_id}.auth"), None)


def collect_state(target: Path) -> State:
    """Doctor's environment + provider rows for ``target``, plus the three git conditions.

    This is :func:`rayspec.cli.commands.doctor.run_doctor`'s body minus pricing and probes, using
    only public functions. The project ``.rayspec/.env`` is deliberately **not** applied: it is a
    credential surface of the checkout, and quickstart is the first command somebody runs in a
    directory they may have just cloned.
    """
    home = rayspec_home()
    project_root = project_root_for(target, home)
    with contextlib.suppress(RayspecError):  # a broken .env must not stop the diagnosis
        load_env(project_root, home=home, include_project=False)  # the home file only
    checks, config = doctor.environment_checks(start=target, project_root=project_root, home=home)
    checks = [*checks]
    checks += doctor.claude_checks(config.providers.get("claude", {}))
    checks += doctor.codex_checks(config.providers.get("codex", {}))
    providers = [
        provider_state(pid, checks, config.providers.get(pid, {})) for pid in LOGIN_PROVIDERS
    ]
    return State(
        target=target,
        project_root=project_root,
        home=home,
        config=config,
        checks=checks,
        git=git_state(next((c for c in checks if c.id == "git"), None), target),
        providers=providers,
    )


# --------------------------------------------------------------------------------------------------
# the login subprocess
# --------------------------------------------------------------------------------------------------


def login_command(provider_id: str, cli_path: str) -> list[str]:
    """The argv that logs ``provider_id`` in, using ``cli_path`` — always an absolute path.

    ``claude login`` does not exist (the bundled CLI exposes ``auth {login,logout,status}`` and
    ``setup-token``); ``codex login`` does. The path is never the bare name: the bundled
    ``codex`` is **not** on ``PATH``, which is exactly why a fresh user who types ``codex login``
    gets "command not found".
    """
    if provider_id == "claude":
        return [cli_path, "auth", "login"]
    return [cli_path, "login"]


def run_login(argv: Sequence[str]) -> tuple[bool, str | None]:
    """Spawn a provider's login and report ``(ok, why not)``.

    No ``shell=True``, no captured output (the child must own the terminal for the browser
    hand-off or the device code), the inherited environment (so ``CLAUDE_CONFIG_DIR`` /
    ``CODEX_HOME`` land where doctor looks) and no timeout — a human is typing. A
    ``KeyboardInterrupt`` is left to the caller: Ctrl-C during a login must not lose the dry run.
    """
    try:
        completed = subprocess.run(list(argv), check=False)
    except OSError as exc:
        return False, f"could not start {argv[0]}: {exc.strerror or exc}"
    if completed.returncode != 0:
        return False, f"the login command exited {completed.returncode}"
    return True, None


def login_fallback(provider: ProviderState) -> str:
    """What to try when the login command itself failed, per provider.

    Both are doctor's hint verbatim — they already encode the trap that neither bundled binary
    is on ``PATH``, and a second wording of either would drift. claude also gets the version-proof
    fallback appended: an older CLI has no ``auth login``.
    """
    if provider.id == "claude":
        return (
            f"{doctor.claude_login_hint(provider.cli_path)}"
            f" — or open `{provider.cli_path}` and use /login"
        )
    return doctor.codex_login_hint(provider.cli_path)


# --------------------------------------------------------------------------------------------------
# the dry run
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RunOutcome:
    """What one invocation of the builtin ``run`` command reported."""

    exit_code: int
    run_id: str | None = None
    status: str | None = None
    reason: str | None = None


def stubs_argument(stubs: Path, project_root: Path) -> str:
    """How ``--stubs`` has to be written to work from the directory quickstart was run in.

    Relative to the project root when the cwd already **is** that root — which is what a person
    typing this by hand would write — and absolute otherwise, because ``--stubs`` resolves
    against the cwd and ``--root`` does not change that.

    One answer, used twice: for the command quickstart runs and for the command it tells you to
    run next. They were computed in two places, so from a subdirectory of an existing project
    the closing ``what to do next:`` line was the one copy-paste in the whole output that failed.
    """
    same_cwd = Path.cwd().resolve() == project_root
    return stubs.relative_to(project_root).as_posix() if same_cwd else str(stubs)


def dry_run_argv(
    workflow: str, *, project_root: Path, stubs: Path | None, json_: bool
) -> list[str]:
    """The argv the builtin ``run`` command is invoked with — and the command that is printed."""
    same_cwd = Path.cwd().resolve() == project_root
    argv = [workflow, "--dry-run"]
    if stubs is not None:
        argv += ["--stubs", stubs_argument(stubs, project_root)]
    if not same_cwd:  # otherwise `run` walks up to the same root anyway
        argv += ["--root", str(project_root)]
    argv += ["--no-interactive"]  # a dry run auto-approves gates; this is belt and braces
    if json_:
        argv += ["--json", "--quiet"]
    return argv


def printed_command(argv: Sequence[str]) -> str:
    """The command line ``argv`` is — what quickstart prints, and what it then runs."""
    return "rayspec run " + shlex.join(argv)


def invoke_run(argv: list[str], *, json_: bool) -> RunOutcome:
    """Run the **builtin** ``rayspec run`` with ``argv``, through the real parser.

    Catching here is load-bearing: the root ``ErrorBoundaryGroup`` would otherwise turn a
    :class:`~rayspec.errors.RayspecError` from inside ``run`` into ``error: … ; exit 2`` and
    quickstart would never print its summary. It has to be ``typer.Exit`` — typer ≥ 0.20 vendors
    click, so the installed ``click.exceptions.Exit`` is a different class.

    ``make_context(..., parent=None)`` is also what keeps the checkout's ``.rayspec/.env`` out of
    the process: :func:`~rayspec.cli.commands._loader_common.invoked_command` walks up from that
    context and does not answer ``"run"``, which is the correct rule for a first-run command.

    Under ``--json`` stdout is captured so the run's JSONL and summary stay out of quickstart's
    one JSON document, and the summary object (the documented last stdout line) is parsed back.
    """
    mini = typer.Typer()
    run_module.register(mini)  # the BUILTIN run, never a plugin's
    built: Any = typer.main.get_command(mini)
    # a Typer app holding one command collapses to that command rather than to a group
    command = built.commands["run"] if hasattr(built, "commands") else built
    captured = io.StringIO()
    code = 0
    reason: str | None = None
    redirect: Any = contextlib.redirect_stdout(captured) if json_ else contextlib.nullcontext()
    try:
        with redirect, command.make_context("run", list(argv), parent=None) as sub:
            command.invoke(sub)
    except typer.Exit as exc:  # typer._click.exceptions.Exit — NOT click.exceptions.Exit
        code = exc.exit_code
    except typer.Abort:
        code = EXIT_INTERRUPTED
    except (RayspecError, OSError) as exc:
        code, reason = 2, str(exc)
    outcome = RunOutcome(exit_code=code, reason=reason)
    if json_:
        _read_summary(captured.getvalue(), outcome)
    return outcome


def _read_summary(text: str, outcome: RunOutcome) -> None:
    """Fill ``outcome`` from ``run --json``'s summary object — the last non-empty stdout line."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return
    try:
        payload = jsonlib.loads(lines[-1])
    except ValueError:
        return
    if not isinstance(payload, dict) or set(payload) != run_module.SUMMARY_KEYS:
        return
    outcome.run_id = payload.get("run_id")
    outcome.status = payload.get("status")
    outcome.reason = payload.get("reason")
    with contextlib.suppress(TypeError, ValueError):
        outcome.exit_code = int(payload["exit_code"])


def choose_workflow(
    state: State, project_root: Path
) -> tuple[str | None, ResolvedWorkflow | None, str | None]:
    """``(name, resolved workflow, why not)`` — the workflow the dry run should use.

    ``example`` when the project has one, else the first discovered workflow that loads. The
    config is passed explicitly so a broken ``config.yaml`` stays the row the state block already
    printed rather than becoming an exception here.
    """
    try:
        refs = [ref for ref in discover_workflows(project_root, home=state.home) if not ref.error]
    except RayspecError as exc:
        return None, None, f"{exc} — see `rayspec validate`"
    if not refs:
        return None, None, "no workflow in `.rayspec/workflows/` yet"
    ref = next((r for r in refs if r.name == "example"), refs[0])
    try:
        resolved = load_workflow(
            ref, project_root=project_root, home=state.home, config=state.config
        )
    except RayspecError as exc:
        return ref.name, None, f"{exc} — see `rayspec validate`"
    return ref.name, resolved, None


def required_inputs(resolved: ResolvedWorkflow) -> list[str]:
    """The inputs of ``resolved`` that have to be supplied on the command line."""
    return [name for name, spec in resolved.workflow.inputs.items() if spec.required]


def isolation_of(
    declared: str | None, git: GitState, where: Path, workflow: str | None
) -> Isolation:
    """What the next real run gets, from the workflow document **and** the three git conditions.

    Both scaffolds declare ``isolation: none``, so "you will get a worktree" would be false even
    on a perfect machine; and a repository with no commits cannot produce a worktree at all, so
    ``git init`` on its own is not the whole answer either. Both halves are said where they are
    true.

    With no workflow there is no ``isolation:`` line to read, and ``declared or "worktree"``
    would describe a document that does not exist as one that asked for a worktree per run. So
    that case is answered before the machine is consulted at all.
    """
    named = f"`{workflow}` " if workflow else ""
    if not git.binary:
        return Isolation(
            "blocked",
            False,
            "nothing runs yet — `rayspec run` refuses without git, dry runs included",
        )
    if workflow is None:
        return Isolation(
            "blocked",
            git.worktree_available,
            "nothing to run yet — there is no workflow to read an `isolation:` line from. "
            f"Write one in {where / init.PROJECT_DIR / 'workflows'} "
            "(`rayspec new workflow <name>`).",
        )
    available = git.worktree_available
    if (declared or "worktree") == "none":
        reason = f"in place, in {where} — the {named}workflow declares isolation: none."
        if available:
            reason += " Delete that line and each run gets its own git worktree instead."
        elif not git.repository:
            reason += (
                " Worktree isolation needs one more thing: this directory is not a git"
                " repository. Run `git init` here and drop the isolation: none line to get a"
                " worktree per run."
            )
        else:
            reason += (
                " Worktree isolation needs one more thing: this repository has no commits yet"
                " (a worktree is created from a commit). Commit something and drop the"
                " isolation: none line to get a worktree per run."
            )
        return Isolation("none", available, reason)
    if available:
        return Isolation(
            "worktree",
            True,
            f"its own git worktree, off {where} — the {named}workflow does not declare "
            "isolation: none",
        )
    if not git.repository:
        return Isolation(
            "blocked",
            False,
            f"blocked — {where} is not a git repository and the {named}workflow asks for a "
            "worktree per run. Run `git init` here.",
        )
    return Isolation(
        "blocked",
        False,
        f"blocked — a worktree needs at least one commit and {where} has none yet "
        "(a worktree is created from a commit)",
    )


# --------------------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------------------

#: Doctor rows quickstart renders itself (``python``, ``git``) or answers instead of printing.
_OWN_ROWS: frozenset[str] = frozenset({"python", "git", "rayspec", "uv", "project"})

_LABEL = 14
_STEP_LABEL = 9


def _row(out: Console, glyph: tuple[str, str], label: str, detail: str) -> None:
    out.print(
        Text.assemble("  ", glyph, " ", (f"{label:<{_LABEL}}", "bold"), detail), soft_wrap=True
    )


def _note(out: Console, text: str) -> None:
    out.print(Text(f"    {text}", style="dim"), soft_wrap=True)


def _step_line(out: Console, step_id: str, action: str, detail: str, *, dash: bool = True) -> None:
    label = f"{STEP_LABELS[step_id]:<{_STEP_LABEL}}"
    joined = f"{action} — {detail}" if (detail and dash) else f"{action} {detail}".strip()
    out.print(Text(f"{label}{joined}"), soft_wrap=True)


def print_state(out: Console, state: State, marks: Glyphs) -> None:
    """The state block: python, the three git conditions, both CLIs, both auth rows.

    Every row is a :class:`~rayspec.cli.commands.doctor.Check` doctor produced, rendered — never
    a second answer to the same question. Any *other* row that is not ``ok`` (a broken
    ``config.yaml``, an unreadable ``.env``, an unwritable ``RAYSPEC_HOME``, a failing secret
    source) is printed too: those are the states that stop every other command dead.
    """
    python = state.check("python")
    if python is not None:
        _row(out, marks.of(python.status), "python", python.detail)
    if state.git.binary:
        _row(
            out,
            (marks.ok, "green"),
            "git",
            f"{state.git.version or 'version unknown'} · {state.git.binary}",
        )
    else:
        _row(out, (marks.fail, "red"), "git", "not found on PATH")
        _note(
            out,
            "Consequence: every `rayspec run` refuses — dry runs included. rayspec asks git "
            "which directory it is in before it starts anything.",
        )
        _note(out, f"Install it:  {state.git.install_command}")
    print_repository_row(out, state, marks)
    for check in state.checks:
        if check.id in _OWN_ROWS or check.id.startswith(("claude.", "codex.")):
            continue
        if check.status == "ok":
            continue
        _row(out, marks.of(check.status), check.label, check.detail)
        if check.hint:
            _note(out, f"hint: {check.hint}")
    for provider in state.providers:
        check = state.check(f"{provider.id}.cli")
        if check is not None:
            _row(out, marks.of(check.status), f"{provider.id} CLI", check.detail)
            if check.status != "ok" and check.hint:
                _note(out, f"hint: {check.hint}")
    # the note is indented like every other per-row note, so it has to sit under a row that DID
    # find something — printed after the loop it landed under whichever auth row came last, which
    # is usually the one that just said nothing was found
    last_found = next((p.id for p in reversed(state.providers) if p.credentials), None)
    for provider in state.providers:
        check = state.check(f"{provider.id}.auth")
        if check is None:
            continue
        glyph = (marks.ok, "green") if provider.credentials else (marks.warn, "yellow")
        _row(out, glyph, f"{provider.id} auth", check.detail)
        if provider.id == last_found:
            _note(
                out,
                "credentials found, not checked — `rayspec doctor --probe` is the only thing "
                "that proves a login.",
            )


def print_repository_row(out: Console, state: State, marks: Glyphs) -> None:
    """The second git condition, and the third whenever it is the one in the way."""
    git = state.git
    if not git.binary:
        return
    if not git.repository:
        _row(out, (marks.warn, "yellow"), "repository", f"{state.target} is not a git repository")
        _note(
            out,
            "Consequence: agent runs will edit this directory directly instead of an isolated "
            "copy.",
        )
        return
    if git.commits is True:
        _row(
            out,
            (marks.ok, "green"),
            "repository",
            f"{state.target} is a git repository (worktree isolation available)",
        )
        return
    _row(
        out,
        (marks.warn, "yellow"),
        "repository",
        f"{state.target} is a git repository with no commits yet",
    )
    _note(out, "Consequence: a worktree is created from a commit, so there is none to create.")


def print_menu(err: Console, offered: list[ProviderState]) -> list[str]:
    """Print the login menu, built from what is actually missing; return the answer keys."""
    err.print(
        Text(
            "\nThe whole authoring loop — write, validate, plan, dry-run — needs no login and "
            "costs nothing. Only real agent runs need one.",
            style="dim",
        ),
        soft_wrap=True,
    )
    err.print(Text("\nLog in now?"), soft_wrap=True)
    keys: list[str] = []
    width = max((len(ACCOUNT_OF[p.id]) for p in offered), default=0)
    for provider in offered:
        keys.append(provider.id)
        err.print(
            Text(
                f"  {len(keys)}) {provider.id.capitalize():<8}"
                f"{ACCOUNT_OF[provider.id]:<{width + 3}}{provider.login_command}"
            ),
            soft_wrap=True,
        )
    if len(offered) > 1:
        keys.append("both")
        err.print(Text(f"  {len(keys)}) Both"), soft_wrap=True)
    keys.append("none")
    err.print(Text(f"  {len(keys)}) Not now — dry runs only"), soft_wrap=True)
    return keys


def read_answer(prompt: str) -> str | None:
    """One typed answer, or ``None`` at end of input. A ``KeyboardInterrupt`` is left to raise.

    Deliberately **not** ``typer.prompt`` / ``typer.confirm``: click catches ``KeyboardInterrupt``
    and ``EOFError`` in the same ``except`` and raises one ``Abort`` for both, so a question asked
    through it cannot tell Ctrl-C from end of input. quickstart has to. Ctrl-C is ``quickstart
    interrupted`` and exit 130 with nothing written; end of input is "not now" and the dry run
    still happens. Read through click the two questions had drifted to opposite wrong answers —
    the menu read Ctrl-C as "not now" and said nothing, the ``git init`` question let click's bare
    ``Aborted.`` and exit **1** through, which is the code that means "this machine cannot run
    rayspec".

    The prompt goes to stderr, so stdout stays a clean report; ``input()`` reads the answer, so a
    terminal keeps its line editing and raises the two conditions separately.
    """
    typer.echo(prompt, nl=False, err=True)
    try:
        return input().strip()
    except EOFError:
        return None


def ask_login(err: Console, offered: list[ProviderState]) -> tuple[str, ...]:
    """Ask which provider(s) to log in to.

    An answer outside the menu is re-asked rather than read as "not now": the ``git init``
    question one line later already re-asks, and two adjacent questions with opposite validation
    is how somebody who meant to log in walks away believing they did not need to. The default is
    shown, so Enter is visibly the last entry.
    """
    keys = print_menu(err, offered)
    default = len(keys)  # the last entry is always "Not now"
    while True:
        answer = read_answer(f"> [{default}] ")
        if answer is None:  # end of input: not an answer, and not an interruption either
            return ()
        picked = answer or str(default)
        if picked.isdigit() and 1 <= int(picked) <= len(keys):
            break
        err.print(
            Text(f"Error: answer 1-{default}, or press Enter for {default}", style="red"),
            soft_wrap=True,
        )
    key = keys[int(picked) - 1]
    if key == "none":
        return ()
    if key == "both":
        return tuple(p.id for p in offered)
    return (key,)


def ask_git_init(err: Console, target: Path) -> bool:
    """``Run `git init` in <dir>? [y/N]`` — click's re-ask, without click's Ctrl-C."""
    while True:
        answer = read_answer(f"\nRun `git init` in {target}? [y/N]: ")
        if answer is None:  # end of input: rayspec never creates a repository unasked
            return False
        value = answer.lower()
        if value in {"y", "yes"}:
            return True
        if value in {"", "n", "no"}:
            return False
        err.print(Text("Error: invalid input", style="red"), soft_wrap=True)


def plan_lines(
    state: State,
    offered: list[ProviderState],
    *,
    chosen: Sequence[str],
    yes: bool,
    no_init: bool,
    no_run: bool,
) -> list[tuple[str, str]]:
    """The plan a non-interactive run prints instead of asking: what it would ask, what it does.

    This is the deliberate reading of "change nothing that needs an answer": the steps that need
    no answer still happen, so `rayspec quickstart --no-interactive` in a Dockerfile is a useful
    command rather than a no-op.
    """
    rows: list[tuple[str, str]] = []
    if offered or chosen:
        commands = [
            p.login_command for p in state.providers if p.login_command and not p.credentials
        ]
        text = "would ask; log in yourself:"
        text += "".join(f"\n  {command}" for command in commands)
        text += (
            f"\nor set {' / '.join([*doctor.CLAUDE_AUTH_VARS, doctor.CODEX_AUTH_VAR])} "
            "in ~/.rayspec/.env"
        )
        rows.append(("login", text))
    if state.git.binary and not state.git.repository:
        rows.append(
            (
                "git init",
                "would ask; run it yourself:  git init"
                if not yes
                else "accepted by --yes; running `git init` here",
            )
        )
    rows.append(("project", "kept as it is" if no_init else "will be scaffolded"))
    if no_run:
        rows.append(("dry run", "skipped (--no-run)"))
    elif not state.git.binary:
        # git-lessness is known before the plan is rendered, so "will run" would be a prediction
        # this command already knows is false — and the state block said so twelve lines above
        rows.append(
            ("dry run", "cannot run — `rayspec run` refuses without git, dry runs included")
        )
    else:
        rows.append(("dry run", "will run"))
    return rows


def print_plan(err: Console, rows: Sequence[tuple[str, str]]) -> None:
    """Render :func:`plan_lines`."""
    err.print(Text("\nnot a terminal — nothing will be asked.", style="dim"), soft_wrap=True)
    for label, text in rows:
        first, *rest = text.splitlines()
        err.print(Text(f"  {label:<{_STEP_LABEL}}{first}"), soft_wrap=True)
        for line in rest:
            err.print(Text(f"  {'':<{_STEP_LABEL}}{line}"), soft_wrap=True)
    err.print(Text(""), soft_wrap=True)


def credentials_sentence(state: State) -> str:
    """``claude ok · codex none`` — present or not, and never "verified".

    The qualifier is only added when something WAS found: "present, not checked" over two `none`
    rows says nothing, and the sentence has to stay true in both states.
    """
    parts = " · ".join(f"{p.id} {'ok' if p.credentials else 'none'}" for p in state.providers)
    if any(p.credentials for p in state.providers):
        return f"{parts} — present, not checked (`rayspec doctor --probe` proves a login)"
    return f"{parts} — nothing found here, which is all a dry run needs"


def print_summary(out: Console, outcome: Outcome, *, exit_code: int) -> None:
    """The closing block: isolation, credentials, the four commands, the money line."""
    state = outcome.state
    if exit_code == 0:
        out.print(Text("\nyou are set up.", style="green"), soft_wrap=True)
    else:
        out.print(Text("\nnot finished.", style="yellow"), soft_wrap=True)
    if outcome.isolation is not None:
        out.print(Text(f"  next run: {outcome.isolation.reason}"), soft_wrap=True)
    out.print(Text(f"  credentials: {credentials_sentence(state)}"), soft_wrap=True)
    for provider in state.providers:
        if not provider.credentials and provider.login_command:
            out.print(Text(f"  log in to {provider.id}: {provider.login_command}"), soft_wrap=True)
    if outcome.next_step_lines:
        out.print(Text("\nwhat to do next:"), soft_wrap=True)
        if Path.cwd().resolve() != outcome.project_root:
            out.print(Text(f"  cd {outcome.project_root}", style="dim"), soft_wrap=True)
        for line in outcome.next_step_lines:
            out.print(Text(f"  {line}"), soft_wrap=True)
        # only when one of the listed commands IS a real run: without git the two `rayspec run`
        # rows are not listed at all, and "the last one spends money" would then point at
        # `rayspec plan`
        if any(row["cost"] == "provider" for row in next_step_rows(outcome.next_step_lines)):
            out.print(
                Text(
                    "  the last one calls the provider: it needs a login and it spends money. "
                    "Everything above it is free.",
                    style="dim",
                ),
                soft_wrap=True,
            )
    if not state.git.binary:
        out.print(
            Text(
                f"\nInstall git — {state.git.install_command} — then re-run `rayspec "
                "quickstart`; the scaffold above is already in place and will be kept.",
                style="yellow",
            ),
            soft_wrap=True,
        )


def next_step_rows(lines: Sequence[str]) -> list[dict[str, str]]:
    """``init``'s printed lines, split into ``{command, note, cost}``.

    One wording, two renderings: the text block prints the lines and ``--json`` carries the same
    commands with the money statement in the shape (``cost``), so "a real run spends money"
    survives into the machine-readable form.
    """
    rows: list[dict[str, str]] = []
    for line in lines:
        command, _, note = line.partition("#")
        command = command.strip()
        free = "--dry-run" in command or not command.startswith("rayspec run ")
        rows.append(
            {"command": command, "note": note.strip(), "cost": "free" if free else "provider"}
        )
    return rows


def payload_of(outcome: Outcome, *, exit_code: int) -> dict[str, Any]:
    """The ``--json`` document — one object, keys exactly :data:`QUICKSTART_KEYS`."""
    state = outcome.state
    report = doctor.Report(list(state.checks))
    project = outcome.project or Project(path=state.target / init.PROJECT_DIR, existed=False)
    isolation = outcome.isolation or Isolation("blocked", state.git.worktree_available, "")
    payload: dict[str, Any] = {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "rayspec": __version__,
        "root": str(state.target),
        "project_root": str(outcome.project_root),
        "interactive": outcome.interactive,
        "environment": {
            "python": platform.python_version(),
            "git": state.git.to_dict(),
            "providers": [p.to_dict() for p in state.providers],
        },
        "steps": [outcome.steps[step_id].to_dict() for step_id in STEP_IDS],
        "project": project.to_dict(),
        "run": outcome.dry_run.to_dict(),
        "isolation": isolation.to_dict(),
        "next_steps": next_step_rows(outcome.next_step_lines),
        "doctor": {
            "ok": report.ok,
            "exit_code": report.exit_code,
            "failed_required": [c.id for c in report.failed_required],
        },
    }
    assert set(payload) == QUICKSTART_KEYS, "QUICKSTART_KEYS drifted from the payload"
    return payload


# --------------------------------------------------------------------------------------------------
# the steps
# --------------------------------------------------------------------------------------------------


def do_login(
    outcome: Outcome, chosen: Sequence[str], *, out: Console, json_: bool, interactive: bool
) -> None:
    """Step 4 — log in to each chosen provider, in the order ``claude``, ``codex``.

    **A login never runs without a terminal, whatever the flags.** With ``--no-interactive
    --provider claude`` the exact login command is printed and the step is skipped: that is the
    anti-hang guarantee, because a browser or device-code flow in a container is the one thing
    that must never be started.
    """
    state = outcome.state
    if not chosen:
        outcome.skipped("login", "not now; dry runs need no login")
    elif not interactive:
        commands = [
            p.login_command or f"install the {p.id} CLI" for p in state.providers if p.id in chosen
        ]
        outcome.skipped("login", "not a terminal — log in yourself: " + " · ".join(commands))
    else:
        details: list[str] = []
        actions: list[str] = []
        for provider_id in LOGIN_PROVIDERS:
            if provider_id not in chosen:
                continue
            action, detail = _login_one(outcome, provider_id, out=out, json_=json_)
            actions.append(action)
            details.append(f"{provider_id}: {detail}")
        action = "failed" if "failed" in actions else ("done" if "done" in actions else "skipped")
        outcome.steps["login"] = Step("login", action, " · ".join(details))
    if not json_:
        step = outcome.steps["login"]
        _step_line(out, "login", step.action, step.detail)


def _login_one(outcome: Outcome, provider_id: str, *, out: Console, json_: bool) -> tuple[str, str]:
    """One provider's login: skip it, or print the absolute path and spawn it."""
    state = outcome.state
    provider = next(p for p in state.providers if p.id == provider_id)
    if provider.credentials:
        return "skipped", f"credentials already found ({provider.credentials_source})"
    if not provider.cli_ok or not provider.cli_path:
        return "skipped", provider.cli_problem or f"no usable {provider_id} CLI on this machine"
    argv = login_command(provider_id, provider.cli_path)
    if not json_:
        # the absolute path goes on screen BEFORE the spawn, so it is there whether the login
        # works or not — a user who types `codex login` afterwards must know which binary it is
        out.print(Text(f"  $ {shlex.join(argv)}"), soft_wrap=True)
    try:
        ok, problem = run_login(argv)
    except KeyboardInterrupt:  # Ctrl-C during a login must not lose the dry run
        return "skipped", "login cancelled"
    if not ok:
        detail = f"{problem} — {login_fallback(provider)}"
        return "failed", detail
    row = auth_row_after_login(provider_id, state.config.providers.get(provider_id, {}))
    if row is not None and row.status in doctor.CONFIGURED_AUTH_STATUSES:
        provider.credentials = True
        provider.credentials_source = row.detail
        return "done", f"credentials found ({row.detail})"
    return "failed", "the login command finished, but no credentials were found here"


def do_git_init(outcome: Outcome, consented: bool, *, out: Console, json_: bool, yes: bool) -> None:
    """Step 5 — ``git init``, and only ever after somebody said so.

    ``run_git`` is the one git invoker (``GIT_TERMINAL_PROMPT=0``, ``GIT_PAGER=cat``, ``LC_ALL=C``,
    stdin closed). No ``-b`` and no ``-c``: the user's ``init.defaultBranch`` is theirs.
    """
    state = outcome.state
    target = state.target
    extra: str | None = None
    if state.git.repository:
        outcome.skipped("git_init", "already a git repository")
    elif not state.git.binary:
        outcome.skipped("git_init", "git is not installed")
    elif not consented:
        outcome.skipped(
            "git_init", "not run (rayspec never creates a repository without being asked)"
        )
        extra = "run this yourself if you want isolated runs:  git init"
    else:
        result = run_git(["init"], cwd=target, check=False)
        if result.returncode != 0:
            outcome.failed("git_init", result.stderr or f"git init exited {result.returncode}")
        else:
            first = result.stdout.splitlines()[0] if result.stdout else f"initialised {target}"
            outcome.done("git_init", first + (" (accepted by --yes)" if yes else ""))
            state.git = git_state(state.check("git"), target)
    if not json_:
        step = outcome.steps["git_init"]
        _step_line(out, "git_init", step.action, step.detail)
        if extra is not None:
            out.print(Text(f"{'':<{_STEP_LABEL}}{extra}", style="dim"), soft_wrap=True)


def is_rayspec_home(directory: Path, home: Path) -> bool:
    """Whether ``directory/.rayspec`` IS rayspec's own home rather than a project."""
    return (directory / init.PROJECT_DIR).resolve() == home.resolve()


def project_root_for(target: Path, home: Path) -> Path:
    """:func:`find_project_root` for ``target``, minus the one directory that is not a project.

    ``~/.rayspec`` is RAYSPEC_HOME, and it exists on every machine that has ever performed a run
    (it holds ``projects/``). ``find_project_root`` walks up looking for a ``.rayspec/``
    directory, so from any directory under ``$HOME`` that is not itself a project or a git
    checkout the walk lands on ``$HOME`` — and quickstart then refused to scaffold ``~/myproj``
    because it believed it was already inside a project, writing nothing and reporting success.
    rayspec's home is not a project. When the walk lands on it, quickstart writes where it was
    pointed, which is ``rayspec init``'s rule anyway.
    """
    root = find_project_root(target)
    return target if root != target and is_rayspec_home(root, home) else root


def existing_project(target: Path, project_root: Path, *, home: Path) -> Path | None:
    """The ``.rayspec/`` project ``target`` is already part of, or ``None``.

    A refinement over ``rayspec init``, whose ``--root`` writes exactly where it is pointed and
    would happily nest a second project inside an existing one. A first-run command must not:
    "an existing project is respected" has to mean the enclosing one too — but never
    ``rayspec_home()``, which is not a project and is above every directory in ``$HOME``.
    """
    if (target / init.PROJECT_DIR).is_dir() and not is_rayspec_home(target, home):
        return target
    if (
        project_root != target
        and (project_root / init.PROJECT_DIR).is_dir()
        and not is_rayspec_home(project_root, home)
    ):
        return project_root
    return None


def do_scaffold(
    outcome: Outcome,
    *,
    kind: init.TemplateKind | None,
    no_init: bool,
    no_skill: bool,
    out: Console,
    json_: bool,
) -> Path:
    """Step 6 — scaffold ``.rayspec/`` (never with ``--force``), and return the project root."""
    state = outcome.state
    target = state.target
    enclosing = existing_project(target, state.project_root, home=state.home)
    if enclosing is not None:
        detail = (
            f"{enclosing / init.PROJECT_DIR} already exists; nothing was written"
            if enclosing == target
            else f"this directory is inside the rayspec project at {enclosing} "
            f"(its {init.PROJECT_DIR}/); nothing was written here"
        )
        outcome.skipped("scaffold", detail)
        outcome.project = Project(
            path=enclosing / init.PROJECT_DIR, existed=True, kind=init.detect_kind(enclosing)
        )
        if not json_:
            _step_line(out, "scaffold", "skipped", detail)
        return enclosing
    if no_init:
        outcome.skipped("scaffold", "not scaffolded (--no-init)")
        outcome.project = Project(path=target / init.PROJECT_DIR)
        if not json_:
            _step_line(out, "scaffold", "skipped", "not scaffolded (--no-init)")
        return state.project_root
    # `rayspec init --kind content` is what the non-git warning already tells a user to run;
    # quickstart resolves that question instead of printing it, and says which flavour it chose.
    chosen = kind.value if kind is not None else ("code" if state.git.repository else "content")
    project = Project(path=target / init.PROJECT_DIR, kind=chosen)
    outcome.project = project
    if not json_:
        why = (
            "(--kind)"
            if kind is not None
            else ("(a git checkout)" if state.git.repository else "(not a git checkout)")
        )
        _step_line(out, "scaffold", "scaffolding", f"{project.path} · {chosen} {why}", dash=False)
    try:
        results = init.scaffold(target, kind=chosen, force=False)  # never --force
    except OSError as exc:
        outcome.failed("scaffold", f"cannot write the scaffold: {exc}")
        if not json_:
            err_console().print(Text(f"error: cannot write the scaffold: {exc}", style="red"))
        return state.project_root
    project.files_written = sum(1 for r in results if r.action != "skipped")
    if not json_:
        for item in results:
            verb = "created" if item.action == "created" else "exists "
            out.print(Text.assemble((verb, "green"), "  ", item.relative), soft_wrap=True)
        out.print(
            Text(f"{chosen} scaffold in {project.path}: {project.files_written} file(s) written"),
            soft_wrap=True,
        )
    if not no_skill:
        for skill in SKILLS:
            try:
                written = install_skill(skill, project_skill_dir(skill, target), force=False)
            except OSError as exc:
                outcome.failed("scaffold", f"cannot write the skill: {exc}")
                continue
            project.skills_written += sum(1 for r in written if r.action != "skipped")
            if not json_:
                print_install_result(written, project_skill_dir(skill, target), label="project")
    if outcome.steps["scaffold"].action != "failed":
        outcome.done(
            "scaffold",
            f"{chosen} scaffold in {project.path}: {project.files_written} file(s) written",
        )
    return target


def _has_cases(project_root: Path) -> bool:
    """Whether ``rayspec test`` has anything to run in ``project_root``.

    Asked of :func:`~rayspec.testing.spec.discover_suites` — the same discovery the command
    itself performs — rather than of a list of directories, so a project whose cases live in a
    root ``checks.yaml`` with no ``.rayspec/tests/`` at all answers yes, and a place a case may
    live in future is covered without this being edited. A malformed case file counts as a case:
    `rayspec test` will run and report the problem, which is a next step that works.
    """
    from rayspec.testing.spec import CaseFileError, discover_suites

    try:
        return any(suite.checks for suite in discover_suites(project_root))
    except (CaseFileError, OSError):
        return True


def do_dry_run(
    outcome: Outcome,
    *,
    project_root: Path,
    no_run: bool,
    no_init: bool,
    out: Console,
    json_: bool,
) -> None:
    """Steps 7 + 8 — pick a workflow, say why not when there is none, otherwise prove it works."""
    state = outcome.state
    dry = outcome.dry_run
    name, resolved, problem = choose_workflow(state, project_root)
    dry.workflow = name
    kind = outcome.project.kind if outcome.project else None
    stubs = project_root / init.PROJECT_DIR / "stubs" / f"{name}.yaml" if name is not None else None
    has_stubs = stubs is not None and stubs.is_file()
    if name is not None:
        lines = init.next_steps(
            kind or "code",
            skill=False,
            doctor=False,
            workflow=name,
            # the same token the executed argv gets, so the line below the run can be copied
            stubs=stubs_argument(stubs, project_root) if has_stubs and stubs else False,
            # quickstart, unlike `rayspec init`, can stand in a project it did not scaffold, so
            # the case it would name may not exist. `rayspec test` exits 2 there.
            tests=_has_cases(project_root),
        )
        if not state.git.binary:
            # `rayspec run` refuses without git, dry runs included — the state block said so and
            # the isolation sentence says so again. Listing it as a next step three lines later
            # hands a first-time user a list where half the items exit 2.
            lines = [line for line in lines if not line.startswith("rayspec run ")]
        outcome.next_step_lines = lines
    outcome.isolation = isolation_of(
        resolved.workflow.isolation if resolved is not None else None,
        state.git,
        project_root,
        name,
    )

    reason: str | None = None
    if no_run:
        reason = "not run (--no-run)"
    elif not state.git.binary:
        reason = "`rayspec run` refuses without git — dry runs included (see above)"
        dry.blocking = True
    elif problem is not None:
        # no workflow at all, or one that does not load: quickstart set out to prove the install
        # and could not. Unless the caller declined the project, that is not a finished run.
        reason = problem
        dry.blocking = not no_init
    elif resolved is not None and (needed := required_inputs(resolved)):
        placeholders = " ".join(f"-i {n}=..." for n in needed)
        reason = f"`{name}` needs {placeholders}"
        dry.command = f"rayspec run {name} {placeholders} --dry-run"
    if reason is not None or resolved is None:
        dry.attempted = False
        dry.blocking = dry.blocking or reason is None
        dry.skipped_reason = reason or "no workflow to run"
        outcome.skipped("dry_run", dry.skipped_reason)
        if not json_:
            _step_line(out, "dry_run", "skipped", dry.skipped_reason)
            if dry.command:
                out.print(Text(f"{'':<{_STEP_LABEL}}{dry.command}", style="dim"), soft_wrap=True)
        return

    argv = dry_run_argv(
        name or "", project_root=project_root, stubs=stubs if has_stubs else None, json_=json_
    )
    dry.attempted = True
    dry.command = printed_command(argv)
    if not json_:
        out.print(
            Text("\ndry run — scripted agents, no login, no cost:", style="bold"), soft_wrap=True
        )
        out.print(Text(f"  $ {dry.command}"), soft_wrap=True)
    result = invoke_run(argv, json_=json_)
    dry.run_id = result.run_id
    dry.status = result.status
    dry.exit_code = result.exit_code
    dry.reason = result.reason
    detail = f"run {result.run_id} {result.status}" if result.run_id else f"exit {result.exit_code}"
    if result.exit_code == 0:
        outcome.done("dry_run", detail)
    else:
        outcome.failed("dry_run", result.reason or detail)
        if not json_:
            _step_line(out, "dry_run", "failed", result.reason or detail)
            if result.run_id:
                out.print(
                    Text(f"{'':<{_STEP_LABEL}}rayspec logs {result.run_id}", style="dim"),
                    soft_wrap=True,
                )
            else:
                out.print(
                    Text(f"{'':<{_STEP_LABEL}}rayspec logs <run>", style="dim"), soft_wrap=True
                )


# --------------------------------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    @app.command()
    def quickstart(  # noqa: PLR0917 - Typer options are positional by construction
        provider: Annotated[
            LoginTarget | None,
            typer.Option(
                "--provider",
                help="Which provider(s) to offer a login for: claude, codex, both or none. "
                "Unlike `rayspec doctor --provider ID` — which restricts the *checks* over every "
                "registration, plugins included — this chooses a *login flow*, and one exists "
                "only for the two bundled CLIs. Default: ask on a terminal, `none` off one.",
                show_default=False,
            ),
        ] = None,
        yes: Annotated[
            bool,
            typer.Option(
                "--yes",
                "-y",
                help="Accept every yes/no offer (today: `git init`). It does not pick a "
                "provider: a login is a browser round-trip, and --yes may not choose whose "
                "account you sign in with.",
            ),
        ] = False,
        no_interactive: Annotated[
            bool, typer.Option("--no-interactive", help="Never ask; print the plan instead.")
        ] = False,
        kind: Annotated[
            init.TemplateKind | None,
            typer.Option(
                "--kind",
                help="Scaffold flavour. Default: `code` inside a git repository, `content` "
                "outside one.",
                show_default=False,
            ),
        ] = None,
        no_init: Annotated[
            bool, typer.Option("--no-init", help="Do not scaffold `.rayspec/`.")
        ] = False,
        no_run: Annotated[
            bool, typer.Option("--no-run", help="Do not perform the free dry run at the end.")
        ] = False,
        no_skill: Annotated[
            bool,
            typer.Option("--no-skill", help="Do not write the coding-agent skills to `.claude/`."),
        ] = False,
        json_: JsonOption = False,
        output: OutputOption = None,
        root: RootOption = None,
    ) -> None:
        """First run on a fresh machine: check the environment, offer a login and `git init`,
        scaffold a project, and prove it with a dry run that needs no login and costs nothing."""
        json_ = resolve_output(output, json_)
        # `init`'s rule, not the walk-up: quickstart writes where it is pointed. A `--root` that
        # is not a directory is a usage error and is never created.
        target = (checked_root(root) or Path.cwd()).resolve()
        refuse_rayspec_home(target)
        try:
            outcome, code = _quickstart(
                target=target,
                provider=provider,
                yes=yes,
                no_interactive=no_interactive,
                kind=kind,
                no_init=no_init,
                no_run=no_run,
                no_skill=no_skill,
                json_=json_,
            )
        except KeyboardInterrupt:
            err_console().print(Text("quickstart interrupted", style="yellow"))
            raise typer.Exit(code=EXIT_INTERRUPTED) from None
        if json_:
            print_json(payload_of(outcome, exit_code=code))
        raise typer.Exit(code=code)


def refuse_rayspec_home(target: Path) -> None:
    """Refuse to scaffold ``$HOME`` itself — a first-run command must not create that trap.

    The README's getting-started is two lines and a user who has just opened a terminal is
    standing in ``$HOME``. Scaffolded there, ``.rayspec/`` **is** RAYSPEC_HOME: the project and
    rayspec's own home become the same directory (the policy row prints the same path twice),
    the skills land in the user's *global* ``~/.claude/skills/`` rather than a project's, and
    from then on every directory under ``$HOME`` walks up and finds that project, so quickstart
    is a permanent no-op there. Nothing is written and the way out is one line.
    """
    home = rayspec_home()
    if target != Path.home().resolve() and not is_rayspec_home(target, home):
        return
    fail(
        f"{target} is your home directory, not a project — {target / init.PROJECT_DIR} is where "
        "rayspec keeps its own state (runs, logs, home-scope workflows), and scaffolding a "
        "project on top of it makes every directory below it part of that project",
        hint="make a directory first:  mkdir myproj && cd myproj && rayspec quickstart",
    )


def _quickstart(
    *,
    target: Path,
    provider: LoginTarget | None,
    yes: bool,
    no_interactive: bool,
    kind: init.TemplateKind | None,
    no_init: bool,
    no_run: bool,
    no_skill: bool,
    json_: bool,
) -> tuple[Outcome, int]:
    """Collect, ask, then do — in that order, so ``--no-interactive`` is a pure plan mode.

    Every question is asked first and the plan is executed afterwards; nothing alternates
    question, work, question, work.
    """
    marks = Glyphs.current()
    out = console()
    err = err_console()
    state = collect_state(target)
    # `--json` implies non-interactive (`rayspec cancel`'s precedent, where `--json` waives the
    # confirmation): a machine caller cannot answer, and a first-run report is not a mid-run gate.
    interactive = runs_common.stdin_is_tty() and not no_interactive and not json_
    outcome = Outcome(state=state, interactive=interactive, project_root=state.project_root)

    if not json_:
        out.print(Text(f"rayspec {__version__} — let's get you running.\n"), soft_wrap=True)
        print_state(out, state, marks)

    # ---- step 3: ask everything, before anything is done ---------------------------------
    chosen: tuple[str, ...] = provider.ids() if provider is not None else ()
    offered = [p for p in state.providers if not p.credentials and p.cli_ok and p.cli_path]
    consented = False
    if interactive:
        if provider is None and not yes and offered:
            chosen = ask_login(err, offered)
        if state.git.binary and not state.git.repository:
            consented = yes or ask_git_init(err, target)
        if not json_:
            out.print(Text(""), soft_wrap=True)
    else:
        consented = yes
        if not json_:
            print_plan(
                err,
                plan_lines(state, offered, chosen=chosen, yes=yes, no_init=no_init, no_run=no_run),
            )

    # ---- steps 4 to 8 --------------------------------------------------------------------
    do_login(outcome, chosen, out=out, json_=json_, interactive=interactive)
    do_git_init(outcome, consented, out=out, json_=json_, yes=yes)
    project_root = do_scaffold(
        outcome, kind=kind, no_init=no_init, no_skill=no_skill, out=out, json_=json_
    )
    outcome.project_root = project_root
    do_dry_run(
        outcome, project_root=project_root, no_run=no_run, no_init=no_init, out=out, json_=json_
    )

    # The exit code says whether quickstart finished and nothing is broken — not whether
    # somebody logged in. A failed login is never exit 1: the machine is fine, the account is
    # not. Every OTHER failed step is, and so is a dry run that never happened for a reason
    # nobody chose: a scaffold that could not be written, or a project with no workflow in it,
    # used to print a hard `error:` line, run nothing, and then say "you are set up." with exit
    # 0 and `"ok": true` — which is the one thing a first-run command must never get wrong.
    failed_step = any(
        step.action == "failed" for sid, step in outcome.steps.items() if sid != "login"
    )
    blocked = not state.git.binary
    code = 1 if (failed_step or blocked or outcome.dry_run.blocking) else 0

    # ---- step 9 --------------------------------------------------------------------------
    if not json_:
        print_summary(out, outcome, exit_code=code)
    return outcome, code


__all__ = [
    "ACCOUNT_OF",
    "LOGIN_PROVIDERS",
    "OS_RELEASE_PATH",
    "QUICKSTART_KEYS",
    "STEP_IDS",
    "DryRun",
    "GitState",
    "Glyphs",
    "Isolation",
    "LoginTarget",
    "Outcome",
    "Project",
    "ProviderState",
    "RunOutcome",
    "State",
    "Step",
    "ask_git_init",
    "ask_login",
    "auth_row_after_login",
    "choose_workflow",
    "collect_state",
    "dry_run_argv",
    "existing_project",
    "git_install_command",
    "git_state",
    "invoke_run",
    "is_rayspec_home",
    "isolation_of",
    "login_command",
    "login_fallback",
    "next_step_rows",
    "payload_of",
    "printed_command",
    "project_root_for",
    "provider_state",
    "read_answer",
    "refuse_rayspec_home",
    "register",
    "required_inputs",
    "run_login",
    "stubs_argument",
]

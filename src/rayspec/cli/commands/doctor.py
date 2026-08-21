# SPDX-License-Identifier: Apache-2.0
"""`rayspec doctor [--probe] [--provider ID]... [--json] [--root DIR]` — health checks.

Boundary: CLI diagnostics only. SDK modules are imported lazily (``importlib``), binaries are
located the way the SDKs do, version probes are tolerant subprocess calls, the pricing nudge
(``<id>.pricing``) reads config + the registry's capability tables, and ``--probe`` delegates
to each provider's ``healthcheck(probe=True)`` through the registry. Nothing here is imported
by the engine or the providers.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, Literal

import anyio
import typer
from rich.table import Table
from rich.text import Text

from rayspec import __version__
from rayspec.actor import ACTOR_ENV
from rayspec.cli.commands import _pricing_common as pricing
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    console,
    fail,
    resolve_output,
)
from rayspec.config import (
    Config,
    load_config,
    load_env,
    parse_env_text,
    project_env_info,
    rayspec_home,
)
from rayspec.errors import RayspecError
from rayspec.loader import discover_workflows, find_project_root
from rayspec.procenv import env_file_origin
from rayspec.providers.registry import create_provider, get_registration, list_registrations

CheckStatus = Literal["ok", "warn", "fail", "info"]

#: Minimum interpreter the package declares (``requires-python``).
MIN_PYTHON: tuple[int, int] = (3, 11)
#: Seconds a ``--version`` probe may take before it is reported as unknown.
VERSION_TIMEOUT_S = 5.0
#: Outer bound for one provider probe (the adapters bound their own turn at 120 s).
PROBE_TIMEOUT_S = 180.0
#: Outer bound on ``provider.aclose()`` after a probe (hung adapters must not block the report).
CLOSE_TIMEOUT_S = 15.0
#: Credential variables the Claude adapter recognises (else the CLI's own login is used).
CLAUDE_AUTH_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
#: Credential variable the Codex adapter recognises (else the ``codex login`` state is used).
CODEX_AUTH_VAR = "OPENAI_API_KEY"
#: Where the ``claude`` CLI keeps its claude.ai login (plus the macOS keychain item).
CLAUDE_CREDENTIALS_HINT = "~/.claude/.credentials.json or the macOS keychain"
#: Auth-row statuses that mean "credentials were found" (the provider counts as configured).
CONFIGURED_AUTH_STATUSES: frozenset[str] = frozenset({"ok", "info"})

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_STATUS_MARK: Mapping[str, str] = {
    "ok": "[green]ok[/green]",
    "warn": "[yellow]warn[/yellow]",
    "fail": "[red]FAIL[/red]",
    "info": "[dim]info[/dim]",
}


@dataclass(frozen=True, slots=True)
class Check:
    """One doctor row. ``required`` rows decide the exit code when they ``fail``."""

    id: str
    label: str
    status: CheckStatus
    detail: str
    required: bool = False
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON shape of ``--json``: the fields in declaration order (``id, label, status, detail,
        required, hint``)."""
        return asdict(self)


@dataclass(slots=True)
class Report:
    """All checks of one ``rayspec doctor`` invocation."""

    checks: list[Check]

    @property
    def failed_required(self) -> list[Check]:
        """Required checks that failed (empty ⇒ exit 0)."""
        return [c for c in self.checks if c.required and c.status == "fail"]

    @property
    def ok(self) -> bool:
        return not self.failed_required

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "checks": [c.to_dict() for c in self.checks],
        }


# --------------------------------------------------------------------------------------------------
# tolerant helpers (monkeypatched by the tests)
# --------------------------------------------------------------------------------------------------


def version_of(cmd: list[str], *, timeout_s: float = VERSION_TIMEOUT_S) -> str | None:
    """Run ``cmd`` (e.g. ``[claude, -v]``); return its first output line, ``None`` on any failure.

    Never raises: a missing binary, a missing exec bit, a timeout or a non-zero exit all yield
    ``None`` (the caller reports "version unknown").
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            if line.strip():
                return line.strip()
    return None


def parse_version(text: str | None) -> str | None:
    """The first ``x.y.z`` in ``text`` (``None`` when absent)."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def _import(name: str) -> ModuleType | None:
    """``importlib.import_module(name)`` or ``None`` (import errors included)."""
    try:
        return importlib.import_module(name)
    except Exception:  # ImportError, or an SDK that explodes at import time
        return None


def _module_version(module: ModuleType, dist: str) -> str:
    version = getattr(module, "__version__", None)
    if version:
        return str(version)
    try:
        from importlib.metadata import version as dist_version

        return dist_version(dist)
    except Exception:
        return "unknown version"


def _cli_detail(path: str, version: str | None, source: str) -> str:
    shown = version if version else "version unknown"
    return f"{path} · {shown} ({source})"


# --------------------------------------------------------------------------------------------------
# environment checks
# --------------------------------------------------------------------------------------------------


def _python_check() -> Check:
    version = platform.python_version()
    detail = f"{version} ({sys.executable})"
    if sys.version_info[:2] >= MIN_PYTHON:
        return Check("python", "python", "ok", detail, required=True)
    return Check(
        "python",
        "python",
        "fail",
        detail,
        required=True,
        hint=f"rayspec needs Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
    )


def _rayspec_check() -> Check:
    import rayspec

    return Check("rayspec", "rayspec", "ok", f"{__version__} ({Path(rayspec.__file__).parent})")


def _home_check(home: Path) -> Check:
    if home.exists():
        if home.is_dir() and os.access(home, os.W_OK):
            return Check("home", "RAYSPEC_HOME", "ok", f"{home} (exists)", required=True)
        return Check(
            "home",
            "RAYSPEC_HOME",
            "fail",
            f"{home} is not a writable directory",
            required=True,
            hint="set RAYSPEC_HOME to a writable directory (default ~/.rayspec)",
        )
    parent = home
    while not parent.exists() and parent.parent != parent:
        parent = parent.parent
    if os.access(parent, os.W_OK):
        return Check(
            "home",
            "RAYSPEC_HOME",
            "ok",
            f"{home} (not created yet; first run creates it)",
            required=True,
        )
    return Check(
        "home",
        "RAYSPEC_HOME",
        "fail",
        f"{home} cannot be created ({parent} is not writable)",
        required=True,
        hint="set RAYSPEC_HOME to a writable directory (default ~/.rayspec)",
    )


def _config_check(project_root: Path, home: Path) -> tuple[Check, Config]:
    try:
        config = load_config(project_root, home=home)
    except RayspecError as exc:
        return (
            Check("config", "config", "fail", str(exc), required=True, hint=exc.hint),
            Config(),
        )
    sources = [
        p.as_posix()
        for p in (home / "config.yaml", project_root / ".rayspec" / "config.yaml")
        if p.is_file()
    ]
    shown = " + ".join(sources) if sources else "built-in defaults (no config.yaml)"
    detail = f"default_provider {config.default_provider} · {shown}"
    return Check("config", "config", "ok", detail, required=True), config


def _project_check(start: Path, project_root: Path, home: Path) -> Check:
    if not (project_root / ".rayspec").is_dir():
        return Check(
            "project",
            "project",
            "warn",
            f"no .rayspec/ at or above {start}",
            hint="run `rayspec init` to scaffold one (workflows, agents, prompts, config, stubs)",
        )
    try:
        refs = discover_workflows(project_root, home=home)
    except RayspecError as exc:
        return Check("project", "project", "warn", f"{project_root} · {exc}")
    broken = [r.name for r in refs if r.error]
    detail = f"{project_root} · {len(refs)} workflow(s)"
    if broken:
        detail += f" ({len(broken)} not loadable: {', '.join(broken)})"
        return Check("project", "project", "warn", detail, hint="see `rayspec validate`")
    return Check("project", "project", "ok", detail)


def _home_env_check(home: Path) -> Check | None:
    """``home .env`` row: ``$RAYSPEC_HOME/.env`` exists and is applied by EVERY command.

    That is the point of the row. This file is loaded by every rayspec invocation, and
    ``$RAYSPEC_HOME`` is exported into every workflow step — so a step that writes it changes the
    environment of your later commands, and until you look, nothing says it is there. ``None``
    when the file is absent.
    """
    path = home / ".env"
    if not path.is_file():
        return None
    try:
        count = len(parse_env_text(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        count = 0
    detail = f"{path} ({count} var{'s' if count != 1 else ''}, applied by every command)"
    hint = None
    if env_file_origin(ACTOR_ENV) == str(path):
        detail += f" — including {ACTOR_ENV}"
        hint = f"{ACTOR_ENV} here is not used as an identity: a workflow step can write this file"
        return Check("home.env", "home .env", "warn", detail, hint=hint)
    return Check("home.env", "home .env", "info", detail)


def _project_env_check(project_root: Path) -> Check | None:
    """``project .env`` row: the checkout's ``.rayspec/.env`` exists — it is NOT loaded by
    ``doctor`` (or any inspection command); only ``run``/``resume``/``approve``/``reject`` apply
    it. ``None`` when the file is absent."""
    info = project_env_info(project_root)
    if info is None:
        return None
    detail = (
        f"{info.path} ({info.count} var{'s' if info.count != 1 else ''}, applied only by "
        "run/resume/approve/reject)"
    )
    return Check("project.env", "project .env", "info", detail)


def _tool_check(
    check_id: str, name: str, *, required: bool, version_args: list[str], hint: str
) -> Check:
    path = shutil.which(name)
    if path is None:
        status: CheckStatus = "fail" if required else "warn"
        return Check(check_id, name, status, "not found on PATH", required=required, hint=hint)
    version = version_of([path, *version_args])
    detail = f"{path} · {version}" if version else f"{path} · version unknown"
    return Check(check_id, name, "ok", detail, required=required)


def _secrets_check(config: Config, project_root: Path) -> Check | None:
    """``secrets`` row: the configured sources and whether each resolves — **never** a
    value. ``None`` when ``config.secrets`` is empty.

    Resolution is attempted so a broken source (a missing variable, a world-readable file, a
    helper that is not installed) shows up here rather than at the start of a run; the failure
    text comes from the source itself and names ``secrets.<NAME>``, not the secret. A failing
    row is ``required``: `rayspec run` refuses to start on it, so `rayspec doctor`
    must exit non-zero too — otherwise a preflight that checks the exit code reads "all required
    checks passed" while a red FAIL row sits above it.

    A value shorter than :data:`~rayspec.redact.MIN_REDACTABLE_LEN` is flagged as well: it is
    NOT redacted (a two-character needle would rewrite every log), so the user has to know.
    """
    if not config.secrets:
        return None
    from rayspec.redact import MIN_REDACTABLE_LEN
    from rayspec.secrets import SecretError, provider_for

    provider = provider_for(config, base_dir=project_root)
    rows: list[str] = []
    status: CheckStatus = "ok"
    for name, source in provider.describe():
        try:
            value = provider.get(name)
        except SecretError as exc:
            rows.append(f"{name} ← {source} (FAILED: {exc})")
            status = "fail"
            continue
        if value is None:
            rows.append(f"{name} ← {source} (absent, optional)")
        elif len(value) < MIN_REDACTABLE_LEN:
            rows.append(f"{name} ← {source} (too short to redact — it WILL appear in logs)")
            status = "warn" if status == "ok" else status
        else:
            rows.append(f"{name} ← {source}")
    detectors = config.redact.resolved_detectors()
    detail = " · ".join(rows)
    detail += f" · redact detectors: {', '.join(detectors) if detectors else 'off (default)'}"
    return Check(
        "secrets",
        "secrets",
        status,
        detail,
        hint="values are never shown, never persisted and reach shell/python steps only",
        required=status == "fail",
    )


def environment_checks(
    *, start: Path, project_root: Path, home: Path
) -> tuple[list[Check], Config]:
    """Python, rayspec, ``RAYSPEC_HOME``, config, project detection, ``git`` and ``uv``."""
    config_check, config = _config_check(project_root, home)
    checks = [
        _python_check(),
        _rayspec_check(),
        _home_check(home),
        config_check,
        _project_check(start, project_root, home),
        *filter(None, [_home_env_check(home)]),
        *filter(None, [_project_env_check(project_root)]),
        *filter(None, [_secrets_check(config, project_root)]),
        _tool_check(
            "git",
            "git",
            required=True,
            version_args=["--version"],
            hint="install git: worktree isolation, project slugs and --repo need it",
        ),
        _tool_check(
            "uv",
            "uv",
            required=False,
            version_args=["--version"],
            hint="optional: uv installs/updates rayspec (https://docs.astral.sh/uv/)",
        ),
    ]
    return checks, config


# --------------------------------------------------------------------------------------------------
# claude
# --------------------------------------------------------------------------------------------------


def known_claude_locations() -> tuple[Path, ...]:
    """Where the Claude Agent SDK looks for a system ``claude`` after the bundled one and PATH."""
    home = Path.home()
    if platform.system() == "Windows":
        return (home / ".local/bin/claude.exe",)
    return (
        home / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        home / ".local/bin/claude",
        home / "node_modules/.bin/claude",
        home / ".yarn/bin/claude",
        home / ".claude/local/claude",
    )


def find_claude_cli(
    sdk: ModuleType | None, settings: Mapping[str, Any]
) -> tuple[str, str] | Check | None:
    """``(path, source)`` of the ``claude`` binary: config ``cli_path`` → bundled → PATH → known.

    ``None`` when nothing is found; a failed :class:`Check` when the configured path is missing.
    """
    configured = settings.get("cli_path")
    if configured:
        path = str(configured)
        if os.path.exists(path):
            return path, "config providers.claude.cli_path"
        return Check(
            "claude.cli",
            "claude CLI",
            "fail",
            f"{path} does not exist (config providers.claude.cli_path)",
            required=True,
            hint="fix providers.claude.cli_path or remove it to use the bundled CLI",
        )
    # Windows discovery is best-effort only (not a supported target yet): npm installs a
    # `claude.cmd` shim that `which("claude.exe")` misses, and the SDK prefers a native exe.
    binary = "claude.exe" if platform.system() == "Windows" else "claude"
    sdk_file = getattr(sdk, "__file__", None) if sdk is not None else None
    if sdk_file:
        bundled = Path(sdk_file).parent / "_bundled" / binary
        if bundled.is_file():
            return str(bundled), "bundled with claude-agent-sdk"
    which = shutil.which(binary)
    if which:
        return which, "PATH"
    for location in known_claude_locations():
        if location.is_file():
            return str(location), "known install location"
    return None


def claude_login_source() -> str | None:
    """Evidence of the ``claude`` CLI's own login (``~/.claude/.credentials.json`` or, on macOS,
    the ``Claude Code-credentials`` keychain item), as reported by the Claude adapter's
    ``cli_login_source`` — existence only, never the secret. ``None`` when nothing is found or
    the adapter is not importable (no SDK). Module-level so tests can monkeypatch it."""
    adapter = _import("rayspec.providers.claude")
    if adapter is None:
        return None
    try:
        return adapter.cli_login_source()
    except Exception:  # a diagnosis must never crash on a lookup
        return None


def claude_checks(settings: Mapping[str, Any]) -> list[Check]:
    """SDK import + version, CLI path + ``-v`` probe, auth state for the Claude adapter (env
    key, else the CLI's own login)."""
    sdk = _import("claude_agent_sdk")
    checks: list[Check] = []
    if sdk is None:
        checks.append(
            Check(
                "claude.sdk",
                "claude SDK",
                "fail",
                "claude-agent-sdk is not importable",
                required=True,
                hint="reinstall rayspec (uv tool install --reinstall rayspec) or "
                "pip install claude-agent-sdk",
            )
        )
    else:
        detail = f"claude-agent-sdk {_module_version(sdk, 'claude-agent-sdk')}"
        cli_version_mod = _import("claude_agent_sdk._cli_version")
        bundled_version = getattr(cli_version_mod, "__cli_version__", None)
        if bundled_version:
            detail += f" · bundled CLI {bundled_version}"
        checks.append(Check("claude.sdk", "claude SDK", "ok", detail, required=True))

    found = find_claude_cli(sdk, settings)
    if found is None:
        checks.append(
            Check(
                "claude.cli",
                "claude CLI",
                "fail",
                "claude binary not found (bundled, PATH, known locations)",
                required=True,
                hint="reinstall claude-agent-sdk (it bundles the CLI), `npm install -g "
                "@anthropic-ai/claude-code`, or set providers.claude.cli_path in config.yaml",
            )
        )
    elif isinstance(found, Check):
        checks.append(found)
    else:
        path, source = found
        version = parse_version(version_of([path, "-v"]))
        if version is None:
            checks.append(
                Check(
                    "claude.cli",
                    "claude CLI",
                    "warn",
                    _cli_detail(path, None, source),
                    required=True,
                    hint="`claude -v` did not report a version (not executable? run it by hand)",
                )
            )
        else:
            checks.append(
                Check(
                    "claude.cli",
                    "claude CLI",
                    "ok",
                    _cli_detail(path, version, source),
                    required=True,
                )
            )

    auth_var = next((v for v in CLAUDE_AUTH_VARS if os.environ.get(v)), None)
    if auth_var:
        checks.append(Check("claude.auth", "claude auth", "ok", f"via {auth_var}"))
    elif login := claude_login_source():
        checks.append(Check("claude.auth", "claude auth", "ok", login))
    else:
        checks.append(
            Check(
                "claude.auth",
                "claude auth",
                "warn",
                "login state unknown (no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN, no "
                f"{CLAUDE_CREDENTIALS_HINT}; a `claude` login elsewhere is still used)",
                hint=f"log in once with `claude`, set a key in ~/.rayspec/.env{VERIFY_WITH_PROBE}",
            )
        )
    return checks


# --------------------------------------------------------------------------------------------------
# codex
# --------------------------------------------------------------------------------------------------


def find_codex_cli(settings: Mapping[str, Any]) -> tuple[str, str] | Check:
    """``(path, source)`` of the ``codex`` binary, or the failed :class:`Check` saying why not."""
    configured = settings.get("codex_bin")
    if configured:
        path = str(configured)
        if os.path.exists(path):
            return path, "config providers.codex.codex_bin"
        return Check(
            "codex.cli",
            "codex CLI",
            "fail",
            f"{path} does not exist (config providers.codex.codex_bin)",
            required=True,
            hint="fix providers.codex.codex_bin or remove it to use the bundled runtime",
        )
    runtime = _import("codex_cli_bin")
    if runtime is None:
        return Check(
            "codex.cli",
            "codex CLI",
            "fail",
            "bundled codex runtime (openai-codex-cli-bin) is not installed",
            required=True,
            hint="pip install openai-codex (pulls openai-codex-cli-bin) or set "
            "providers.codex.codex_bin in config.yaml",
        )
    try:
        return str(runtime.bundled_codex_path()), "bundled with openai-codex-cli-bin"
    except Exception as exc:  # FileNotFoundError: wheel without a binary for this platform
        return Check(
            "codex.cli",
            "codex CLI",
            "fail",
            f"bundled codex runtime is broken: {exc}",
            required=True,
            hint="reinstall openai-codex-cli-bin or set providers.codex.codex_bin",
        )


VERIFY_WITH_PROBE = ", or verify with --probe"
"""Suffix of an ``<id>.auth`` login hint; dropped when the hint is reused after a failed probe."""


def codex_login_hint(cli_path: str | None) -> str:
    """How to log in to Codex on *this* machine: ``codex login`` when ``codex`` is on
    ``PATH``, else the bundled binary by its full path (it is not on ``PATH``), or the API key."""
    if shutil.which("codex") is not None:
        command = "run `codex login`"
    elif cli_path:
        command = f"run `{cli_path} login` (the bundled codex; it is not on PATH)"
    else:
        command = "install the codex runtime and run `codex login`"
    return f"{command}, or set {CODEX_AUTH_VAR} in ~/.rayspec/.env{VERIFY_WITH_PROBE}"


def codex_checks(settings: Mapping[str, Any]) -> list[Check]:
    """SDK import + version, runtime path + ``--version`` probe, auth hint for the Codex adapter."""
    checks: list[Check] = []
    cli_path: str | None = None
    sdk = _import("openai_codex")
    if sdk is None:
        checks.append(
            Check(
                "codex.sdk",
                "codex SDK",
                "fail",
                "openai-codex is not importable",
                required=True,
                hint="reinstall rayspec (uv tool install --reinstall rayspec) or "
                "pip install openai-codex",
            )
        )
    else:
        detail = f"openai-codex {_module_version(sdk, 'openai-codex')}"
        checks.append(Check("codex.sdk", "codex SDK", "ok", detail, required=True))

    found = find_codex_cli(settings)
    if isinstance(found, Check):
        checks.append(found)
    else:
        path, source = found
        cli_path = path
        version = parse_version(version_of([path, "--version"]))
        if version is None:
            checks.append(
                Check(
                    "codex.cli",
                    "codex CLI",
                    "warn",
                    _cli_detail(path, None, source),
                    required=True,
                    hint="`codex --version` did not report a version "
                    "(not executable? run it by hand)",
                )
            )
        else:
            checks.append(
                Check(
                    "codex.cli",
                    "codex CLI",
                    "ok",
                    _cli_detail(path, version, source),
                    required=True,
                )
            )

    if os.environ.get(CODEX_AUTH_VAR):
        checks.append(Check("codex.auth", "codex auth", "ok", f"via {CODEX_AUTH_VAR}"))
    else:
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        auth_file = codex_home / "auth.json"
        if auth_file.is_file():
            checks.append(
                Check(
                    "codex.auth",
                    "codex auth",
                    "info",
                    f"login state unknown (no {CODEX_AUTH_VAR}; {auth_file} present — "
                    "verify with --probe)",
                )
            )
        else:
            checks.append(
                Check(
                    "codex.auth",
                    "codex auth",
                    "warn",
                    f"login state unknown (no {CODEX_AUTH_VAR}, no {auth_file})",
                    hint=codex_login_hint(cli_path),
                )
            )
    return checks


# --------------------------------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------------------------------


def pricing_check(provider_id: str, config: Config) -> Check | None:
    """``<id>.pricing`` for a provider that reports no USD cost: are its tier/alias models priced?

    ``None`` for providers with ``cost_reporting`` (nothing to nudge about) and for ids the
    registry does not know. Never required: ``info`` when no pricing table exists at all (the
    tokens-only nudge) or when every model is deliberately disabled (``null`` entries — no nudge),
    ``warn`` when a table exists but misses a model or is malformed, ``ok`` when every configured
    model resolves to a price.
    """
    try:
        registration = get_registration(provider_id)
    except RayspecError:
        return None
    if registration.capabilities.cost_reporting:
        return None
    check_id, label = f"{provider_id}.pricing", f"{provider_id} pricing"
    models = pricing.configured_models(config, provider_id)
    coverage = pricing.pricing_coverage(config, provider_id, models)
    detail = pricing.describe(coverage)
    status: CheckStatus
    if coverage.error is not None:
        status = "warn"
    elif coverage.unpriced:
        status = "warn" if coverage.configured else "info"
    elif coverage.priced:
        status = "ok"
    else:
        status = "info"  # every model disabled on purpose
    hint = None
    if status != "ok":
        hint = f"see {pricing.PRICING_DOCS}; cost shows as ~$ once the model is priced"
    return Check(check_id, label, status, detail, hint=hint)


def pricing_checks(provider_ids: list[str], config: Config) -> list[Check]:
    """One :func:`pricing_check` per provider in ``provider_ids`` that yields one."""
    checks: list[Check] = []
    for pid in provider_ids:
        check = pricing_check(pid, config)
        if check is not None:
            checks.append(check)
    return checks


# --------------------------------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------------------------------


async def _probe_one(provider_id: str, settings: Mapping[str, Any]) -> Check:
    label = f"{provider_id} probe"
    try:
        provider = create_provider(provider_id, settings)
    except Exception as exc:
        hint = getattr(exc, "hint", None)
        return Check(
            f"{provider_id}.probe", label, "fail", f"{type(exc).__name__}: {exc}", True, hint
        )
    try:
        with anyio.fail_after(PROBE_TIMEOUT_S):
            health = await provider.healthcheck(probe=True)
    except TimeoutError:
        return Check(
            f"{provider_id}.probe",
            label,
            "fail",
            f"probe timed out after {PROBE_TIMEOUT_S:.0f}s",
            True,
            "check the login state and network, then retry",
        )
    except Exception as exc:
        hint = getattr(exc, "hint", None)
        return Check(
            f"{provider_id}.probe", label, "fail", f"{type(exc).__name__}: {exc}", True, hint
        )
    finally:
        # A probe reports, never raises — and never waits forever on cleanup: a hung adapter
        # (e.g. the codex sync client pinned to a thread after the timeout cancelled it) must
        # not block the report. Its thread may still delay interpreter exit after printing.
        with contextlib.suppress(Exception), anyio.move_on_after(CLOSE_TIMEOUT_S):
            await provider.aclose()
    parts = list(health.details)
    if health.cli_version and not any("CLI" in p or health.cli_version in p for p in parts):
        parts.insert(0, f"CLI {health.cli_version}")
    detail = "; ".join(parts) if parts else ("ok" if health.ok else "failed")
    status: CheckStatus = "ok" if health.ok else "fail"
    hint = (
        None if health.ok else "see the details; `rayspec doctor` without --probe lists the basics"
    )
    return Check(f"{provider_id}.probe", label, status, detail, True, hint)


async def _probe_all(provider_ids: list[str], config: Config) -> list[Check]:
    return [await _probe_one(pid, config.providers.get(pid, {})) for pid in provider_ids]


def probe_checks(provider_ids: list[str], config: Config) -> list[Check]:
    """Run ``healthcheck(probe=True)`` of every provider in ``provider_ids`` (in order)."""
    return anyio.run(_probe_all, provider_ids, config, backend="asyncio")


def login_hint_for(provider_id: str, checks: list[Check]) -> str | None:
    """The login hint of ``<id>.auth`` (how to log in on this machine), when that row has one.

    The ``verify with --probe`` clause is dropped: the hint is reused on a failed probe row,
    which must not tell the user to do what just failed.
    """
    hint = next((c.hint for c in checks if c.id == f"{provider_id}.auth" and c.hint), None)
    return hint.removesuffix(VERIFY_WITH_PROBE) if hint else None


def apply_probe_policy(checks: list[Check], *, explicit: set[str]) -> list[Check]:
    """The ``--probe`` exit-code rule and the probe-verifies-auth rule.

    A failed ``<id>.probe`` stays a *required* failure (exit 1) when the provider was requested
    explicitly with ``--provider`` or its ``<id>.auth`` row found credentials (``ok``/``info``, or
    no auth row at all — a provider that needs no login). A provider with no credentials at all
    that was merely probed by default is *unused* on this machine: its failed probe becomes a
    ``warn`` row whose hint says how to scope doctor (``--provider <other>``) and how to log in.
    A *successful* probe turns a non-``ok`` ``<id>.auth`` row ``ok`` (``probe OK``) — the probe
    is the verification the row's hint asked for.
    """
    by_id = {c.id: c for c in checks}
    probed = [c.id[: -len(".probe")] for c in checks if c.id.endswith(".probe")]
    for check in checks:
        if not check.id.endswith(".probe"):
            continue
        pid = check.id[: -len(".probe")]
        auth = by_id.get(f"{pid}.auth")
        configured = pid in explicit or auth is None or auth.status in CONFIGURED_AUTH_STATUSES
        if check.status == "fail" and configured:
            login = login_hint_for(pid, checks)
            if login and (pid in explicit or (auth is not None and auth.status != "ok")):
                by_id[check.id] = replace(check, hint=login)
        elif check.status == "fail" and not configured:
            # providers worth scoping to: those with credentials (stub & co. need none)
            candidates = [p for p in probed if p != pid and f"{p}.auth" in by_id]
            others = " ".join(f"--provider {p}" for p in candidates) or "--provider <id>"
            login = login_hint_for(pid, checks) or "log in or set its API key"
            by_id[check.id] = replace(
                check,
                status="warn",
                required=False,
                hint=f"{pid} has no credentials on this machine; if you do not use it, scope "
                f"the probe: `rayspec doctor --probe {others}` — to use it: {login}",
            )
        elif check.status == "ok" and auth is not None and auth.status != "ok":
            by_id[auth.id] = replace(
                auth,
                status="ok",
                detail="probe OK (verified by --probe: a one-turn probe succeeded)",
                hint=None,
            )
    return [by_id[c.id] for c in checks]


# --------------------------------------------------------------------------------------------------
# assembly + rendering
# --------------------------------------------------------------------------------------------------


def run_doctor(*, root: Path | None, probe: bool, providers: list[str]) -> Report:
    """Collect every check. ``providers`` restricts the per-provider sections and the probes."""
    start = (root or Path.cwd()).resolve()
    project_root = find_project_root(start)
    home = rayspec_home()
    with contextlib.suppress(RayspecError):  # a broken .env must not stop the diagnosis
        load_env(project_root, home=home, include_project=False)  # the home file only
    checks, config = environment_checks(start=start, project_root=project_root, home=home)
    registered = [r.id for r in list_registrations()]
    selected = providers or registered
    if "claude" in selected:
        checks.extend(claude_checks(config.providers.get("claude", {})))
    if "codex" in selected:
        checks.extend(codex_checks(config.providers.get("codex", {})))
    checks.extend(pricing_checks(selected, config))
    if probe:
        checks.extend(probe_checks(selected, config))
        checks = apply_probe_policy(checks, explicit=set(providers))
    return Report(checks)


def render_table(report: Report) -> Table:
    """The human table: label, status, detail."""
    table = Table(title="rayspec doctor", show_lines=False)
    table.add_column("check", style="bold", no_wrap=True)
    table.add_column("status", justify="center", no_wrap=True)
    table.add_column("detail", overflow="fold")
    for check in report.checks:
        # labels/details embed config-controlled text (model ids, paths, error messages): never
        # let rich parse them as markup ("[bold]" would vanish, "[/x]" would raise)
        table.add_row(Text(check.label), _STATUS_MARK[check.status], Text(check.detail))
    return table


def register(app: typer.Typer) -> None:
    @app.command()
    def doctor(
        probe: Annotated[
            bool,
            typer.Option(
                "--probe",
                help="Also run each provider's one-turn healthcheck (needs a login; slow).",
            ),
        ] = False,
        provider: Annotated[
            list[str] | None,
            typer.Option(
                "--provider",
                help="Restrict the provider checks/probes to these ids (repeatable).",
                show_default=False,
            ),
        ] = None,
        json_: JsonOption = False,
        output: OutputOption = None,
        root: RootOption = None,
    ) -> None:
        """Check the environment: Python, RAYSPEC_HOME, git/uv, SDKs, CLIs, auth (+ --probe)."""
        json_ = resolve_output(output, json_)
        providers = list(provider or [])
        for pid in providers:
            try:
                get_registration(pid)
            except RayspecError as exc:
                fail(str(exc), hint=exc.hint)
        report = run_doctor(root=root, probe=probe, providers=providers)
        if json_:
            typer.echo(json.dumps(report.to_dict(), indent=2))
            raise typer.Exit(code=report.exit_code)
        out = console()
        out.print(render_table(report))
        for check in report.checks:
            if check.hint and check.status != "ok":
                out.print(f"[dim]hint ({check.label}): {check.hint}[/dim]")
        failed = report.failed_required
        if failed:
            names = ", ".join(c.label for c in failed)
            out.print(
                f"[red]{len(report.checks)} checks · {len(failed)} required check(s) failed: "
                f"{names}[/red]"
            )
        else:
            out.print(f"[green]{len(report.checks)} checks · all required checks passed[/green]")
        raise typer.Exit(code=report.exit_code)


__all__ = [
    "CLAUDE_AUTH_VARS",
    "CODEX_AUTH_VAR",
    "CONFIGURED_AUTH_STATUSES",
    "VERIFY_WITH_PROBE",
    "Check",
    "Report",
    "apply_probe_policy",
    "claude_checks",
    "claude_login_source",
    "codex_checks",
    "codex_login_hint",
    "environment_checks",
    "find_claude_cli",
    "find_codex_cli",
    "known_claude_locations",
    "login_hint_for",
    "parse_version",
    "pricing_check",
    "pricing_checks",
    "probe_checks",
    "render_table",
    "run_doctor",
    "version_of",
]

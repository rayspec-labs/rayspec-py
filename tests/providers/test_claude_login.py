"""Claude CLI login detection used by ``healthcheck`` / ``rayspec doctor``.

Only the *existence* of the ``claude`` login is checked: the credentials file, or on macOS the
keychain item — never its contents.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rayspec.providers import claude as claude_mod
from rayspec.providers.claude import ClaudeProvider, cli_login_source

pytestmark = pytest.mark.anyio


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(claude_mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(claude_mod.platform, "system", lambda: "Linux")
    return tmp_path


def test_no_login_anywhere(no_env: Path) -> None:
    assert cli_login_source() is None


def test_credentials_file_is_a_login(no_env: Path) -> None:
    creds = no_env / ".claude" / ".credentials.json"
    creds.parent.mkdir()
    creds.write_text('{"claudeAiOauth": {"accessToken": "sk-ant-secret"}}')
    source = cli_login_source()
    assert source is not None
    assert "claude.ai login" in source and ".credentials.json" in source
    assert "secret" not in source  # never the content


def test_credentials_file_honours_claude_config_dir(
    no_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = tmp_path / "cfg"
    other.mkdir()
    (other / ".credentials.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    source = cli_login_source()
    assert source is not None and "cfg/.credentials.json" in source


def test_macos_keychain_item_is_a_login(no_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(claude_mod.shutil, "which", lambda name: "/usr/bin/security")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="attributes…", stderr="")

    monkeypatch.setattr(claude_mod.subprocess, "run", fake_run)
    source = cli_login_source()
    assert source == "claude.ai login (macOS keychain)"
    [cmd] = calls
    assert cmd[:2] == ["/usr/bin/security", "find-generic-password"]
    assert "Claude Code-credentials" in cmd
    assert "-w" not in cmd and "-g" not in cmd  # existence only: never ask for the secret


def test_macos_keychain_lookup_is_guarded(no_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_mod.platform, "system", lambda: "Darwin")
    # item missing (security exits 44)
    monkeypatch.setattr(claude_mod.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.setattr(
        claude_mod.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found"),
    )
    assert cli_login_source() is None

    # `security` not installed
    monkeypatch.setattr(claude_mod.shutil, "which", lambda name: None)
    assert cli_login_source() is None

    # `security` hangs → timeout → unknown, never a crash
    monkeypatch.setattr(claude_mod.shutil, "which", lambda name: "/usr/bin/security")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(claude_mod.subprocess, "run", hang)
    assert cli_login_source() is None

    def explode(cmd, **kw):
        raise OSError("no fork for you")

    monkeypatch.setattr(claude_mod.subprocess, "run", explode)
    assert cli_login_source() is None


def test_keychain_is_not_consulted_off_macos(no_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd, **kw):
        raise AssertionError("security must not run on Linux")

    monkeypatch.setattr(claude_mod.subprocess, "run", boom)
    monkeypatch.setattr(claude_mod.shutil, "which", lambda name: "/usr/bin/security")
    assert cli_login_source() is None


async def test_healthcheck_reports_the_cli_login_as_auth_ok(
    no_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho '2.1.237 (Claude Code)'\n")
    cli.chmod(0o755)
    monkeypatch.setattr(claude_mod, "_bundled_cli_path", lambda: str(cli))
    health = await ClaudeProvider({}).healthcheck()
    assert health.auth == "unknown"
    assert "auth: login state unknown" in health.details

    creds = no_env / ".claude" / ".credentials.json"
    creds.parent.mkdir()
    creds.write_text("{}")
    health = await ClaudeProvider({}).healthcheck()
    assert health.ok is True and health.auth == "ok"
    assert any(d.startswith("auth: claude.ai login") for d in health.details), health.details
    assert not any("login state unknown" in d for d in health.details)

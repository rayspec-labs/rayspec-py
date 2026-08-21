"""`rayspec doctor [--probe] [--provider ID] [--json]` with monkeypatched SDK modules (no network)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import doctor as doctor_mod
from rayspec.providers.base import ProviderHealth

CLAUDE_VERSION = "2.1.999"
CODEX_VERSION = "0.147.9"


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
    """Fake `claude_agent_sdk`, `openai_codex`, `codex_cli_bin`; canned `-v` answers; git + uv."""
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
        if cmd[0] == "git":
            return "git version 2.45.0"
        if cmd[0] == "uv":
            return "uv 0.8.0"
        return None

    monkeypatch.setattr(doctor_mod, "version_of", fake_version)
    tools = {"git": "/usr/bin/git", "uv": "/usr/local/bin/uv"}
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: tools.get(name))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(tmp_path / "home" / ".rayspec"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # no `claude` CLI login unless a test says so
    monkeypatch.setattr(doctor_mod, "claude_login_source", lambda: None)
    return fake


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    res = CliRunner().invoke(app, ["init", "--root", str(root)])
    assert res.exit_code == 0, res.output
    return root


def _doctor(*args: str) -> tuple[int, str]:
    res = CliRunner().invoke(app, ["doctor", *args])
    return res.exit_code, res.output


def _doctor_json(*args: str) -> tuple[int, dict]:
    res = CliRunner().invoke(app, ["doctor", "--json", *args])
    assert res.stdout.strip().startswith("{"), res.output
    return res.exit_code, json.loads(res.stdout)


def _check(report: dict, check_id: str) -> dict:
    [found] = [c for c in report["checks"] if c["id"] == check_id]
    return found


def test_doctor_table_all_green(sdks: FakeSdks, project: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    code, out = _doctor("--root", str(project))
    assert code == 0, out
    for needle in (
        "python",
        "rayspec",
        "RAYSPEC_HOME",
        "git",
        "uv",
        "claude",
        "codex",
        CLAUDE_VERSION,
        CODEX_VERSION,
        "0.2.900",
        "0.147.9",
        "ANTHROPIC_API_KEY",
        "login state unknown",
        "1 workflow",
    ):
        assert needle in out, f"{needle!r} missing in:\n{out}"
    assert "all required checks passed" in out


def test_doctor_json_shape(sdks: FakeSdks, project: Path) -> None:
    code, report = _doctor_json("--root", str(project))
    assert code == 0
    assert report["ok"] is True and report["exit_code"] == 0
    ids = [c["id"] for c in report["checks"]]
    for expected in (
        "python",
        "rayspec",
        "home",
        "config",
        "project",
        "git",
        "uv",
        "claude.sdk",
        "claude.cli",
        "claude.auth",
        "codex.sdk",
        "codex.cli",
        "codex.auth",
    ):
        assert expected in ids, ids
    for check in report["checks"]:
        assert set(check) == {"id", "label", "status", "required", "detail", "hint"}
        assert check["status"] in {"ok", "warn", "fail", "info"}
    assert _check(report, "claude.cli")["detail"].startswith(str(sdks.claude_bin))
    assert CLAUDE_VERSION in _check(report, "claude.cli")["detail"]
    assert _check(report, "codex.cli")["detail"].startswith(str(sdks.codex_bin))
    assert _check(report, "claude.auth")["status"] == "warn"
    assert "login state unknown" in _check(report, "claude.auth")["detail"]
    assert _check(report, "codex.auth")["status"] == "warn"
    assert "OPENAI_API_KEY" in _check(report, "codex.auth")["detail"]
    assert "1 workflow" in _check(report, "project")["detail"]


def test_doctor_auth_env_vars(sdks: FakeSdks, project: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    _, report = _doctor_json("--root", str(project))
    assert _check(report, "claude.auth")["status"] == "ok"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in _check(report, "claude.auth")["detail"]
    assert _check(report, "codex.auth")["status"] == "ok"
    assert "OPENAI_API_KEY" in _check(report, "codex.auth")["detail"]


def test_doctor_does_not_load_the_project_env_file(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    """A checkout's ``.rayspec/.env`` is a credential surface — ``doctor`` reports it as a
    row but never applies it (only run/resume/approve/reject do); ``~/.rayspec/.env`` still
    loads."""
    # load_env() writes into the real os.environ; make monkeypatch own the keys so its teardown
    # removes them again even when an assert below fails.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sentinel-to-be-unset")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-to-be-unset")
    monkeypatch.delenv("OPENAI_API_KEY")
    (project / ".rayspec" / ".env").write_text(
        "ANTHROPIC_API_KEY=from-env-file\nANTHROPIC_BASE_URL=https://attacker.example\n"
    )
    home = Path(os.environ["RAYSPEC_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("OPENAI_API_KEY=from-home-env\n")
    _, report = _doctor_json("--root", str(project))
    assert os.environ.get("ANTHROPIC_API_KEY") is None
    assert "ANTHROPIC_BASE_URL" not in os.environ
    assert _check(report, "claude.auth")["status"] != "ok"
    assert os.environ.get("OPENAI_API_KEY") == "from-home-env"
    row = _check(report, "project.env")
    assert row["status"] == "info"
    assert row["detail"].startswith(str(project / ".rayspec" / ".env"))
    assert "2 vars, applied only by run/resume/approve/reject" in row["detail"]


def test_doctor_configured_claude_cli_path_missing_fails(sdks: FakeSdks, project: Path) -> None:
    """`providers.claude.cli_path` at a missing file → claude.cli fail."""
    missing = project / "no-such-claude"
    (project / ".rayspec" / "config.yaml").write_text(
        f"providers:\n  claude: {{ cli_path: {json.dumps(str(missing))} }}\n"
    )
    code, report = _doctor_json("--root", str(project))
    assert code == 1 and report["ok"] is False
    cli = _check(report, "claude.cli")
    assert cli["status"] == "fail" and cli["required"] is True
    assert str(missing) in cli["detail"] and "cli_path" in cli["detail"]
    assert "cli_path" in (cli["hint"] or "")
    # the bundled CLI is NOT used as a fallback when the configured path is wrong
    assert str(sdks.claude_bin) not in cli["detail"]


def test_doctor_provider_filter_drops_the_other_sections(sdks: FakeSdks, project: Path) -> None:
    """`--provider claude` keeps the environment rows and drops every codex.* row (and vice versa)."""
    code, report = _doctor_json("--provider", "claude", "--root", str(project))
    assert code == 0
    ids = [c["id"] for c in report["checks"]]
    assert "claude.sdk" in ids and "claude.cli" in ids and "claude.auth" in ids
    assert not [i for i in ids if i.startswith("codex.")], ids
    assert "python" in ids and "git" in ids and "project" in ids
    code, report = _doctor_json("--provider", "codex", "--root", str(project))
    ids = [c["id"] for c in report["checks"]]
    assert not [i for i in ids if i.startswith("claude.")], ids
    assert "codex.sdk" in ids and "codex.cli" in ids and "codex.auth" in ids


def test_doctor_claude_cli_falls_back_to_path_then_known_locations(
    sdks: FakeSdks, project: Path, monkeypatch, tmp_path: Path
) -> None:
    sdks.claude_bin.unlink()
    tools = {"git": "/usr/bin/git", "claude": "/opt/bin/claude"}
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: tools.get(name))
    monkeypatch.setattr(
        doctor_mod, "version_of", lambda cmd, *, timeout_s=5.0: None if "claude" in cmd[0] else "x"
    )
    _, report = _doctor_json("--root", str(project))
    cli = _check(report, "claude.cli")
    assert cli["status"] == "warn", cli  # found on PATH, but `-v` said nothing
    assert cli["detail"].startswith("/opt/bin/claude") and "version unknown" in cli["detail"]
    # nothing on PATH either: the SDK's known locations are probed
    tools.pop("claude")
    known = tmp_path / "home" / ".local" / "bin" / "claude"
    known.parent.mkdir(parents=True)
    known.write_text("")
    _, report = _doctor_json("--root", str(project))
    assert _check(report, "claude.cli")["detail"].startswith(str(known))


def test_doctor_missing_claude_cli_fails_required_check(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    sdks.claude_bin.unlink()
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: "/usr/bin/git" if name == "git" else None
    )
    code, report = _doctor_json("--root", str(project))
    assert code == 1 and report["ok"] is False
    cli = _check(report, "claude.cli")
    assert cli["status"] == "fail" and cli["required"] is True
    assert cli["hint"]
    assert _check(report, "uv")["status"] == "warn"  # optional tool: never fails the doctor
    code, out = _doctor("--root", str(project))
    assert code == 1 and "required check" in out and "claude" in out


def test_doctor_missing_codex_runtime_and_sdk(sdks: FakeSdks, project: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "codex_cli_bin", None)  # import -> ImportError
    code, report = _doctor_json("--root", str(project))
    assert code == 1
    cli = _check(report, "codex.cli")
    assert cli["status"] == "fail" and "openai-codex-cli-bin" in (
        cli["detail"] + (cli["hint"] or "")
    )
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    _, report = _doctor_json("--root", str(project))
    assert _check(report, "codex.sdk")["status"] == "fail"
    assert _check(report, "claude.sdk")["status"] == "ok"


def test_doctor_codex_bin_from_config(sdks: FakeSdks, project: Path, tmp_path: Path) -> None:
    custom = tmp_path / "custom-codex"
    custom.write_text("")
    (project / ".rayspec" / "config.yaml").write_text(
        f"providers:\n  codex: {{ codex_bin: {json.dumps(str(custom))} }}\n"
    )
    _, report = _doctor_json("--root", str(project))
    assert _check(report, "codex.cli")["detail"].startswith(str(custom))
    assert "codex_bin" in _check(report, "codex.cli")["detail"]


def test_doctor_without_project_and_without_git(
    sdks: FakeSdks, tmp_path: Path, monkeypatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code, report = _doctor_json("--root", str(empty))
    assert code == 0
    project = _check(report, "project")
    assert project["status"] == "warn" and "rayspec init" in (project["hint"] or "")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    code, report = _doctor_json("--root", str(empty))
    assert code == 1
    assert _check(report, "git")["status"] == "fail" and _check(report, "git")["required"]


def test_doctor_broken_config_is_a_failed_check(sdks: FakeSdks, project: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text("default_provider: [not, a, string]\n")
    code, report = _doctor_json("--root", str(project))
    assert code == 1
    assert _check(report, "config")["status"] == "fail"


def test_doctor_probe_with_the_stub_provider(sdks: FakeSdks, project: Path) -> None:
    code, out = _doctor("--probe", "--provider", "stub", "--root", str(project))
    assert code == 0, out
    assert "stub" in out and "probe" in out
    code, report = _doctor_json("--probe", "--provider", "stub", "--root", str(project))
    assert code == 0
    probe = _check(report, "stub.probe")
    assert probe["status"] == "ok" and probe["required"] is True
    assert "probe: ok" in probe["detail"]
    assert not [c for c in report["checks"] if c["id"] in {"claude.probe", "codex.probe"}]


class _FakeProvider:
    def __init__(self, health: ProviderHealth) -> None:
        self._health = health
        self.closed = False
        self.probe_arg: bool | None = None

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
        self.probe_arg = probe
        return self._health

    async def aclose(self) -> None:
        self.closed = True


def test_doctor_probe_every_provider_and_failures_exit_1(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    made: dict[str, _FakeProvider] = {}
    healths = {
        "claude": ProviderHealth(
            ok=True,
            sdk_version="0.2.900",
            cli_version=CLAUDE_VERSION,
            auth="ok",
            details=("probe: ok",),
        ),
        "codex": ProviderHealth(
            ok=False,
            sdk_version="0.147.9",
            auth="missing",
            details=("auth: no codex login", "probe: skipped"),
        ),
    }

    def fake_create(provider_id: str, settings=None) -> _FakeProvider:
        if provider_id == "stub":
            from rayspec.providers.stub import StubProvider

            return StubProvider(settings)  # type: ignore[return-value]
        made[provider_id] = _FakeProvider(healths[provider_id])
        return made[provider_id]

    monkeypatch.setattr(doctor_mod, "create_provider", fake_create)
    codex_home = Path(os.environ["HOME"]) / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")  # credentials exist → codex counts as configured
    code, report = _doctor_json("--probe", "--root", str(project))
    assert code == 1
    assert _check(report, "claude.probe")["status"] == "ok"
    codex = _check(report, "codex.probe")
    assert codex["status"] == "fail" and "no codex login" in codex["detail"]
    assert codex["required"] is True
    assert _check(report, "stub.probe")["status"] == "ok"
    assert all(p.closed and p.probe_arg is True for p in made.values())
    # the successful probe *is* the verification — the auth row turns ok, its hint goes
    claude_auth = _check(report, "claude.auth")
    assert claude_auth["status"] == "ok" and "probe" in claude_auth["detail"].lower()
    assert claude_auth["hint"] is None
    code, out = _doctor("--probe", "--root", str(project))
    assert "hint (claude auth)" not in out


def test_doctor_probe_exception_is_reported_not_raised(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    def boom(provider_id: str, settings=None):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(doctor_mod, "create_provider", boom)
    code, report = _doctor_json("--probe", "--provider", "claude", "--root", str(project))
    assert code == 1
    probe = _check(report, "claude.probe")
    assert probe["status"] == "fail" and "adapter exploded" in probe["detail"]


def test_doctor_probe_timeout_and_hung_aclose_are_bounded(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    """A probe that exceeds PROBE_TIMEOUT_S is a failed `timed out` check, and a hung aclose()
    is cut off by CLOSE_TIMEOUT_S so the report is still printed."""
    import anyio

    class _Hanging:
        closed_started = False

        async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
            await anyio.sleep(60)
            raise AssertionError("unreachable")

        async def aclose(self) -> None:
            _Hanging.closed_started = True
            await anyio.sleep(60)

    monkeypatch.setattr(doctor_mod, "create_provider", lambda pid, settings=None: _Hanging())
    monkeypatch.setattr(doctor_mod, "PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(doctor_mod, "CLOSE_TIMEOUT_S", 0.05)
    code, report = _doctor_json("--probe", "--provider", "claude", "--root", str(project))
    assert code == 1
    probe = _check(report, "claude.probe")
    assert probe["status"] == "fail" and "timed out" in probe["detail"], probe
    assert _Hanging.closed_started


def test_doctor_unknown_provider_is_a_usage_error(sdks: FakeSdks, project: Path) -> None:
    code, out = _doctor("--probe", "--provider", "nope", "--root", str(project))
    assert code == 2
    assert "nope" in out


def test_version_of_is_tolerant(tmp_path: Path) -> None:
    assert doctor_mod.version_of(["/definitely/not/here"]) is None
    script = tmp_path / "v.sh"
    script.write_text("#!/bin/sh\necho 'tool 1.2.3'\n")
    script.chmod(0o755)
    assert doctor_mod.version_of([str(script), "--version"]) == "tool 1.2.3"
    assert doctor_mod.parse_version("tool 1.2.3 (x)") == "1.2.3"
    assert doctor_mod.parse_version("no digits") is None


def test_doctor_help_lists_options() -> None:
    res = CliRunner().invoke(app, ["doctor", "--help"])
    assert res.exit_code == 0
    for flag in ("--probe", "--provider", "--json", "--root"):
        assert flag in res.output


# --------------------------------------------------------------------------------------------------
# pricing nudge
# --------------------------------------------------------------------------------------------------


def test_doctor_nudges_when_codex_has_no_pricing(sdks: FakeSdks, project: Path) -> None:
    code, report = _doctor_json("--root", str(project))
    assert code == 0
    check = _check(report, "codex.pricing")
    assert check["status"] == "info" and check["required"] is False
    assert "tokens only — add pricing.gpt-5.4 for estimates" in check["detail"]
    assert "docs/providers.md#pricing" in check["hint"]
    assert not [c for c in report["checks"] if c["id"] == "claude.pricing"]
    _, out = _doctor("--root", str(project))
    assert "tokens only" in out and "docs/providers.md#pricing" in out


def test_doctor_pricing_ok_when_tier_models_are_priced(sdks: FakeSdks, project: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        'pricing:\n  "gpt-5.4*": { input: 2.0, cached_input: 0.5, output: 8.0 }\n'
    )
    _, report = _doctor_json("--root", str(project))
    check = _check(report, "codex.pricing")
    assert check["status"] == "ok", check
    assert "gpt-5.4" in check["detail"] and check["hint"] is None


def test_doctor_pricing_warns_on_unmatched_tier_and_alias_models(
    sdks: FakeSdks, project: Path
) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        "tiers:\n  codex: { large: gpt-5.5 }\n"
        'aliases:\n  "@mini": { provider: codex, model: gpt-5.4-mini }\n'
        'pricing:\n  "gpt-5.4": { input: 2.0, cached_input: 0.5, output: 8.0 }\n'
    )
    _, report = _doctor_json("--root", str(project))
    check = _check(report, "codex.pricing")
    assert check["status"] == "warn", check
    assert "gpt-5.4-mini" in check["detail"] and "gpt-5.5" in check["detail"]
    assert "pricing.gpt-5.4-mini" in check["detail"]


def test_doctor_pricing_warns_on_a_broken_table(sdks: FakeSdks, project: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text('pricing:\n  "gpt-5.4": { input: -1 }\n')
    code, report = _doctor_json("--root", str(project))
    assert code == 0
    check = _check(report, "codex.pricing")
    assert check["status"] == "warn" and "missing price field(s)" in check["detail"]


def test_doctor_provider_filter_drops_the_pricing_check(sdks: FakeSdks, project: Path) -> None:
    _, report = _doctor_json("--root", str(project), "--provider", "claude")
    assert not [c for c in report["checks"] if c["id"].endswith(".pricing")]


def test_doctor_pricing_reports_null_disabled_models_without_a_nudge(
    sdks: FakeSdks, project: Path
) -> None:
    """A ``null`` entry is a deliberate opt-out: no warn, no "add pricing.<model>"."""
    (project / ".rayspec" / "config.yaml").write_text('pricing:\n  "gpt-5.4": null\n')
    _, report = _doctor_json("--root", str(project))
    check = _check(report, "codex.pricing")
    assert check["status"] == "info", check
    assert "pricing disabled (null) for gpt-5.4" in check["detail"]
    assert "add pricing.gpt-5.4" not in check["detail"]
    # a disabled model next to an unmatched one: only the unmatched one is nudged
    (project / ".rayspec" / "config.yaml").write_text(
        "tiers:\n  codex: { large: gpt-5.5 }\n"
        'aliases:\n  "@mini": { provider: codex, model: gpt-5.4-mini }\n'
        'pricing:\n  "gpt-5.5": null\n'
    )
    _, report = _doctor_json("--root", str(project))
    check = _check(report, "codex.pricing")
    assert check["status"] == "warn", check
    assert "pricing disabled (null) for gpt-5.5" in check["detail"]
    assert "pricing.gpt-5.4-mini for estimates" in check["detail"]
    assert "pricing.gpt-5.5" not in check["detail"]


def test_doctor_pricing_uses_the_provider_table_when_the_global_one_is_broken(
    sdks: FakeSdks, project: Path
) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        "providers:\n  codex:\n    pricing:\n"
        '      "gpt-5.4": { input: 2.0, cached_input: 0.5, output: 8.0 }\n'
        'pricing:\n  "gpt-5.4": { input: -1 }\n'
    )
    _, report = _doctor_json("--root", str(project))
    check = _check(report, "codex.pricing")
    assert check["status"] == "warn", check
    assert "estimated from the pricing table (~$) for gpt-5.4" in check["detail"]
    assert "pricing table invalid" in check["detail"]
    assert "add pricing.gpt-5.4" not in check["detail"]


def test_render_table_does_not_treat_details_as_rich_markup() -> None:
    """Model ids are copied into ``pricing.<model>``; rich markup must not eat them."""
    from io import StringIO

    from rich.console import Console

    report = doctor_mod.Report(
        [
            doctor_mod.Check(
                "codex.pricing", "codex [b]pricing", "info", "add pricing.gpt-5[bold]x"
            ),
            doctor_mod.Check("x", "x", "ok", "id with [/x] closing tag"),
        ]
    )
    buf = StringIO()
    Console(file=buf, width=200, force_terminal=False).print(doctor_mod.render_table(report))
    out = buf.getvalue()
    assert "pricing.gpt-5[bold]x" in out
    assert "codex [b]pricing" in out
    assert "[/x]" in out


# -- claude login detection --------------------------------------------------------------


def test_doctor_claude_auth_ok_when_the_cli_login_is_found(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        doctor_mod, "claude_login_source", lambda: "claude.ai login (~/.claude/.credentials.json)"
    )
    code, report = _doctor_json("--root", str(project))
    assert code == 0
    auth = _check(report, "claude.auth")
    assert auth["status"] == "ok"
    assert auth["detail"] == "claude.ai login (~/.claude/.credentials.json)"
    assert auth["hint"] is None
    code, out = _doctor("--root", str(project))
    assert "hint (claude auth)" not in out
    assert "login state unknown (no ANTHROPIC_API_KEY" not in out


def test_doctor_claude_auth_unknown_names_what_was_looked_for(
    sdks: FakeSdks, project: Path
) -> None:
    _, report = _doctor_json("--root", str(project))
    auth = _check(report, "claude.auth")
    assert auth["status"] == "warn"
    assert "login state unknown" in auth["detail"]
    assert ".credentials.json" in auth["detail"]
    assert "--probe" in (auth["hint"] or "")


def test_doctor_claude_login_source_delegates_to_the_adapter(monkeypatch) -> None:
    """The doctor asks the Claude adapter (lazily) — and degrades to unknown when it is not
    importable (no SDK) instead of crashing."""
    import types

    fake = types.ModuleType("rayspec.providers.claude")
    fake.cli_login_source = lambda: "claude.ai login (macOS keychain)"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rayspec.providers.claude", fake)
    assert doctor_mod.claude_login_source() == "claude.ai login (macOS keychain)"
    monkeypatch.setitem(sys.modules, "rayspec.providers.claude", None)
    assert doctor_mod.claude_login_source() is None


# -- actionable codex hints, probe exit-code rule ----------------------------------------


def test_doctor_codex_hint_names_the_bundled_binary_when_codex_is_not_on_path(
    sdks: FakeSdks, project: Path
) -> None:
    """`codex` is not on PATH in the fixture (only the bundled binary exists): the login hint
    must be runnable as written."""
    _, report = _doctor_json("--root", str(project))
    auth = _check(report, "codex.auth")
    assert auth["status"] == "warn"
    hint = auth["hint"] or ""
    assert f"`{sdks.codex_bin} login`" in hint
    assert "OPENAI_API_KEY" in hint
    assert "run `codex login`" not in hint


def test_doctor_codex_hint_uses_plain_codex_login_when_on_path(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    tools = {"git": "/usr/bin/git", "uv": "/usr/local/bin/uv", "codex": "/opt/bin/codex"}
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: tools.get(name))
    _, report = _doctor_json("--root", str(project))
    assert "run `codex login`" in (_check(report, "codex.auth")["hint"] or "")


def _fake_probe_world(monkeypatch, *, codex_ok: bool) -> dict[str, _FakeProvider]:
    made: dict[str, _FakeProvider] = {}
    healths = {
        "claude": ProviderHealth(
            ok=True,
            sdk_version="0.2.900",
            cli_version=CLAUDE_VERSION,
            auth="ok",
            details=("auth: claude.ai login (macOS keychain)", "probe: ok"),
        ),
        "codex": ProviderHealth(
            ok=codex_ok,
            sdk_version="0.147.9",
            auth="ok" if codex_ok else "missing",
            details=("probe: ok",) if codex_ok else ("auth: no codex login", "probe: skipped"),
        ),
    }

    def fake_create(provider_id: str, settings=None):
        if provider_id == "stub":
            from rayspec.providers.stub import StubProvider

            return StubProvider(settings)
        made[provider_id] = _FakeProvider(healths[provider_id])
        return made[provider_id]

    monkeypatch.setattr(doctor_mod, "create_provider", fake_create)
    return made


def test_doctor_probe_of_an_unconfigured_unused_provider_is_a_warning(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    """A claude-only box: codex has no credentials at all and was not requested → its failed
    probe is a warn row (exit 0) whose hint says how to scope doctor — and how to log in."""
    _fake_probe_world(monkeypatch, codex_ok=False)
    code, report = _doctor_json("--probe", "--root", str(project))
    assert code == 0, report
    codex = _check(report, "codex.probe")
    assert codex["status"] == "warn" and codex["required"] is False
    hint = codex["hint"] or ""
    assert "--provider claude" in hint
    assert f"`{sdks.codex_bin} login`" in hint or "OPENAI_API_KEY" in hint
    assert _check(report, "claude.probe")["status"] == "ok"
    code, out = _doctor("--probe", "--root", str(project))
    assert code == 0
    assert "all required checks passed" in out
    assert "--provider claude" in out


def test_doctor_probe_failure_of_an_explicitly_requested_provider_exits_1(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    _fake_probe_world(monkeypatch, codex_ok=False)
    code, report = _doctor_json("--probe", "--provider", "codex", "--root", str(project))
    assert code == 1
    codex = _check(report, "codex.probe")
    assert codex["status"] == "fail" and codex["required"] is True
    assert "--provider" not in (codex["hint"] or "")


def test_doctor_probe_failure_of_a_configured_provider_exits_1(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    """OPENAI_API_KEY is set (auth ok) but the probe fails: that is a real failure."""
    _fake_probe_world(monkeypatch, codex_ok=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-broken")
    code, report = _doctor_json("--probe", "--root", str(project))
    assert code == 1
    assert _check(report, "codex.probe")["status"] == "fail"


def test_doctor_probe_success_turns_a_warn_auth_row_ok(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    _fake_probe_world(monkeypatch, codex_ok=True)
    code, report = _doctor_json("--probe", "--root", str(project))
    assert code == 0
    for pid in ("claude", "codex"):
        auth = _check(report, f"{pid}.auth")
        assert auth["status"] == "ok", auth
        assert "probe" in auth["detail"].lower()
        assert auth["hint"] is None


def test_doctor_probe_hint_of_a_required_failure_does_not_say_verify_with_probe(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    """The probe just failed: its hint says how to log in, not "verify with --probe"."""
    _fake_probe_world(monkeypatch, codex_ok=False)
    code, report = _doctor_json("--probe", "--provider", "codex", "--root", str(project))
    assert code == 1
    hint = _check(report, "codex.probe")["hint"] or ""
    assert "verify with --probe" not in hint and "--probe" not in hint
    assert f"`{sdks.codex_bin} login`" in hint or "OPENAI_API_KEY" in hint
    # the auth row keeps its full hint (it still points at --probe as the verification)
    assert "verify with --probe" in (_check(report, "codex.auth")["hint"] or "")


def test_doctor_probe_hint_of_an_unused_provider_omits_verify_and_the_stub(
    sdks: FakeSdks, project: Path, monkeypatch
) -> None:
    _fake_probe_world(monkeypatch, codex_ok=False)
    code, report = _doctor_json("--probe", "--root", str(project))
    assert code == 0
    hint = _check(report, "codex.probe")["hint"] or ""
    assert "--provider claude" in hint
    assert "--provider stub" not in hint
    assert "verify with --probe" not in hint

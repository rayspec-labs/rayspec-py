"""Secret sources: ``env`` / ``file`` / ``cmd`` resolution and their refusals.

This package deliberately has **no** ``__init__.py``: ``tests/`` is on ``sys.path`` during a
run, so a package named ``secrets`` here would shadow the standard library's ``secrets`` module
for every dependency that imports it (starlette does). Without it the directory is at most an
implicit namespace package, which the real stdlib module wins over.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rayspec.config import Config
from rayspec.secrets import (
    ConfigSecretProvider,
    SecretError,
    describe_sources,
    resolve_config_secrets,
)


def _config(**secrets: dict[str, object]) -> Config:
    return Config.parse({"secrets": secrets}, source="<test>")


def test_env_source_reads_the_named_variable() -> None:
    config = _config(TOKEN={"env": "GH_TOKEN"})
    values = resolve_config_secrets(config, env={"GH_TOKEN": "s3cr3t-value"}, base_dir=Path.cwd())
    assert values == {"TOKEN": "s3cr3t-value"}


def test_env_source_missing_is_an_actionable_error() -> None:
    config = _config(TOKEN={"env": "GH_TOKEN"})
    with pytest.raises(SecretError) as exc:
        resolve_config_secrets(config, env={}, base_dir=Path.cwd())
    assert "secrets.TOKEN" in str(exc.value) and "GH_TOKEN" in str(exc.value)


def test_optional_env_source_may_be_absent() -> None:
    config = _config(TOKEN={"env": "GH_TOKEN", "required": False})
    assert resolve_config_secrets(config, env={}, base_dir=Path.cwd()) == {}


def test_file_source_reads_a_0600_file(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("file-value\n")
    path.chmod(0o600)
    config = _config(TOKEN={"file": str(path)})
    assert resolve_config_secrets(config, env={}, base_dir=tmp_path) == {"TOKEN": "file-value"}


def test_file_source_refuses_a_group_or_world_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("file-value\n")
    path.chmod(0o644)
    config = _config(TOKEN={"file": str(path)})
    with pytest.raises(SecretError) as exc:
        resolve_config_secrets(config, env={}, base_dir=tmp_path)
    message = str(exc.value)
    assert "0644" in message and "chmod 600" in (exc.value.hint or "")
    assert "file-value" not in message


def test_file_source_resolves_relative_to_the_base_dir(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("rel-value")
    path.chmod(0o600)
    config = _config(TOKEN={"file": "token"})
    assert resolve_config_secrets(config, env={}, base_dir=tmp_path) == {"TOKEN": "rel-value"}


def test_cmd_source_runs_the_command_and_takes_stdout(tmp_path: Path) -> None:
    config = _config(TOKEN={"cmd": "printf cmd-value"})
    assert resolve_config_secrets(config, env={}, base_dir=tmp_path) == {"TOKEN": "cmd-value"}


def test_cmd_source_failure_names_the_secret_and_the_exit_code(tmp_path: Path) -> None:
    config = _config(TOKEN={"cmd": ["sh", "-c", "echo boom >&2; exit 3"]})
    with pytest.raises(SecretError) as exc:
        resolve_config_secrets(config, env={}, base_dir=tmp_path)
    assert "secrets.TOKEN" in str(exc.value) and "exit code 3" in str(exc.value)


def test_a_source_must_name_exactly_one_of_env_file_cmd() -> None:
    with pytest.raises(Exception) as exc:
        Config.parse({"secrets": {"T": {"env": "A", "file": "b"}}}, source="<test>")
    assert "exactly one of" in str(exc.value)
    with pytest.raises(Exception) as empty:
        Config.parse({"secrets": {"T": {}}}, source="<test>")
    assert "exactly one of" in str(empty.value)


def test_an_empty_value_is_refused(tmp_path: Path) -> None:
    config = _config(TOKEN={"env": "GH_TOKEN"})
    with pytest.raises(SecretError) as exc:
        resolve_config_secrets(config, env={"GH_TOKEN": "  "}, base_dir=tmp_path)
    assert "empty" in str(exc.value)


def test_provider_resolves_lazily_and_caches(tmp_path: Path) -> None:
    calls: list[str] = []
    path = tmp_path / "token"
    path.write_text("lazy")
    path.chmod(0o600)
    config = Config.parse(
        {"secrets": {"a": {"file": str(path)}, "b": {"env": "MISSING", "required": False}}},
        source="<test>",
    )
    provider = ConfigSecretProvider(config.secrets, env={}, base_dir=tmp_path)
    assert provider.names() == ("a", "b")
    assert provider.get("a") == "lazy"
    path.unlink()  # cached: a second get must not touch the file again
    assert provider.get("a") == "lazy"
    assert provider.get("b") is None
    assert provider.get("nope") is None
    assert calls == []


def test_describe_sources_never_shows_a_value(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("file-value")
    path.chmod(0o600)
    config = Config.parse(
        {
            "secrets": {
                "A": {"env": "GH_TOKEN"},
                "B": {"file": str(path)},
                "C": {"cmd": "printf x"},
            }
        },
        source="<test>",
    )
    rows = describe_sources(config.secrets)
    assert rows == (("A", "env GH_TOKEN"), ("B", f"file {path}"), ("C", "cmd printf x"))
    assert all("file-value" not in row[1] for row in rows)


def test_file_mode_check_is_skipped_when_the_platform_has_no_modes(tmp_path: Path) -> None:
    """Sanity: the mode we check is the permission bits only, not the file type."""
    path = tmp_path / "token"
    path.write_text("v")
    path.chmod(0o600)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_failing_cmd_never_echoes_its_stderr(tmp_path: Path) -> None:
    """Real helpers print sensitive material on stderr (a partially written value, an auth URL
    with a token). The message names the exit code and argv[0] — never the output."""
    config = _config(TOKEN={"cmd": ["sh", "-c", "echo SUPERSECRETLEAK >&2; exit 1"]})
    with pytest.raises(SecretError) as exc:
        resolve_config_secrets(config, env={}, base_dir=tmp_path)
    message = str(exc.value)
    assert "SUPERSECRETLEAK" not in message
    assert "exit code 1" in message and "'sh'" in message
    assert exc.value.hint is not None and "RAYSPEC_DEBUG" in exc.value.hint


def test_rayspec_debug_opts_into_one_line_of_the_stderr_tail(tmp_path: Path) -> None:
    config = _config(TOKEN={"cmd": ["sh", "-c", "echo first >&2; echo why-it-failed >&2; exit 1"]})
    with pytest.raises(SecretError) as exc:
        resolve_config_secrets(config, env={"RAYSPEC_DEBUG": "1"}, base_dir=tmp_path)
    assert "why-it-failed" in str(exc.value)
    assert "first" not in str(exc.value)


def test_a_secret_name_must_be_an_environment_variable_name() -> None:
    """The name becomes an environment variable of every shell/python step, so a name that is
    not one — or that collides with the engine's own — is refused at load time."""
    with pytest.raises(Exception) as exc:
        Config.parse({"secrets": {"not an ident": {"env": "A"}}}, source="<test>")
    assert "not an ident" in str(exc.value)


def test_a_secret_name_may_not_shadow_a_rayspec_variable() -> None:
    with pytest.raises(Exception) as exc:
        Config.parse({"secrets": {"RAYSPEC_CONTEXT": {"env": "A"}}}, source="<test>")
    assert "RAYSPEC_" in str(exc.value)


def test_a_secret_name_may_not_shadow_path() -> None:
    with pytest.raises(Exception) as exc:
        Config.parse({"secrets": {"PATH": {"env": "A"}}}, source="<test>")
    assert "PATH" in str(exc.value)
